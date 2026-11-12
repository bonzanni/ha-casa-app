"""The operator's summons of a specialist's bot in a group is a turn of the
specialist's own chat whose final text lands in the group.

The dispatcher answers the guest query with the placeholder first, then puts
ONE bus turn on the agent's queue: an own-chat turn in every respect (the
operator's chat id, the bot it arrived on, the operator markers), plus two
reserved markers — the group it was summoned in (``_guest_chat``) and the
inline message bound to it (``_guest_query``, the turn's cid). The group is
never a chat id, a route or a delivery. The agent's final text edits the
group's message through the channel's ``guest_finish``; everything else it
sends goes to its chat with the operator. A question asked in that chat puts the group's
message in waiting, and a question still open when the turn ends says so.

Bot 222 is the specialist's (``finance``), the operator is 100, the group -5.
"""
from __future__ import annotations

import types
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent as agent_mod
import chat_notes
import own_voice
import provenance
import tools
import verdict_broker
from bus import BusMessage, MessageBus, MessageType
from channels.guest_reply import GuestReply
from channels.telegram import REPLY_QUOTE_CHARS, TelegramChannel
from output_boundary_testing import admitted
from specialist_registry import DelegationComplete
from test_agent_process import FakeClient, _make_agent
from test_guest_intake import (  # noqa: F401 — fixtures
    FRIEND, GROUP, OPERATOR, OWN_BOT, _guest, _user, world,
)
from test_guest_reply import ASKED_ELSEWHERE, DECLINE, FAIL, WAITING, _Bot
from test_handoff import _CapturingClient, _cfg
from test_own_chat_routing import _turns
from two_endpoints import build_two_endpoints
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio

TITLE = "Test Group"


async def _noop(_msg):
    return None


def _endpoint(*, register: bool = True, bot=None):
    """The specialist's real endpoint over a recording bot, its agent's queue
    registered unless told otherwise."""
    bus = MessageBus()
    if register:
        bus.register("finance", _noop)
    bot = bot if bot is not None else _Bot()
    ch = TelegramChannel(bot_token="222:t", chat_id=str(OPERATOR), default_agent="finance",
                         bus=bus, bot=bot, is_casa=False, known_bot_id=OWN_BOT)
    ch._app = types.SimpleNamespace(bot=bot)
    ch._session_registry = None
    return ch, bus, bot


@pytest.fixture(autouse=True)
def ticker(monkeypatch):
    """The ticker is pinned in its own suite; here only that it was started,
    and when, matters."""
    started: list[str] = []
    monkeypatch.setattr(GuestReply, "start_ticker", lambda self: started.append(self.key))
    return started


@pytest.fixture
def opening(monkeypatch):
    """The own chat's lifecycle as the dispatchers ask it: whether the chat has
    opened yet, whether it moved, and every mark."""
    state = NS(unopened=False, moved=False, asked=[], marks=[])

    def chat_opening(bot_id):
        state.asked.append(bot_id)
        return state.unopened

    async def mark_open(bot_id):
        state.marks.append(bot_id)
        state.unopened = False
    monkeypatch.setattr(own_voice, "chat_opening", chat_opening, raising=False)
    monkeypatch.setattr(own_voice, "chat_moved", lambda bot_id: state.moved, raising=False)
    monkeypatch.setattr(own_voice, "mark_open", mark_open, raising=False)
    return state


@pytest.fixture
def names(monkeypatch):
    monkeypatch.setattr(tools, "_display_name_for_role",
                        lambda role: {"finance": "Judge"}.get(role, role))


async def _dispatched(ch, bus, update=None):
    assert await ch._dispatch_summoned_turn(update or _guest()) is True
    [turn] = _turns(bus, "finance")
    return turn


# ---------------------------------------------------------------------------
# the turn the dispatcher builds
# ---------------------------------------------------------------------------

