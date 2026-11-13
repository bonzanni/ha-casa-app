"""The agent keeps a turn's route: the bot a turn was routed to when it was
born is the bot every output of that turn, and of the turns derived from it,
goes out through.

Two bots behind one transport (``two_endpoints``): 111 is Casa's own endpoint
(role ``assistant``) and 222 serves role ``finance``. Production runs one bot,
so these are the cases where using the wrong one would be visible.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from bus import BusMessage, MessageType
from channels import DeliveryOutcome
from channels.tg_address import CASA
from ingress_identity import ingress_identity
from specialist_registry import DelegationComplete
from test_agent_process import _make_agent, _mk_assistant, patch_retry_sleep
from test_pin_1121_turn_limit import _Client, _limit_script, _reset
from two_endpoints import (
    CASA_BOT, FINANCE_BOT, OPERATOR, FakeEndpoint, build_two_endpoints, calls,
)


_REPLY_METHODS = {"send", "send_response"}


def _replies(ep) -> list[tuple[str, tuple]]:
    return [c for c in calls(ep) if c[0] in _REPLY_METHODS]


def _agent_on(tmp_path, role: str, te):
    agent = _make_agent(tmp_path, role=role)
    agent._channel_manager.register(te.transport)
    return agent


async def _run(agent, msg):
    with patch("sdk_client_pool._default_make_client", _Client), \
            patch_retry_sleep():
        try:
            return await agent.handle_message(msg)
        finally:
            await agent.aclose()


def _narration(*, origin_route, context_route=None, channel="telegram",
               role="finance") -> BusMessage:
    origin = {"role": role, "channel": channel, "chat_id": str(OPERATOR),
              "cid": "route-1", "user_text": "reconcile the ledger"}
    if origin_route is not None:
        origin["_tg_bot"] = origin_route
    context = {"chat_id": str(OPERATOR), "cid": "route-1",
               "delegation_id": "deleg-1"}
    if context_route is not None:
        context["_tg_bot"] = context_route
    return BusMessage(
        type=MessageType.NOTIFICATION, source="assistant", target=role,
        content=DelegationComplete(
            delegation_id="deleg-1", agent="assistant", status="ok",
            text="Ledger reconciled.", origin=origin),
        channel=channel, context=context, on_delivery=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# The completion synthesizer carries the delegating turn's route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("context_route", [None, FINANCE_BOT],
                         ids=["notice_without_route", "notice_on_role_bot"])
async def test_a_narration_goes_through_the_origin_route_not_the_role_bot(
        tmp_path, context_route):
    """The delegating turn arrived on Casa's bot (111); the narrating role is
    bound to 222. The reply goes through 111, the route its origin
    persisted."""
    te = build_two_endpoints()
    agent = _agent_on(tmp_path, "finance", te)
    msg = _narration(origin_route=CASA_BOT, context_route=context_route)
    _reset([_mk_assistant("The ledger is reconciled.")])
    await _run(agent, msg)
    sent = _replies(te.casa)
    assert len(sent) == 1
    assert "The ledger is reconciled." in sent[0][1][0]
    assert sent[0][1][1]["_tg_bot"] == CASA_BOT
    assert _replies(te.finance) == []
    assert msg.on_delivery.await_count == 1


def test_the_synthesized_context_takes_the_route_from_the_origin(tmp_path):
    agent = _make_agent(tmp_path, role="finance")
    synth = agent._synthesize_delegation_turn(
        _narration(origin_route=CASA, context_route=FINANCE_BOT))
    assert synth.context["_tg_bot"] == CASA


def test_a_route_less_origin_adds_no_route(tmp_path):
    agent = _make_agent(tmp_path, role="finance")
    synth = agent._synthesize_delegation_turn(_narration(origin_route=None))
    assert "_tg_bot" not in synth.context


# ---------------------------------------------------------------------------
# The route is on the turn's origin, where delegation and tools read it
# ---------------------------------------------------------------------------


def test_the_route_is_a_copied_context_marker():
    import agent as agent_mod
    assert "_tg_bot" in agent_mod.COPIED_CONTEXT_MARKERS


# ---------------------------------------------------------------------------
# An opted-in webhook fire's final reply goes through its route
# ---------------------------------------------------------------------------


def _webhook_fire(route) -> BusMessage:
    return BusMessage(
        type=MessageType.SCHEDULED, source="webhook", target="finance",
        content="Webhook 'voicemail' triggered with payload: {}",
        channel="webhook",
        trusted_user_origin=ingress_identity(
            "webhook_trigger", webhook_name="voicemail", clearance="public"),
        context={"webhook_name": "voicemail", "cid": "w-1",
                 "_origin_route": "webhook_trigger",
                 "_origin_clearance": "public",
                 "_webhook_deliver": "operator",
                 # the execution chat keys the session; numeric, so it would
                 # override the endpoint's own chat if it leaked into delivery
                 "chat_id": "777001",
                 "_tg_bot": route},
    )


@pytest.mark.asyncio
async def test_an_opted_in_webhook_reply_goes_through_its_route(tmp_path):
    te = build_two_endpoints()
    agent = _agent_on(tmp_path, "finance", te)
    _reset([_mk_assistant("Voicemail from Bob: call him back.")])
    await _run(agent, _webhook_fire(FINANCE_BOT))
    sent = _replies(te.finance)
    assert len(sent) == 1
    assert "Voicemail from Bob" in sent[0][1][0]
    # a fresh delivery context: the route and nothing the fire carried
    assert sent[0][1][1] == {"_tg_bot": FINANCE_BOT}
    assert _replies(te.casa) == []


@pytest.mark.asyncio
async def test_an_opted_in_webhook_reply_on_the_casa_route(tmp_path):
    te = build_two_endpoints()
    agent = _agent_on(tmp_path, "finance", te)
    _reset([_mk_assistant("Voicemail from Bob.")])
    await _run(agent, _webhook_fire(CASA))
    sent = _replies(te.casa)
    assert len(sent) == 1 and sent[0][1][1] == {"_tg_bot": CASA}
    assert _replies(te.finance) == []


# ---------------------------------------------------------------------------
# The limit-stop line addressed to the operator is Casa's own notice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_operator_limit_line_goes_through_casas_endpoint(tmp_path):
    """A voice-origin narration on the finance role that stops at its step
    limit: the line goes to the operator's chat on Casa's bot, never on the
    role's."""
    te = build_two_endpoints()
    agent = _agent_on(tmp_path, "finance", te)
    msg = _narration(origin_route=None, channel="voice")
    msg.context["chat_id"] = "kitchen"
    _reset(_limit_script(None))
    await _run(agent, msg)
    lines = [c for c in calls(te.casa)
             if c[0] == "send" and "step limit" in c[1][0].lower()]
    assert len(lines) == 1
    assert lines[0][1][1] == {"chat_id": OPERATOR}
    assert calls(te.finance) == []


