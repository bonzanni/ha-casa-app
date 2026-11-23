"""A protected call's approval card is posted after the reply of the turn
that raised it (#1621).

The model often writes a line and then makes the protected call in the same
turn. The line is sent when the turn ends, so a card posted at the call landed
above the words that introduce it. On an ordinary Telegram turn whose reply
goes to the chat the card is posted in, the card now waits for the reply,
bounded by one deadline counted from the challenge's registration; the
authorization hook answers at once instead of waiting for the post. A card
that then cannot be shown is told to the operator in one line.

Nothing else moves: a card raised where the turn's reply does not go to the
card's chat (another chat, a hand-off's child run, a job) and every other
keyboard the coordinator posts (install, persona, trigger, callback, event and
erase consents) are posted as before.

Every case drives the real ``ChallengeCoordinator`` and a fresh real
``VerdictBroker``; the hook cases the real ``make_resident_authz_hook``; the
turn cases the real ``Agent.handle_message``. Order is read from one call log
on the channel double, and counts are asserted, never presence.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent as agent_mod
import approval_card
import authz_grants
import output_boundary as ob
import tools as tools_mod
import verdict_broker
from authz_grants import (
    ChallengeCoordinator, GrantStore, make_resident_authz_hook,
    take_delegation_awaiting_approval,
    _DENY_DELIVERY_FAILED, _DENY_PENDING, _DENY_POSTED,
)
from channels import DeliveryOutcome

import test_authz_hook as hook_h
import test_consent_cards as consent_h
import test_pending_approval_output as turn_h
from test_authz_hook import _OriginCtx, _deny_reason, _engagement, _expected_key
from test_chosen_silence_announcements import (
    ROLE, _Factory, _Hook, _make_agent, _patch_retry_sleep,
)
from test_consent_cards import env  # noqa: F401 — the consent harness
from test_pending_approval_output import _Gate, _call, _msg, _result, _text

pytestmark = [pytest.mark.asyncio]

NOT_SHOWN = "The approval request could not be shown — ask again."
BOT = 111
CHAT = 42            # test_authz_hook._origin()'s chat
ARGS = {"amount": 10}


def _armed_scope(*, bot: int = BOT, chat: int = CHAT) -> ob.TurnScope:
    """The scope of an ordinary Telegram turn whose reply is not out yet."""
    scope = ob.TurnScope(id="turn-1", cid="c1", role="finance",
                         display_name="Alex", channel="telegram",
                         message_type="channel_in", markers={})
    scope.reply_out = asyncio.Event()
    scope.reply_chat = (bot, chat)
    return scope


async def _drivers(coord: ChallengeCoordinator, timeout: float = 5) -> None:
    """Every card the coordinator still owes has been posted or given up."""
    pending = list(coord._drivers)
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout)
    for _ in range(5):
        await asyncio.sleep(0)


async def _spin(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.fixture
def broker(monkeypatch):
    fresh = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture
def written(monkeypatch):
    """The card is Casa's own fallback, written at once; the event says the
    post driver has it and has reached whatever comes before the send."""
    done = asyncio.Event()

    async def _write(req):
        done.set()
        return req.fallback

    monkeypatch.setattr(approval_card, "write_card", _write)
    return done


# ===========================================================================
# The hook and the coordinator
# ===========================================================================


class _HookChannel(hook_h._FakeChannel):
    """The hook tests' endpoint double, with one ordered log of what reached
    the chat and the Casa notice a card that is never shown ends in."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[tuple] = []
        self.notice_ok = True

    async def post_dm_keyboard(self, **kw):
        self.log.append(("card", kw["chat_id"]))
        return await super().post_dm_keyboard(**kw)

    async def deliver_casa_notice(self, chat_id, text):
        self.log.append(("notice", chat_id, text))
        return self.notice_ok

    def cards(self) -> int:
        return sum(1 for entry in self.log if entry[0] == "card")

    def notices(self) -> list[tuple]:
        return [entry for entry in self.log if entry[0] == "notice"]


@pytest.fixture
def hooked(broker, written):
    channel = _HookChannel()
    coord = ChallengeCoordinator()
    deps = hook_h.AuthzDeps(channel=channel, grants=GrantStore(),
                            challenges=coord)
    return channel, coord, hook_h._mk_hook(deps=deps), broker


