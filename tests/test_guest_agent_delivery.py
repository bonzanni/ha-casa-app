"""A summoned turn's final text reaches the group's one inline message through
the seam the agent uses for every other reply: ``Agent.handle_message`` over
the real ``TelegramTransport``, with the specialist's real ``TelegramChannel``
(bot 222, ``finance``) registered for the route and a recording bot behind it.

The agent holds the transport, never an endpoint, so what is pinned here is
the forwarding by route as much as the delivery itself. Casa's own lines — the
plugin-health notice, the turn-limit line — keep going to the DM.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import RetryAfter

import channels.guest_reply as guest_reply
import plugin_health
from bus import BusMessage, MessageBus, MessageType
from channels import DeliveryOutcome
from error_kinds import ErrorKind, _USER_MESSAGES
from ingress_identity import ingress_identity
from test_agent_process import _make_agent
from test_guest_reply import DECLINE, FAIL, ASKED_ELSEWHERE, _Bot, _opened
from two_endpoints import FINANCE_BOT, build_two_endpoints

pytestmark = [pytest.mark.asyncio]

CHAT = "100"
KEY = "gq-1"


def _finance_endpoint(bot):
    from channels.telegram import TelegramChannel
    ep = TelegramChannel(bot_token="222:t", chat_id=CHAT, default_agent="finance",
                         bot=bot, is_casa=False, known_bot_id=FINANCE_BOT)
    ep._app = MagicMock(bot=bot)
    return ep


async def _summoned(tmp_path, bot):
    """The finance agent over the transport, with a reply already opened and
    bound under the key a summoned turn's context carries."""
    agent = _make_agent(tmp_path, role="finance")
    ep = _finance_endpoint(bot)
    h = build_two_endpoints(finance=ep)
    agent._channel_manager.register(h.transport)
    reply = await _opened(bot, key=KEY)
    ep._guest_replies[KEY] = reply
    return agent, ep, reply


def _turn(text: str = "what is the rate?") -> BusMessage:
    m = BusMessage(type=MessageType.CHANNEL_IN, source="telegram", target="finance",
                   content=text, channel="telegram",
                   context={"chat_id": CHAT, "_tg_bot": FINANCE_BOT,
                            "_guest_query": KEY, "cid": "c1"})
    m.trusted_user_origin = ingress_identity(
        "telegram", sender_id="9001", sender_is_operator=False)
    return m


def _limit_stop(text: str):
    async def _proc(msg, on_token=None, turn_report=None, **kw):
        turn_report["limit_stop"] = {"turns": 21}
        return text
    return _proc


def _notice(monkeypatch, text="PLUGIN-DEGRADED x"):
    plugin_health._notice_memo.clear()
    monkeypatch.setattr(plugin_health, "render_notice",
                        lambda role, path=plugin_health.HEALTH_PATH: text)
    return text


async def test_text_reaches_the_group_through_the_transport(tmp_path, monkeypatch):
    bot = _Bot()
    agent, ep, reply = await _summoned(tmp_path, bot)
    spies = {m: AsyncMock(name=m) for m in ("send_response", "finalize_stream",
                                            "finalize_response_stream")}
    for m, spy in spies.items():
        monkeypatch.setattr(ep, m, spy)
    with patch.object(agent, "_process", AsyncMock(return_value="The rate is **1.08**.")):
        await agent.handle_message(_turn())
    assert bot.edits[-1]["inline_message_id"] == reply.inline_message_id
    assert bot.edits[-1]["text"] == "The rate is 1.08."
    assert [(e.offset, e.length) for e in bot.edits[-1]["entities"]] == [(12, 4)]
    assert all(spy.await_count == 0 for spy in spies.values())
    assert bot.sent == []
    assert KEY not in ep._guest_replies
    assert reply.terminal is True


async def test_a_turn_cancelled_during_a_flood_wait_still_lands_the_terminal_edit(
        tmp_path, monkeypatch):
    """The turn's dispatch is torn down (its role evicted) while the terminal
    edit waits out a ``RetryAfter``: the edit still lands before the binding
    goes, the cancellation still ends the dispatch, and a later teardown
    edits nothing."""
    bot = _Bot(edits=[RetryAfter(30), None])
    agent, ep, reply = await _summoned(tmp_path, bot)
    waits: list[float] = []
    waiting, release = asyncio.Event(), asyncio.Event()

    async def sleep(s):
        waits.append(s)
        waiting.set()
        await release.wait()
    monkeypatch.setattr(guest_reply, "_sleep", sleep)

    bus = MessageBus()
    bus.register("finance", agent.handle_message)
    bus.start_agent_loop("finance")
    with patch.object(agent, "_process", AsyncMock(return_value="the answer")):
        await bus.send(_turn())
        await asyncio.wait_for(waiting.wait(), timeout=5)
        [dispatch] = bus.dispatch_tasks_for("finance")
        assert [e["text"] for e in bot.edits] == ["the answer"]      # the rejected edit
        evicted = asyncio.create_task(bus.unregister_and_wait("finance"))
        for _ in range(10):
            await asyncio.sleep(0)
        # the turn is cancelled; the edit still waits and the binding stands
        assert not evicted.done()
        assert KEY in ep._guest_replies and len(bot.edits) == 1
        release.set()
        await asyncio.wait_for(evicted, timeout=5)
    assert dispatch.cancelled()                                      # the cancellation went on
    assert waits == [30.0]
    assert [e["text"] for e in bot.edits] == ["the answer", "the answer"]
    assert reply.terminal is True
    assert KEY not in ep._guest_replies
    ctx = {**_turn().context, "_turn_scope": NS(detached_ask=False)}
    await ep.turn_finished(ctx)                                       # nothing left to edit
    assert len(bot.edits) == 2


