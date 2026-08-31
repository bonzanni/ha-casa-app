"""#403 — the configurator's typed trigger edits.

`agents/<role>/triggers.yaml` has a second writer: the resident's own reminder
tools, running inside the Casa process on its event loop. The configurator runs
in a SEPARATE CLI child process, so its Read→Edit spans model thinking time and
no lock may be held across it — a reminder set inside that window was silently
discarded by the stale rewrite, and `config_git_commit`'s `git add -A` committed
the loss.

These pin the replacement: the hand edit is denied (see
`test_hooks.py::TestTriggerFileWriteGuard`) and the change is made HERE instead,
where the whole read-modify-write is one synchronous step on the loop.
"""
from __future__ import annotations

import json

import pytest
import yaml

pytestmark = pytest.mark.asyncio


@pytest.fixture
def configurator_origin():
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import agent as agent_mod

    agents_dir = tmp_path / "agents"
    (agents_dir / "butler").mkdir(parents=True)
    rt = type("_RT", (), {"role_configs": {"butler": object()},
                          "agents_dir": str(agents_dir)})()
    monkeypatch.setattr(agent_mod, "active_runtime", rt, raising=False)
    return rt


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _path(runtime):
    import os
    return os.path.join(runtime.agents_dir, "butler", "triggers.yaml")


def _write(runtime, triggers, version=2):
    p = _path(runtime)
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"schema_version": version, "triggers": list(triggers)},
                       fh, sort_keys=False)
    return p


