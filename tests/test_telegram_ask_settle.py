"""``edit_topic_message`` is a bare text edit: it never touches the message's
reply markup (PTB drops a ``None`` param, so sending none leaves any keyboard
as it was).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio

SUPERGROUP = -1002


def _mk_channel():
    from channels.telegram import TelegramChannel

    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    ch = TelegramChannel(
        bot=bot, chat_id=100, engagement_supergroup_id=SUPERGROUP,
    )
    return ch, bot


async def test_edit_topic_message_without_clear_keeps_bare_edit():
    ch, bot = _mk_channel()
    await ch.edit_topic_message(999, 7002, "plain text")
    _, kwargs = bot.edit_message_text.call_args
    # A plain edit must NOT touch the markup.
    assert "reply_markup" not in kwargs
