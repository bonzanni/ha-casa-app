"""H5 (v0.52.0): the Telegram webhook ACK must not block on turn processing.

Pre-fix ``process_webhook_update`` awaited ``Application.process_update``,
which ran the whole engagement SDK turn (minutes) before the aiohttp route
could return 200 — Telegram timed out and redelivered the update, duplicating
turns. The fix enqueues the update onto PTB's ``update_queue`` (fast ACK) and
dedupes redelivered ``update_id``s.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _channel_with_fake_app():
    from channels.telegram import TelegramChannel

    ch = TelegramChannel(bot_token="t", chat_id="1")
    app = MagicMock()
    app.update_queue = asyncio.Queue()
    # If the (buggy) code path awaited process_update inline, make it hang
    # so the wait_for below fails loudly.
    never = asyncio.Event()

    async def _hang(update):
        await never.wait()

    app.process_update = AsyncMock(side_effect=_hang)
    ch._app = app
    return ch, app


async def test_webhook_ack_not_blocked_by_slow_turn(monkeypatch):
    ch, app = _channel_with_fake_app()
    upd = MagicMock(update_id=42)
    monkeypatch.setattr(
        "channels.telegram.Update",
        MagicMock(de_json=MagicMock(return_value=upd)),
    )
    # Regression: pre-fix this awaited process_update (a full SDK turn,
    # minutes) and would hang here forever.
    await asyncio.wait_for(
        ch.process_webhook_update({"update_id": 42}), timeout=1.0,
    )
    assert app.update_queue.qsize() == 1          # queued for PTB's fetcher
    app.process_update.assert_not_awaited()       # never awaited inline


async def test_webhook_duplicate_update_id_dropped(monkeypatch):
    ch, app = _channel_with_fake_app()
    upd = MagicMock(update_id=99)
    monkeypatch.setattr(
        "channels.telegram.Update",
        MagicMock(de_json=MagicMock(return_value=upd)),
    )
    first = await ch.process_webhook_update({"update_id": 99})
    second = await ch.process_webhook_update({"update_id": 99})  # redelivery
    assert app.update_queue.qsize() == 1                # retry deduped
    # #428: the two outcomes must be distinguishable by the caller.
    assert first == "accepted"
    assert second == "duplicate"


async def test_webhook_outcome_ignored_when_not_started(monkeypatch):
    # #428: the silent early exits (channel not started, unparseable payload)
    # report "ignored" rather than masquerading as accepted.
    from channels.telegram import TelegramChannel

    ch = TelegramChannel(bot_token="t", chat_id="1")
    ch._app = None
    assert await ch.process_webhook_update({"update_id": 1}) == "ignored"

    ch2, _app = _channel_with_fake_app()
    monkeypatch.setattr(
        "channels.telegram.Update",
        MagicMock(de_json=MagicMock(return_value=None)),
    )
    assert await ch2.process_webhook_update({"bogus": True}) == "ignored"


async def test_webhook_distinct_update_ids_both_queued(monkeypatch):
    ch, app = _channel_with_fake_app()

    def _de_json(payload, _bot):
        return MagicMock(update_id=payload["update_id"])

    monkeypatch.setattr(
        "channels.telegram.Update", MagicMock(de_json=_de_json),
    )
    await ch.process_webhook_update({"update_id": 1})
    await ch.process_webhook_update({"update_id": 2})
    assert app.update_queue.qsize() == 2


async def test_webhook_seen_ids_bounded(monkeypatch):
    """The dedup LRU must stay bounded so long-running add-ons don't grow it
    without limit."""
    ch, app = _channel_with_fake_app()

    def _de_json(payload, _bot):
        return MagicMock(update_id=payload["update_id"])

    monkeypatch.setattr(
        "channels.telegram.Update", MagicMock(de_json=_de_json),
    )
    for i in range(1000):
        await ch.process_webhook_update({"update_id": i})
    assert len(ch._seen_update_ids) <= 256
    assert app.update_queue.qsize() == 1000
