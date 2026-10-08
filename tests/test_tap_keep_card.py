"""#1362: a file button that leaves its card live — the `keep_card` button kind. A call
button may carry ``"keep_card": true`` only when its stored call is #1303's file sibling
(a capability whose one slot delivers ``operator_file``). Its tap runs the whole admission
chain but claims and commits nothing: the card keeps its text and every button, the file
goes out through the same pinned one-call desk use, and no line is ever written over the
card by that tap (INV-PROP-010)."""
from __future__ import annotations

import dataclasses
import json
import types

import pytest

import pinned_run as pr
import result_broker as rb
import specialist_desk as sd
import tools as tools_mod
from plugin_grants import PluginContract, ResultContractMap
from test_desk_tap import LABEL, OPERATOR as DESK_OPERATOR, _echo, _tool, env as desk_env  # noqa: F401
from test_desk_tap import _tap as _desk_tap
from test_proposal_slot import ARTIFACT, SEG, SRV, _identity
from test_proposal_slot import _tool as _slot_tool
from test_proposal_tap import OPERATOR, RID, _cq, _settle, _tap, env as tap_env  # noqa: F401
from test_tap_delivers_file import FILE, _respond

PDF = f"mcp__plugin_{SEG}_{SRV}__pdf"
APPLY = f"mcp__plugin_{SEG}_{SRV}__apply"
MORE = f"mcp__plugin_{SEG}_{SRV}__more"


# --- the deposit -------------------------------------------------------------------

def _call():
    tools = {APPLY: _slot_tool("apply"),
             MORE: _slot_tool("more", "capability", ("proposal",), {"proposal": "operator_proposal"}),
             PDF: _slot_tool("pdf", "capability", ("doc",), {"doc": "operator_file"})}
    cmap = ResultContractMap(tools=tools, plugins={SEG: PluginContract(ARTIFACT, True, frozenset(),
                                                                       name="probe")})
    entry = _slot_tool("offer", "capability", ("proposal",), {"proposal": "operator_proposal"})
    return types.SimpleNamespace(identity=_identity(), entry=entry, contract_map=cmap, protected={},
                                 tool_use_id="call-1")


@pytest.fixture
def labels(monkeypatch):
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "finance": types.SimpleNamespace(character=types.SimpleNamespace(name="Finance"))})


CONFIRM = {"label": "Confirm", "call": {"tool": "apply", "arguments": {"pid": 7}}}
SEE = {"label": "See PDF", "call": {"tool": "pdf", "arguments": {"doc_id": 3}}, "keep_card": True}


def _deposit(*buttons):
    return rb.proposal_ok(json.dumps({"text": "Card 1 of 2 · to confirm", "buttons": list(buttons)}),
                          _call())


def test_a_file_button_may_keep_its_card_and_is_kept_as_its_kind(labels):
    parsed, why = _deposit(CONFIRM, SEE)
    assert why is None
    assert "keep_card" not in parsed["buttons"][0]
    assert parsed["buttons"][1]["keep_card"] is True
    assert parsed["buttons"][1]["call"]["runtime_name"] == PDF


@pytest.mark.parametrize("button", [
    {"label": "Confirm", "call": {"tool": "apply", "arguments": {}}, "keep_card": True},
    {"label": "More", "call": {"tool": "more", "arguments": {}}, "keep_card": True},
    {"label": "See PDF", "call": {"tool": "pdf", "arguments": {}}, "keep_card": "yes"},
    {"label": "See PDF", "call": {"tool": "pdf", "arguments": {}}, "keep_card": 1},
    {"label": "See PDF", "call": {"tool": "pdf", "arguments": {}}, "keep_card": False},
    {"label": "📎 Add", "arm_file": True, "keep_card": True},
])
def test_keep_card_on_anything_but_a_file_call_or_not_json_true_is_a_bad_proposal(labels, button):
    assert _deposit(CONFIRM, button) == (None, "bad_proposal")


