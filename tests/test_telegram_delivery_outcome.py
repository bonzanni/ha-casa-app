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

    async def test_a_silent_run_card_keeps_its_stop_key(self):
        import stop_keys
        ch, bot = _mk_channel_with_fake_bot()
        ch._known_bot_id = 111                  # the keys belong to this bot's chat
        starting = stop_keys.Item("run", "conf-x", "Runpod", 1.0)
        assert await ch.deliver_run_card(
            42, "Browsing: Runpod", starting=starting, silent=True) == 12345
        assert bot.send_message.await_count == 1
        sent = bot.send_message.await_args.kwargs
        assert sent["disable_notification"] is True
        assert (sent["chat_id"], sent["text"]) == (42, "Browsing: Runpod")
        (key,), = sent["reply_markup"].keyboard
        assert (key.text, key.style) == ("⏹ Stop «Runpod»", "danger")

    async def test_a_run_card_that_is_not_silent_names_no_notification_setting(self):
        ch, bot = _mk_channel_with_fake_bot()
        ch._known_bot_id = 111
        assert await ch.deliver_run_card(42, "Browsing: Runpod") == 12345
        assert await ch.deliver_run_card(42, "Browsing: Runpod", silent=False) == 12345
        assert [set(call.kwargs) for call in bot.send_message.await_args_list] == [
            {"chat_id", "text", "reply_markup"}] * 2
