"""Task 5 — block-mode send_response rich-text rendering + plain fallback."""
from __future__ import annotations

import pytest
from telegram import MessageEntity
from telegram.error import BadRequest, TimedOut

from test_telegram_topic_stream import _mk_channel_with_fake_bot
from output_boundary_testing import admitted

pytestmark = pytest.mark.asyncio


async def test_send_response_bold_sends_entities():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_response(admitted("**hi**"), {"chat_id": "42"})
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "hi"
    assert kw["entities"][0].type == MessageEntity.BOLD


async def test_send_response_plain_no_entities():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_response(admitted("just text"), {"chat_id": "42"})
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "just text"
    assert "entities" not in kw


async def test_send_response_badrequest_falls_back_to_original():
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message.side_effect = [BadRequest("bad entity"), None]
    await ch.send_response(admitted("**hi**"), {"chat_id": "42"})
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[-1].kwargs["text"] == "**hi**"  # ORIGINAL
    assert "entities" not in bot.send_message.await_args_list[-1].kwargs


async def test_send_response_timedout_makes_exactly_one_attempt():
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message.side_effect = TimedOut("network")
    with pytest.raises(TimedOut):
        await ch.send_response(admitted("**hi**"), {"chat_id": "42"})
    assert bot.send_message.await_count == 1  # no duplicate on non-BadRequest


