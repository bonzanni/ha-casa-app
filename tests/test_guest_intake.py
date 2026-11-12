"""A specialist's bot summoned by @mention in a group it is not a member of.

Telegram sends such a summons as ``Update.guest_message``. On an agent's own
bot it bypasses the own-chat lifecycle (it is not a message of the agent's
chat) and is routed by who sent it:

* the operator, on a bound bot → a summoned turn, under the operator's own
  rate limit (the same key his direct-chat turns use, whichever group);
* anyone else, in a group the operator allowed for this specialist → noted,
  not answered yet;
* everything else → dropped silently, with no Telegram call.

Casa's own bot never asks for these updates and registers no handler for them.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import guest_groups
import own_voice
import own_voice_lifecycle
from bus import MessageBus
from channels.telegram import TelegramChannel, _IntakeQueue
from rate_limit import RateLimiter

pytestmark = pytest.mark.asyncio

OPERATOR = 100
CASA_BOT = 111
OWN_BOT = 222
FRIEND = 777
GROUP = -5


async def _noop(_msg):
    return None


def _endpoint(kind: str = "own", *, rate_limiter=None):
    casa = kind == "casa"
    role = "assistant" if casa else "finance"
    bus = MessageBus()
    bus.register(role, _noop)
    ch = TelegramChannel(bot_token="T", chat_id=str(OPERATOR), default_agent=role, bus=bus,
                         is_casa=casa, known_bot_id=CASA_BOT if casa else OWN_BOT,
                         rate_limiter=rate_limiter)
    ch._start_typing = lambda *a, **k: None
    ch._stop_typing = lambda *a, **k: None
    ch._app = types.SimpleNamespace(bot=AsyncMock())
    ch._session_registry = None
    return ch, bus


def _user(uid=OPERATOR):
    return types.SimpleNamespace(id=uid, first_name="Op", is_bot=False)


def _guest(uid=OPERATOR, *, chat_id=GROUP, text="@FinanceBot how much?", update_id=3):
    chat = types.SimpleNamespace(id=chat_id, title="Test Group", type="group")
    user = _user(uid)
    gm = types.SimpleNamespace(chat=chat, from_user=user, text=text, guest_query_id="q1",
                               reply_to_message=None, message_id=58)
    return types.SimpleNamespace(
        update_id=update_id, message=None, guest_message=gm, callback_query=None,
        effective_chat=chat, effective_user=user, effective_message=gm,
        to_dict=lambda: {"update_id": update_id, "guest_message": {"text": text}})


def _text(text="hi", uid=OPERATOR):
    user = _user(uid)
    message = types.SimpleNamespace(text=text, message_id=70, from_user=user, sender_chat=None,
                                    chat=types.SimpleNamespace(id=OPERATOR),
                                    reply_to_message=None)
    return types.SimpleNamespace(
        update_id=5070, message=message, guest_message=None, callback_query=None,
        effective_chat=types.SimpleNamespace(id=str(OPERATOR)), effective_user=user,
        to_dict=lambda: {"update_id": 5070, "message": {"text": text}})


@pytest.fixture
def intake(monkeypatch):
    calls = []

    async def fake(bot_id, update_json):
        calls.append((bot_id, update_json))
        return "deliver"
    monkeypatch.setattr(own_voice, "intake", fake)
    return calls


@pytest.fixture
def world(monkeypatch):
    """A bound bot; the group allowed or not, as each test sets it."""
    w = types.SimpleNamespace(bound=True, allowed=False, asked=[])

    class _Store:
        def allowed(self, role, chat_id, component_id):
            w.asked.append((role, chat_id, component_id))
            return w.allowed
    monkeypatch.setattr(own_voice, "is_bound", lambda bot_id: w.bound and bot_id == OWN_BOT)
    monkeypatch.setattr(guest_groups, "store", lambda: _Store())
    monkeypatch.setattr(guest_groups, "component_of", lambda role: f"{role}-component")
    return w


# ---------------------------------------------------------------------------
# the intake queue
# ---------------------------------------------------------------------------

async def test_a_guest_update_skips_the_own_voice_intake(intake):
    ch, _bus = _endpoint()
    q = _IntakeQueue(ch)
    update = _guest()
    await q.put(update)
    assert intake == []
    assert q.qsize() == 1 and q.get_nowait() is update


async def test_a_text_update_still_passes_the_intake(intake):
    ch, _bus = _endpoint()
    q = _IntakeQueue(ch)
    update = _text()
    await q.put(update)
    assert intake == [(OWN_BOT, update.to_dict())]
    assert q.qsize() == 1


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uid,allowed,expected", [(OPERATOR, False, "operator"),
                                                  (FRIEND, True, "guest"),
                                                  (FRIEND, False, "drop")])
async def test_the_decision(world, uid, allowed, expected):
    ch, _bus = _endpoint()
    world.allowed = allowed
    assert ch._guest_decision(_guest(uid)) == expected
    if uid == FRIEND:
        assert world.asked == [("finance", GROUP, "finance-component")]


async def test_a_summons_on_an_unbound_bot_is_dropped(world):
    ch, _bus = _endpoint()
    world.bound = False
    world.allowed = True
    assert ch._guest_decision(_guest(OPERATOR)) == "drop"
    assert ch._guest_decision(_guest(FRIEND)) == "drop"


async def test_is_bound_reads_the_bot_phase_without_side_effects(monkeypatch):
    monkeypatch.setattr(own_voice_lifecycle, "_CURRENT", None)
    assert own_voice.is_bound(OWN_BOT) is False
    assert own_voice_lifecycle._CURRENT is None  # no lifecycle was created
    store = types.SimpleNamespace(bots={OWN_BOT: {"phase": own_voice_lifecycle.BOUND},
                                        333: {"phase": own_voice_lifecycle.UNBOUND}})
    monkeypatch.setattr(own_voice_lifecycle, "_CURRENT", types.SimpleNamespace(store=store))
    assert own_voice.is_bound(OWN_BOT) is True
    assert own_voice.is_bound(333) is False
    assert own_voice.is_bound(444) is False


# ---------------------------------------------------------------------------
# the handler
# ---------------------------------------------------------------------------

async def test_the_operators_summons_is_handed_to_the_summoned_turn(world):
    ch, bus = _endpoint()
    ch._dispatch_summoned_turn = AsyncMock(return_value=True)
    update = _guest(OPERATOR)
    await ch._on_guest_message(update, None)
    ch._dispatch_summoned_turn.assert_awaited_once_with(update)


async def test_a_friend_in_an_allowed_group_is_dropped_in_this_release(world, caplog):
    ch, bus = _endpoint()
    ch._dispatch_summoned_turn = AsyncMock()
    world.allowed = True
    with caplog.at_level("INFO"):
        await ch._on_guest_message(_guest(FRIEND), None)
    ch._dispatch_summoned_turn.assert_not_awaited()
    assert ch._app.bot.mock_calls == []  # no answer_guest_query, nothing sent
    assert bus.queues["finance"].empty()
    assert any("not answered in this release" in r.getMessage() for r in caplog.records)


async def test_a_drop_makes_no_telegram_call(world):
    ch, bus = _endpoint()
    ch._dispatch_summoned_turn = AsyncMock()
    await ch._on_guest_message(_guest(FRIEND), None)
    ch._dispatch_summoned_turn.assert_not_awaited()
    assert ch._app.bot.mock_calls == []
    assert bus.queues["finance"].empty()


async def test_no_operator_identity_drops_the_summons(world, monkeypatch):
    ch, _bus = _endpoint(rate_limiter=RateLimiter(capacity=5))
    ch._dispatch_summoned_turn = AsyncMock()
    monkeypatch.setattr(ch, "operator_user_id", lambda: None)
    await ch._on_guest_message(_guest(OPERATOR), None)
    ch._dispatch_summoned_turn.assert_not_awaited()
    assert ch._app.bot.mock_calls == []


async def test_two_groups_share_the_operators_limiter(world, caplog):
    limiter = RateLimiter(capacity=1, window_s=3600)
    ch, _bus = _endpoint(rate_limiter=limiter)
    ch._dispatch_summoned_turn = AsyncMock(return_value=True)
    await ch._on_guest_message(_guest(OPERATOR, chat_id=-5), None)
    with caplog.at_level("INFO"):
        await ch._on_guest_message(_guest(OPERATOR, chat_id=-6, update_id=4), None)
    assert ch._dispatch_summoned_turn.await_count == 1
    assert set(limiter._buckets) == {str(OPERATOR)}
    assert ch._app.bot.mock_calls == []  # a refusal is not answered in the group
    assert any("rate limit" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def _started_app(monkeypatch):
    import channels.telegram as tg_mod
    app = MagicMock()
    app.initialize, app.start = AsyncMock(), AsyncMock()
    app.bot = MagicMock(id=None)
    app.bot.set_webhook = AsyncMock()
    app.updater = MagicMock(start_polling=AsyncMock(), stop=AsyncMock(), running=False)
    chain = MagicMock()
    chain.token.return_value = chain
    chain.build.return_value = app
    monkeypatch.setattr(tg_mod.Application, "builder", lambda: chain)
    return app


@pytest.mark.parametrize("casa,count", [(True, 0), (False, 1)])
async def test_casas_bot_registers_no_guest_handler(monkeypatch, casa, count):
    import channels.telegram as tg_mod
    app = _started_app(monkeypatch)
    app.bot.id = CASA_BOT if casa else OWN_BOT
    made = []

    def handler(flt, callback, **kw):
        made.append((callback, kw))
        return types.SimpleNamespace(callback=callback, **kw)
    monkeypatch.setattr(tg_mod, "MessageHandler", handler)
    ch = TelegramChannel(bot_token="t", chat_id=str(OPERATOR), is_casa=casa,
                         default_agent="assistant" if casa else "finance",
                         known_bot_id=None if casa else OWN_BOT)
    ch.check_agent_logs = AsyncMock()
    await ch._rebuild()
    guest = [kw for cb, kw in made if cb == ch._on_guest_message]
    assert guest == [{"block": False}] * count
    registered = [c.args[0] for c in app.add_handler.call_args_list]
    assert sum(getattr(h, "callback", None) == ch._on_guest_message
               for h in registered) == count
