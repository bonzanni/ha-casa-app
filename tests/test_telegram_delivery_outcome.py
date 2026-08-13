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
        """Sol + Terra diff r3: the first version asserted DELIVERED, which a
        direct send would also produce — it proved nothing about delegation.
        The delegate is stubbed with a distinctive outcome so ONLY verbatim
        propagation can satisfy it."""
        ch, bot = _mk_channel_with_fake_bot()
        ctx = {"chat_id": "42"}
        # Sol diff r4: an ENUM sentinel is a singleton, so a delegate whose
        # result is discarded and replaced by the same member still passes. An
        # opaque object can only arrive here by being propagated.
        sentinel = object()
        called: list[str] = []

        async def _fake_send(text, context):
            called.append(text)
            return sentinel

        ch.send = _fake_send
        assert await ch.send_response("just text", ctx) is sentinel
        assert called == ["just text"]

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

    async def test_timeout_after_acceptance_is_unknown_not_a_negative(self):
        """Sol + Terra diff r1, independently: a lost ACKNOWLEDGEMENT is not
        evidence of a lost MESSAGE. Telegram may have applied the edit, so
        claiming NOT_DELIVERED would re-offer a notice already read — which is
        what the pre-contract code avoided by swallowing the error.
        """
        from telegram.error import TimedOut

        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        bot.edit_message_text.side_effect = TimedOut("ack lost")
        assert await ch.finalize_stream(
            "hi", ctx, on_token) is DeliveryOutcome.UNKNOWN

    async def test_a_server_refusal_is_an_established_negative(self):
        """Sol diff r2: the first fix over-corrected. Forbidden/InvalidToken/
        RetryAfter/ChatMigrated are responses Telegram actually sent — the call
        was evaluated and declined, so nothing was shown. Treating them as
        ambiguous would consume an unseen notice, re-breaking #556 for the
        operator who blocks the bot and later unblocks it."""
        from telegram.error import (
            ChatMigrated, Conflict, Forbidden, InvalidToken, RetryAfter,
        )

        for exc in (Forbidden("bot was blocked by the user"),
                    InvalidToken(),
                    RetryAfter(30),
                    ChatMigrated(new_chat_id=-100999),
                    Conflict("terminated by other getUpdates request")):
            ch, bot = _mk_channel_with_fake_bot()
            ch._delivery_mode = "stream"
            ctx = {"chat_id": "42"}
            on_token = ch.create_on_token(ctx)
            await on_token("partial")
            ctx.pop("_delivery_head_sent", None)
            bot.edit_message_text.side_effect = exc
            assert await ch.finalize_stream("hi", ctx, on_token) is \
                DeliveryOutcome.NOT_DELIVERED, f"{type(exc).__name__} is a refusal"

    async def test_overflow_head_timeout_is_unknown(self):
        """Terra + Sol diff r2: the overflow head-edit catch had no ambiguity
        test, so reverting only that site left every test green."""
        from telegram.error import TimedOut

        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        bot.edit_message_text.side_effect = TimedOut("ack lost")
        assert await ch.finalize_stream(
            "x" * 9000, ctx, on_token) is DeliveryOutcome.UNKNOWN
        assert bot.edit_message_text.await_count == 1   # the head WAS attempted

    async def test_overflow_head_refusal_is_not_delivered(self):
        from telegram.error import Forbidden

        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        bot.edit_message_text.side_effect = Forbidden("blocked")
        assert await ch.finalize_stream(
            "x" * 9000, ctx, on_token) is DeliveryOutcome.NOT_DELIVERED

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

    async def test_timeout_on_page1_edit_is_unknown(self):
        """Same ambiguity on the rich finalizer's page-1 edit."""
        from telegram.error import TimedOut

        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        bot.edit_message_text.side_effect = TimedOut("ack lost")
        assert await ch.finalize_response_stream(
            "**hi**", ctx, on_token) is DeliveryOutcome.UNKNOWN
        assert "_delivery_head_sent" not in ctx

    async def test_delegates_verbatim_when_no_stream_started(self):
        """No message_id -> send_response; its outcome must pass through.

        Sol diff r2: the first version of this test set `_app = None`, so it
        returned at the availability guard and never reached the delegation it
        is named after — deleting that delegation would not have failed it. The
        app stays present here and `send_response` is stubbed with a
        distinctive outcome, so only propagation can satisfy it.
        """
        ch, bot = _mk_channel_with_fake_bot()
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)     # no tokens -> no message_id

        sentinel = object()          # opaque: only propagation can pass (Sol r4)
        called: list[str] = []

        async def _fake_send_response(text, context):
            called.append(text)
            return sentinel

        ch.send_response = _fake_send_response
        assert await ch.finalize_response_stream(
            "**hi**", ctx, on_token) is sentinel
        assert called == ["**hi**"]

    async def test_plain_text_delegates_to_finalize_stream_verbatim(self):
        """Same correction as the send_response delegation test: a distinctive
        stubbed outcome, so bypassing the delegate cannot pass."""
        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)

        sentinel = object()          # opaque: only propagation can pass (Sol r4)
        called: list[str] = []

        async def _fake_finalize_stream(text, context, tok):
            called.append(text)
            return sentinel

        ch.finalize_stream = _fake_finalize_stream
        assert await ch.finalize_response_stream(
            "plain text", ctx, on_token) is sentinel
        assert called == ["plain text"]

