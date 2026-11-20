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
from channels.tg_rich import plain_blocks, split
from telegram_channel_double import _mk_channel_with_fake_bot, rich_sent
from output_boundary_testing import admitted

pytestmark = pytest.mark.asyncio


class TestSend:
    async def test_send_returns_delivered_and_stamps_context(self):
        ch, bot = _mk_channel_with_fake_bot()
        ctx = {"chat_id": "42"}
        assert await ch.send(admitted("hi"), ctx) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True
        assert bot.do_api_request.await_count == 1

    async def test_send_without_app_reports_not_delivered(self):
        """The reconnect window of #556 — returns normally, sends nothing."""
        ch, bot = _mk_channel_with_fake_bot()
        ch._app = None
        ctx = {"chat_id": "42"}
        assert await ch.send(admitted("hi"), ctx) is DeliveryOutcome.NOT_DELIVERED
        assert "_delivery_head_sent" not in ctx
        assert bot.send_message.await_count == bot.do_api_request.await_count == 0


TWO_RICH_PARTS = "\n\n".join(f"row {i} **bold** " + "x" * 300 for i in range(150))
TWO_PLAIN_PARTS = "\n\n".join(f"row {i} plain words " + "y" * 300 for i in range(150))


class TestSendResponse:
    async def test_rendered_single_page_is_delivered(self):
        ch, bot = _mk_channel_with_fake_bot()
        ctx = {"chat_id": "42"}
        assert await ch.send_response(admitted("**hi**"), ctx) is DeliveryOutcome.DELIVERED
        assert ctx["_delivery_head_sent"] is True
        assert bot.do_api_request.await_count == 1

    async def test_without_app_reports_not_delivered(self):
        ch, bot = _mk_channel_with_fake_bot()
        ch._app = None
        ctx = {"chat_id": "42"}
        assert await ch.send_response(admitted("**hi**"), ctx) is DeliveryOutcome.NOT_DELIVERED
        assert "_delivery_head_sent" not in ctx
        assert bot.send_message.await_count == bot.do_api_request.await_count == 0

    async def test_head_lands_then_tail_raises_keeps_the_stamp(self):
        """Message 1 DISPLAYED the notice; the raise must not read as 'showed
        nothing', or the operator is re-told something they already read."""
        from telegram.error import TimedOut

        ch, bot = _mk_channel_with_fake_bot()
        assert len(split(TWO_PLAIN_PARTS)) == 2
        bot.do_api_request.side_effect = [
            MagicMock(message_id=1), TimedOut("network")]
        ctx = {"chat_id": "42"}
        with pytest.raises(TimedOut):
            await ch.send_response(admitted(TWO_PLAIN_PARTS), ctx)
        assert ctx["_delivery_head_sent"] is True
        assert bot.do_api_request.await_count == 2


def _silenced(bot) -> list:
    return [kw.get("disable_notification") for kw in rich_sent(bot)]


class TestDeliveriesThatDoNotNotify:
    """A plugin's message and the run status card can be sent without a
    notification. Every physical message of the delivery carries the flag, and
    a delivery that is not silent sends exactly what it sent before."""

    @pytest.mark.parametrize("text", [TWO_RICH_PARTS, TWO_PLAIN_PARTS],
                             ids=["rich-parts", "plain-parts"])
    async def test_every_page_of_a_silent_message_is_sent_without_a_notification(self, text):
        assert len(split(text)) == 2
        loud, loud_bot = _mk_channel_with_fake_bot()
        assert await loud.deliver_operator_message(42, text) is DeliveryOutcome.DELIVERED
        ch, bot = _mk_channel_with_fake_bot()
        assert await ch.deliver_operator_message(
            42, text, silent=True) is DeliveryOutcome.DELIVERED
        assert len(rich_sent(bot)) == len(rich_sent(loud_bot)) == 2
        assert _silenced(bot) == [True, True]
        # the flag is the only difference
        for quiet, heard in zip(rich_sent(bot), rich_sent(loud_bot)):
            assert {k: v for k, v in quiet.items() if k != "disable_notification"} == heard

    async def test_the_unformatted_resend_of_a_refused_message_is_silent_too(self):
        """The platform refuses the message: it is resent unformatted, silent
        like the first try."""
        from telegram.error import BadRequest

        text = "a" * 50 + "[t](https://e.example/q)"
        ch, bot = _mk_channel_with_fake_bot()
        bot.do_api_request.side_effect = [
            BadRequest("Rich_message_parse_failed"), MagicMock(message_id=1)]
        assert await ch.deliver_operator_message(
            42, text, silent=True) is DeliveryOutcome.DELIVERED
        assert [kw["rich_message"] for kw in rich_sent(bot)][1] == {"blocks": plain_blocks(text)}
        assert _silenced(bot) == [True, True]

    @pytest.mark.parametrize("silent", [{}, {"silent": False}],
                             ids=["not-asked", "asked-not-to"])
    async def test_a_message_that_is_not_silent_names_no_notification_setting(self, silent):
        from telegram.error import BadRequest

        ch, bot = _mk_channel_with_fake_bot()
        assert await ch.deliver_operator_message(
            42, TWO_RICH_PARTS, **silent) is DeliveryOutcome.DELIVERED
        assert await ch.deliver_operator_message(
            42, TWO_PLAIN_PARTS, **silent) is DeliveryOutcome.DELIVERED
        bot.do_api_request.side_effect = [
            BadRequest("Rich_message_parse_failed"), MagicMock(message_id=1)]
        assert await ch.deliver_operator_message(
            42, "a" * 50 + "[t](https://e.example/q)", **silent) is DeliveryOutcome.DELIVERED
        assert len(rich_sent(bot)) == 6
        assert not any("disable_notification" in kw for kw in rich_sent(bot))

    async def test_a_silent_run_card_carries_no_keyboard(self):
        ch, bot = _mk_channel_with_fake_bot()
        assert await ch.deliver_run_card(
            42, "Browsing: Runpod", silent=True) == 12345
        assert bot.send_message.await_count == 1
        sent = bot.send_message.await_args.kwargs
        assert sent == {"chat_id": 42, "text": "Browsing: Runpod", "entities": None,
                        "disable_notification": True}

    async def test_a_run_card_that_is_not_silent_names_no_notification_setting(self):
        ch, bot = _mk_channel_with_fake_bot()
        assert await ch.deliver_run_card(42, "Browsing: Runpod") == 12345
        assert await ch.deliver_run_card(42, "Browsing: Runpod", silent=False) == 12345
        assert [set(call.kwargs) for call in bot.send_message.await_args_list] == [
            {"chat_id", "text", "entities"}] * 2