def _read(runtime):
    with open(_path(runtime), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


HEARTBEAT = {"name": "heartbeat", "type": "interval", "minutes": 60,
             "channel": "telegram", "prompt": "hb"}
REMINDER = {"name": "reminder-abc", "type": "date", "one_shot": True,
            "at": "2099-01-01T09:00:00+01:00", "channel": "telegram",
            "prompt": "bins", "managed_by": "agent"}


class TestUpsert:
    async def test_creates_the_file_when_absent(self, configurator_origin,
                                                runtime):
        from tools import config_trigger_upsert
        out = _payload(await config_trigger_upsert.handler({
            "role": "butler", "name": "morning", "type": "cron",
            "schedule": "0 7 * * 1-5", "channel": "telegram",
            "prompt": "brief me"}))
        assert out["status"] == "ok" and out["outcome"] == "added"
        doc = _read(runtime)
        assert doc["schema_version"] == 2
        assert doc["triggers"][0]["schedule"] == "0 7 * * 1-5"

    async def test_leaves_a_reminder_untouched(self, configurator_origin,
                                               runtime):
        """The whole point: the reminder is still there afterwards."""
        from tools import config_trigger_upsert
        _write(runtime, [REMINDER])
        out = _payload(await config_trigger_upsert.handler({
            "role": "butler", "name": "heartbeat", "type": "interval",
            "minutes": 60, "channel": "telegram", "prompt": "hb"}))
        assert out["status"] == "ok"
        assert _read(runtime)["triggers"][0] == REMINDER

    async def test_replaces_in_place(self, configurator_origin, runtime):
        from tools import config_trigger_upsert
        _write(runtime, [HEARTBEAT, REMINDER])
        out = _payload(await config_trigger_upsert.handler({
            "role": "butler", "name": "heartbeat", "type": "interval",
            "minutes": 5, "channel": "telegram", "prompt": "hb"}))
        assert out["outcome"] == "replaced"
        doc = _read(runtime)
        assert [e["name"] for e in doc["triggers"]] == [
            "heartbeat", "reminder-abc"]
        assert doc["triggers"][0]["minutes"] == 5

    async def test_refuses_to_overwrite_a_reminder(self, configurator_origin,
                                                  runtime):
        from tools import config_trigger_upsert
        _write(runtime, [REMINDER])
        out = _payload(await config_trigger_upsert.handler({
            "role": "butler", "name": "reminder-abc", "type": "interval",
            "minutes": 5, "channel": "telegram", "prompt": "mine"}))
        assert out["status"] == "error"
        assert out["kind"] == "trigger_write_refused"
        assert _read(runtime)["triggers"] == [REMINDER]

    async def test_refuses_a_schema_violation_and_writes_nothing(
        self, configurator_origin, runtime,
    ):
        from tools import config_trigger_upsert
        _write(runtime, [HEARTBEAT])
        before = _read(runtime)
        out = _payload(await config_trigger_upsert.handler({
            "role": "butler", "name": "bad", "type": "cron",
            "channel": "telegram", "prompt": "x"}))   # no schedule
        assert out["status"] == "error"
        assert _read(runtime) == before

    async def test_drops_keys_the_schema_does_not_know(
        self, configurator_origin, runtime,
    ):
        """The typed surface is the filter: `additionalProperties: false` would
        reject an unknown key, so one must never reach the writer."""
        from tools import config_trigger_upsert
        out = _payload(await config_trigger_upsert.handler({
            "role": "butler", "name": "morning", "type": "cron",
            "schedule": "0 7 * * *", "channel": "telegram", "prompt": "x",
            "sneaky": "value"}))
        assert out["status"] == "ok"
        assert "sneaky" not in _read(runtime)["triggers"][0]

    async def test_refuses_a_non_resident_role(self, configurator_origin,
                                               runtime):
        """triggers.yaml is FORBIDDEN for a specialist or executor — creating
        one there is boot-fatal, so an unknown role is refused, not written."""
        from tools import config_trigger_upsert
        out = _payload(await config_trigger_upsert.handler({
            "role": "finance", "name": "x", "type": "interval", "minutes": 1,
            "channel": "telegram", "prompt": "x"}))
        assert out["kind"] == "unknown_role"

    async def test_refuses_an_unprivileged_caller(self, runtime):
        import agent as agent_mod
        from tools import config_trigger_upsert
        tok = agent_mod.origin_var.set({"role": "assistant"})
        try:
            out = _payload(await config_trigger_upsert.handler({
                "role": "butler", "name": "x", "type": "interval",
                "minutes": 1, "channel": "telegram", "prompt": "x"}))
        finally:
            agent_mod.origin_var.reset(tok)
        assert out["kind"] == "not_authorized"


class TestDelete:
    async def test_removes_an_operator_trigger(self, configurator_origin,
                                               runtime):
        from tools import config_trigger_delete
        _write(runtime, [HEARTBEAT, REMINDER])
        out = _payload(await config_trigger_delete.handler({
            "role": "butler", "name": "heartbeat"}))
        assert out["status"] == "ok"
        assert [e["name"] for e in _read(runtime)["triggers"]] == [
            "reminder-abc"]

    async def test_refuses_a_reminder_by_ownership_not_by_pretending_absence(
        self, configurator_origin, runtime,
    ):
        from tools import config_trigger_delete
        _write(runtime, [REMINDER])
        out = _payload(await config_trigger_delete.handler({
            "role": "butler", "name": "reminder-abc"}))
        assert out["kind"] == "not_owned"
        assert "cancel" in out["message"]
        assert _read(runtime)["triggers"] == [REMINDER]

    async def test_reports_not_found(self, configurator_origin, runtime):
        from tools import config_trigger_delete
        _write(runtime, [HEARTBEAT])
        out = _payload(await config_trigger_delete.handler({
            "role": "butler", "name": "nope"}))
        assert out["kind"] == "not_found"

    async def test_refuses_an_unprivileged_caller(self, runtime):
        import agent as agent_mod
        from tools import config_trigger_delete
        tok = agent_mod.origin_var.set({"role": "assistant"})
        try:
            out = _payload(await config_trigger_delete.handler({
                "role": "butler", "name": "heartbeat"}))
        finally:
            agent_mod.origin_var.reset(tok)
        assert out["kind"] == "not_authorized"


async def test_configurator_writers_serialize_under_the_pass_lock(tmp_path):
    """The serialization IS the fix. #403 got it by keeping the whole
    read-modify-write a single synchronous step on the loop, so nothing could
    land between the read and the write. #458 replaced that with
    ``trigger_write_lock.PASS_LOCK``, held by every writer of ``triggers.yaml``
    AND by the whole ``config_sync`` pass: a strictly stronger guarantee, and
    the reason the tools now hand the mutator to a worker thread instead of
    holding the loop. This pins the property the old "no awaits" assertion
    protected — no other writer can land between this one's read and write —
    against the mechanism now responsible for it.
    """
    import threading

    import reminders
    import trigger_write_lock

    for i, (fn, arg) in enumerate((
        (reminders.upsert_entry, {"name": "reminder-cccccc", "type": "date",
                                  "at": "2099-01-01T08:00:00+00:00",
                                  "one_shot": True, "channel": "telegram",
                                  "prompt": "x", "managed_by": "agent"}),
        (reminders.delete_entry, "reminder-cccccc"),
    )):
        # #778: a managed path, and a DISTINCT one per iteration — this used to
        # be a leaked `tempfile.mkdtemp()`, and the second iteration must not
        # see the first's mutation. `tmp_path` is a real filesystem path, so the
        # worker thread below is handed exactly what it was before.
        tmp = str(tmp_path / f"triggers-{i}.yaml")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("schema_version: 2\ntriggers: []\n")
        done = threading.Event()

        def _call(fn=fn, arg=arg, tmp=tmp) -> None:
            try:
                fn(tmp, arg)
            except Exception:  # noqa: BLE001 — delete on an absent name is fine
                pass
            done.set()

        with trigger_write_lock.PASS_LOCK:
            t = threading.Thread(target=_call, daemon=True)
            t.start()
            assert not done.wait(timeout=1.0), (
                f"{fn.__name__} completed while PASS_LOCK was held — it does "
                f"not take the lock, so a config_sync pass can clobber its write"
            )
        assert done.wait(timeout=5.0)
        t.join(timeout=5.0)

    # #778: both iterations wrote inside the managed root, and nowhere else.
    assert sorted(q.name for q in tmp_path.iterdir()) == [
        "triggers-0.yaml", "triggers-1.yaml"]


class TestLegacySchemaV1:
    """Both reviewers, round 1: a `schema_version: 1` document REQUIRES `path`
    on a webhook trigger (v2 removed it — the name is the endpoint — and
    forbids it). Omitting the field from the typed surface left such a file
    with no supported route at all: the hand edit is denied and the tool could
    not express what the schema demands."""

    async def test_a_v1_webhook_can_be_written_with_its_path(
        self, configurator_origin, runtime,
    ):
        from tools import config_trigger_upsert
        _write(runtime, [], version=1)
        out = _payload(await config_trigger_upsert.handler({
            "role": "butler", "name": "paypal", "type": "webhook",
            "path": "/paypal"}))
        assert out["status"] == "ok"
        assert _read(runtime)["triggers"][0]["path"] == "/paypal"

    async def test_a_v2_webhook_still_refuses_a_path(self, configurator_origin,
                                                     runtime):
        """Present on the surface, refused by the schema on a v2 document —
        the correct answer, and a different thing from a dead end."""
        from tools import config_trigger_upsert
        _write(runtime, [], version=2)
        out = _payload(await config_trigger_upsert.handler({
            "role": "butler", "name": "paypal", "type": "webhook",
            "path": "/paypal"}))
        assert out["status"] == "error"
        assert _read(runtime)["triggers"] == []
