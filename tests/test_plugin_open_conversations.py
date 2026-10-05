"""#1145 — a plugin change that reaches a specialist with open conversations warns first.

Rulings: #1255 (option 2) — one sentence per kind of change, with the plugin's name;
#1263 (option 1) — an acknowledgement naming an open conversation of ANY specialist the
change reaches confirms it, and the open conversations of the other reached specialists
are named after the change, never a reason to refuse or ask again.

These tests drive the REAL plugin handlers and the REAL sync cores against
`test_plugin_tools`'s in-memory registry harness; the population is a REAL
`EngagementRegistry`. plugin_remove's erase arms reuse `test_plugin_erase_flow`'s
`flow` fixture: the REAL erase gate, question ids, records and choice grants.

RED at the base for the reason each test names: no plugin handler consults open
engagements or reads `acknowledged_conversations`, so each commits (or posts the erase
question) on the first call and names nothing afterwards. Assertions are counts; new
result fields are read with `.get()` so their absence fails an assertion rather than
raising.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import plugin_erase_consent as pec
import plugin_erasure as pe
from test_plugin_erase_flow import ART, _spec, flow  # noqa: F401 — fixtures
from test_plugin_tools import _State, _entry, _pr, _wire

pytestmark = pytest.mark.asyncio

PENDING = "open_conversations_unconfirmed"
OLD_NOTICE_FRAGMENT = "close it with `/complete`"
ADDED = "Its open conversations keep the plugins they started with and will not get probe."
REMOVED = ("Its open conversations keep probe loaded but lose its approvals, so a "
           "protected call asks again and earlier references to it stop working.")
UPDATED = "Its open conversations keep the previous version and lose its approvals."


# --- population ----------------------------------------------------------------------

@pytest.fixture
def reg(tmp_path, monkeypatch):
    import tools as tools_mod
    from engagement_registry import EngagementRegistry
    r = EngagementRegistry(tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    monkeypatch.setattr(tools_mod, "_engagement_registry", r)
    return r


async def _open(reg, slug="fin", *, topic=101, task="Review household budget"):
    origin = {"channel": "telegram", "chat_id": "42", "user_id": 42, "role": "assistant"}
    return await reg.create(kind="specialist", role_or_type=slug, driver="in_casa",
                            task=task, origin=origin, topic_id=topic)


# --- the plugin harness ---------------------------------------------------------------

class _Harness:
    def __init__(self, st, tm):
        self.st, self.tm = st, tm
        self.invalidations: list = []

    def entry(self):
        return next((e for e in self.st.raw["plugins"] if e.get("name") == "probe"), None)

    def snapshot(self) -> str:
        return json.dumps(self.st.raw, sort_keys=True)

    def count(self, what: str) -> int:
        return self.st.log.count(what)


def _counted_invalidation(monkeypatch, tm, h):
    real = tm._invalidate_lifecycle

    def counted(**kw):
        h.invalidations.append(kw)
        return real(**kw)
    monkeypatch.setattr(tm, "_invalidate_lifecycle", counted)


@pytest.fixture
def plug(monkeypatch, tmp_path):
    """`probe` registered (operator-owned, targets set per test) on the in-memory
    registry; publish returns a different artifact; reload succeeds."""
    st = _State()
    st.raw["plugins"].append(_entry())
    tm = _wire(monkeypatch, tmp_path, st, publish=_pr())
    monkeypatch.setattr(tm, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    h = _Harness(st, tm)
    _counted_invalidation(monkeypatch, tm, h)
    return h


def _targets(h, *targets):
    h.st.raw["plugins"][0]["targets"] = list(targets)


def _out(r) -> dict:
    return json.loads(r["content"][0]["text"])


async def _call(h, tool: str, **args) -> dict:
    return _out(await getattr(h.tm, tool).handler(args))


def _assert_pending(out: dict, sentence: str, expected: set) -> None:
    """The pending-warning contract: envelope, exact (slug, id) rows, one block per
    specialist, the sentence exactly once per block, never the #1095 notice."""
    assert out.get("ok") is False, out
    assert out.get("kind") == PENDING, out
    assert out.get("activation_committed") is False, out
    assert out.get("runtime_ready") is False, out
    assert out.get("verify") == {}, out
    rows = {(r.get("slug"), r.get("engagement_id")) for r in out.get("conversations", [])}
    assert rows == expected, (rows, expected)
    assert len(out.get("conversations", [])) == len(expected), out
    warning = out.get("warning", "")
    slugs = sorted({s for s, _ in expected})
    assert warning.count("The specialist ") == len(slugs), warning
    assert warning.count(sentence) == len(slugs), warning
    assert OLD_NOTICE_FRAGMENT not in warning, warning
    # The sentence follows EACH block: every block ends with it.
    blocks = [b for b in warning.split("The specialist ") if b.strip()]
    for b in blocks:
        assert b.rstrip().endswith(sentence), (b, sentence)