async def test_a_summons_runs_as_an_own_chat_turn(world, opening, names, ticker):
    ch, bus, bot = _endpoint()
    turn = await _dispatched(ch, bus, _guest(text="@FinanceBot how much?"))
    ctx = turn.context
    assert turn.channel == "telegram" and turn.source == "telegram" and turn.target == "finance"
    assert turn.type is MessageType.CHANNEL_IN
    assert turn.content == "@FinanceBot how much?"
    assert ctx["chat_id"] == str(OPERATOR) and ctx["user_id"] == OPERATOR
    assert ctx["_tg_bot"] == OWN_BOT
    assert ctx["_origin_route"] == "telegram" and ctx["_origin_clearance"] == "private"
    assert ctx["_operator_turn"] is True and ctx["_operator_words"] == "@FinanceBot how much?"
    assert ctx["_guest_chat"] == {"id": GROUP, "title": TITLE, "type": "group"}
    trusted = turn.trusted_user_origin
    assert trusted.user_peer == f"telegram:{OPERATOR}"
    assert trusted.server_origin.clearance == "private"           # the operator's own
    assert trusted.authenticated_user.configured_display_name == "Op"
    # the inline message is bound under the turn's own cid
    key = ctx["_guest_query"]
    assert key == ctx["cid"]
    reply = ch._guest_replies[key]
    assert isinstance(reply, GuestReply) and reply.key == key
    assert reply.name == "Judge" and reply.guest_query_id == "q1"
    assert bot.answers[0]["guest_query_id"] == "q1"
    assert reply.meta["role"] == "finance" and reply.meta["caller_id"] == OPERATOR
    assert reply.meta["chat_id"] == GROUP and reply.meta["title"] == TITLE
    assert isinstance(reply.meta["t0"], float)
    assert ticker == [key]
    # not a chat message: no reply note, no file, no opening of an open chat
    assert "_reply_note" not in ctx and "_received_file" not in ctx
    assert "_chat_opening" not in ctx and "_guest_reply_note" not in ctx


async def test_the_group_never_appears_as_chat_id_or_route(world, opening):
    ch, bus, bot = _endpoint()
    typing = []
    ch._start_typing = lambda *a, **k: typing.append(a)
    turn = await _dispatched(ch, bus)
    ctx = turn.context
    group_values = {GROUP, str(GROUP)}
    assert ctx["chat_id"] not in group_values and ctx["_tg_bot"] not in group_values
    flat = {k: v for k, v in ctx.items() if k != "_guest_chat"}
    assert not (set(flat.values()) & group_values)
    assert typing == []                              # no typing lease in any chat
    assert bot.sent == []                            # nothing sent anywhere


async def test_the_placeholder_goes_out_before_the_turn_is_queued(world, opening, monkeypatch):
    ch, bus, bot = _endpoint()
    order: list[str] = []
    real_answer = bot.answer_guest_query

    async def answer(*a, **k):
        order.append("placeholder")
        return await real_answer(*a, **k)
    bot.answer_guest_query = answer
    real_send = bus.send_checked

    async def send_checked(msg):
        order.append("queued")
        return await real_send(msg)
    monkeypatch.setattr(bus, "send_checked", send_checked)
    monkeypatch.setattr(GuestReply, "start_ticker", lambda self: order.append("ticker"))
    assert await ch._dispatch_summoned_turn(_guest()) is True
    assert order == ["placeholder", "ticker", "queued"]


async def test_a_refused_placeholder_queues_no_turn(world, opening, ticker):
    ch, bus, bot = _endpoint()
    bot.answer_error = RuntimeError("query expired")
    assert await ch._dispatch_summoned_turn(_guest()) is False
    assert _turns(bus, "finance") == []
    assert ch._guest_replies == {} and ticker == []
    assert bot.edits == [] and bot.sent == []


