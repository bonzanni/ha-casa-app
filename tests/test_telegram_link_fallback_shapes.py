"""The delivery latch of a reply sent as more than one message.

Replies go out as rich messages carrying the agent's Markdown
(`test_telegram_link_fallback.py`).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from telegram.error import TimedOut

from telegram_channel_double import _mk_channel_with_fake_bot
from output_boundary_testing import admitted

# ------------------------------------------------------------- the latch


async def test_head_message_landing_stamps_delivery_before_the_tail_raises():
    """A reply past one message's budget goes as two; the first landing is
    stamped where it survives the second raising."""
    from channels.tg_rich import split

    ch, bot = _mk_channel_with_fake_bot()
    reply = "a" * 20_000 + "\n\n" + "b" * 20_000
    assert len(split(reply)) == 2
    bot.do_api_request.side_effect = [MagicMock(message_id=1), TimedOut("network")]
    context: dict = {"chat_id": "42"}
    with pytest.raises(TimedOut):
        await ch.send_response(admitted(reply), context)

    # The head is on the operator's screen; a raising tail must not erase that.
    assert bot.do_api_request.await_count == 2
    assert context["_delivery_head_sent"] is True