def _named_after(out: dict) -> set:
    return {(r.get("slug"), r.get("engagement_id"))
            for r in out.get("opened_after_confirmation", [])}


def _assert_no_notice_of_1095(h, out: dict) -> None:
    assert h.tm.SPECIALIST_OPEN_CONVERSATION_NOTICE not in json.dumps(out), out


# --- clause 1: an unconfirmed call warns and changes nothing (one per tool) ------------

async def test_add_warns_before_publish(reg, plug):
    plug.st.raw["plugins"] = []
    f1 = await _open(reg, "fin")
    before = plug.snapshot()
    out = await _call(plug, "plugin_add", name="probe", repo="o/r", ref="v1",
                      targets=["specialist:fin"])
    _assert_pending(out, ADDED, {("fin", f1.id)})
    _assert_no_notice_of_1095(plug, out)
    assert plug.snapshot() == before
    assert (plug.count("publish"), plug.count("save"), len(plug.invalidations)) == (0, 0, 0)


async def test_new_assignment_warns(reg, plug):
    _targets(plug)
    f1 = await _open(reg, "fin")
    before = plug.snapshot()
    out = await _call(plug, "plugin_assign", name="probe", target="specialist:fin")
    _assert_pending(out, ADDED, {("fin", f1.id)})
    assert plug.snapshot() == before
    assert (plug.count("save"), len(plug.invalidations)) == (0, 0)


async def test_unassign_warns(reg, plug):
    _targets(plug, "specialist:fin")
    f1 = await _open(reg, "fin")
    before = plug.snapshot()
    out = await _call(plug, "plugin_unassign", name="probe", target="specialist:fin")
    _assert_pending(out, REMOVED, {("fin", f1.id)})
    assert plug.snapshot() == before
    assert (plug.count("save"), len(plug.invalidations)) == (0, 0)
    assert plug.entry()["targets"] == ["specialist:fin"]


async def test_update_warns_before_publish(reg, plug):
    _targets(plug, "specialist:fin")
    f1 = await _open(reg, "fin")
    before = plug.snapshot()
    out = await _call(plug, "plugin_update", name="probe", new_ref="v2")
    _assert_pending(out, UPDATED, {("fin", f1.id)})
    assert plug.snapshot() == before
    assert (plug.count("resolve"), plug.count("publish"), plug.count("save"),
            len(plug.invalidations)) == (0, 0, 0, 0)


async def test_remove_without_eraser_warns(reg, flow):
    flow.specs = []
    flow.st.raw["plugins"][0]["targets"] = ["specialist:fin"]
    f1 = await _open(reg, "fin")
    before = json.dumps(flow.st.raw, sort_keys=True)
    out = _out(await flow.tm.plugin_remove.handler({"name": "probe"}))
    _assert_pending(out, REMOVED, {("fin", f1.id)})
    assert json.dumps(flow.st.raw, sort_keys=True) == before
    assert flow.st.log.count("save") == 0 and flow.prompts == []


async def test_erasing_remove_warns_before_question(reg, flow):
    flow.st.raw["plugins"][0]["targets"] = ["specialist:fin"]
    f1 = await _open(reg, "fin")
    out = _out(await flow.tm.plugin_remove.handler({"name": "probe"}))
    _assert_pending(out, REMOVED, {("fin", f1.id)})
    assert flow.prompts == [] and flow.st.log.count("save") == 0
    assert pe.QUESTIONS.current("plugin:probe") is None