async def test_a_turn_the_queue_refuses_fails_the_reply(world, opening, monkeypatch):
    ch, bus, bot = _endpoint(register=False)
    intake = AsyncMock(return_value="deliver")
    monkeypatch.setattr(own_voice, "intake", intake)
    hand_back = AsyncMock()
    monkeypatch.setattr(ch, "_hand_back", hand_back)
    assert await ch._dispatch_summoned_turn(_guest()) is False
    assert [e["text"] for e in bot.edits] == [FAIL]
    assert ch._guest_replies == {}
    assert intake.await_count == 0 and hand_back.await_count == 0
    assert "finance" not in bus.queues


@pytest.mark.parametrize("text", ["@CasaJudgeBot", None])
async def test_a_mention_only_summons_runs(world, opening, text):
    ch, bus, _bot = _endpoint()
    turn = await _dispatched(ch, bus, _guest(text=text))
    assert turn.content == (text or "")
    assert turn.context["_operator_words"] == (text or "")


def _quoted(*, from_id, text, caption=None):
    return NS(from_user=NS(id=from_id, is_bot=from_id == OWN_BOT), text=text,
              caption=caption, sender_chat=None, date=None, message_id=3)


async def test_the_reply_note_quotes_the_earlier_answer(world, opening):
    ch, bus, _bot = _endpoint()
    update = _guest(text="@FinanceBot and in euros?")
    update.guest_message.reply_to_message = _quoted(from_id=OWN_BOT, text="The rate is 1.08.")
    turn = await _dispatched(ch, bus, update)
    assert turn.context["_guest_reply_note"] == (
        f"You answered earlier in «{TITLE}»: The rate is 1.08.")
    assert "_reply_note" not in turn.context

    # someone else's message quoted: no note of an answer
    update = _guest(text="@FinanceBot and in euros?", update_id=4)
    update.guest_message.reply_to_message = _quoted(from_id=FRIEND, text="who knows")
    turn = await _dispatched(ch, bus, update)
    assert "_guest_reply_note" not in turn.context

    # a long answer is clipped as a reply's quote is
    update = _guest(text="@FinanceBot more", update_id=5)
    update.guest_message.reply_to_message = _quoted(from_id=OWN_BOT, text="x" * 2000)
    turn = await _dispatched(ch, bus, update)
    quoted = turn.context["_guest_reply_note"].split(": ", 1)[1]
    assert quoted == chat_notes.clip("x" * 2000, REPLY_QUOTE_CHARS)
    assert len(quoted) <= REPLY_QUOTE_CHARS


# ---------------------------------------------------------------------------
# the markers
# ---------------------------------------------------------------------------

class _Capturing(FakeClient):
    """The SDK client of a real ``_process``: keeps each prompt and the origin
    bound while it ran."""
    prompts: list[str] = []
    origins: list[dict] = []

    async def query(self, text):
        type(self).prompts.append(text)
        type(self).origins.append(dict(agent_mod.origin_var.get(None) or {}))
        await super().query(text)


@pytest.fixture
def summoned(tmp_path, world, opening, names):
    """The specialist's agent over the transport, its endpoint registered for
    the route, and the tools wired to the same transport."""
    ch, bus, bot = _endpoint()
    h = build_two_endpoints(finance=ch)
    agent = _make_agent(tmp_path, role="finance")
    agent._channel_manager.register(h.transport)
    tools.init_tools(channel_manager=h.manager, bus=MagicMock(),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock())
    _Capturing.reset()
    _Capturing.prompts, _Capturing.origins = [], []
    return NS(ch=ch, bus=bus, bot=bot, agent=agent, h=h, opening=opening)


async def test_the_markers_are_reserved_and_copied(summoned):
    forged = {"chat_id": "1", "_guest_chat": {"id": -9, "title": "x", "type": "group"},
              "_guest_query": "k"}
    assert provenance.sanitize_external_context(forged) == {"chat_id": "1"}
    assert {"_guest_chat", "_guest_query"} <= set(agent_mod.COPIED_CONTEXT_MARKERS)

    w = summoned
    turn = await _dispatched(w.ch, w.bus)
    with patch("sdk_client_pool._default_make_client", _Capturing):
        await w.agent.handle_message(turn)
    [origin] = _Capturing.origins
    assert origin["_guest_chat"] == {"id": GROUP, "title": TITLE, "type": "group"}
    assert origin["_guest_query"] == turn.context["_guest_query"]
    assert origin["chat_id"] == str(OPERATOR) and origin["_tg_bot"] == OWN_BOT
    assert w.bot.edits[-1]["text"] == "pong"           # the real turn's text, in the group


