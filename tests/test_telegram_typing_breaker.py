"""L6 (v0.52.0): typing circuit breaker.

Transient transport errors (NetworkError/TimedOut) must NOT trip the 401
circuit breaker, and a successful _rebuild must heal a previously-tripped
breaker so a past outage doesn't kill typing for the process lifetime.
"""

from __future__ import annotations

import asyncio
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import channels.telegram as tg
from channels.telegram import TelegramChannel
from telegram.error import NetworkError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _seed_lease(ch: TelegramChannel, chat_id: str = "1") -> None:
    """Give the chat a long-lived lease so ``_typing_loop`` (invoked directly)
    keeps running — the loop now exits when no live lease remains (r1-2)."""
    ch._typing_leases[chat_id] = {"cid": time.monotonic() + 3600.0}


async def test_network_errors_do_not_trip_typing_breaker(monkeypatch):
    monkeypatch.setattr(tg, "_TYPING_BACKOFF_INIT", 0.0)
    monkeypatch.setattr(tg, "_TYPING_BACKOFF_MAX", 0.0)
    monkeypatch.setattr(tg, "_TYPING_INTERVAL", 0.0)
    ch = TelegramChannel(bot_token="T", chat_id="1")
    _seed_lease(ch)
    calls = {"n": 0}

    async def failing_send(**_kw):
        calls["n"] += 1
        if calls["n"] > tg._TYPING_CIRCUIT_BREAK + 5:
            raise asyncio.CancelledError  # end the loop after >10 failures
        raise NetworkError("transient outage")

    ch._app = types.SimpleNamespace(
        bot=types.SimpleNamespace(send_chat_action=failing_send))

    await ch._typing_loop("1")

    assert calls["n"] > tg._TYPING_CIRCUIT_BREAK
    assert ch._typing_suspended is False
    assert ch._typing_consecutive_failures == 0


async def test_non_transport_error_still_trips_breaker(monkeypatch):
    """A genuine (non-transport) TelegramError must still trip the breaker —
    the 401 guard is preserved."""
    monkeypatch.setattr(tg, "_TYPING_BACKOFF_INIT", 0.0)
    monkeypatch.setattr(tg, "_TYPING_BACKOFF_MAX", 0.0)
    monkeypatch.setattr(tg, "_TYPING_INTERVAL", 0.0)
    ch = TelegramChannel(bot_token="T", chat_id="1")
    _seed_lease(ch)

    async def failing_send(**_kw):
        raise tg.TelegramError("Unauthorized")

    ch._app = types.SimpleNamespace(
        bot=types.SimpleNamespace(send_chat_action=failing_send))

    await ch._typing_loop("1")

    assert ch._typing_suspended is True
    assert ch._typing_consecutive_failures >= tg._TYPING_CIRCUIT_BREAK


async def test_successful_rebuild_heals_breaker(monkeypatch):
    """A successful _rebuild must clear a tripped breaker (polling mode)."""
    from telegram.ext import Application

    app = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.add_handler = MagicMock()
    app.add_error_handler = MagicMock()
    app.updater = MagicMock()
    app.updater.start_polling = AsyncMock()
    app.bot = MagicMock()
    app.bot.id = 4242       # initialize() ran getMe

    def fake_builder():
        chain = MagicMock()
        chain.token = MagicMock(return_value=chain)
        chain.build = MagicMock(return_value=app)
        return chain

    monkeypatch.setattr(Application, "builder", fake_builder)

    ch = TelegramChannel(bot_token="T", chat_id="1")  # polling (no webhook_url)
    ch._typing_suspended = True
    ch._typing_consecutive_failures = tg._TYPING_CIRCUIT_BREAK

    await ch._rebuild()

    assert ch._typing_suspended is False
    assert ch._typing_consecutive_failures == 0