async def test_erasing_remove_warning_withdraws_an_open_question(reg, flow):
    flow.st.raw["plugins"][0]["targets"] = ["specialist:fin"]
    f1 = await _open(reg, "fin")
    pe.QUESTIONS.open("plugin:probe")
    out = _out(await flow.tm.plugin_remove.handler({"name": "probe"}))
    _assert_pending(out, REMOVED, {("fin", f1.id)})
    assert flow.prompts == [] and pe.QUESTIONS.current("plugin:probe") is None
    assert "withdrawn" in out.get("detail", ""), out


async def test_unreached_ack_does_not_confirm(reg, plug):
    _targets(plug, "specialist:fin")
    f1 = await _open(reg, "fin")
    o1 = await _open(reg, "ops", topic=202)
    before = plug.snapshot()
    out = await _call(plug, "plugin_update", name="probe", new_ref="v2",
                      acknowledged_conversations=[o1.id])
    _assert_pending(out, UPDATED, {("fin", f1.id)})
    assert plug.snapshot() == before
    assert (plug.count("publish"), plug.count("save"), len(plug.invalidations)) == (0, 0, 0)


async def test_warning_groups_all_reached_conversations(reg, plug):
    _targets(plug, "specialist:fin", "specialist:ops")
    f1 = await _open(reg, "fin")
    f2 = await _open(reg, "fin", topic=102, task="Second")
    o1 = await _open(reg, "ops", topic=202)
    out = await _call(plug, "plugin_update", name="probe", new_ref="v2")
    _assert_pending(out, UPDATED, {("fin", f1.id), ("fin", f2.id), ("ops", o1.id)})
    assert (plug.count("publish"), plug.count("save")) == (0, 0)


# --- clause 2: a confirmed call commits, and names what the acknowledgement did not ----

async def test_acknowledged_add_commits(reg, plug):
    plug.st.raw["plugins"] = []
    f1 = await _open(reg, "fin")
    out = await _call(plug, "plugin_add", name="probe", repo="o/r", ref="v1",
                      targets=["specialist:fin"], acknowledged_conversations=[f1.id])
    assert out.get("kind") != PENDING, out
    assert (plug.count("publish"), plug.count("save")) == (1, 1)
    assert plug.entry()["targets"] == ["specialist:fin"]
    assert _named_after(out) == set(), out


async def test_acknowledged_assign_commits(reg, plug):
    _targets(plug)
    f1 = await _open(reg, "fin")
    out = await _call(plug, "plugin_assign", name="probe", target="specialist:fin",
                      acknowledged_conversations=[f1.id])
    assert out.get("kind") != PENDING, out
    assert plug.count("save") == 1 and plug.entry()["targets"] == ["specialist:fin"]


async def test_acknowledged_unassign_commits(reg, plug):
    _targets(plug, "specialist:fin")
    f1 = await _open(reg, "fin")
    out = await _call(plug, "plugin_unassign", name="probe", target="specialist:fin",
                      acknowledged_conversations=[f1.id])
    assert out.get("ok") is True, out
    assert plug.count("save") == 1 and plug.entry()["targets"] == []
    assert len(plug.invalidations) == 1


async def test_update_cross_specialist_confirmation(reg, plug):
    _targets(plug, "specialist:fin", "specialist:ops")
    f1 = await _open(reg, "fin")
    o1 = await _open(reg, "ops", topic=202)
    out = await _call(plug, "plugin_update", name="probe", new_ref="v2",
                      acknowledged_conversations=[f1.id])
    assert out.get("kind") != PENDING, out
    assert (plug.count("publish"), plug.count("save"), len(plug.invalidations)) == (1, 1, 1)
    assert plug.entry()["artifact_id"] == "a" * 64
    assert _named_after(out) == {("ops", o1.id)}, out
    assert out.get("open_conversation_notice", "").count(UPDATED) == 1, out


async def test_remove_cross_specialist_confirmation(reg, flow):
    flow.specs = []
    flow.st.raw["plugins"][0]["targets"] = ["specialist:fin", "specialist:ops"]
    f1 = await _open(reg, "fin")
    o1 = await _open(reg, "ops", topic=202)
    out = _out(await flow.tm.plugin_remove.handler(
        {"name": "probe", "acknowledged_conversations": [f1.id]}))
    assert out.get("kind") != PENDING, out
    assert flow.st.log.count("save") == 1
    assert not any(e["name"] == "probe" for e in flow.st.raw["plugins"])
    assert _named_after(out) == {("ops", o1.id)}, out
    assert out.get("open_conversation_notice", "").count(REMOVED) == 1, out