async def test_a_narration_turn_carries_no_guest_markers(summoned):
    w = summoned
    turn = await _dispatched(w.ch, w.bus)
    origin = {**turn.context, "role": "finance", "channel": "telegram", "user_text": "how much?"}
    notice = BusMessage(
        type=MessageType.NOTIFICATION, source="finance", target="finance",
        content=DelegationComplete(delegation_id="d" * 32, agent="browser", status="ok",
                                   text="done", origin=origin),
        channel="telegram", context={"cid": "c", "chat_id": str(OPERATOR), "_tg_bot": OWN_BOT})
    narration = w.agent._synthesize_delegation_turn(notice)
    assert "_guest_query" not in narration.context and "_guest_chat" not in narration.context
    assert narration.context["_tg_bot"] == OWN_BOT

    # the brief handed to a delegation or an engagement keeps its route and
    # loses the summons
    out, _note = tools._launch_note(dict(origin), "collect the invoices")
    assert "_guest_query" not in out and "_guest_chat" not in out
    assert out["_tg_bot"] == OWN_BOT and out["_operator_turn"] is True


async def test_a_delegated_run_carries_no_guest_markers(monkeypatch):
    _CapturingClient.prompts, _CapturingClient.origins = [], []
    roles = {"finance": _cfg("finance", "Judge"), "browser": _cfg("browser", "Fido")}
    monkeypatch.setattr(tools, "_agent_role_map", roles, raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _CapturingClient)
    token = agent_mod.origin_var.set({
        "role": "finance", "execution_role": "finance", "channel": "telegram",
        "chat_id": str(OPERATOR), "user_id": OPERATOR, "cid": "c", "user_text": "collect",
        "_tg_bot": OWN_BOT, "_operator_turn": True,
        "_guest_chat": {"id": GROUP, "title": TITLE, "type": "group"}, "_guest_query": "c"})
    try:
        await tools._run_delegated_agent(roles["browser"], "download the invoices", "")
    finally:
        agent_mod.origin_var.reset(token)
    [origin] = _CapturingClient.origins
    assert "_guest_chat" not in origin and "_guest_query" not in origin
    assert origin["_tg_bot"] == OWN_BOT and origin["execution_role"] == "browser"


async def test_drop_guest_markers_removes_exactly_the_two():
    ctx = {"chat_id": "1", "_tg_bot": 222, "_guest_chat": {"id": -5}, "_guest_query": "k"}
    out = provenance.drop_guest_markers(ctx)
    assert out is ctx and out == {"chat_id": "1", "_tg_bot": 222}
    assert provenance.drop_guest_markers({}) == {}


# ---------------------------------------------------------------------------
# the frame note
# ---------------------------------------------------------------------------

async def test_the_frame_note_names_the_group(summoned):
    w = summoned
    update = _guest(text="@FinanceBot and in euros?")
    update.guest_message.reply_to_message = _quoted(from_id=OWN_BOT, text="The rate is 1.08.")
    turn = await _dispatched(w.ch, w.bus, update)
    with patch("sdk_client_pool._default_make_client", _Capturing):
        await w.agent.handle_message(turn)
    [prompt] = _Capturing.prompts
    note = agent_mod.GUEST_CALL_NOTE.format(title=TITLE)
    assert note == ("The person summoned you in the group «Test Group». Your text answer is "
                    "posted there; a card, a file or anything else you send goes to your "
                    "chat with them.")
    assert prompt.count(note) == 1
    earlier = f"You answered earlier in «{TITLE}»: The rate is 1.08."
    assert prompt.count(earlier) == 1
    assert prompt.index(note) < prompt.index(earlier) < prompt.index("@FinanceBot and in euros?")

    # an ordinary turn of the chat carries neither
    plain = BusMessage(type=MessageType.CHANNEL_IN, source="telegram", target="finance",
                       content="and August?", channel="telegram",
                       context={"chat_id": str(OPERATOR), "user_id": OPERATOR, "cid": "c2",
                                "_tg_bot": OWN_BOT, "_operator_turn": True})
    plain.trusted_user_origin = turn.trusted_user_origin
    with patch("sdk_client_pool._default_make_client", _Capturing):
        await w.agent.handle_message(plain)
    assert "summoned you" not in _Capturing.prompts[1]
    assert "You answered earlier" not in _Capturing.prompts[1]