async def _ask(hook, call_id: str, args: dict | None = None):
    return await hook({"tool_name": hook_h.TOOL,
                       "tool_input": dict(ARGS if args is None else args)},
                      call_id, {})


def _turn_origin(scope: ob.TurnScope, **over) -> dict:
    return hook_h._origin(turn_scope=scope, **over)


async def test_the_hook_answers_at_once_and_the_card_waits_for_the_reply(hooked):
    channel, coord, hook, _broker = hooked
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope)):
        out = await asyncio.wait_for(_ask(hook, "c1"), 2)

    assert _deny_reason(out) == _DENY_POSTED
    # #1252's record is written before the card is posted
    assert scope.approvals == {"c1": ("pending", _expected_key(ARGS))}
    await _spin()
    assert channel.log == []

    scope.reply_out.set()
    await _drivers(coord)
    assert channel.log == [("card", CHAT)]


@pytest.mark.parametrize("write_s, low, high", [
    (0.5, 0.98, 1.4),      # a hold counted from the write's end: 1.5 s
    (1.5, 1.48, 2.3),      # a hold counted from the write's end: 2.5 s
])
async def test_the_deadline_posts_the_card_when_the_turn_does_not_end(
        hooked, monkeypatch, write_s, low, high):
    channel, coord, hook, _broker = hooked
    assert authz_grants.CARD_HOLD_S == 20.0     # the hold the documents state
    monkeypatch.setattr(authz_grants, "CARD_HOLD_S", 1.0)

    async def _slow_write(req):
        await asyncio.sleep(write_s)
        return req.fallback

    monkeypatch.setattr(approval_card, "write_card", _slow_write)
    scope = _armed_scope()
    loop = asyncio.get_running_loop()
    posted_at: list[float] = []
    post = channel.post_dm_keyboard

    async def _timed_post(**kw):
        posted_at.append(loop.time())
        return await post(**kw)

    channel.post_dm_keyboard = _timed_post
    started = loop.time()
    with _OriginCtx(_turn_origin(scope)):
        out = await asyncio.wait_for(_ask(hook, "c1"), 2)
    assert _deny_reason(out) == _DENY_POSTED

    await _drivers(coord)
    # one deadline, counted from the registration: the card's own writing
    # time is inside it, and the turn never ended
    assert not scope.reply_out.is_set()
    assert len(posted_at) == 1
    assert low <= posted_at[0] - started < high
    assert channel.log == [("card", CHAT)]


@pytest.mark.parametrize("post", ["fails", "raises", "lands"])
async def test_a_deferred_card_that_cannot_be_sent_is_told_once(hooked, post):
    channel, coord, hook, _broker = hooked
    channel.post_result = None if post == "fails" else 55
    channel.post_raises = post == "raises"
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope, _delegation_id="dlg-1")):
        out = await asyncio.wait_for(_ask(hook, "c1"), 2)
    assert _deny_reason(out) == _DENY_POSTED

    scope.reply_out.set()
    await _drivers(coord)

    assert channel.cards() == 1
    if post == "lands":
        assert channel.log == [("card", CHAT)]
        assert take_delegation_awaiting_approval("dlg-1") is True
    else:
        assert channel.log == [("card", CHAT), ("notice", CHAT, NOT_SHOWN)]
        assert take_delegation_awaiting_approval("dlg-1") is False


async def test_a_notice_that_cannot_be_sent_is_logged_and_ends_there(
        hooked, caplog):
    channel, coord, hook, _broker = hooked
    channel.post_result = None
    channel.notice_ok = False
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope)):
        await asyncio.wait_for(_ask(hook, "c1"), 2)
    scope.reply_out.set()
    with caplog.at_level("WARNING", logger="authz_grants"):
        await _drivers(coord)

    assert channel.notices() == [("notice", CHAT, NOT_SHOWN)]
    assert sum("could not be told" in r.getMessage()
               for r in caplog.records) == 1


@pytest.mark.parametrize("when", ["before the card is written", "while it waits"])
async def test_a_request_cancelled_before_its_card_is_told_once(
        hooked, written, when):
    channel, coord, hook, broker = hooked
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope, _delegation_id="dlg-2")):
        out = await asyncio.wait_for(_ask(hook, "c1"), 2)
    assert _deny_reason(out) == _DENY_POSTED
    if when == "while it waits":
        await asyncio.wait_for(written.wait(), 2)
        await _spin()
    # what /new does to the chat's open approvals
    assert broker.cancel_scope(namespace="resident_ask",
                               scope=f"authz:{BOT}:{CHAT}",
                               reason="new_session") == 1
    scope.reply_out.set()
    await _drivers(coord)

    assert channel.log == [("notice", CHAT, NOT_SHOWN)]
    assert take_delegation_awaiting_approval("dlg-2") is False


