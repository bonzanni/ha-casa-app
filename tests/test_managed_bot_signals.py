"""The two signals of a bot created for Casa's bot to manage, on Casa's
endpoint, and the update types every endpoint asks for.

* The ``managed_bot_created`` service message is handed to the own-chat
  lifecycle only when the operator sent it in his direct chat with Casa's bot.
  It is never a turn and never answered — its handler is registered ahead of
  the non-text one, which it would otherwise reach and be answered as a
  message Casa cannot read.
* A ``managed_bot`` update alone is handed to the lifecycle's update entry,
  never to its creation entry.
* ``allowed_updates`` is stated on every ``setWebhook`` and ``getUpdates``:
  only Casa's bot asks for ``managed_bot``.

The payloads keep the shape of the two updates Telegram sent when an operator
created a bot (the creation message first, then the ``managed_bot`` update, in
the same second); the ids and names are made up.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import own_voice
from bus import MessageBus
from channels.telegram import (
    ALLOWED_UPDATES_AGENT, ALLOWED_UPDATES_CASA, TelegramChannel, _IntakeQueue,
)
from test_telegram_new_reset import _drain_bus

OPERATOR = 100
CASA_BOT = 111
NEW_BOT = 9100000001

CREATED = {"message": {
    "channel_chat_created": False, "delete_chat_photo": False, "group_chat_created": False,
    "managed_bot_created": {"bot": {"first_name": "FinanceTest", "id": NEW_BOT, "is_bot": True,
                                    "username": "ExampleFinanceTestBot"}},
    "supergroup_chat_created": False,
    "chat": {"first_name": "Op", "id": OPERATOR, "type": "private", "username": "op_example"},
    "date": 1794339353, "message_id": 5436,
    "from": {"first_name": "Op", "id": OPERATOR, "is_bot": False, "language_code": "en",
             "username": "op_example"}},
    "update_id": 392654410}
UPDATED = {"managed_bot": {
    "bot": {"first_name": "FinanceTest", "id": NEW_BOT, "is_bot": True,
            "username": "ExampleFinanceTestBot"},
    "user": {"first_name": "Op", "id": OPERATOR, "is_bot": False, "language_code": "en",
             "username": "op_example"}},
    "update_id": 392654411}

pytestmark = pytest.mark.asyncio


def _user(d: dict):
    return types.SimpleNamespace(id=d["id"], first_name=d.get("first_name"),
                                 is_bot=d.get("is_bot", False), to_dict=lambda: dict(d))


def _created_update(*, sender: int = OPERATOR, chat: int = OPERATOR):
    raw = json.loads(json.dumps(CREATED))
    m = raw["message"]
    m["from"]["id"], m["chat"]["id"] = sender, chat
    msg = types.SimpleNamespace(
        message_id=m["message_id"], text=None, caption=None, reply_to_message=None,
        chat=types.SimpleNamespace(id=m["chat"]["id"], type="private"),
        from_user=_user(m["from"]),
        managed_bot_created=types.SimpleNamespace(bot=_user(m["managed_bot_created"]["bot"])))
    return types.SimpleNamespace(update_id=raw["update_id"], message=msg, managed_bot=None,
                                 effective_user=msg.from_user, effective_chat=msg.chat)


def _updated_update(*, owner: int = OPERATOR):
    raw = json.loads(json.dumps(UPDATED))
    raw["managed_bot"]["user"]["id"] = owner
    mb = types.SimpleNamespace(bot=_user(raw["managed_bot"]["bot"]),
                               user=_user(raw["managed_bot"]["user"]))
    return types.SimpleNamespace(update_id=raw["update_id"], message=None, managed_bot=mb)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return types.SimpleNamespace(message_id=1)


def _endpoint(*, casa: bool = True):
    bus = MessageBus()
    bus.register("assistant", AsyncMock())
    ch = TelegramChannel(bot_token="T", chat_id=str(OPERATOR), default_agent="assistant" if casa
                         else "finance", bus=bus, is_casa=casa,
                         known_bot_id=CASA_BOT if casa else 222)
    ch._app = types.SimpleNamespace(bot=_Bot())
    return ch, bus


@pytest.fixture
def lifecycle(monkeypatch):
    created, updated = AsyncMock(), AsyncMock()
    monkeypatch.setattr(own_voice, "on_managed_bot_created", created)
    monkeypatch.setattr(own_voice, "on_managed_bot_updated", updated)
    return types.SimpleNamespace(created=created, updated=updated)


# ---------------------------------------------------------------------------
# the creation message
# ---------------------------------------------------------------------------

async def test_the_creation_message_is_handed_to_the_lifecycle_and_is_never_a_turn(lifecycle):
    ch, bus = _endpoint()
    await ch._on_managed_bot_created(_created_update())
    lifecycle.created.assert_awaited_once_with(
        CREATED["message"]["managed_bot_created"]["bot"], OPERATOR)
    lifecycle.updated.assert_not_awaited()
    assert await _drain_bus(bus) == []                      # no turn
    assert ch._app.bot.sent == []                           # no reply


@pytest.mark.parametrize("sender,chat", [(555, OPERATOR), (OPERATOR, 555), (555, 555)])
async def test_a_creation_message_not_from_the_operator_in_his_chat_is_ignored(
        lifecycle, sender, chat):
    ch, bus = _endpoint()
    await ch._on_managed_bot_created(_created_update(sender=sender, chat=chat))
    lifecycle.created.assert_not_awaited()
    assert await _drain_bus(bus) == [] and ch._app.bot.sent == []


async def test_a_creation_message_on_an_agents_own_bot_is_ignored(lifecycle):
    ch, _bus = _endpoint(casa=False)
    await ch._on_managed_bot_created(_created_update())
    lifecycle.created.assert_not_awaited()


async def test_a_managed_bot_update_alone_never_registers_a_bot(lifecycle):
    ch, bus = _endpoint()
    await ch._on_managed_bot_updated(_updated_update())
    lifecycle.created.assert_not_awaited()
    lifecycle.updated.assert_awaited_once_with(UPDATED["managed_bot"]["bot"], OPERATOR)
    assert await _drain_bus(bus) == [] and ch._app.bot.sent == []


async def test_a_managed_bot_update_of_another_owner_is_passed_with_that_owner(lifecycle):
    """The lifecycle ignores another owner's bot; the endpoint names the owner."""
    ch, _bus = _endpoint()
    await ch._on_managed_bot_updated(_updated_update(owner=555))
    lifecycle.updated.assert_awaited_once_with(UPDATED["managed_bot"]["bot"], 555)


