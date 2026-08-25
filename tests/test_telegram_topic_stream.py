"""Tests for TopicStreamHandle — per-AssistantMessage streaming to engagement topics (Phase 3b / Bug 1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TelegramError

pytestmark = pytest.mark.asyncio


def _mk_channel_with_fake_bot(supergroup_id: int = -1001):
    """Build a TelegramChannel with a fake _app.bot for testing."""
    from channels.telegram import TelegramChannel

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(
        return_value=MagicMock(message_id=12345)
    )
    fake_bot.edit_message_text = AsyncMock()

    fake_app = MagicMock()
    fake_app.bot = fake_bot

    ch = TelegramChannel(
        bot_token="x:y",
        chat_id=100,
        default_agent="assistant",
        engagement_supergroup_id=supergroup_id,
    )
    ch._app = fake_app
    return ch, fake_bot


class TestTopicStreamFirstEmit:
    async def test_first_emit_sends_new_message_and_stores_id(self):
        ch, bot = _mk_channel_with_fake_bot()
        handle = ch.create_topic_stream(topic_id=42)

        await handle.emit("hello world")

        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == -1001
        assert kwargs["message_thread_id"] == 42
        assert kwargs["text"] == "hello world"
        assert handle._message_id == 12345


class TestTopicStreamThrottle:
    async def test_subsequent_emit_within_throttle_skipped(self, monkeypatch):
        ch, bot = _mk_channel_with_fake_bot()
        clock = [1000.0]
        monkeypatch.setattr(
            "channels.telegram.time.monotonic",
            lambda: clock[0],
        )

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("first")
        clock[0] += 0.5  # less than _STREAM_THROTTLE (1.0)
        await handle.emit("first second")

        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_not_awaited()

    async def test_subsequent_emit_after_throttle_window_edits(self, monkeypatch):
        ch, bot = _mk_channel_with_fake_bot()
        clock = [1000.0]
        monkeypatch.setattr(
            "channels.telegram.time.monotonic",
            lambda: clock[0],
        )

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("first")
        clock[0] += 1.5  # > _STREAM_THROTTLE
        await handle.emit("first second")

        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once()
        kwargs = bot.edit_message_text.await_args.kwargs
        assert kwargs["chat_id"] == -1001
        assert kwargs["message_id"] == 12345
        assert kwargs["text"] == "first second"


class TestTopicStreamFinalize:
    async def test_finalize_after_emit_edits_with_full_text(self):
        ch, bot = _mk_channel_with_fake_bot()
        handle = ch.create_topic_stream(topic_id=42)

        await handle.emit("partial")
        await handle.finalize("partial complete final")

        bot.send_message.assert_awaited_once()  # the emit
        bot.edit_message_text.assert_awaited_once()
        assert bot.edit_message_text.await_args.kwargs["text"] == "partial complete final"

    async def test_finalize_without_prior_emit_sends_message(self):
        ch, bot = _mk_channel_with_fake_bot()
        handle = ch.create_topic_stream(topic_id=42)

        await handle.finalize("only this text")

        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_not_awaited()
        assert bot.send_message.await_args.kwargs["text"] == "only this text"

    async def test_finalize_overflow_splits_into_multiple_messages(self):
        ch, bot = _mk_channel_with_fake_bot()
        handle = ch.create_topic_stream(topic_id=42)

        await handle.emit("short")
        big_text = "X" * 5000
        await handle.finalize(big_text)

        bot.edit_message_text.assert_awaited()
        # 1 from initial emit + ≥1 overflow chunk(s)
        assert bot.send_message.await_count >= 2


class TestTopicStreamErrorHandling:
    async def test_emit_swallows_not_modified_error(self, monkeypatch):
        ch, bot = _mk_channel_with_fake_bot()
        bot.edit_message_text.side_effect = TelegramError(
            "Bad Request: message is not modified"
        )

        clock = [1000.0]
        monkeypatch.setattr(
            "channels.telegram.time.monotonic",
            lambda: clock[0],
        )

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("hi")
        clock[0] += 1.5
        # Should not raise
        await handle.emit("hi")

        # #665 (revised deliberately, coverage kept): the emit landed, so the
        # turn's head is on screen and finalize reports DELIVERED even though
        # the final edit hits "not modified" too.
        from channels import DeliveryOutcome
        assert await handle.finalize("hi") is DeliveryOutcome.DELIVERED

    async def test_emit_logs_other_errors(self, caplog):
        import logging
        ch, bot = _mk_channel_with_fake_bot()
        bot.send_message.side_effect = TelegramError("network down")

        handle = ch.create_topic_stream(topic_id=42)
        with caplog.at_level(logging.WARNING, logger="channels.telegram"):
            await handle.emit("hello")

        assert any("Stream" in rec.message for rec in caplog.records), (
            f"expected a 'Stream' warning, got: {[r.message for r in caplog.records]}"
        )
        # #665 (revised deliberately, coverage kept): emit still swallows into
        # a WARN — but the failure is no longer invisible: a bare TelegramError
        # is not a positive refusal, so finalize (whose fresh send also fails
        # here) reports the honest ambiguity, not a claim.
        from channels import DeliveryOutcome
        assert await handle.finalize("hello") is DeliveryOutcome.UNKNOWN


class TestTopicStreamDeliveryOutcome:
    """#665: finalize reports its DeliveryOutcome. Keys on the FIRST unit of
    output (INV-TG-006) — a landed emit latches DELIVERED; NOT_DELIVERED is a
    CLAIM and needs a positive refusal (or zero possible API calls)."""

    async def test_persistent_forbidden_is_not_delivered(self):
        from telegram.error import Forbidden
        from channels import DeliveryOutcome
        ch, bot = _mk_channel_with_fake_bot()
        bot.send_message.side_effect = Forbidden("blocked")

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("partial")
        outcome = await handle.finalize("partial complete")

        assert bot.send_message.await_count == 2  # emit + finalize fresh send
        assert bot.edit_message_text.await_count == 0
        assert outcome is DeliveryOutcome.NOT_DELIVERED

    async def test_failed_emit_then_successful_finalize_send_delivers(self):
        from telegram.error import Forbidden
        from channels import DeliveryOutcome
        ch, bot = _mk_channel_with_fake_bot()
        ok = MagicMock(message_id=7)
        bot.send_message.side_effect = [Forbidden("blocked"), ok]

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("partial")
        outcome = await handle.finalize("partial complete")

        assert bot.send_message.await_count == 2
        assert outcome is DeliveryOutcome.DELIVERED

    async def test_landed_emit_latches_through_final_edit_refusal(self):
        from telegram.error import Forbidden
        from channels import DeliveryOutcome
        ch, bot = _mk_channel_with_fake_bot()
        bot.edit_message_text.side_effect = Forbidden("blocked")

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("partial")
        outcome = await handle.finalize("partial complete")

        assert bot.send_message.await_count == 1
        assert bot.edit_message_text.await_count >= 1
        assert outcome is DeliveryOutcome.DELIVERED

    async def test_not_modified_final_edit_is_a_silent_delivered(self, caplog):
        import logging
        from channels import DeliveryOutcome
        ch, bot = _mk_channel_with_fake_bot()
        bot.edit_message_text.side_effect = TelegramError(
            "Bad Request: message is not modified")

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("same text")
        with caplog.at_level(logging.WARNING, logger="channels.telegram"):
            outcome = await handle.finalize("same text")

        assert outcome is DeliveryOutcome.DELIVERED
        assert not caplog.records  # stays a silent success

    async def test_no_emit_single_page_timeout_is_unknown(self):
        from telegram.error import TimedOut
        from channels import DeliveryOutcome
        ch, bot = _mk_channel_with_fake_bot()
        bot.send_message.side_effect = TimedOut()

        handle = ch.create_topic_stream(topic_id=42)
        outcome = await handle.finalize("only this")

        assert outcome is DeliveryOutcome.UNKNOWN

    async def test_no_emit_multi_page_refusal_is_unknown(self):
        """The declared imprecision, pinned as DELIBERATE: a multi-page raise
        cannot say whether page 1 landed, and NOT_DELIVERED would false-kill a
        partially-visible turn. Page-level truth needs a change to the shared
        send_response_to_topic, which the #665 scope fence forbids."""
        from telegram.error import Forbidden
        from channels import DeliveryOutcome
        ch, bot = _mk_channel_with_fake_bot()
        bot.send_message.side_effect = Forbidden("blocked")

        handle = ch.create_topic_stream(topic_id=42)
        outcome = await handle.finalize("X" * 5000)

        assert outcome is DeliveryOutcome.UNKNOWN

    async def test_absent_bot_is_not_delivered_with_zero_api_calls(self):
        from channels import DeliveryOutcome
        ch, bot = _mk_channel_with_fake_bot()
        ch._app = None

        handle = ch.create_topic_stream(topic_id=42)
        outcome = await handle.finalize("text nobody could send")

        assert bot.send_message.await_count == 0
        assert bot.edit_message_text.await_count == 0
        assert outcome is DeliveryOutcome.NOT_DELIVERED

    async def test_absent_bot_after_landed_emit_stays_delivered(self):
        """The latch dominates the availability guard: NOT_DELIVERED claims
        nothing reached the operator, and after a landed emit that is false
        however unreachable the app is now."""
        from channels import DeliveryOutcome
        ch, bot = _mk_channel_with_fake_bot()

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("landed")
        ch._app = None
        outcome = await handle.finalize("landed and more")

        assert outcome is DeliveryOutcome.DELIVERED

    async def test_overflow_page2_failure_still_attempts_page3(self):
        from telegram.error import Forbidden
        from channels import DeliveryOutcome
        from channels.telegram import render_paged
        ch, bot = _mk_channel_with_fake_bot()

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("short")

        big_text = "X" * 9000
        pages = render_paged(big_text)
        assert len(pages) >= 3, "arrangement must actually overflow twice"
        # emit's send succeeded already; page 2 (first overflow send) fails,
        # page 3 must still be attempted.
        ok = MagicMock(message_id=8)
        bot.send_message.side_effect = [Forbidden("blocked")] + [ok] * 10

        outcome = await handle.finalize(big_text)

        # 1 emit + one send per overflow page, page-2 failure non-aborting.
        assert bot.send_message.await_count == 1 + (len(pages) - 1)
        assert outcome is DeliveryOutcome.DELIVERED
