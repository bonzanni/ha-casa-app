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
from telegram_channel_double import _mk_channel_with_fake_bot
from output_boundary_testing import admitted

pytestmark = pytest.mark.asyncio


class TestSend:
    async def test_send_returns_delivered_and_stamps_context(self):
        ch, bot = _mk_channel_with_fake_bot()
        ctx = {"chat_id": "42"}
        assert await ch.send(admitted("hi"), ctx) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True
        assert bot.send_message.await_count == 1

    async def test_send_without_app_reports_not_delivered(self):
        """The reconnect window of #556 — returns normally, sends nothing."""
        ch, bot = _mk_channel_with_fake_bot()
        ch._app = None
        ctx = {"chat_id": "42"}
        assert await ch.send(admitted("hi"), ctx) is DeliveryOutcome.NOT_DELIVERED
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
        assert await ch.send_response(admitted("just text"), ctx) is sentinel
        assert called == ["just text"]

    async def test_rendered_single_page_is_delivered(self):
        ch, bot = _mk_channel_with_fake_bot()
        ctx = {"chat_id": "42"}
        assert await ch.send_response(admitted("**hi**"), ctx) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True
        assert bot.send_message.await_count == 1

    async def test_without_app_reports_not_delivered(self):
        ch, bot = _mk_channel_with_fake_bot()
        ch._app = None
        ctx = {"chat_id": "42"}
        assert await ch.send_response(admitted("**hi**"), ctx) is DeliveryOutcome.NOT_DELIVERED
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
            await ch.send_response(admitted(long_text), ctx)
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
            admitted("hi"), ctx, on_token) is DeliveryOutcome.DELIVERED
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
            admitted("hi"), ctx, on_token) is DeliveryOutcome.NOT_DELIVERED
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
            admitted("hi"), ctx, on_token) is DeliveryOutcome.UNKNOWN

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
            assert await ch.finalize_stream(admitted("hi"), ctx, on_token) is \
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
            admitted("x" * 9000), ctx, on_token) is DeliveryOutcome.UNKNOWN
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
            admitted("x" * 9000), ctx, on_token) is DeliveryOutcome.NOT_DELIVERED

    async def test_without_app_is_not_delivered(self):
        ch, bot = _mk_channel_with_fake_bot()
        ch._delivery_mode = "stream"
        ctx = {"chat_id": "42"}
        on_token = ch.create_on_token(ctx)
        await on_token("partial")
        ctx.pop("_delivery_head_sent", None)
        ch._app = None
        assert await ch.finalize_stream(
            admitted("hi"), ctx, on_token) is DeliveryOutcome.NOT_DELIVERED

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
            admitted(" " * 5000), ctx, on_token) is DeliveryOutcome.NOT_DELIVERED
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
            admitted("**hi**"), ctx, on_token) is DeliveryOutcome.DELIVERED
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
            admitted("**hi**"), ctx, on_token) is DeliveryOutcome.DELIVERED
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
            admitted("**hi**"), ctx, on_token) is DeliveryOutcome.NOT_DELIVERED
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
            admitted("**hi**"), ctx, on_token) is DeliveryOutcome.UNKNOWN
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
            admitted("**hi**"), ctx, on_token) is sentinel
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
            admitted("plain text"), ctx, on_token) is sentinel
        assert called == ["plain text"]



THREE_RICH_PAGES = "\n".join(f"row {i} **bold**" for i in range(280))
THREE_PLAIN_PAGES = "\n".join(f"row {i} plain words" for i in range(500))


def _silenced(bot) -> list:
    return [call.kwargs.get("disable_notification")
            for call in bot.send_message.await_args_list]