async def test_a_lifecycle_failure_is_logged_not_raised(lifecycle, caplog):
    lifecycle.created.side_effect = RuntimeError("boom")
    ch, bus = _endpoint()
    await ch._on_managed_bot_created(_created_update())
    assert "registering a created bot failed" in caplog.text
    assert await _drain_bus(bus) == [] and ch._app.bot.sent == []


# ---------------------------------------------------------------------------
# registration: the creation handler ahead of every other message handler
# ---------------------------------------------------------------------------

def _rebuild_registrations(monkeypatch, *, casa: bool):
    import channels.telegram as tg_mod
    import telegram.ext as tg_ext
    created, text, non_text = object(), object(), object()
    monkeypatch.setattr(tg_mod, "_managed_bot_created_filter", lambda: created)
    monkeypatch.setattr(tg_mod, "_text_filter", lambda: text)
    monkeypatch.setattr(tg_mod, "_non_text_filter", lambda: non_text)
    monkeypatch.setattr(tg_mod, "MessageHandler",
                        lambda f, cb, block=True: ("MessageHandler", f, cb, block))

    class _Updated:
        def __init__(self, cb, block=True):
            self.cb, self.block = cb, block
    monkeypatch.setattr(tg_ext, "ManagedBotUpdatedHandler", _Updated, raising=False)
    registered = []
    app = MagicMock()
    app.add_handler = registered.append
    app.initialize = AsyncMock(side_effect=RuntimeError("stop after registration"))
    app.shutdown, app.stop = AsyncMock(), AsyncMock()
    chain = MagicMock()
    chain.token.return_value = chain
    chain.build.return_value = app
    monkeypatch.setattr(tg_mod.Application, "builder", lambda: chain)
    ch = TelegramChannel(bot_token="t", chat_id=str(OPERATOR), is_casa=casa,
                         default_agent="assistant" if casa else "finance",
                         known_bot_id=None if casa else 222)
    return ch, registered, chain, (created, text, non_text), _Updated


async def test_casa_registers_the_creation_handler_before_the_other_message_handlers(monkeypatch):
    ch, registered, _chain, (created, text, non_text), updated_cls = _rebuild_registrations(
        monkeypatch, casa=True)
    with pytest.raises(RuntimeError, match="stop after registration"):
        await ch._rebuild()
    filters_in_order = [r[1] for r in registered if isinstance(r, tuple)]
    assert filters_in_order.index(created) < filters_in_order.index(non_text)
    assert filters_in_order.index(created) < filters_in_order.index(text)
    assert ("MessageHandler", created, ch._on_managed_bot_created, False) in registered
    [updated] = [r for r in registered if isinstance(r, updated_cls)]
    assert updated.cb == ch._on_managed_bot_updated and updated.block is False


async def test_an_agents_own_bot_registers_neither_and_queues_through_its_intake(monkeypatch):
    ch, registered, chain, (created, _text, _non_text), updated_cls = _rebuild_registrations(
        monkeypatch, casa=False)
    with pytest.raises(RuntimeError, match="stop after registration"):
        await ch._rebuild()
    assert created not in [r[1] for r in registered if isinstance(r, tuple)]
    assert not [r for r in registered if isinstance(r, updated_cls)]
    [queue] = chain.update_queue.call_args.args
    assert isinstance(queue, _IntakeQueue) and queue._channel is ch