def _private_summons(*, first_name=None, last_name=None, text="@FinanceBot and in euros?",
                     update_id=3):
    """A summons from a private chat: no title; Telegram names it after the
    person it is with, when it names it at all."""
    update = _guest(chat_id=4242, text=text, update_id=update_id)
    chat = NS(id=4242, title=None, type="private", first_name=first_name, last_name=last_name)
    update.guest_message.chat = chat
    update.effective_chat = chat
    return update


async def test_a_summons_from_a_private_chat_names_the_person_not_an_empty_group(summoned):
    w = summoned
    update = _private_summons(first_name="Ada", last_name="Lovelace")
    update.guest_message.reply_to_message = _quoted(from_id=OWN_BOT, text="The rate is 1.08.")
    turn = await _dispatched(w.ch, w.bus, update)
    assert turn.context["_guest_chat"] == {"id": 4242, "title": "Ada Lovelace", "type": "private"}
    assert turn.context["_guest_reply_note"] == (
        "You answered earlier in a chat with «Ada Lovelace»: The rate is 1.08.")
    assert w.ch._guest_replies[turn.context["_guest_query"]].meta["title"] == "Ada Lovelace"
    with patch("sdk_client_pool._default_make_client", _Capturing):
        await w.agent.handle_message(turn)
    [prompt] = _Capturing.prompts
    note = agent_mod.GUEST_CALL_NOTE_CHAT.format(name="Ada Lovelace")
    assert note == ("The person summoned you in a chat with «Ada Lovelace». Your text answer "
                    "is posted there; a card, a file or anything else you send goes to your "
                    "chat with them.")
    assert prompt.count(note) == 1
    assert "«»" not in prompt and "the group" not in prompt
    assert w.bot.edits[-1]["text"] == "pong"


async def test_a_summons_from_a_nameless_chat_says_another_chat(summoned):
    w = summoned
    update = _private_summons()
    update.guest_message.reply_to_message = _quoted(from_id=OWN_BOT, text="The rate is 1.08.")
    turn = await _dispatched(w.ch, w.bus, update)
    assert turn.context["_guest_chat"] == {"id": 4242, "title": "", "type": "private"}
    assert turn.context["_guest_reply_note"] == (
        "You answered earlier in another chat: The rate is 1.08.")
    with patch("sdk_client_pool._default_make_client", _Capturing):
        await w.agent.handle_message(turn)
    [prompt] = _Capturing.prompts
    assert prompt.count(agent_mod.GUEST_CALL_NOTE_ELSEWHERE) == 1
    assert agent_mod.GUEST_CALL_NOTE_ELSEWHERE.startswith(
        "The person summoned you in another chat. Your text answer is posted there;")
    assert "«" not in prompt.split("The person summoned you")[1].split(".")[0]


# ---------------------------------------------------------------------------
# a question or a card in the own chat, while the group waits
# ---------------------------------------------------------------------------

def _origin_of(msg: BusMessage) -> dict:
    """The origin ``_process`` binds for *msg*, as far as the tools read it."""
    ctx = msg.context
    origin = {"role": "finance", "execution_role": "finance", "channel": msg.channel,
              "chat_id": ctx.get("chat_id"), "user_id": ctx.get("user_id"),
              "cid": ctx.get("cid"), "message_type": msg.type.value, "source": msg.source,
              "user_text": str(msg.content), "turn_scope": ctx["_turn_scope"]}
    for key in agent_mod.COPIED_CONTEXT_MARKERS:
        if key in ctx:
            origin[key] = ctx[key]
    return origin