async def test_the_registered_kinds_name_the_keep_card_button(labels, monkeypatch):
    import verdict_broker as vb
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    parsed, _ = _deposit(CONFIRM, SEE)
    seen = {}

    async def fake_post(chat_id, text, labels_, rid, *, post=None):
        seen["meta"] = vb.BROKER.get_meta(namespace="proposal", scope=f"proposal:{chat_id}",
                                         request_id=rid)
        return 501
    monkeypatch.setattr(rb, "_post_operator_proposal", fake_post)
    monkeypatch.setattr(rb, "_telegram_channel", lambda: None)
    post = rb.PostRecord(role="finance", operator_id=42, plugin=SEG, slot="proposal",
                         tool_use_id="call-1", owner="d-1", posted_at=1.0, kind="proposal")
    delivered, *_ = await rb._post_proposal(_identity(), SEG, "proposal", _call(), parsed,
                                            "📊 Finance", post)
    assert delivered and seen["meta"]["kinds"] == ["call", "keep_card"]


# --- the tap -----------------------------------------------------------------------

def _keep_meta():
    return {"options": ["Confirm", "See PDF"], "kinds": ["call", "keep_card"],
            "calls": [{"server": "api", "wire_name": "apply", "runtime_name": APPLY,
                       "proposal": False, "arguments": {"pid": 7}, "canonical": '{"pid":7}'},
                      {"server": "api", "wire_name": "pdf", "runtime_name": PDF,
                       "proposal": True, "arguments": {"doc_id": 3}, "canonical": '{"doc_id":3}'}]}


