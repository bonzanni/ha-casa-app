"""#1095 — a change to a specialist that has open conversations warns first.

Ruling (ruling-1095-3, refined by ruling-1095-4): before a persona is applied to
a specialist, or it is upgraded, rolled back or uninstalled, while it has open
conversations, Casa says what will happen to each and asks for confirmation;
cancelling changes nothing; with no open conversations there is no warning. For
the ordinary changes the warning carries the ruled sentence verbatim.

The carrier is a pending-first tool result plus an acknowledgement input
(`acknowledged_conversations`) on the commit tool. These tests drive the REAL
handlers; the population is a REAL `EngagementRegistry`; only the library commit
(to count calls) and the uninstall's disk/reload leaves are doubled. The
uninstall cases reuse `test_plugin_erase_flow`'s fixtures: the REAL erase gate,
question ids, fence, records and choice grants.

RED at the base for the reason each test names: no handler consults open
engagements, so each commits (or opens the erase question) on the first call.
Assertions are counts; new result fields are read with `.get()` so their absence
fails an assertion rather than raising.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import plugin_erasure as pe
from test_plugin_erase_flow import _spec, flow, sflow  # noqa: F401 — fixtures

pytestmark = pytest.mark.asyncio

RULED = ("When it resumes, it picks up Casa's updated settings, but it keeps its "
         "personality and the plugin versions it started with. To get everything "
         "new, close it with `/complete` and ask the assistant for a new conversation.")
# #1409: the ruling's sentence names the CONFIGURED assistant; with no persona
# registered (these fixtures, and every tool description, built at import) it
# says "the assistant".
PENDING = "open_conversations_unconfirmed"
SUBJECT = "specialist:fin"


# --- population ----------------------------------------------------------------------

@pytest.fixture
def reg(tmp_path, monkeypatch):
    import tools as tools_mod
    from engagement_registry import EngagementRegistry
    r = EngagementRegistry(tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    monkeypatch.setattr(tools_mod, "_engagement_registry", r)
    return r


async def _open(reg, *, slug="fin", topic=101, task="Review household budget",
                kind="specialist", job=None):
    origin = {"channel": "telegram", "chat_id": "42", "user_id": 42, "role": "assistant"}
    if job is not None:
        origin["job"] = job
    return await reg.create(kind=kind, role_or_type=slug, driver="in_casa", task=task,
                            origin=origin, topic_id=topic)


def _out(r) -> dict:
    return json.loads(r["content"][0]["text"])


def _assert_pending(out: dict, *ids: str) -> None:
    assert out.get("ok") is False, out
    assert out.get("kind") == PENDING, out
    listed = [row.get("engagement_id") for row in out.get("conversations", [])]
    for i in ids:
        assert listed.count(i) == 1, (i, listed)


# --- persona_apply on a specialist ----------------------------------------------------

@pytest.fixture
def persona(monkeypatch, tmp_path):
    """Every precondition of `persona_apply`'s specialist arm satisfied, the
    library apply (the commit) counted."""
    import agent_loader
    import persona_install
    import persona_pack
    import role_artifact
    import role_slot
    import specialist_registry
    import tools as tools_mod

    calls: list = []
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    monkeypatch.setattr(persona_install, "installed_personas_root", lambda: tmp_path)
    monkeypatch.setattr(persona_pack, "load_persona_pack",
                        lambda *a, **k: SimpleNamespace(persona_id="acme/calm", version="1.0.0"))

    class _Index:
        def load(self):
            return None

        def installed_component_role_dirs(self):
            return {"fin": tmp_path / "fin"}
    monkeypatch.setattr(specialist_registry, "InstalledSpecialistIndex", _Index)
    monkeypatch.setattr(role_artifact, "load_role_artifact", lambda d: object())
    monkeypatch.setattr(role_slot, "materialize_role", lambda **k: object())
    monkeypatch.setattr(role_slot, "_ha_model_options", lambda: {})
    monkeypatch.setattr(agent_loader, "make_candidate_compile_validator",
                        lambda role: (lambda *a, **k: None))

    def apply(**kw):
        calls.append(kw)
        return SimpleNamespace(binding=SimpleNamespace(binding_digest="d" * 8))
    monkeypatch.setattr(persona_install, "apply_persona_override", apply)
    return calls


def _persona_args(**extra):
    return {"target_role_id": "specialist:fin", "persona_id": "acme/calm",
            "persona_version": "1.0.0", **extra}


async def test_rc1_persona_apply_warns_then_commits_on_acknowledgement(reg, persona):
    import tools as tools_mod
    a = await _open(reg)
    out = _out(await tools_mod.persona_apply.handler(_persona_args()))
    _assert_pending(out, a.id)
    assert RULED in out.get("warning", ""), out
    assert len(persona) == 0, "the persona was applied before the operator confirmed"
    out = _out(await tools_mod.persona_apply.handler(
        _persona_args(acknowledged_conversations=[a.id])))
    assert out.get("ok") is True, out
    assert len(persona) == 1
    assert reg.get(a.id).status == "active"          # an ordinary change closes nothing


# --- rollback ---------------------------------------------------------------------------

@pytest.fixture
def rollback(monkeypatch):
    import specialist_bundle_journal
    import specialist_install
    import tools as tools_mod

    state = SimpleNamespace(calls=0, entries=0, during=None)
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())

    def lib(**kw):
        state.calls += 1
        if state.during is not None:
            state.during()
        return (SimpleNamespace(slug="fin", state="active"),
                SimpleNamespace(slug="fin", removed_artifact_ids=(), journal_path=None))
    monkeypatch.setattr(specialist_install, "rollback_specialist", lib)

    async def seq(slug, **kw):
        return {"ok": True, "reloaded": ["fin"], "verify": {}}
    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", seq)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)
    monkeypatch.setattr(tools_mod, "_swap_removal_disclosure", lambda txn: {})
    real_run = tools_mod._run_bundle_transaction

    async def run(op):
        state.entries += 1
        return await real_run(op)
    monkeypatch.setattr(tools_mod, "_run_bundle_transaction", run)
    return state


async def test_rc2_rollback_warns_and_enters_no_transaction(reg, rollback):
    import tools as tools_mod
    a = await _open(reg)
    out = _out(await tools_mod.specialist_rollback.handler({"slug": "fin"}))
    _assert_pending(out, a.id)
    assert RULED in out.get("warning", ""), out
    assert rollback.entries == 0 and rollback.calls == 0
    out = _out(await tools_mod.specialist_rollback.handler(
        {"slug": "fin", "acknowledged_conversations": [a.id]}))
    assert out.get("ok") is True, out
    assert rollback.calls == 1
    assert reg.get(a.id).status == "active"


async def test_rc8_an_engagement_opened_during_an_acknowledged_rollback_is_named(reg, rollback):
    """j3: an arrival after the acknowledgement does not refuse the change — it
    commits, and the result names the arrival with the ruled sentence."""
    import threading
    import tools as tools_mod
    a = await _open(reg)
    entered, go = threading.Event(), threading.Event()

    def during():
        entered.set()
        go.wait(5)
    rollback.during = during
    task = asyncio.get_running_loop().create_task(tools_mod.specialist_rollback.handler(
        {"slug": "fin", "acknowledged_conversations": [a.id]}))
    for _ in range(500):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    b = await _open(reg, topic=102, task="Plan the holiday budget")
    go.set()
    out = _out(await task)
    assert out.get("ok") is True, out
    assert rollback.calls == 1
    named = out.get("opened_after_confirmation", [])
    assert sum(b.id in str(row) for row in named) == 1, out
    assert sum(a.id in str(row) for row in named) == 0, out
    assert RULED in json.dumps(out), out
    assert reg.get(a.id).status == "active" and reg.get(b.id).status == "active"


# --- upgrade --------------------------------------------------------------------------

@pytest.fixture
def upgrade(monkeypatch):
    """`specialist_upgrade` past `validate_resume_inputs` (the run's q11 probe
    setup), the library commit counted and its instance state chosen per call."""
    import specialist_bundle_journal
    import specialist_install
    import specialist_receipt
    import tools as tools_mod

    class _Checked:
        ok = True

        class receipt:  # noqa: N801
            receipt_id = "r1"
            receipt_digest = "d"
            plugins = ()

        class component:  # noqa: N801
            component_id = "c"
            version = "2"
            slug = "fin"
            checksum = "x"

            class role:  # noqa: N801
                role = {}
            default_persona_ref = None
            default_persona_checksum = None
        dependencies = ()
        root_digest = "rd"

    state = SimpleNamespace(calls=0, entries=0, prunes=0, states=[])
    monkeypatch.setattr(specialist_install, "validate_resume_inputs", lambda **k: _Checked)
    monkeypatch.setattr(specialist_receipt, "load", lambda rid, **k: object())
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())

    def lib(**kw):
        state.calls += 1
        st = state.states.pop(0) if state.states else "active"
        return (SimpleNamespace(slug="fin", state=st),
                SimpleNamespace(slug="fin", removed_artifact_ids=(), journal_path=None))
    monkeypatch.setattr(specialist_install, "upgrade_specialist", lib)

    async def seq(slug, **kw):
        return {"ok": True, "reloaded": ["fin"], "verify": {}}
    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", seq)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)
    monkeypatch.setattr(tools_mod, "_swap_removal_disclosure", lambda txn: {})

    def prune(rid):
        state.prunes += 1
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", prune)
    monkeypatch.setattr(specialist_install, "reclaim_staging_tree", lambda d: None)
    monkeypatch.setattr(tools_mod, "_pending_resume_inputs",
                        lambda inspection, receipt, *, tool_name: {"receipt_id": "r1"})
    real_run = tools_mod._run_bundle_transaction

    async def run(op):
        state.entries += 1
        return await real_run(op)
    monkeypatch.setattr(tools_mod, "_run_bundle_transaction", run)
    return state


def _upgrade_args(version="2", **extra):
    return {"slug": "fin", "component_id": "c", "version": version, "root_digest": "rd",
            "staged_dir": "/nonexistent", "receipt_id": "r1", **extra}


@pytest.mark.parametrize("version", ["2", "1"])
async def test_rc3_upgrade_warns_before_the_transaction(reg, upgrade, version):
    """D-2: the warning is not keyed on a version change — a same-version
    (settings) upgrade warns exactly like a version change."""
    import tools as tools_mod
    a = await _open(reg)
    out = _out(await tools_mod.specialist_upgrade.handler(_upgrade_args(version)))
    _assert_pending(out, a.id)
    assert RULED in out.get("warning", ""), out
    assert upgrade.entries == 0 and upgrade.calls == 0 and upgrade.prunes == 0
    out = _out(await tools_mod.specialist_upgrade.handler(
        _upgrade_args(version, acknowledged_conversations=[a.id])))
    assert out.get("ok") is True, out
    assert upgrade.calls == 1


async def test_rc9_the_pending_configuration_follow_up_carries_the_acknowledgement(reg, upgrade):
    """The follow-up re-commit of an upgrade that landed pending-configuration
    carries the first acknowledgement (re-passed by the model, echoed by the
    result) — the operator is not asked twice; an arrival is named."""
    import tools as tools_mod
    a = await _open(reg)
    upgrade.states = ["pending-configuration", "active"]
    first = _out(await tools_mod.specialist_upgrade.handler(
        _upgrade_args(acknowledged_conversations=[a.id])))
    assert first.get("ok") is True and first.get("state") == "pending-configuration", first
    assert first.get("acknowledged_conversations") == [a.id], first
    b = await _open(reg, topic=102, task="Plan the holiday budget")
    second = _out(await tools_mod.specialist_upgrade.handler(
        _upgrade_args(acknowledged_conversations=first["acknowledged_conversations"])))
    assert second.get("kind") != PENDING and second.get("ok") is True, second
    assert upgrade.calls == 2
    named = second.get("opened_after_confirmation", [])
    assert sum(b.id in str(row) for row in named) == 1, second


# --- uninstall: the warning composes with the erase question ------------------------

def _prompts(state) -> int:
    return len(state.prompts)


async def _uninstall(tm, **args):
    return _out(await tm.specialist_uninstall.handler({"slug": "fin", **args}))


async def test_rc4_uninstall_erase_unset_voids_the_question_and_posts_nothing(reg, sflow):
    a = await _open(reg)
    pe.QUESTIONS.open(SUBJECT)
    out = await _uninstall(sflow.tm)
    _assert_pending(out, a.id)
    assert pe.QUESTIONS.current(SUBJECT) is None
    assert _prompts(sflow) == 0 and sflow.uninstalled == []


async def test_rc5_j4_a_declined_reask_after_a_complete_erasure(reg, sflow):
    """M2: the erasure completed but could not be delivered; the re-ask is
    warned. The refused call has exactly base's effect on erase state (the old
    question is void, so the complete fence lifts and its records cannot be
    taken) and nothing more (no new question, no DM)."""
    a = await _open(reg)
    q = pe.QUESTIONS.open(SUBJECT)
    pe.RECORDS.put("plugin:fin.bank", "1" * 64, "complete", "bank gone", q)
    pe.FENCE.raise_(["fin.bank"], SUBJECT, q)
    pe.FENCE.completed(["fin.bank"], q)
    assert pe.FENCE.fenced("fin.bank") is not False
    out = await _uninstall(sflow.tm)
    _assert_pending(out, a.id)
    assert pe.FENCE.fenced("fin.bank") is False
    assert pe.QUESTIONS.current(SUBJECT) is None
    assert _prompts(sflow) == 0
    out = await _uninstall(sflow.tm, erase_data=True)
    assert out.get("kind") == "erase_not_confirmed", out
    assert sflow.uninstalled == []


async def test_rc6_keep_path_closes_the_question_then_commits_on_acknowledgement(reg, sflow):
    a = await _open(reg)
    pe.QUESTIONS.open(SUBJECT)
    out = await _uninstall(sflow.tm, erase_data=False)
    _assert_pending(out, a.id)
    assert pe.QUESTIONS.current(SUBJECT) is None
    assert sflow.uninstalled == []
    out = await _uninstall(sflow.tm, erase_data=False, acknowledged_conversations=[a.id])
    assert out.get("ok") is True, out
    assert sflow.uninstalled == [True] and _prompts(sflow) == 0


async def test_rc7_a_set_that_grew_since_the_acknowledgement_re_warns(reg, sflow):
    a = await _open(reg)
    b = await _open(reg, topic=102, task="Plan the holiday budget")
    out = await _uninstall(sflow.tm, acknowledged_conversations=[a.id])
    _assert_pending(out, b.id)
    assert _prompts(sflow) == 0 and sflow.uninstalled == []


# --- one constant ----------------------------------------------------------------------

async def test_rc10_the_ruled_sentence_has_one_source(reg, persona):
    import tools as tools_mod
    notice = getattr(tools_mod, "SPECIALIST_OPEN_CONVERSATION_NOTICE", None)
    assert notice == RULED
    for t in (tools_mod.persona_apply, tools_mod.specialist_upgrade,
              tools_mod.specialist_rollback):
        assert t.description.count(RULED) == 1, t.name
    a = await _open(reg)
    out = _out(await tools_mod.persona_apply.handler(_persona_args()))
    assert out.get("warning", "").count(RULED) == 1, out
    assert reg.get(a.id).status == "active"


async def test_rc11_the_ruled_sentence_names_the_configured_assistant(
        reg, persona, monkeypatch):
    """#1409: a renamed assistant is the one the operator is told to ask —
    never the default persona's name."""
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(character=SimpleNamespace(name="Marta"))})
    named = RULED.replace("the assistant", "Marta")
    a = await _open(reg)
    out = _out(await tools_mod.persona_apply.handler(_persona_args()))
    assert out.get("warning", "").count(named) == 1, out
    assert "Ellen" not in json.dumps(out)
    assert reg.get(a.id).status == "active"