async def test_an_agent_exception_fails_the_reply_with_the_guest_error_line(tmp_path):
    bot = _Bot()
    agent, ep, reply = await _summoned(tmp_path, bot)
    with patch.object(agent, "_process", AsyncMock(side_effect=RuntimeError("boom"))):
        await agent.handle_message(_turn())
    assert [e["text"] for e in bot.edits] == [FAIL]
    assert _USER_MESSAGES[ErrorKind.UNKNOWN] not in [e["text"] for e in bot.edits]
    assert bot.sent == []


@pytest.mark.parametrize("output", ["", "<silent/>", "  \n "])
async def test_empty_and_silent_output_go_to_turn_finished(tmp_path, monkeypatch, output):
    bot = _Bot()
    agent, ep, reply = await _summoned(tmp_path, bot)
    finishes = AsyncMock(name="guest_finish")
    monkeypatch.setattr(ep, "guest_finish", finishes)
    with patch.object(agent, "_process", AsyncMock(return_value=output)):
        await agent.handle_message(_turn())
    assert [e["text"] for e in bot.edits] == [DECLINE]
    assert finishes.await_count == 0
    assert KEY not in ep._guest_replies


async def test_a_detached_ask_says_ill_answer_there(tmp_path, monkeypatch):
    bot = _Bot()
    agent, ep, reply = await _summoned(tmp_path, bot)
    finishes = AsyncMock(name="guest_finish")
    monkeypatch.setattr(ep, "guest_finish", finishes)

    async def _asks_then_goes_quiet(msg, on_token=None, turn_report=None, **kw):
        # what ask_user does once its question is live: marks the turn's scope
        msg.context["_turn_scope"].detached_ask = True
        return ""

    with patch.object(agent, "_process", _asks_then_goes_quiet):
        await agent.handle_message(_turn())
    assert [e["text"] for e in bot.edits] == [ASKED_ELSEWHERE]
    assert finishes.await_count == 0


async def test_a_pending_health_notice_goes_to_the_dm_not_the_group(tmp_path, monkeypatch):
    notice = _notice(monkeypatch)
    bot = _Bot()
    agent, ep, reply = await _summoned(tmp_path, bot)
    with patch.object(agent, "_process", AsyncMock(return_value="The rate is 1.08.")):
        await agent.handle_message(_turn())
    assert bot.edits[-1]["text"] == "The rate is 1.08."
    assert [(m["chat_id"], m["text"]) for m in bot.sent] == [(int(CHAT), notice)]
    # consumed: the next turn carries no notice
    with patch.object(agent, "_process", AsyncMock(return_value="again")):
        await agent.handle_message(BusMessage(
            type=MessageType.CHANNEL_IN, source="telegram", target="finance",
            content="x", channel="telegram",
            context={"chat_id": CHAT, "_tg_bot": FINANCE_BOT, "cid": "c2"}))
    assert [m["text"] for m in bot.sent] == [notice, "again"]


async def test_a_not_delivered_terminal_releases_the_notice_cooldown(tmp_path, monkeypatch):
    from telegram.error import BadRequest
    notice = _notice(monkeypatch)
    forgotten: list = []
    real_forget = plugin_health.forget_notice
    monkeypatch.setattr(plugin_health, "forget_notice",
                        lambda role, n: (forgotten.append((role, n)), real_forget(role, n)))
    bot = _Bot(edits=[BadRequest("Message to edit not found")])
    agent, ep, reply = await _summoned(tmp_path, bot)
    with patch.object(agent, "_process", AsyncMock(return_value="The rate is 1.08.")):
        await agent.handle_message(_turn())
    assert forgotten == [("finance", notice)]
    assert bot.sent == []                     # no notice send after a lost terminal
    assert len(bot.edits) == 1


async def test_a_raising_guest_delivery_during_a_limit_stop_still_posts_casas_line(
        tmp_path, monkeypatch, caplog):
    bot = _Bot()
    agent, ep, reply = await _summoned(tmp_path, bot)
    monkeypatch.setattr(ep, "guest_finish", AsyncMock(side_effect=RuntimeError("route down")))
    caplog.set_level(logging.WARNING)
    with patch.object(agent, "_process", _limit_stop("Partway through...")):
        with pytest.raises(RuntimeError):
            await agent.handle_message(_turn())
    assert len(bot.sent) == 1
    assert "step limit" in bot.sent[0]["text"].lower()
    assert bot.sent[0]["chat_id"] == int(CHAT)


async def test_a_limit_stop_with_text_delivers_it_and_posts_the_line_to_the_dm(
        tmp_path, caplog):
    bot = _Bot()
    agent, ep, reply = await _summoned(tmp_path, bot)
    caplog.set_level(logging.WARNING)
    with patch.object(agent, "_process", _limit_stop("Partway through...")):
        await agent.handle_message(_turn())
    assert bot.edits[-1]["text"] == "Partway through..."
    assert len(bot.sent) == 1 and "step limit" in bot.sent[0]["text"].lower()
    assert bot.sent[0]["chat_id"] == int(CHAT)


async def test_an_empty_turn_limit_stop_declines_and_posts_the_line(tmp_path, caplog):
    bot = _Bot()
    agent, ep, reply = await _summoned(tmp_path, bot)
    caplog.set_level(logging.WARNING)
    with patch.object(agent, "_process", _limit_stop("")):
        await agent.handle_message(_turn())
    assert [e["text"] for e in bot.edits] == [DECLINE]
    assert len(bot.sent) == 1 and "step limit" in bot.sent[0]["text"].lower()
    assert bot.sent[0]["chat_id"] == int(CHAT)
    assert reply.terminal is True