async def test_a_keep_tap_dispatches_and_leaves_the_card_live(tap_env):
    tap_env.register(meta=_keep_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    await _settle()
    assert len(tap_env.taps) == 1 and tap_env.taps[0]["idx"] == 1
    assert tap_env.bot.edited == [] and tap_env.bot.markups == [] and tap_env.bot.sent == []
    assert tap_env.broker.is_live_unclaimed(namespace="proposal", scope=f"proposal:{OPERATOR}",
                                            request_id=RID)


async def test_a_keep_tap_can_be_repeated_and_the_card_still_settles_from_another_button(tap_env):
    tap_env.register(meta=_keep_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|0")) == "✔"
    await _settle()
    assert [t["idx"] for t in tap_env.taps] == [1, 1, 0]
    assert len(tap_env.bot.edited) == 1 and "⏳ Confirm" in tap_env.bot.edited[0]["text"]
    # settled now: a later keep tap runs nothing
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "already answered"
    await _settle()
    assert len(tap_env.taps) == 3


async def test_a_keep_tap_still_runs_the_whole_admission_chain(tap_env):
    tap_env.register(meta=_keep_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1", user_id=7)) == "not for you"
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1", message_id=999)) == "expired"
    await _settle()
    assert tap_env.taps == []


async def test_a_keep_tap_on_an_expired_card_runs_nothing(tap_env):
    tap_env.register(meta=_keep_meta(), deadline=-1)
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "expired"
    await _settle()
    assert tap_env.taps == []


async def test_a_keep_tap_on_a_full_desk_is_told_and_leaves_the_card(tap_env):
    tap_env.register(meta=_keep_meta())
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    held = [desk.reserve() for _ in range(64)]
    held = [h for h in held if h is not None]
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    await _settle()
    assert tap_env.taps == []
    assert any("busy" in m["text"] for m in tap_env.bot.sent)
    assert tap_env.bot.edited == []
    for h in held:
        h.release()


# --- the desk ----------------------------------------------------------------------

@pytest.fixture
def keep_env(desk_env):
    desk_env.build = dataclasses.replace(desk_env.build, contract_map=ResultContractMap(
        tools={"mcp__plugin_probe_probe__apply": _tool("apply"),
               FILE: _tool("package", "capability", provides=("package",),
                           delivers={"package": "operator_file"})},
        plugins=desk_env.build.contract_map.plugins))
    return desk_env


def _desk_meta(**over):
    base = {"options": ["Confirm", "See PDF"], "kinds": ["call", "keep_card"], "calls": [
        {"server": "probe", "wire_name": "apply", "runtime_name": "mcp__plugin_probe_probe__apply",
         "proposal": False, "arguments": {}, "canonical": "{}"},
        {"server": "probe", "wire_name": "package", "runtime_name": FILE, "proposal": True,
         "arguments": {}, "canonical": "{}"}]}
    base.update(over)
    return base


async def test_a_landed_file_from_a_keep_tap_writes_no_line_over_the_card(keep_env):
    env = keep_env
    env.respond = _respond(pr.Capture("delivered", "operator_file"))
    await _desk_tap(env, idx=1, meta=_desk_meta())
    assert env.channel.marks == [] and env.channel.notices == [] and env.channel.replies == []
    assert [(e.who, e.text) for e in env.desk.log] == [("operator", "[tapped: See PDF]"),
                                                       ("specialist", sd.POSTED_FILE)]
    assert _echo() == [f"{LABEL} applied your tap (See PDF)."]


async def test_a_failed_keep_tap_is_told_without_marking_the_card(keep_env):
    env = keep_env
    env.respond = _respond(pr.Capture("withheld", "not delivered"))
    await _desk_tap(env, idx=1, meta=_desk_meta())
    assert env.channel.marks == []
    assert env.channel.notices == [(DESK_OPERATOR, f"{LABEL} could not apply your tap (not delivered).")]
    assert _echo() == [f"{LABEL} refused your tap (See PDF): not delivered."]


async def test_a_refused_keep_tap_is_told_without_marking_the_card(keep_env):
    env = keep_env
    env.fence.names.add("probe")
    await _desk_tap(env, idx=1, meta=_desk_meta())
    assert env.calls == [] and env.channel.marks == []
    assert env.channel.notices == [(DESK_OPERATOR, f"{LABEL} could not apply your tap (plugin erasing).")]


async def test_an_admitted_keep_tap_past_the_deadline_still_sends_the_file(keep_env):
    """d1 (both reviewers): a keep tap that waited on the desk past the card's TTL is
    not dropped in silence — it was admitted while the card was live, so the file goes out."""
    env = keep_env
    env.respond = _respond(pr.Capture("delivered", "operator_file"))
    await _desk_tap(env, idx=1, meta=_desk_meta(deadline=0.0))
    assert len(env.calls) == 1 and env.channel.marks == []
    assert _echo() == [f"{LABEL} applied your tap (See PDF)."]


async def test_a_settling_tap_past_the_deadline_still_executes_nothing(keep_env):
    env = keep_env
    await _desk_tap(env, idx=0, meta=_desk_meta(deadline=0.0))
    assert env.calls == [] and env.channel.marks == ["⌛ expired"]


async def test_a_refused_permit_on_a_keep_tap_is_told_without_marking(keep_env):
    env = keep_env
    env.limiter.refuse = True
    await _desk_tap(env, idx=1, meta=_desk_meta())
    assert env.calls == [] and env.channel.marks == []
    assert any("busy" in text for _, text in env.channel.notices)


async def test_a_keep_tap_on_a_settled_card_known_only_to_the_broker_runs_nothing(tap_env):
    """A settled card the channel's memory has dropped is answered from the broker's
    retired record: a keep tap there must not run, since nothing is live."""
    tap_env.register(meta=_keep_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|0")) == "✔"
    await _settle()
    tap_env.ch._proposal_settled.clear()
    assert tap_env.broker.get_meta(namespace="proposal", scope=f"proposal:{OPERATOR}",
                                   request_id=RID) is not None
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "expired"
    await _settle()
    assert [t["idx"] for t in tap_env.taps] == [0]