def _turn_that(call, *, then: str):
    """A ``_process`` that runs *call* (a tool, as the SDK would) under the
    turn's origin and then answers *then*."""
    async def _proc(msg, on_token=None, turn_report=None, **kw):
        token = agent_mod.origin_var.set(_origin_of(msg))
        try:
            await call(msg)
        finally:
            agent_mod.origin_var.reset(token)
        return then
    return _proc


@pytest.fixture
def broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


async def _ask(_msg):
    res = await tools.ask_user.handler({"question": "Which quarter?", "options": ["Q2", "Q3"]})
    _ask.result = __import__("json").loads(res["content"][0]["text"])


async def test_an_ask_in_the_own_chat_puts_the_reply_in_waiting(summoned, broker):
    w = summoned
    turn = await _dispatched(w.ch, w.bus)
    with patch.object(w.agent, "_process", _turn_that(_ask, then="Q3 it is, then.")):
        await w.agent.handle_message(turn)
    assert _ask.result["status"] == "awaiting_user"
    # the question went to the operator's chat on this bot, the group was told
    assert [m["chat_id"] for m in w.bot.sent] == [OPERATOR]
    assert "Which quarter?" in w.bot.sent[0]["text"]
    assert [e["text"] for e in w.bot.edits] == [WAITING, "Q3 it is, then."]
    assert turn.context["_turn_scope"].detached_ask is True
    assert w.ch._guest_replies == {}


async def test_a_detached_question_ends_with_ill_answer_there(summoned, broker):
    w = summoned
    turn = await _dispatched(w.ch, w.bus)
    with patch.object(w.agent, "_process", _turn_that(_ask, then="")):
        await w.agent.handle_message(turn)
    assert _ask.result["status"] == "awaiting_user"
    assert turn.context["_turn_scope"].detached_ask is True
    assert [e["text"] for e in w.bot.edits] == [WAITING, ASKED_ELSEWHERE]
    assert w.ch._guest_replies == {}


async def test_a_card_wait_then_silence_ends_in_a_decline(summoned):
    w = summoned
    turn = await _dispatched(w.ch, w.bus)

    async def _challenge(_msg):
        # a protected call's challenge, as authz_grants posts it: the same seam
        mid = await w.ch.post_dm_keyboard(
            chat_id=OPERATOR, request_id="r1", text=admitted("Reset the invoices?"),
            options=["Approve", "Deny"])
        assert isinstance(mid, int)
    with patch.object(w.agent, "_process", _turn_that(_challenge, then="")):
        await w.agent.handle_message(turn)
    assert [m["chat_id"] for m in w.bot.sent] == [OPERATOR]
    assert [e["text"] for e in w.bot.edits] == [WAITING, DECLINE]
    assert turn.context["_turn_scope"].detached_ask is False


class _RefusingBot(_Bot):
    async def send_message(self, **kw):
        self.sent.append(kw)
        raise RuntimeError("Forbidden: bot was blocked by the user")


async def test_a_failed_ask_delivery_leaves_detached_ask_false(tmp_path, world, opening, names,
                                                               broker):
    ch, bus, bot = _endpoint(bot=_RefusingBot())
    h = build_two_endpoints(finance=ch)
    agent = _make_agent(tmp_path, role="finance")
    agent._channel_manager.register(h.transport)
    tools.init_tools(channel_manager=h.manager, bus=MagicMock(),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock())
    turn = await _dispatched(ch, bus)
    with patch.object(agent, "_process", _turn_that(_ask, then="")):
        await agent.handle_message(turn)
    assert _ask.result == {"status": "error", "kind": "delivery_failed",
                           "message": "could not deliver the question to the operator"}
    assert turn.context["_turn_scope"].detached_ask is False
    assert len(bot.sent) == 1                             # the one failed post
    assert [e["text"] for e in bot.edits] == [DECLINE]     # never "waiting"


