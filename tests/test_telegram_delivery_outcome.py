"""#556 — deliveries report whether anything actually reached Telegram.

The defect these pin: every delivery method returns normally when the PTB
application is absent (a reconnect window), having made ZERO Bot API calls.
A caller that suppresses a one-shot operator notice cannot tell that apart
from success, so the notice is consumed without ever being displayed.

Assertions are on Bot API call COUNTS and the returned outcome, never on a
method merely having returned.
"""
from __future__ import annotations

import pytest

from channels import DeliveryOutcome
from test_telegram_topic_stream import _mk_channel_with_fake_bot

pytestmark = pytest.mark.asyncio


class TestSend:
    async def test_send_returns_delivered_and_stamps_context(self):
        ch, bot = _mk_channel_with_fake_bot()
        ctx = {"chat_id": "42"}
        assert await ch.send("hi", ctx) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True
        assert bot.send_message.await_count == 1

    async def test_send_without_app_reports_not_delivered(self):
        """The reconnect window of #556 — returns normally, sends nothing."""
        ch, bot = _mk_channel_with_fake_bot()
        ch._app = None
        ctx = {"chat_id": "42"}
        assert await ch.send("hi", ctx) is DeliveryOutcome.NOT_DELIVERED
        assert "_delivery_head_sent" not in ctx
        assert bot.send_message.await_count == 0