async def test_a_request_that_settles_ends_the_wait_at_once(hooked, written):
    """A held card does not sit out its hold once there is no request left to
    show: the turn's reply never comes out here, and the driver still ends."""
    channel, coord, hook, broker = hooked
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope)):
        await asyncio.wait_for(_ask(hook, "c1"), 2)
    await asyncio.wait_for(written.wait(), 2)
    await _spin()
    started = asyncio.get_running_loop().time()
    assert broker.cancel_scope(namespace="resident_ask",
                               scope=f"authz:{BOT}:{CHAT}",
                               reason="new_session") == 1
    await _drivers(coord, timeout=2)          # the hold is CARD_HOLD_S = 20 s
    assert asyncio.get_running_loop().time() - started < 2
    assert not scope.reply_out.is_set()
    assert channel.log == [("notice", CHAT, NOT_SHOWN)]


async def test_casa_stopping_forgets_the_mark_and_posts_nothing(hooked, written):
    channel, coord, hook, broker = hooked
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope, _delegation_id="dlg-stop")):
        await asyncio.wait_for(_ask(hook, "c1"), 2)
    await asyncio.wait_for(written.wait(), 2)
    await _spin()
    assert broker.cancel_scope(namespace="resident_ask",
                               scope=f"authz:{BOT}:{CHAT}",
                               reason="casa_shutdown") == 1
    await _drivers(coord, timeout=2)
    assert channel.log == []                   # no card, no notice
    assert take_delegation_awaiting_approval("dlg-stop") is False


async def test_an_answer_that_raced_the_card_is_not_a_failure(hooked, written):
    channel, coord, hook, broker = hooked
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope, _delegation_id="dlg-3")):
        await asyncio.wait_for(_ask(hook, "c1"), 2)
    await asyncio.wait_for(written.wait(), 2)
    await _spin()
    (rid,) = broker.pending(namespace="resident_ask", scope=f"authz:{BOT}:{CHAT}")
    claim = broker.claim(namespace="resident_ask", scope=f"authz:{BOT}:{CHAT}",
                         request_id=rid, option_index=1, actor_id=100)
    assert not isinstance(claim, str), claim
    assert broker.commit(claim) is True

    scope.reply_out.set()
    await _drivers(coord)

    assert channel.log == []
    assert take_delegation_awaiting_approval("dlg-3") is True


async def test_the_same_call_again_in_the_turn_does_not_wait_for_the_card(hooked):
    channel, coord, hook, _broker = hooked
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope)):
        first = await asyncio.wait_for(_ask(hook, "c1"), 2)
        second = await asyncio.wait_for(_ask(hook, "c2"), 2)

    assert [_deny_reason(first), _deny_reason(second)] == [
        _DENY_POSTED, _DENY_PENDING]
    key = _expected_key(ARGS)
    assert scope.approvals == {"c1": ("pending", key), "c2": ("pending", key)}
    assert channel.log == []

    scope.reply_out.set()
    await _drivers(coord)
    assert channel.log == [("card", CHAT)]


@pytest.mark.parametrize("lost", ["the send fails", "casa stops"])
async def test_a_repeat_from_a_delegated_run_is_forgotten_with_the_card(
        hooked, written, lost):
    """Every delegation whose call was answered for a held card is forgotten
    when that card is never shown — the one that raised it and the repeat's."""
    channel, coord, hook, broker = hooked
    channel.post_result = None
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope, _delegation_id="dlg-first")):
        await asyncio.wait_for(_ask(hook, "c1"), 2)
    child = ob.TurnScope.for_child(scope, "")
    with _OriginCtx(_turn_origin(child, _delegation_id="dlg-repeat")):
        again = await asyncio.wait_for(_ask(hook, "c2"), 2)
    assert _deny_reason(again) == _DENY_PENDING
    if lost == "casa stops":
        await asyncio.wait_for(written.wait(), 2)
        await _spin()
        assert broker.cancel_scope(namespace="resident_ask",
                                   scope=f"authz:{BOT}:{CHAT}",
                                   reason="casa_shutdown") == 1
    scope.reply_out.set()
    await _drivers(coord)

    assert take_delegation_awaiting_approval("dlg-first") is False
    assert take_delegation_awaiting_approval("dlg-repeat") is False
    assert [e for e in channel.log if e[0] == "notice"] == (
        [] if lost == "casa stops" else [("notice", CHAT, NOT_SHOWN)])