@pytest.mark.asyncio
async def test_the_operator_limit_line_without_telegram_is_logged(
        tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    agent = _make_agent(tmp_path, role="finance")
    msg = _narration(origin_route=None, channel="voice")
    _reset(_limit_script(None))
    await _run(agent, msg)
    assert any("no telegram channel" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Streaming before Casa's bot has any id: the turn behaves as an unready
# endpoint does, and the transport adds no outcome of its own
# ---------------------------------------------------------------------------


def _unready_casa():
    from telegram_channel_double import MemoryBotIdStore
    casa = FakeEndpoint("assistant", bot_id=None, is_casa=True, ready=False)
    te = build_two_endpoints(casa=casa, store=MemoryBotIdStore(None))
    assert te.transport.casa_route() == CASA
    return te


def _dm_on_casa_route(text: str = "go and check now") -> BusMessage:
    return BusMessage(
        type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
        content=text, channel="telegram",
        context={"chat_id": str(OPERATOR), "cid": "dm-1", "_tg_bot": CASA},
        trusted_user_origin=ingress_identity(
            "telegram", sender_id=str(OPERATOR), sender_is_operator=True),
        on_delivery=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_streaming_on_an_unready_casa_endpoint_reports_not_delivered(
        tmp_path):
    te = _unready_casa()
    agent = _agent_on(tmp_path, "assistant", te)
    msg = _dm_on_casa_route()
    _reset([_mk_assistant("All clear.")])
    await _run(agent, msg)
    names = [c[0] for c in calls(te.casa)]
    assert "create_on_token" in names
    on_token = te.casa.last_on_token
    assert callable(on_token)
    await on_token("more")  # raises nothing
    finals = _replies(te.casa)
    assert [c[0] for c in finals] == ["send_response"]
    assert finals[0][1][0] == "All clear."
    # the endpoint reported it not delivered; nothing acknowledged the turn
    assert msg.on_delivery.await_count == 0
    assert calls(te.finance) == []


@pytest.mark.asyncio
async def test_a_silent_turn_on_an_unready_casa_endpoint_tears_down(tmp_path):
    te = _unready_casa()
    agent = _agent_on(tmp_path, "assistant", te)
    _reset([_mk_assistant("<silent/>")])
    await _run(agent, _dm_on_casa_route())
    assert [c[0] for c in calls(te.casa)].count("turn_finished") == 1
    assert calls(te.finance) == []


@pytest.mark.asyncio
async def test_the_unready_endpoints_not_delivered_is_passed_through():
    te = _unready_casa()
    out = await te.transport.send_response("x", {"_tg_bot": CASA})
    assert out is DeliveryOutcome.NOT_DELIVERED


# ---------------------------------------------------------------------------
# The opening memory key is the receiving bot's: read from the route the
# receiving endpoint stamped on the inbound message
# ---------------------------------------------------------------------------


def _opening_capture(route):
    from agent import _TurnCapture
    msg = BusMessage(
        type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
        content="ping", channel="telegram",
        context={"chat_id": str(OPERATOR), "message_id": "501",
                 "_operator_turn": True, "_tg_bot": route},
        trusted_user_origin=ingress_identity(
            "telegram", sender_id=str(OPERATOR), sender_is_operator=True))
    return _TurnCapture(role="assistant", msg=msg, user_text="ping",
                        origin={}, scheduled=False, delegated_result=False)


def _strict_message_key(seen: list):
    from channels.tg_address import AddressError

    def message_key(channel, bot, chat, mid):
        seen.append((channel, bot, chat, mid))
        if isinstance(bot, bool) or not isinstance(bot, int):
            raise AddressError(f"not a Telegram bot id: {bot!r}")
        return f"key:{channel}:{bot}:{chat}:{mid}"
    return message_key


@pytest.mark.asyncio
async def test_the_opening_key_carries_the_receiving_bot(monkeypatch):
    from memory import capture, eligibility, occurrence
    seen: list = []
    monkeypatch.setattr(occurrence, "message_key", _strict_message_key(seen),
                        raising=False)
    monkeypatch.setattr(eligibility, "admit", lambda *a, **k: "rule")
    events: list = []
    monkeypatch.setattr(capture, "event",
                        lambda **kw: events.append(kw) or kw)
    monkeypatch.setattr(capture, "record", AsyncMock(
        return_value=type("R", (), {"source_key": "k"})()))
    assert await _opening_capture(FINANCE_BOT)._opening() is True
    assert seen == [("telegram", FINANCE_BOT, str(OPERATOR), "501")]
    assert events[0]["source_key"] == (
        f"key:telegram:{FINANCE_BOT}:{OPERATOR}:501")


@pytest.mark.asyncio
async def test_an_opening_on_the_casa_route_is_refused_not_raised(
        monkeypatch, caplog):
    from memory import capture, eligibility, occurrence
    monkeypatch.setattr(occurrence, "message_key", _strict_message_key([]),
                        raising=False)
    monkeypatch.setattr(eligibility, "admit", lambda *a, **k: "rule")
    record = AsyncMock()
    monkeypatch.setattr(capture, "record", record)
    caplog.set_level(logging.ERROR)
    assert await _opening_capture(CASA)._opening() is False
    assert record.await_count == 0
    assert any("opening failed" in r.getMessage() for r in caplog.records)