async def test_erased_finish_not_ready_still_reports_ops(reg, flow, monkeypatch):
    import reload as reload_mod
    flow.st.raw["plugins"][0]["targets"] = ["specialist:fin", "specialist:ops"]
    f1 = await _open(reg, "fin")
    o1 = await _open(reg, "ops", topic=202)

    async def failing_dispatch(scope, *, runtime, role=None):
        return {"status": "error"}
    monkeypatch.setattr(reload_mod, "dispatch", failing_dispatch)
    pe.RECORDS.put("plugin:probe", ART, "complete", "Everything erased.",
                   pe.QUESTIONS.open("plugin:probe"))
    out = _out(await flow.tm.plugin_remove.handler(
        {"name": "probe", "erase_data": True, "acknowledged_conversations": [f1.id]}))
    assert not any(e["name"] == "probe" for e in flow.st.raw["plugins"])
    assert flow.st.log.count("save") == 1 and flow.prompts == []
    assert out.get("ok") is False and out.get("activation_committed") is True, out
    assert out.get("runtime_ready") is False, out
    assert _named_after(out) == {("ops", o1.id)}, out
    assert out.get("open_conversation_notice", "").count(REMOVED) == 1, out


async def test_postcommit_conversation_is_reported(reg, plug, monkeypatch):
    _targets(plug, "specialist:fin", "specialist:ops")
    f1 = await _open(reg, "fin")
    f2 = await _open(reg, "fin", topic=102, task="Second")
    o1 = await _open(reg, "ops", topic=202)
    real = plug.tm._reload_and_verify_targets
    late: list = []

    async def reload_then_open(*a, **kw):
        late.append(await _open(reg, "ops", topic=203, task="Late"))
        return await real(*a, **kw)
    monkeypatch.setattr(plug.tm, "_reload_and_verify_targets", reload_then_open)
    out = await _call(plug, "plugin_update", name="probe", new_ref="v2",
                      acknowledged_conversations=[f1.id, f2.id, o1.id])
    assert plug.count("save") == 1, out
    assert _named_after(out) == {("ops", late[0].id)}, out
    assert out.get("open_conversation_notice", "").count(UPDATED) == 1, out


async def test_closed_engagement_confirms(reg, plug):
    _targets(plug, "specialist:fin", "specialist:ops")
    f1 = await _open(reg, "fin")
    f2 = await _open(reg, "fin", topic=102, task="Second")
    o1 = await _open(reg, "ops", topic=202)
    await reg.mark_cancelled(f1.id)
    assert reg.get(f1.id) is not None and reg.get(f1.id).status != "active"
    out = await _call(plug, "plugin_update", name="probe", new_ref="v2",
                      acknowledged_conversations=[f1.id])
    assert out.get("kind") != PENDING, out
    assert plug.count("save") == 1
    assert _named_after(out) == {("fin", f2.id), ("ops", o1.id)}, out
    assert out.get("open_conversation_notice", "").count(UPDATED) == 2, out


# --- clause 4: plugin_remove carries the acknowledgement through the erase question ----

def _capture_continuations(monkeypatch, tm) -> list:
    delivered: list = []

    def deliverer(channel, eng, *, inbound_reservation=None):
        async def deliver(text):
            delivered.append(text)
            return True
        return deliver
    monkeypatch.setattr(tm, "_engagement_deliverer", deliverer)
    return delivered


@pytest.mark.parametrize("choice,erase", [(pec.KEEP, "false"), (pec.ERASE, "true")],
                         ids=["keep", "erase"])
async def test_continuation_carries_ack(reg, flow, monkeypatch, choice, erase):
    flow.st.raw["plugins"][0]["targets"] = ["specialist:fin", "specialist:ops"]
    f1 = await _open(reg, "fin")
    await _open(reg, "ops", topic=202)
    delivered = _capture_continuations(monkeypatch, flow.tm)
    out = _out(await flow.tm.plugin_remove.handler(
        {"name": "probe", "acknowledged_conversations": [f1.id]}))
    assert out.get("kind") == "erase_choice_pending", out
    [prompt] = flow.prompts
    assert await prompt["continue_cb"](choice) is True
    [text] = delivered
    call = f"plugin_remove(name='probe', erase_data={erase}"
    assert text.count(call) == 1, text
    assert text.count(f"acknowledged_conversations={[f1.id]!r}") == 1, text