async def test_a_repeat_of_a_dead_request_leaves_no_delegation_waiting(hooked, written):
    """A repeat that finds the request already settled, its card never posted,
    marks nothing as awaiting an approval that cannot come."""
    channel, coord, hook, broker = hooked
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope, _delegation_id="dlg-first")):
        await asyncio.wait_for(_ask(hook, "c1"), 2)
    assert broker.cancel_scope(namespace="resident_ask",
                               scope=f"authz:{BOT}:{CHAT}",
                               reason="new_session") == 1
    child = ob.TurnScope.for_child(scope, "")
    with _OriginCtx(_turn_origin(child, _delegation_id="dlg-late")):
        again = await asyncio.wait_for(_ask(hook, "c2"), 2)
    await _drivers(coord)

    assert _deny_reason(again) == _DENY_PENDING
    assert take_delegation_awaiting_approval("dlg-late") is False
    assert take_delegation_awaiting_approval("dlg-first") is False
    assert channel.log == [("notice", CHAT, NOT_SHOWN)]


async def test_the_same_call_again_after_the_card_is_posted_is_as_before(hooked):
    channel, coord, hook, _broker = hooked
    first_turn = _armed_scope()
    with _OriginCtx(_turn_origin(first_turn)):
        await asyncio.wait_for(_ask(hook, "c1"), 2)
    first_turn.reply_out.set()
    await _drivers(coord)
    assert channel.log == [("card", CHAT)]

    later_turn = _armed_scope()
    with _OriginCtx(_turn_origin(later_turn)):
        again = await asyncio.wait_for(_ask(hook, "c2"), 2)

    assert _deny_reason(again) == _DENY_PENDING
    assert later_turn.approvals == {"c2": ("pending", _expected_key(ARGS))}
    assert channel.log == [("card", CHAT)]


async def test_a_repeat_of_a_dead_request_notes_nothing_pending(hooked, written):
    channel, coord, hook, broker = hooked
    scope = _armed_scope()
    with _OriginCtx(_turn_origin(scope)):
        first = await asyncio.wait_for(_ask(hook, "c1"), 2)
        assert _deny_reason(first) == _DENY_POSTED
        await asyncio.wait_for(written.wait(), 2)
        await _spin()
        assert broker.cancel_scope(namespace="resident_ask",
                                   scope=f"authz:{BOT}:{CHAT}",
                                   reason="new_session") == 1
        # the request is dead: the hook waits for what became of its card, as
        # it always did; the held card ended its wait when the request did, so
        # that is over without the turn's reply
        out = await asyncio.wait_for(_ask(hook, "c2"), 2)
        assert not scope.reply_out.is_set()
    await _drivers(coord)

    assert _deny_reason(out) == _DENY_PENDING
    assert scope.approvals == {"c1": ("pending", _expected_key(ARGS))}
    assert channel.log == [("notice", CHAT, NOT_SHOWN)]


@pytest.mark.parametrize("case", [
    "another chat", "another bot", "a child run", "a job", "reply already out",
    "no turn",
])
@pytest.mark.parametrize("post", ["lands", "fails"])
async def test_elsewhere_the_card_is_posted_at_the_call_as_before(
        hooked, case, post):
    channel, coord, hook, _broker = hooked
    channel.post_result = 55 if post == "lands" else None
    scope = _armed_scope()
    origin, engagement = _turn_origin(scope), None
    if case == "another chat":
        scope.reply_chat = (BOT, CHAT + 1)
    elif case == "another bot":
        scope.reply_chat = (BOT + 1, CHAT)
    elif case == "a child run":
        child = ob.TurnScope.for_child(scope, "")
        assert child.reply_out is None and child.reply_chat is None
        origin = _turn_origin(child)
    elif case == "a job":
        engagement = _engagement()
    elif case == "reply already out":
        scope.reply_out.set()
    elif case == "no turn":
        origin = hook_h._origin()

    with _OriginCtx(origin, engagement=engagement):
        out = await asyncio.wait_for(_ask(hook, "c1"), 2)

    # the hook waited for the post and answers by its outcome
    assert channel.log == [("card", CHAT)]
    assert _deny_reason(out) == (
        _DENY_POSTED if post == "lands" else _DENY_DELIVERY_FAILED)
    await _drivers(coord)
    assert channel.log == [("card", CHAT)]      # and no notice either way


