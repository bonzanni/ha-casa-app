# tests/test_engagement_ask.py
"""Unit tests for ``TelegramChannel.edit_topic_message`` — the plain text edit
of a posted engagement-topic message, exercised directly via
``TelegramChannel.__new__``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestEditTopicMessage:
    async def test_edits_text_plain(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = 555
        ch._bot = MagicMock()
        ch._bot.edit_message_text = AsyncMock()

        ok = await ch.edit_topic_message(42, 101, "Proceed?\n\nAnswered: Yes")
        assert ok is True
        ch._bot.edit_message_text.assert_awaited_once_with(
            chat_id=555, message_id=101, text="Proceed?\n\nAnswered: Yes",
        )

    async def test_not_modified_is_tolerated_as_success(self) -> None:
        from channels import telegram as tg_mod
        from telegram.error import BadRequest

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = 555
        bot = MagicMock()
        bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified"),
        )
        ch._bot = bot

        ok = await ch.edit_topic_message(42, 101, "same text")
        assert ok is True

    async def test_other_badrequest_returns_false(self) -> None:
        from channels import telegram as tg_mod
        from telegram.error import BadRequest

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = 555
        bot = MagicMock()
        bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("message to edit not found"),
        )
        ch._bot = bot

        ok = await ch.edit_topic_message(42, 101, "text")
        assert ok is False

    async def test_no_supergroup_configured_returns_false(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = None
        ch._bot = MagicMock()
        ch._bot.edit_message_text = AsyncMock()

        ok = await ch.edit_topic_message(42, 101, "text")
        assert ok is False
        ch._bot.edit_message_text.assert_not_called()
