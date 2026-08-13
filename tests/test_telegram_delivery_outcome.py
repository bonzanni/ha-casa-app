"""#556 — deliveries report whether anything actually reached Telegram.

The defect these pin: every delivery method returns normally when the PTB
application is absent (a reconnect window), having made ZERO Bot API calls.
A caller that suppresses a one-shot operator notice cannot tell that apart
from success, so the notice is consumed without ever being displayed.

Assertions are on Bot API call COUNTS and the returned outcome, never on a
method merely having returned.
"""
from __future__ import annotations

from unittest.mock import MagicMock

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


class TestSendResponse:
    async def test_plain_text_delegates_outcome_verbatim(self):
        ch, bot = _mk_channel_with_fake_bot()
        ctx = {"chat_id": "42"}
        assert await ch.send_response("just text", ctx) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True

    async def test_rendered_single_page_is_delivered(self):
        ch, bot = _mk_channel_with_fake_bot()
        ctx = {"chat_id": "42"}
        assert await ch.send_response("**hi**", ctx) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True
        assert bot.send_message.await_count == 1

    async def test_without_app_reports_not_delivered(self):
        ch, bot = _mk_channel_with_fake_bot()
        ch._app = None
        ctx = {"chat_id": "42"}
        assert await ch.send_response("**hi**", ctx) is DeliveryOutcome.NOT_DELIVERED
        assert "_delivery_head_sent" not in ctx
        assert bot.send_message.await_count == 0

    async def test_head_lands_then_tail_raises_keeps_the_stamp(self):
        """Page 1 DISPLAYED the notice; the raise must not read as 'showed
        nothing', or the operator is re-told something they already read."""
        from telegram.error import TimedOut

        ch, bot = _mk_channel_with_fake_bot()
        long_text = "\n\n".join([f"para {i} with some words"
                                 for i in range(600)])
        bot.send_message.side_effect = [
            MagicMock(message_id=1), TimedOut("network")]
        ctx = {"chat_id": "42"}
        with pytest.raises(TimedOut):
            await ch.send_response(long_text, ctx)
        assert ctx["_delivery_head_sent"] is True
        assert bot.send_message.await_count == 2


class TestFinalizeStream:
    async def test_not_modified_is_delivered(self):
        """The edit text IS the notice-bearing text, so an unchanged message
        already displays the notice (#556 design §2.4 ruling 1)."""
        from telegram.error import BadRequest

        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        bot.edit_message_text.side_effect = BadRequest("Message is not modified")
        assert await ch.finalize_stream(
            "hi", ctx, on_token) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True

    async def test_failed_edit_is_not_delivered(self):
        from telegram.error import BadRequest

        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        bot.edit_message_text.side_effect = BadRequest("chat not found")
        assert await ch.finalize_stream(
            "hi", ctx, on_token) is DeliveryOutcome.NOT_DELIVERED
        assert "_delivery_head_sent" not in ctx

    async def test_without_app_is_not_delivered(self):
        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        ch._app = None
        assert await ch.finalize_stream(
            "hi", ctx, on_token) is DeliveryOutcome.NOT_DELIVERED

    async def test_whitespace_overflow_is_not_delivered(self):
        """#305 drops unsendable chunks and returns without editing. The
        operator saw streamed prose, but the notice is prepended AFTER
        streaming — it was never in what they saw."""
        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        bot.edit_message_text.reset_mock()
        assert await ch.finalize_stream(
            " " * 5000, ctx, on_token) is DeliveryOutcome.NOT_DELIVERED
        assert bot.edit_message_text.await_count == 0


class TestFinalizeResponseStream:
    async def test_page1_edit_is_delivered(self):
        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        assert await ch.finalize_response_stream(
            "**hi**", ctx, on_token) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True

    async def test_badrequest_fallback_still_delivered(self):
        """The BadRequest -> fallback0 retry landed, so the notice showed."""
        from telegram.error import BadRequest

        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        bot.edit_message_text.side_effect = [BadRequest("bad entity"), None]
        assert await ch.finalize_response_stream(
            "**hi**", ctx, on_token) is DeliveryOutcome.DELIVERED
        assert bot.edit_message_text.await_count == 2

    async def test_failed_page1_edit_is_not_delivered(self):
        from telegram.error import BadRequest

        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        bot.edit_message_text.side_effect = BadRequest("chat not found")
        assert await ch.finalize_response_stream(
            "**hi**", ctx, on_token) is DeliveryOutcome.NOT_DELIVERED
        assert "_delivery_head_sent" not in ctx

    async def test_delegates_verbatim_when_no_stream_started(self):
        """No message_id -> send_response; its outcome must pass through."""
        ch, bot = _mk_channel_with_fake_bot()
        ch._app = None
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        assert await ch.finalize_response_stream(
            "**hi**", ctx, on_token) is DeliveryOutcome.NOT_DELIVERED

    async def test_plain_text_delegates_to_finalize_stream_verbatim(self):
        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        assert await ch.finalize_response_stream(
            "plain text", ctx, on_token) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True