@pytest.mark.parametrize("kind", [
    "specialist", "persona", "trigger", "callback", "event", "erase"])
async def test_no_other_keyboard_waits_for_the_reply(env, kind):  # noqa: F811
    """Install and persona consents, the uninstall's erase question, and the
    trigger, callback and event consents (the ones ``consent_reprompt``
    re-issues) are awaited by their tools inside the turn: they are posted
    at once, whatever the turn's scope says."""
    scope = _armed_scope(bot=111, chat=100)
    token = agent_mod.origin_var.set({"turn_scope": scope})
    try:
        if kind == "specialist":
            _record, handle = await consent_h._post_specialist(env)
        elif kind == "persona":
            _record, handle = await consent_h._post_persona(env)
        else:
            handle = {"trigger": consent_h._post_trigger,
                      "callback": consent_h._post_callback,
                      "event": consent_h._post_event,
                      "erase": consent_h._post_erase}[kind](env)
    finally:
        agent_mod.origin_var.reset(token)

    assert handle.deferred is False
    assert await asyncio.wait_for(handle.settled_post(), 5) == "posted"
    assert len(env.channel.posts) == 1
    assert not scope.reply_out.is_set()


async def test_only_a_caller_that_asks_for_it_defers(broker, written):
    """The same registration, under the same armed turn, with and without the
    opt-in: only the protected-call path passes it."""
    channel = _HookChannel()
    coord = ChallengeCoordinator()
    scope = _armed_scope()
    card = approval_card.CardRequest(
        kind="authz", who="Alex", facts={},
        fallback=approval_card.Card(title="🔐 Allow Alex to use p?"))

    def _register(key, **kw):
        token = agent_mod.origin_var.set({"turn_scope": scope})
        try:
            return coord.register_challenge(
                key, bot_id=BOT, chat_id=CHAT, operator_id=100,
                channel=channel, challenge_text="x", card_request=card, **kw)
        finally:
            agent_mod.origin_var.reset(token)

    plain = _register("plain")
    assert plain.deferred is False
    assert await asyncio.wait_for(plain.settled_post(), 2) == "posted"
    assert channel.cards() == 1

    held = _register("held", defer_until_reply=True)
    assert held.deferred is True and held.live is True
    await asyncio.wait_for(written.wait(), 2)
    await _spin()
    assert channel.cards() == 1
    scope.reply_out.set()
    assert await asyncio.wait_for(held.settled_post(), 2) == "posted"
    assert channel.cards() == 2


# ===========================================================================
# The turn: Agent.handle_message
# ===========================================================================