async def test_an_ordinary_turns_keyboard_touches_no_inline_message(summoned, broker):
    """The seam reads the turn that posts: a plain own-chat turn's question
    edits nothing in any group, even while a summoned turn's reply is bound."""
    w = summoned
    turn = await _dispatched(w.ch, w.bus)               # a reply is bound now
    plain = BusMessage(type=MessageType.CHANNEL_IN, source="telegram", target="finance",
                       content="ask me", channel="telegram",
                       context={"chat_id": str(OPERATOR), "user_id": OPERATOR, "cid": "c2",
                                "_tg_bot": OWN_BOT, "_operator_turn": True})
    plain.trusted_user_origin = turn.trusted_user_origin
    with patch.object(w.agent, "_process", _turn_that(_ask, then="")):
        await w.agent.handle_message(plain)
    assert _ask.result["status"] == "awaiting_user"
    assert w.bot.edits == []
    assert w.ch._guest_replies[turn.context["_guest_query"]].terminal is False


# ---------------------------------------------------------------------------
# the opening of an unopened chat, and the two destinations in one turn
# ---------------------------------------------------------------------------

async def test_the_opening_marker_is_stamped_and_the_chat_marked_open(world, opening):
    ch, bus, bot = _endpoint()
    opening.unopened = True
    turn = await _dispatched(ch, bus)
    assert turn.context["_chat_opening"] is True
    assert opening.marks == [OWN_BOT] and opening.asked == [OWN_BOT]

    # the same marker, read by both dispatchers
    opening.unopened, opening.moved = True, True
    assert ch._opening_marker(_user()) == "moved"
    opening.moved = False
    assert ch._opening_marker(_user()) is True
    assert ch._opening_marker(_user(FRIEND)) is None
    opening.unopened = False
    assert ch._opening_marker(_user()) is None

    # a turn its queue refuses opens nothing
    ch2, bus2, bot2 = _endpoint(register=False)
    opening.unopened = True
    opening.marks.clear()
    assert await ch2._dispatch_summoned_turn(_guest()) is False
    assert opening.marks == [] and opening.unopened is True
    assert [e["text"] for e in bot2.edits] == [FAIL]


async def test_a_summons_before_the_chat_opened_still_answers_in_the_group(summoned):
    w = summoned
    w.opening.unopened = True
    turn = await _dispatched(w.ch, w.bus)
    assert turn.context["_chat_opening"] is True and w.opening.marks == [OWN_BOT]
    with patch("sdk_client_pool._default_make_client", _Capturing):
        await w.agent.handle_message(turn)
    [prompt] = _Capturing.prompts
    assert agent_mod.CHAT_OPENING_NOTE in prompt
    assert agent_mod.GUEST_CALL_NOTE.format(title=TITLE) in prompt
    assert w.bot.edits[-1]["text"] == "pong"          # the answer still lands in the group
    assert w.bot.sent == []                            # and nothing in the chat


async def test_a_send_message_tool_call_lands_in_the_dm_and_the_text_edits_the_group_once(
        summoned):
    w = summoned
    turn = await _dispatched(w.ch, w.bus)

    async def _card(_msg):
        res = await tools.send_message.handler({"message": "Here is the breakdown."})
        assert "Error" not in res["content"][0]["text"]
    with patch.object(w.agent, "_process", _turn_that(_card, then="The rate is 1.08.")):
        await w.agent.handle_message(turn)
    assert [(str(m["chat_id"]), str(m["text"])) for m in w.bot.sent] == [
        (str(OPERATOR), "Here is the breakdown.")]
    assert [e["text"] for e in w.bot.edits] == ["The rate is 1.08."]
    assert w.bot.edits[0]["inline_message_id"] == "inl-1"
    assert w.ch._guest_replies == {}
