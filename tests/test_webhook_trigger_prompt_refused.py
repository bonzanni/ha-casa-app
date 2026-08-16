"""#608: a webhook trigger's `prompt` is refused when written, and tolerated
(loudly) when already on disk.

The shipped behaviour: `casa_core`'s webhook dispatch builds the turn from the
trigger NAME and the request payload and never reads `prompt`
(`agent_loader._build_triggers` sets `prompt_text = ""` for a webhook and says
so). But `triggers.v1.json` accepted the field for any type and
`_TRIGGER_ENTRY_FIELDS` offered it on the typed tool surface, so the model
wrote it — twice, unprompted, in the reported run — and it was persisted,
committed and silently discarded.

The fix has two halves that must be pinned SEPARATELY, because they pull in
opposite directions and a single combined assertion would let either carry the
other:

* **the writer refuses** — a NEW entry carrying `prompt`/`prompt_file` on a
  webhook fails, so the configurator relays it while the operator is still in
  the conversation;
* **the reader tolerates** — a document ALREADY on disk carrying one still
  loads, with a warning. Without this half the schema change is boot-fatal
  (`agent_loader._validate` raises `LoadError`, `load_all_agents` propagates
  the first one, and a resident load failure takes the whole boot down), and
  worse, `config_sync`'s entry salvage would DROP the operator's trigger
  entirely — silent loss of their configuration on upgrade.
"""
from __future__ import annotations

import json
import logging
import pathlib

import jsonschema
import pytest

SCHEMA = json.loads(pathlib.Path(
    "casa/rootfs/opt/casa/defaults/schema/triggers.v1.json").read_text())


def _doc(trigger: dict, version: int = 2) -> dict:
    return {"schema_version": version, "triggers": [trigger]}


# --- half 1: the writer refuses -------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("prompt", "Reply with HOOKFIRED and the payload."),
    ("prompt_file", "prompts/hook.md"),
])
@pytest.mark.parametrize("version", [1, 2])
def test_schema_rejects_a_prompt_on_a_webhook(field, value, version):
    """Both prose fields, both schema versions. `prompt_file` matters as much
    as `prompt`: `_build_triggers` ignores both for a webhook."""
    trigger = {"name": "hook", "type": "webhook", field: value}
    if version == 1:
        trigger["path"] = "/webhook/hook"   # required on a v1 webhook
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc(trigger, version), SCHEMA)


def test_schema_still_accepts_a_prompt_on_every_scheduled_type():
    """The rejection is scoped to webhooks — a scheduled trigger's prompt is
    the thing that is actually delivered (`trigger_registry` sends
    `content=trig.prompt`), so breaking it would be a far worse bug."""
    jsonschema.validate(_doc({
        "name": "hb", "type": "interval", "minutes": 5,
        "channel": "telegram", "prompt": "ping"}), SCHEMA)
    jsonschema.validate(_doc({
        "name": "am", "type": "cron", "schedule": "0 7 * * *",
        "channel": "telegram", "prompt": "brief me"}), SCHEMA)
    jsonschema.validate(_doc({
        "name": "once", "type": "date", "at": "2030-01-01T08:00:00+00:00",
        "channel": "telegram", "one_shot": True, "prompt": "remind me"}), SCHEMA)


def test_a_webhook_without_a_prompt_is_unaffected():
    jsonschema.validate(_doc({"name": "hook", "type": "webhook",
                              "clearance": "public"}), SCHEMA)


def test_the_upsert_writer_refuses_and_writes_nothing(tmp_path):
    """`config_trigger_upsert` -> `reminders.upsert_entry` -> the strict
    `_validate_candidate`. The operator's file must be untouched on refusal."""
    import reminders

    path = str(tmp_path / "triggers.yaml")
    reminders.upsert_entry(path, {"name": "keep", "type": "webhook",
                                  "clearance": "public"})
    before = pathlib.Path(path).read_text()

    with pytest.raises(ValueError):
        reminders.upsert_entry(path, {
            "name": "hook", "type": "webhook", "clearance": "public",
            "prompt": "Reply with HOOKFIRED."})

    assert pathlib.Path(path).read_text() == before, (
        "a refused upsert must leave the operator's file byte-identical")


def test_the_writer_stays_strict_when_the_reader_is_tolerant(tmp_path):
    """The tolerance must NOT leak into the write path.

    `reminders._schema_error` calls `agent_loader._validate` directly — the
    strict entry point — while readers go through `validate_persisted`. If the
    two were ever unified on the tolerant one, the writer would start accepting
    the field again and the whole fix would be inert.
    """
    import agent_loader
    import reminders

    doc = _doc({"name": "hook", "type": "webhook", "prompt": "x"})
    assert reminders._schema_error(doc, "t.yaml") is not None, (
        "the writer's validator must still reject a webhook prompt")
    # ...and the reader's does not.
    agent_loader.validate_persisted(doc, "triggers", "t.yaml")


# --- half 2: the reader tolerates, loudly ---------------------------------

def test_validate_persisted_tolerates_a_stored_webhook_prompt(caplog):
    """A document already on disk keeps loading, and says so."""
    import agent_loader

    doc = _doc({"name": "s26-plainhook", "type": "webhook",
                "clearance": "public", "prompt": "Reply with HOOKFIRED."})
    with caplog.at_level(logging.WARNING):
        agent_loader.validate_persisted(doc, "triggers", "agents/assistant/triggers.yaml")

    assert any("s26-plainhook" in r.message for r in caplog.records), (
        "the drop that used to be silent must now name the trigger")


def test_validate_persisted_does_not_mutate_the_callers_document():
    """The strip is on a copy. `config_sync`'s entry merge re-emits the
    document it validated; mutating it here would silently rewrite the
    operator's file as a side effect of validating it."""
    import agent_loader

    doc = _doc({"name": "hook", "type": "webhook", "prompt": "keep me"})
    agent_loader.validate_persisted(doc, "triggers", "t.yaml")
    assert doc["triggers"][0]["prompt"] == "keep me"


def test_validate_persisted_still_rejects_a_genuinely_invalid_document():
    """Tolerance is scoped to the one inert field. A document that is wrong for
    any other reason must still fail — otherwise this helper is a hole in every
    reader that uses it."""
    import agent_loader

    with pytest.raises(agent_loader.LoadError):
        agent_loader.validate_persisted(
            _doc({"name": "hook", "type": "webhook", "clearance": "private"}),
            "triggers", "t.yaml")
    with pytest.raises(agent_loader.LoadError):
        agent_loader.validate_persisted(
            _doc({"name": "bad", "type": "interval", "minutes": 5}),
            "triggers", "t.yaml")


def test_tolerance_does_not_apply_to_other_schemas():
    """Keyed on the triggers schema only — a `prompt` key elsewhere is not this
    helper's business, and stripping one would be an invisible edit."""
    import agent_loader

    with pytest.raises(agent_loader.LoadError):
        agent_loader.validate_persisted(
            {"schema_version": 1, "nonsense": True}, "character", "c.yaml")


def test_a_stored_prompt_is_still_not_delivered(caplog):
    """The behaviour the tolerance preserves: tolerated does not mean honoured.

    `_build_triggers` has always dropped it; this asserts the fix did not
    quietly start delivering it, which would be a change to an untrusted-input
    path nobody asked for.
    """
    from agent_loader import _build_triggers

    spec = _build_triggers(
        {"triggers": [{"name": "hook", "type": "webhook",
                       "prompt": "Reply with HOOKFIRED."}]},
        agent_dir="/nonexistent")[0]
    assert spec.prompt == ""