class _OrderedChannel(turn_h._Channel):
    """The turn harness's Telegram double with ONE ordered log: the reply,
    Casa's own sends, the card and the notice, in the order they were sent."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[tuple] = []
        self.reply_raises = False
        self.post_result: int | None = 55

        async def _reply(text, _context):
            if self.reply_raises:
                raise RuntimeError("reply boom")
            self.log.append(("reply", str(text)))
            return DeliveryOutcome.DELIVERED

        async def _send(text, _context):
            self.log.append(("send", str(text)))
            return DeliveryOutcome.DELIVERED

        async def _finished(_context):
            self.log.append(("turn_finished",))

        self.send_response = AsyncMock(side_effect=_reply)
        self.send = AsyncMock(side_effect=_send)
        self.turn_finished = AsyncMock(side_effect=_finished)

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               entities=None):
        self.posts.append((chat_id, request_id, str(text), tuple(options)))
        self.log.append(("card", chat_id))
        return self.post_result

    async def deliver_casa_notice(self, chat_id, text):
        self.log.append(("notice", chat_id, text))
        return True

    async def guest_finish(self, text, _error_kind, _context):
        self.log.append(("group", str(text)))
        return DeliveryOutcome.DELIVERED


@pytest.fixture
async def ordered(tmp_path, monkeypatch, written):
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    agent = _make_agent(tmp_path)
    channel = _OrderedChannel()
    from telegram_channel_double import make_transport
    transport = make_transport(channel).get("telegram")
    agent._channel_manager.register(transport)
    tools_mod.init_tools(
        channel_manager=agent._channel_manager, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )
    coord = ChallengeCoordinator()
    hook = make_resident_authz_hook(
        ROLE, turn_h.PROTECTED,
        lambda: authz_grants.AuthzDeps(channel=transport, grants=GrantStore(),
                                       challenges=coord))
    yield agent, channel, hook, coord
    await agent.aclose()


OPERATOR = turn_h.OPERATOR


@pytest.mark.parametrize("transport", ["dm", "button", "setup"])
@pytest.mark.parametrize("line", ["BEFORE", ""])
async def test_the_card_is_posted_after_the_turns_reply(
        ordered, monkeypatch, transport, line):
    agent, channel, hook, coord = ordered
    gate = _Gate()
    script = ([_text(line)] if line else []) + [
        _call("deny-1"),
        gate.step(lambda: turn_h._ask(hook, "deny-1")),
        _result("deny-1"),
        _text(turn_h.TAIL),
    ]
    held: dict = {}

    await turn_h._drive(agent, monkeypatch, _Factory([script]), _msg(transport),
                        gate, lambda: held.update(log=list(channel.log)))
    await _drivers(coord)

    assert [_deny_reason(a) for a in gate.answers] == [_DENY_POSTED]
    # the hook answered while nothing had reached the chat
    assert held["log"] == []
    first = ("reply", "BEFORE") if line else ("turn_finished",)
    assert channel.log == [first, ("card", OPERATOR)]


@pytest.mark.parametrize("lost", ["the send fails", "a /new while it waits"])
async def test_a_card_lost_after_the_reply_leaves_the_reply_then_one_line(
        ordered, monkeypatch, lost):
    """The reply was already cut at the call, as for a posted card: the
    operator gets the words before the call, then Casa's line."""
    agent, channel, hook, coord = ordered
    channel.post_result = None if lost == "the send fails" else 55
    gate = _Gate()
    script = [_text("BEFORE"), _call("deny-1"),
              gate.step(lambda: turn_h._ask(hook, "deny-1")),
              _result("deny-1"), _text(turn_h.TAIL)]

    def _on_hold():
        if lost == "a /new while it waits":
            assert verdict_broker.BROKER.cancel_scope(
                namespace="resident_ask",
                scope=f"authz:{turn_h.CASA_BOT}:{OPERATOR}",
                reason="new_session") == 1

    await turn_h._drive(agent, monkeypatch, _Factory([script]), _msg("dm"),
                        gate, _on_hold)
    await _drivers(coord)

    assert [_deny_reason(a) for a in gate.answers] == [_DENY_POSTED]
    if lost == "the send fails":
        assert channel.log == [("reply", "BEFORE"), ("card", OPERATOR),
                               ("notice", OPERATOR, NOT_SHOWN)]
    else:
        # the held card ends its wait with the request: Casa's line is out
        # before the turn that raised it has ended
        assert channel.log == [("notice", OPERATOR, NOT_SHOWN), ("reply", "BEFORE")]


async def test_two_protected_calls_post_two_cards_after_the_reply(
        ordered, monkeypatch):
    agent, channel, hook, coord = ordered
    gate = _Gate()
    other = {"amount": 11}

    async def _both():
        return [await turn_h._ask(hook, "deny-1"),
                await turn_h._ask(hook, "deny-2", other)]

    script = [_text("BEFORE"), _call("deny-1"), _call("deny-2", other),
              gate.step(_both), _result("deny-1"), _result("deny-2"),
              _text(turn_h.TAIL)]

    await turn_h._drive(agent, monkeypatch, _Factory([script]), _msg("dm"), gate)
    await _drivers(coord)

    (answers,) = gate.answers
    assert [_deny_reason(a) for a in answers] == [_DENY_POSTED, _DENY_POSTED]
    assert channel.log == [("reply", "BEFORE"),
                           ("card", OPERATOR), ("card", OPERATOR)]
    assert len({post[1] for post in channel.posts}) == 2


