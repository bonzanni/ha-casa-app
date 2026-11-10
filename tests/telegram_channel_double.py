"""A TelegramChannel over a fake bot, shared by the channel's delivery tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


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