async def test_casas_own_bot_keeps_the_librarys_queue(monkeypatch):
    ch, _registered, chain, _f, _u = _rebuild_registrations(monkeypatch, casa=True)
    with pytest.raises(RuntimeError):
        await ch._rebuild()
    chain.update_queue.assert_not_called()


# ---------------------------------------------------------------------------
# allowed_updates
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


@pytest.mark.parametrize("casa,expected", [(True, ALLOWED_UPDATES_CASA),
                                           (False, ALLOWED_UPDATES_AGENT)])
async def test_polling_states_the_endpoints_update_types(monkeypatch, casa, expected):
    app = _started_app(monkeypatch)
    app.bot.id = CASA_BOT if casa else 222
    ch = TelegramChannel(bot_token="t", chat_id=str(OPERATOR), is_casa=casa,
                         default_agent="assistant" if casa else "finance",
                         known_bot_id=None if casa else 222)
    ch.check_agent_logs = AsyncMock()
    await ch._rebuild()
    app.updater.start_polling.assert_awaited_once_with(allowed_updates=list(expected))
    # resuming after a paused restart states them again
    await ch.resume_updates()
    assert app.updater.start_polling.await_args.kwargs == {"allowed_updates": list(expected)}


@pytest.mark.parametrize("casa,expected", [(True, ALLOWED_UPDATES_CASA),
                                           (False, ALLOWED_UPDATES_AGENT)])
async def test_a_webhook_states_the_endpoints_update_types(monkeypatch, casa, expected):
    app = _started_app(monkeypatch)
    app.bot.id = CASA_BOT if casa else 222
    ch = TelegramChannel(bot_token="t", chat_id=str(OPERATOR), is_casa=casa,
                         default_agent="assistant" if casa else "finance",
                         known_bot_id=None if casa else 222,
                         webhook_url="https://casa.example",
                         webhook_secret_for=lambda bot: f"s-{bot}")
    ch.check_agent_logs = AsyncMock()
    await ch._rebuild()
    kwargs = app.bot.set_webhook.await_args.kwargs
    assert kwargs["allowed_updates"] == list(expected)


def test_only_casas_bot_asks_for_managed_bot_updates():
    assert ALLOWED_UPDATES_CASA == ("message", "callback_query", "managed_bot")
    assert ALLOWED_UPDATES_AGENT == ("message", "callback_query")


# ---------------------------------------------------------------------------
# against the real python-telegram-bot, in a subprocess
# ---------------------------------------------------------------------------

_REAL_PTB = textwrap.dedent(r"""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    try:
        from telegram import Update
    except ImportError:
        print(json.dumps({"skip": True})); raise SystemExit(0)
    from telegram.ext import ManagedBotUpdatedHandler
    from channels.telegram import (ALLOWED_UPDATES_AGENT, ALLOWED_UPDATES_CASA,
                                   _managed_bot_created_filter, _non_text_filter, _text_filter)
    created = Update.de_json(json.loads(sys.argv[2]), None)
    updated = Update.de_json(json.loads(sys.argv[3]), None)
    c = _managed_bot_created_filter()
    h = ManagedBotUpdatedHandler(lambda u, ctx: None)
    out = {
      "created": [bool(c.check_update(created)), bool(_non_text_filter().check_update(created)),
                  bool(_text_filter().check_update(created)), bool(h.check_update(created))],
      "updated": [bool(c.check_update(updated)), bool(h.check_update(updated))],
      "bot": created.message.managed_bot_created.bot.to_dict(),
      "from": created.message.from_user.id,
      "managed": [updated.managed_bot.bot.to_dict(), updated.managed_bot.user.id],
      "types": sorted(set(ALLOWED_UPDATES_CASA + ALLOWED_UPDATES_AGENT) - set(Update.ALL_TYPES)),
    }
    print(json.dumps(out))
""")


def test_real_ptb_routes_the_two_signals_as_the_endpoint_assumes():
    """The creation message matches the creation filter AND the non-text one
    (so the registration order is what keeps it from the "can't read" reply)
    and no text filter; the managed_bot update matches only its handler; the
    bot is read as the endpoint passes it; the stated update types are
    Telegram's. Needs python-telegram-bot 22.8 (Bot API 10.0)."""
    root = os.path.join(os.path.dirname(__file__), "..", "casa", "rootfs", "opt", "casa")
    r = subprocess.run([sys.executable, "-c", _REAL_PTB, os.path.abspath(root),
                        json.dumps(CREATED), json.dumps(UPDATED)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if out.get("skip"):
        pytest.skip("python-telegram-bot not installed")
    assert out["created"] == [True, True, False, False]
    assert out["updated"] == [False, True]
    assert out["bot"] == CREATED["message"]["managed_bot_created"]["bot"]
    assert out["from"] == OPERATOR
    assert out["managed"] == [UPDATED["managed_bot"]["bot"], OPERATOR]
    assert out["types"] == []