@pytest.mark.parametrize("ending", ["raises", "cancelled", "reply fails"])
async def test_a_turn_that_does_not_end_well_still_gets_its_card(
        ordered, monkeypatch, ending):
    agent, channel, hook, coord = ordered
    gate = _Gate()
    script = [_text("BEFORE"), _call("deny-1"),
              gate.step(lambda: turn_h._ask(hook, "deny-1"))]
    if ending == "raises":
        script.append(ValueError("boom"))
    else:
        script += [_result("deny-1"), _text(turn_h.TAIL)]
    channel.reply_raises = ending == "reply fails"

    monkeypatch.setattr("sdk_client_pool._default_make_client", _Factory([script]))
    with _patch_retry_sleep():
        task = asyncio.create_task(agent.handle_message(_msg("dm")))
        await asyncio.wait_for(gate.hook_returned.wait(), 10)
        assert channel.log == []
        if ending == "cancelled":
            task.cancel()
        gate.release_result.set()
        if ending == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        elif ending == "reply fails":
            with pytest.raises(RuntimeError, match="reply boom"):
                await asyncio.wait_for(task, 10)
        else:
            await asyncio.wait_for(task, 10)
    # well inside the hold: the turn's end released the card, not the deadline
    await _drivers(coord, timeout=5)

    assert [_deny_reason(a) for a in gate.answers] == [_DENY_POSTED]
    assert channel.log[-1] == ("card", OPERATOR)
    assert sum(1 for entry in channel.log if entry[0] == "card") == 1
    assert [entry[0] for entry in channel.log[:-1]] == {
        "raises": ["send"], "cancelled": [], "reply fails": []}[ending]


@pytest.mark.parametrize("turn, armed", [
    ("dm", True), ("button", True), ("setup", True),
    ("summoned", False), ("no chat", False),
])
async def test_which_turns_tell_their_scope_when_the_reply_is_out(
        ordered, monkeypatch, turn, armed):
    """An ordinary Telegram turn that answers in its own chat carries the
    event and the chat; a turn whose final text goes elsewhere carries
    neither, and its cards are posted at the call."""
    agent, channel, _hook, _coord = ordered
    seen: dict = {}

    async def _look():
        scope = agent_mod.origin_var.get(None)["turn_scope"]
        seen["out"] = scope.reply_out
        seen["set"] = scope.reply_out is not None and scope.reply_out.is_set()
        seen["chat"] = scope.reply_chat
        seen["scope"] = scope

    msg = _msg("dm" if turn in ("summoned", "no chat") else turn)
    if turn == "summoned":
        msg.context["_guest_query"] = "q-1"
    elif turn == "no chat":
        msg.context["chat_id"] = "interval:heartbeat"
    monkeypatch.setattr("sdk_client_pool._default_make_client",
                        _Factory([[_Hook(_look), _text("hello")]]))
    with _patch_retry_sleep():
        await asyncio.wait_for(agent.handle_message(msg), 10)

    if armed:
        assert isinstance(seen["out"], asyncio.Event)
        assert seen["set"] is False
        assert seen["chat"] == (turn_h.CASA_BOT, OPERATOR)
        assert seen["scope"].reply_out.is_set()     # the turn is over
    else:
        assert seen["out"] is None and seen["chat"] is None


async def test_the_reply_chat_is_formed_only_from_a_known_bot_and_chat():
    """No pair, no hold: a channel that resolves no route, a context whose
    chat is a label, and an endpoint with no bot id yet all leave the turn's
    cards to be posted at the call."""
    from channels.tg_address import EndpointNotReady

    class _Endpoint:
        def __init__(self, bot):
            self._bot = bot

        @property
        def bot_id(self):
            if self._bot is None:
                raise EndpointNotReady("no bot id yet")
            return self._bot

    class _Transport:
        def __init__(self, bot):
            self._bot = bot

        def endpoint_for_route(self, context, *, site):
            return _Endpoint(self._bot)

    context = {"chat_id": "42", "_tg_bot": 111}
    assert agent_mod._reply_chat(_Transport(111), context) == (111, 42)
    assert agent_mod._reply_chat(_Transport(None), context) is None
    assert agent_mod._reply_chat(_Transport(True), context) is None
    assert agent_mod._reply_chat(object(), context) is None
    assert agent_mod._reply_chat(
        _Transport(111), {"chat_id": "interval:heartbeat"}) is None
    assert agent_mod._reply_chat(_Transport(111), {}) is None