async def test_keep_call_with_the_carried_ack_commits(reg, flow):
    flow.st.raw["plugins"][0]["targets"] = ["specialist:fin"]
    f1 = await _open(reg, "fin")
    out = _out(await flow.tm.plugin_remove.handler(
        {"name": "probe", "erase_data": False, "acknowledged_conversations": [f1.id]}))
    assert out.get("ok") is True, out
    assert not any(e["name"] == "probe" for e in flow.st.raw["plugins"])


# --- regression companions (GREEN at the base; not red cases) -------------------------

async def test_erased_finish_without_ack_is_not_refused(reg, flow):
    flow.st.raw["plugins"][0]["targets"] = ["specialist:fin"]
    await _open(reg, "fin")
    pe.RECORDS.put("plugin:probe", ART, "complete", "Everything erased.",
                   pe.QUESTIONS.open("plugin:probe"))
    out = _out(await flow.tm.plugin_remove.handler({"name": "probe", "erase_data": True}))
    assert out.get("kind") != PENDING, out
    assert out.get("ok") is True and flow.st.log.count("save") == 1
    assert flow.prompts == []


async def test_nothing_open_is_unchanged(reg, plug):
    _targets(plug, "specialist:fin")
    await _open(reg, "ops", topic=202)                    # an unreached specialist
    out = await _call(plug, "plugin_unassign", name="probe", target="specialist:fin")
    assert out.get("ok") is True, out
    for key in ("acknowledged_conversations", "opened_after_confirmation",
                "opened_while_this_change_ran", "open_conversation_notice",
                "conversations", "warning"):
        assert key not in out, (key, out)


@pytest.mark.parametrize("args,kind", [
    ({"name": "probe", "target": "specialist:fin", "profile": "BAD PROFILE"},
     "invalid_profile"),
    ({"name": "probe", "target": "specialist:fin", "profile": "readonly"},
     "profile_missing_in_plugin"),
    ({"name": "nope", "target": "specialist:fin"}, "not_registered"),
], ids=["invalid_profile", "profile_missing_in_plugin", "not_registered"])
async def test_a_local_guard_refusal_is_not_a_warning(reg, plug, args, kind):
    _targets(plug)
    await _open(reg, "fin")
    out = await _call(plug, "plugin_assign", **args)
    assert out.get("kind") == kind, out
    assert "warning" not in out and plug.count("save") == 0


async def test_a_noop_assign_does_not_warn(reg, plug):
    _targets(plug, "specialist:fin")
    await _open(reg, "fin")
    out = await _call(plug, "plugin_assign", name="probe", target="specialist:fin")
    assert out.get("ok") is True and out.get("was_assigned") is True, out
    assert "warning" not in out and plug.count("save") == 0


async def test_a_noop_unassign_does_not_warn(reg, plug):
    _targets(plug, "resident:assistant")
    await _open(reg, "fin")
    out = await _call(plug, "plugin_unassign", name="probe", target="specialist:fin")
    assert out.get("ok") is True and out.get("was_assigned") is False, out
    assert "warning" not in out and plug.count("save") == 0


@pytest.mark.parametrize("tool,args", [
    ("plugin_assign", {"name": "probe", "target": "specialist:fin"}),
    ("plugin_unassign", {"name": "probe", "target": "specialist:fin"}),
    ("plugin_update", {"name": "probe", "new_ref": "v2"}),
    ("plugin_remove", {"name": "probe"}),
], ids=["assign", "unassign", "update", "remove"])
async def test_an_owned_entry_keeps_its_refusal(reg, plug, tool, args):
    """An entry a specialist's bundle owns is refused `owned_by_specialist` as at
    the base, never warned about (red-case acceptor's return, 2026-10-05)."""
    _targets(plug, "specialist:fin")
    plug.st.raw["plugins"][0]["owner"] = "specialist:fin"
    await _open(reg, "fin")
    before = plug.snapshot()
    out = await _call(plug, tool, **args)
    assert out.get("kind") == "owned_by_specialist", out
    assert "warning" not in out and plug.count("save") == 0
    assert plug.snapshot() == before
