"""Tests for the Telegram side of topic retention & cleanup (v0.65.0).

Covers the 2026-07-10 topic-retention-cleanup design's telegram.py surface:

- [AR-3]/[AR-9] ``TelegramChannel.delete_topic``: calls
  ``delete_forum_topic`` against the configured engagement supergroup,
  raises RuntimeError when the supergroup is unconfigured, refuses the
  General/invalid thread ids (None, 0, 1) with a ValueError, and —
  deliberately unlike ``close_topic`` — PROPAGATES Telegram exceptions
  so the ledger sweep can classify the real error.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import telegram.error as tg_err

import topic_ledger


# ---------------------------------------------------------------------------
# telegram.error stubs — conftest installs a minimal telegram.error stub
# (TelegramError / NetworkError / TimedOut only). The propagation tests also
# need BadRequest; attach the missing classes here mirroring the REAL
# python-telegram-bot hierarchy (BadRequest and TimedOut subclass
# NetworkError in PTB 22.7). hasattr-guarded: no-ops against the real
# library or if another test file (test_topic_ledger.py) attached them first.
# ---------------------------------------------------------------------------


def _ensure_stub_error_classes() -> None:
    if not hasattr(tg_err, "BadRequest"):
        class BadRequest(tg_err.NetworkError):
            pass

        tg_err.BadRequest = BadRequest
    if not hasattr(tg_err, "Forbidden"):
        class Forbidden(tg_err.TelegramError):
            pass

        tg_err.Forbidden = Forbidden
    if not hasattr(tg_err, "RetryAfter"):
        class RetryAfter(tg_err.TelegramError):
            def __init__(self, retry_after):
                super().__init__(
                    f"Flood control exceeded. Retry in {retry_after} seconds"
                )
                self.retry_after = retry_after

        tg_err.RetryAfter = RetryAfter


_ensure_stub_error_classes()


SUPERGROUP = -1001
DM_CHAT = 100


@pytest.fixture(autouse=True)
def _fresh_module_state(monkeypatch, tmp_path):
    """Isolate every test: a fresh module lock (asyncio primitives bind the
    first loop they're used on; pytest-asyncio gives each test its own loop)
    and a tmp default LEDGER_PATH so nothing can ever touch /data."""
    monkeypatch.setattr(topic_ledger, "_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        topic_ledger, "LEDGER_PATH", str(tmp_path / "topic-ledger.json")
    )


def _mk_channel(fake_telegram_bot, supergroup_id=SUPERGROUP):
    from channels.telegram import TelegramChannel

    return TelegramChannel(
        bot=fake_telegram_bot, chat_id=DM_CHAT,
        engagement_supergroup_id=supergroup_id,
    )


# ---------------------------------------------------------------------------
# delete_topic [AR-3]/[AR-9]
# ---------------------------------------------------------------------------


class TestDeleteTopic:
    async def test_calls_delete_forum_topic_with_supergroup_chat_id(
        self, fake_telegram_bot,
    ):
        ch = _mk_channel(fake_telegram_bot)
        fake_telegram_bot.delete_forum_topic = AsyncMock(return_value=True)

        await ch.delete_topic(613)

        fake_telegram_bot.delete_forum_topic.assert_awaited_once_with(
            chat_id=SUPERGROUP, message_thread_id=613,
        )

    async def test_raises_runtime_error_when_supergroup_unconfigured(
        self, fake_telegram_bot,
    ):
        ch = _mk_channel(fake_telegram_bot, supergroup_id=None)
        fake_telegram_bot.delete_forum_topic = AsyncMock()

        with pytest.raises(RuntimeError, match="engagement supergroup"):
            await ch.delete_topic(613)
        fake_telegram_bot.delete_forum_topic.assert_not_awaited()

    @pytest.mark.parametrize("thread_id", [None, 0, 1])
    async def test_refuses_general_and_invalid_thread_ids(
        self, fake_telegram_bot, thread_id,
    ):
        """[AR-9] 1 is the supergroup's General topic; None/0 mean 'no
        topic'. A deletion attempt must be refused before any API call."""
        ch = _mk_channel(fake_telegram_bot)
        fake_telegram_bot.delete_forum_topic = AsyncMock()

        with pytest.raises(ValueError):
            await ch.delete_topic(thread_id)
        fake_telegram_bot.delete_forum_topic.assert_not_awaited()

    async def test_propagates_telegram_errors_unlike_close_topic(
        self, fake_telegram_bot,
    ):
        """[AR-3] The outcome-classification contract depends on callers
        seeing the REAL exception — a close_topic-style swallow-everything
        except would make every failure look like success and silently
        drop ledger entries even under permission denial."""
        ch = _mk_channel(fake_telegram_bot)
        boom = tg_err.BadRequest("not enough rights")
        fake_telegram_bot.delete_forum_topic = AsyncMock(side_effect=boom)

        with pytest.raises(tg_err.BadRequest) as excinfo:
            await ch.delete_topic(613)
        assert excinfo.value is boom
