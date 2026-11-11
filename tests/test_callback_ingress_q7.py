"""A card left in the resident's chat by an agent that now has its own chat:
its tap is refused at the callback's ingress with one line naming that chat,
before any namespace's handler runs — no Stop, no edit, no claim, no lane
reservation. A Casa card, the resident's own card, a card of an agent without
its own chat, and any tap on an agent's own bot are untouched."""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import pytest

import agent_lane
import own_voice
import plugin_bindings
import plugin_stop
import run_admission
import verdict_broker as vb
from test_proposal_tap import OPERATOR, RID, _cq, _settle, env  # noqa: F401
from two_endpoints import CASA_BOT as BOT

pytestmark = pytest.mark.asyncio

LINE = "This card belongs to Finance's chat now — open it there."
CID = "c" * 24


@pytest.fixture
def world(env, monkeypatch):
    """The resident's endpoint (``env.ch``), with Finance declaring its own
    chat, and every effect a tap could have counted."""
    import tools
    monkeypatch.setattr(tools, "_display_name_for_role",
                        lambda role: {"finance": "Finance", "assistant": "Ellen"}.get(role, role))
    declared = {"finance"}
    monkeypatch.setattr(own_voice, "declared", lambda role: role in declared)
    counts = types.SimpleNamespace(stops=0, claims=0, reservations=0, declared=declared)

    async def fake_stop(*a, **k):
        counts.stops += 1
        return {"outcome": "stopped"}
    monkeypatch.setattr(plugin_stop, "stop", fake_stop)
    real_claim = env.broker.claim

    def claim(**kw):
        counts.claims += 1
        return real_claim(**kw)
    monkeypatch.setattr(env.broker, "claim", claim)
    real_reserve = agent_lane.Lane.reserve

    def reserve(self):
        counts.reservations += 1
        return real_reserve(self)
    monkeypatch.setattr(agent_lane.Lane, "reserve", reserve)
    # a stored call of the plugin's declared stop tool, for the proposal's Stop
    monkeypatch.setattr(plugin_stop, "stop_plugins", AsyncMock(return_value=[]))
    # the run card: an admitted request whose binding's executing agent is Finance
    conf = types.SimpleNamespace(status="admitted", binding_ref="b-1", bot_id=BOT,
                                 operator_id=OPERATOR, plugin="browse", request_key="k1")
    binding = types.SimpleNamespace(chat_id=OPERATOR, operator_id=OPERATOR,
                                    chain=("assistant", "finance"))
    monkeypatch.setattr(run_admission.CONFIRMATIONS, "peek",
                        lambda cid: conf if cid == CID else None)
    monkeypatch.setattr(plugin_bindings.BINDINGS, "peek",
                        lambda ref: binding if ref == "b-1" else None)
    import operator_calls
    monkeypatch.setattr(operator_calls, "is_configured_operator", lambda chat, op: True)
    return types.SimpleNamespace(env=env, ch=env.ch, counts=counts, binding=binding)


async def _tap(ch, update):
    await ch._on_inline_callback(update, context=None)
    await _settle()
    for t in list(ch._turn_tasks):
        await t
    return [c.args[0] for c in update.callback_query.answer.await_args_list]


def _nothing_happened(w):
    assert (w.counts.stops, w.counts.claims, w.counts.reservations) == (0, 0, 0)
    assert w.env.bot.edited == [] and w.env.bot.markups == [] and w.env.bot.sent == []
    assert w.env.taps == []


async def test_a_pre_upgrade_run_card_stop_is_refused_with_one_line(world):
    answers = await _tap(world.ch, _cq(data=f"v1|run_stop|{CID}|0"))
    assert answers == [LINE]
    _nothing_happened(world)


async def test_a_pre_upgrade_stored_proposal_stop_is_refused_with_one_line(world, monkeypatch):
    stop_call = {"server": "api", "wire_name": "stop", "runtime_name": "mcp__plugin_probe_api__stop",
                 "proposal": False, "arguments": {"ref": "request:k1"}, "canonical": "{}"}
    world.env.register(meta={"options": ["Stop"], "calls": [stop_call]})
    monkeypatch.setattr(plugin_stop, "stop_plugins", AsyncMock(return_value=[
        ("finance", None, types.SimpleNamespace(artifact_id="7" * 64), "probe", "stop")]))
    answers = await _tap(world.ch, _cq(data=f"v1|proposal|{RID}|0"))
    assert answers == [LINE]
    _nothing_happened(world)
    live = world.env.broker.pending(namespace="proposal", scope=f"proposal:{BOT}:{OPERATOR}")
    assert live == [RID]                                    # the card was not settled


async def test_a_pre_upgrade_stored_call_button_is_refused_with_one_line(world):
    world.env.register()
    answers = await _tap(world.ch, _cq(data=f"v1|proposal|{RID}|1"))
    assert answers == [LINE]
    _nothing_happened(world)


async def test_a_pre_upgrade_question_of_the_agent_is_refused_with_one_line(world):
    scope = f"dm:{BOT}:{OPERATOR}"
    world.env.broker.register(
        namespace="resident_ask", scope=scope, request_id="a" * 32, timeout_s=60,
        detached=True, meta={"options": ["Yes", "No"], "chat_id": OPERATOR, "bot_id": BOT,
                             "operator_id": OPERATOR, "role": "finance", "_scope": scope})
    answers = await _tap(world.ch, _cq(data=f"v1|resident_ask|{'a' * 32}|0"))
    assert answers == [LINE]
    _nothing_happened(world)


async def test_a_casa_card_is_untouched(world):
    """A question with no agent of its own (a Casa card) reaches its handler."""
    scope = f"dm:{BOT}:{OPERATOR}"
    world.env.broker.register(
        namespace="resident_ask", scope=scope, request_id="b" * 32, timeout_s=60,
        detached=True, meta={"options": ["Yes", "No"], "chat_id": OPERATOR, "bot_id": BOT,
                             "operator_id": OPERATOR, "_scope": scope})
    answers = await _tap(world.ch, _cq(data=f"v1|resident_ask|{'b' * 32}|0"))
    assert answers == ["✔"] and world.counts.claims == 1


async def test_a_card_of_an_agent_without_its_own_chat_is_untouched(world):
    world.counts.declared.clear()
    answers = await _tap(world.ch, _cq(data=f"v1|run_stop|{CID}|0"))
    assert answers == ["stopping…"] and world.counts.stops == 1


async def test_the_residents_own_run_card_is_untouched(world):
    world.binding.chain = ("assistant",)
    world.counts.declared.add("assistant")
    answers = await _tap(world.ch, _cq(data=f"v1|run_stop|{CID}|0"))
    assert answers == ["stopping…"] and world.counts.stops == 1


async def test_a_tap_on_an_agents_own_bot_is_never_refused_there(world):
    world.ch.is_casa = False
    answers = await _tap(world.ch, _cq(data=f"v1|run_stop|{CID}|0"))
    assert answers == ["stopping…"] and world.counts.stops == 1


async def test_an_unreadable_declaration_leaves_the_card_to_its_own_checks(world, monkeypatch):
    def broken(role):
        raise RuntimeError("state unreadable")
    monkeypatch.setattr(own_voice, "declared", broken)
    answers = await _tap(world.ch, _cq(data=f"v1|run_stop|{CID}|0"))
    assert answers == ["stopping…"]
