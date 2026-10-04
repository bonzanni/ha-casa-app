"""S4 §3/§7: the route — an operator's swipe-reply on a retained specialist
post becomes one desk turn with the operator's exact words, first in the DM
handler (ahead of /new) and after the chat's rate decision; every other
message takes today's path untouched (INV-DESK-001). An approval
continuation marked for a desk is dispatched to that desk only when the
approver is the operator and the specialist is still delegable.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import pytest

import result_broker as rb
import specialist_desk as sd
from bus import MessageBus
from channels.telegram import TelegramChannel
from test_telegram_new_reset import _drain_bus, _noop

pytestmark = pytest.mark.asyncio

OPERATOR = 42


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=1)


def _channel():
    bus = MessageBus()
    bus.register("assistant", _noop)
    ch = TelegramChannel(bot_token="T", chat_id=str(OPERATOR), default_agent="assistant", bus=bus)
    ch._start_typing = lambda *a, **k: None
    ch._app = types.SimpleNamespace(bot=_FakeBot())
    ch._session_registry = None
    ch._semantic_memory = None
    return ch, bus


def _update(text, *, user_id=OPERATOR, quoted_id=11, quoted_text="📊 Finance\nQ3 report",
            chat_id=str(OPERATOR)):
    user = types.SimpleNamespace(first_name="Nicola", id=user_id)
    quoted = (types.SimpleNamespace(message_id=quoted_id, text=quoted_text, caption=None)
              if quoted_id is not None else None)
    message = types.SimpleNamespace(text=text, message_id=70, reply_to_message=quoted)
    return types.SimpleNamespace(message=message, effective_chat=types.SimpleNamespace(id=chat_id),
                                 effective_user=user)


def _record(**over):
    base = dict(role="finance", operator_id=OPERATOR, plugin="probe", slot="report",
                tool_use_id="call-1", owner="d-1", posted_at=1.0)
    base.update(over)
    return rb.PostRecord(**base)


@pytest.fixture
def routed(monkeypatch):
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    monkeypatch.setattr(sd, "DESKS", sd.DeskRegistry())
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))   # its notices must not reach a later test's prompt
    pm.record(OPERATOR, 11, _record())
    monkeypatch.setattr(sd, "desk_target_ok", lambda resident, role: role == "finance")
    spawned = AsyncMock(return_value=None)
    monkeypatch.setattr(sd, "handle_reply", spawned)
    return pm, spawned


async def _settle(ch):
    for t in list(ch._turn_tasks):
        await t
    await asyncio.sleep(0)                                    # let done-callbacks run
    await asyncio.sleep(0)


async def test_a_reply_on_a_retained_specialist_post_routes_with_the_exact_words(routed):
    pm, spawned = routed
    ch, bus = _channel()
    await ch._handle(_update("  all good, more please  "), None)
    await _settle(ch)
    assert await _drain_bus(bus) == []                      # never a resident turn
    assert spawned.await_count == 1
    kw = spawned.await_args.kwargs
    assert kw["channel"] is ch and kw["chat_id"] == OPERATOR and kw["user_id"] == OPERATOR
    assert kw["text"] == "  all good, more please  "        # unstripped
    assert kw["quoted_text"] == "📊 Finance\nQ3 report" and kw["record"].role == "finance"
    assert kw["resident_role"] == "assistant" and kw["continuation"] is False
    assert kw["message_id"] == 70 and kw["cid"]


async def test_a_new_prefixed_reply_on_a_post_routes_and_resets_nothing(routed):
    pm, spawned = routed
    ch, bus = _channel()
    await ch._handle(_update("/new, start over"), None)
    await _settle(ch)
    assert spawned.await_count == 1 and spawned.await_args.kwargs["text"] == "/new, start over"
    assert ch._app.bot.sent == []                            # no reset acknowledgement
    assert await _drain_bus(bus) == []


@pytest.mark.parametrize("case", ["unquoted", "guest", "unretained", "other-operator",
                                  "resident-post", "not-delegable", "bad-chat"])
async def test_every_other_message_takes_todays_path(routed, monkeypatch, case):
    pm, spawned = routed
    ch, bus = _channel()
    upd = _update("hello")
    if case == "unquoted":
        upd = _update("hello", quoted_id=None)
    elif case == "guest":
        upd = _update("hello", user_id=7)
    elif case == "unretained":
        upd = _update("hello", quoted_id=12)
    elif case == "other-operator":
        pm.record(OPERATOR, 13, _record(operator_id=99))
        upd = _update("hello", quoted_id=13)
    elif case == "resident-post":
        pm.record(OPERATOR, 14, _record(role="assistant"))
        upd = _update("hello", quoted_id=14)
        monkeypatch.setattr(sd, "desk_target_ok", lambda resident, role: True)   # only the resident check refuses
    elif case == "not-delegable":
        monkeypatch.setattr(sd, "desk_target_ok", lambda resident, role: False)
    elif case == "bad-chat":
        upd = _update("hello", chat_id="not-a-chat")
    await ch._handle(upd, None)
    await _settle(ch)
    assert spawned.await_count == 0
    if case in ("guest", "bad-chat"):
        return                                               # today's path refuses them its own way
    queued = await _drain_bus(bus)
    assert len(queued) == 1 and queued[0].content == "hello"


async def test_a_rate_limited_reply_gets_the_notice_and_spawns_nothing(routed):
    pm, spawned = routed
    ch, bus = _channel()
    ch._rate_limiter = types.SimpleNamespace(
        enabled=True, check=lambda chat: types.SimpleNamespace(allowed=False, should_notify=True))
    ch._send_rate_limit_reply = AsyncMock()
    await ch._handle(_update("more"), None)
    await _settle(ch)
    assert spawned.await_count == 0 and ch._send_rate_limit_reply.await_count == 1
    assert await _drain_bus(bus) == []


async def test_the_desk_task_is_tracked_and_the_serial_lock_is_not_held_across_it(routed, monkeypatch):
    pm, spawned = routed
    import asyncio
    gate = asyncio.Event()

    async def _slow(**kw):
        await gate.wait()
    monkeypatch.setattr(sd, "handle_reply", _slow)
    ch, bus = _channel()
    await ch._handle(_update("more"), None)                  # returns before the turn ends
    assert len(ch._turn_tasks) == 1
    lock = ch._chat_serial_locks[str(OPERATOR)]
    assert not lock.locked()
    gate.set()
    await _settle(ch)
    assert ch._turn_tasks == set()


# --- the continuation ----------------------------------------------------------------

async def test_a_desk_continuation_reaches_the_desk_when_operator_and_target_still_hold(routed):
    pm, spawned = routed
    ch, bus = _channel()
    ok = await ch._dispatch_desk_continuation(
        chat_id=OPERATOR, user_id=OPERATOR, desk_role="finance", request_id="r-1",
        text="[authorization approved]: call it")
    await _settle(ch)
    assert ok is True and spawned.await_count == 1
    kw = spawned.await_args.kwargs
    assert kw["continuation"] is True and kw["quoted_text"] is None and kw["record"] is None
    assert kw["text"] == "[authorization approved]: call it" and kw["desk_role"] == "finance"
    assert ch._app.bot.sent == []


@pytest.mark.parametrize("why", ["not-operator", "not-delegable"])
async def test_a_continuation_that_no_longer_qualifies_is_a_notice_not_a_turn(routed, monkeypatch, why):
    pm, spawned = routed
    ch, bus = _channel()
    user_id = 7 if why == "not-operator" else OPERATOR
    if why == "not-delegable":
        monkeypatch.setattr(sd, "desk_target_ok", lambda resident, role: False)
    ok = await ch._dispatch_desk_continuation(
        chat_id=OPERATOR, user_id=user_id, desk_role="finance", request_id="r-1", text="go")
    await _settle(ch)
    assert ok is True and spawned.await_count == 0
    (sent,) = ch._app.bot.sent
    assert sent["chat_id"] == OPERATOR and "could not continue" in sent["text"]
    assert sent["text"].startswith("📊 ") and "go" not in sent["text"]


# --- round 1 folds -------------------------------------------------------------------

async def test_a_full_queue_reply_is_refused_inline_with_no_task(routed, monkeypatch):
    pm, spawned = routed
    ch, bus = _channel()
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    desk.waiting = sd.DESK_QUEUE_MAX
    await ch._handle(_update("more"), None)
    assert ch._turn_tasks == set() and spawned.await_count == 0
    (sent,) = ch._app.bot.sent
    assert sent["text"].startswith("📊 ") and "desk was full" in sent["text"]
    assert await _drain_bus(bus) == [] and desk.waiting == sd.DESK_QUEUE_MAX


async def test_the_route_reserves_before_spawning_and_hands_the_reservation_over(routed):
    pm, spawned = routed
    ch, bus = _channel()
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    await ch._handle(_update("more"), None)
    await _settle(ch)
    assert spawned.await_args.kwargs["reservation"] is not None
    assert desk.waiting == 0                                  # released by the task's done-callback


async def test_a_guest_whose_own_retained_post_exists_is_not_routed_in_accept_all(routed):
    pm, spawned = routed
    ch, bus = _channel()
    ch.chat_id = ""                                            # accept-all: nobody is the operator
    pm.record(OPERATOR, 15, _record(operator_id=7))
    await ch._handle(_update("hello", user_id=7, quoted_id=15), None)
    await _settle(ch)
    assert spawned.await_count == 0


async def test_a_full_queue_continuation_is_refused_inline_with_no_task(routed):
    pm, spawned = routed
    ch, bus = _channel()
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    desk.waiting = sd.DESK_QUEUE_MAX
    ok = await ch._dispatch_desk_continuation(
        chat_id=OPERATOR, user_id=OPERATOR, desk_role="finance", request_id="r-1", text="go")
    assert ok is True and ch._turn_tasks == set() and spawned.await_count == 0
    (sent,) = ch._app.bot.sent
    assert "desk was full" in sent["text"] and sent["text"].startswith("📊 ")


async def test_a_refused_continuation_also_echoes_to_the_resident(routed, monkeypatch):
    pm, spawned = routed
    ch, bus = _channel()
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))
    monkeypatch.setattr(sd, "desk_target_ok", lambda resident, role: False)
    await ch._dispatch_desk_continuation(
        chat_id=OPERATOR, user_id=OPERATOR, desk_role="finance", request_id="r-1", text="go")
    prefix = sd.prompt_prefix(OPERATOR)
    assert "could not continue" in prefix and "go" not in prefix
