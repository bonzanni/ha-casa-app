"""L7 (v0.52.0): turn_finished must stop the per-chat typing indicator.

In block mode (and on any empty/`<silent/>` turn) no send()/send_response()
or first-token teardown runs, so the typing loop started in _handle would issue
send_chat_action forever. turn_finished() is the teardown hook the agent
calls on the suppressed-turn path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from channels.telegram import TelegramChannel

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_turn_finished_cancels_typing_task():
    ch = TelegramChannel(
        bot_token="t", chat_id="123", default_agent="a",
    )
    ch._app = type("_App", (), {"bot": AsyncMock()})()
    ch._start_typing("123", "cid-1")
    task = ch._typing_loops["123"]
    assert not task.done()

    # The delivery context carries the SAME cid the lease was keyed with.
    await ch.turn_finished({"chat_id": "123", "cid": "cid-1"})
    await asyncio.sleep(0)  # let cancellation propagate

    assert task.cancelled() or task.done()
    assert "123" not in ch._typing_loops
    assert "123" not in ch._typing_leases


async def test_turn_finished_falls_back_to_default_chat():
    """A non-numeric context chat_id resolves to the channel default (mirrors
    send()/send_response() via _resolve_chat_id)."""
    ch = TelegramChannel(
        bot_token="t", chat_id="123", default_agent="a",
    )
    ch._app = type("_App", (), {"bot": AsyncMock()})()
    ch._start_typing("123", "cid-1")
    task = ch._typing_loops["123"]

    # No cid in context → release-all fallback for the resolved default chat.
    await ch.turn_finished({"chat_id": "interval:heartbeat"})
    await asyncio.sleep(0)

    assert task.cancelled() or task.done()
    assert "123" not in ch._typing_loops
    assert "123" not in ch._typing_leases