class TestDeliveriesThatDoNotNotify:
    """A plugin's message and the run status card can be sent without a
    notification. Every physical message of the delivery carries the flag, and
    a delivery that is not silent sends exactly what it sent before."""

    @pytest.mark.parametrize("text", [THREE_RICH_PAGES, THREE_PLAIN_PAGES],
                             ids=["rich-pages", "plain-pages"])
    async def test_every_page_of_a_silent_message_is_sent_without_a_notification(self, text):
        from channels.tg_richtext import render_paged

        assert len(render_paged(text)) == 3
        loud, loud_bot = _mk_channel_with_fake_bot()
        assert await loud.deliver_operator_message(42, text) is DeliveryOutcome.DELIVERED
        ch, bot = _mk_channel_with_fake_bot()
        assert await ch.deliver_operator_message(
            42, text, silent=True) is DeliveryOutcome.DELIVERED
        assert bot.send_message.await_count == loud_bot.send_message.await_count == 3
        assert _silenced(bot) == [True, True, True]
        # the flag is the only difference
        for quiet, heard in zip(bot.send_message.await_args_list,
                                loud_bot.send_message.await_args_list):
            assert {k: v for k, v in quiet.kwargs.items()
                    if k != "disable_notification"} == heard.kwargs

    async def test_the_plain_resend_of_a_refused_page_and_its_tail_are_silent_too(self):
        """The platform refuses a page's entities: the page is resent plain,
        and what did not fit follows as its own message."""
        from telegram.error import BadRequest
        from channels.tg_richtext import render_paged

        url = "https://e.example/" + "q" * 60
        text = "a" * 4050 + f"[t]({url})"
        (display, _entities), = render_paged(text)
        ch, bot = _mk_channel_with_fake_bot()
        bot.send_message.side_effect = [
            BadRequest("can't parse entities"), MagicMock(message_id=1),
            MagicMock(message_id=2)]
        assert await ch.deliver_operator_message(
            42, text, silent=True) is DeliveryOutcome.DELIVERED
        assert [c.kwargs["text"] for c in bot.send_message.await_args_list] == [
            display, display, url]
        assert _silenced(bot) == [True, True, True]

    @pytest.mark.parametrize("silent", [{}, {"silent": False}],
                             ids=["not-asked", "asked-not-to"])
    async def test_a_message_that_is_not_silent_names_no_notification_setting(self, silent):
        from telegram.error import BadRequest

        ch, bot = _mk_channel_with_fake_bot()
        assert await ch.deliver_operator_message(
            42, THREE_RICH_PAGES, **silent) is DeliveryOutcome.DELIVERED
        assert await ch.deliver_operator_message(
            42, THREE_PLAIN_PAGES, **silent) is DeliveryOutcome.DELIVERED
        bot.send_message.side_effect = [
            BadRequest("can't parse entities"), MagicMock(message_id=1),
            MagicMock(message_id=2)]
        url = "https://e.example/" + "q" * 60
        assert await ch.deliver_operator_message(
            42, "a" * 4050 + f"[t]({url})", **silent) is DeliveryOutcome.DELIVERED
        assert bot.send_message.await_count == 9
        assert not any("disable_notification" in call.kwargs
                       for call in bot.send_message.await_args_list)

    async def test_a_silent_run_card_keeps_its_buttons(self):
        ch, bot = _mk_channel_with_fake_bot()
        assert await ch.deliver_run_card(
            42, "Browsing: Runpod", [("Stop", "v1|stop|abc")], silent=True) == 12345
        assert bot.send_message.await_count == 1
        sent = bot.send_message.await_args.kwargs
        assert sent["disable_notification"] is True
        assert (sent["chat_id"], sent["text"]) == (42, "Browsing: Runpod")
        (button,), = sent["reply_markup"].inline_keyboard
        assert (button.text, button.callback_data) == ("Stop", "v1|stop|abc")

    async def test_a_run_card_that_is_not_silent_names_no_notification_setting(self):
        ch, bot = _mk_channel_with_fake_bot()
        assert await ch.deliver_run_card(42, "Browsing: Runpod", [("Stop", "v1|stop|abc")]) == 12345
        assert await ch.deliver_run_card(
            42, "Browsing: Runpod", [("Stop", "v1|stop|abc")], silent=False) == 12345
        assert [set(call.kwargs) for call in bot.send_message.await_args_list] == [
            {"chat_id", "text", "reply_markup"}] * 2
