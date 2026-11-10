"""Task 6 — finalize_response_stream rich-text finalize.

Covers every branch Sol flagged: entity edit, BadRequest→original-same-message,
block-mode→send_response, stream-with-no-message_id→send_response, and
finalize_stream (error path) staying plain.
"""
from __future__ import annotations

import pytest
from telegram import MessageEntity
from telegram.error import BadRequest

from telegram_channel_double import _mk_channel_with_fake_bot
from output_boundary_testing import admitted

pytestmark = pytest.mark.asyncio


async def test_finalize_response_stream_applies_entities():
    ch, bot = _mk_channel_with_fake_bot()
    ch._delivery_mode = "stream"
    on_token = ch.create_on_token({"chat_id": "42"})
    await on_token("partial")  # establishes message_id 12345
    await ch.finalize_response_stream(admitted("**hi**"), {"chat_id": "42"}, on_token)
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "hi"
    assert kw["message_id"] == 12345
    assert kw["entities"][0].type == MessageEntity.BOLD


async def test_finalize_response_stream_badrequest_edits_original_same_msg():
    ch, bot = _mk_channel_with_fake_bot()
    ch._delivery_mode = "stream"
    on_token = ch.create_on_token({"chat_id": "42"})
    await on_token("partial")
    bot.edit_message_text.side_effect = [BadRequest("bad"), None]
    await ch.finalize_response_stream(admitted("**hi**"), {"chat_id": "42"}, on_token)
    calls = bot.edit_message_text.await_args_list
    assert [c.kwargs["message_id"] for c in calls] == [12345, 12345]
    assert calls[0].kwargs["text"] == "hi" and "entities" in calls[0].kwargs
    assert calls[1].kwargs["text"] == "**hi**" and "entities" not in calls[1].kwargs


async def test_finalize_response_stream_block_mode_uses_send_response():
    ch, bot = _mk_channel_with_fake_bot()
    ch._delivery_mode = "block"
    on_token = ch.create_on_token({"chat_id": "42"})  # no-op in block mode
    await ch.finalize_response_stream(admitted("**hi**"), {"chat_id": "42"}, on_token)
    assert bot.edit_message_text.await_count == 0
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "hi"
    assert kw["entities"][0].type == MessageEntity.BOLD


async def test_finalize_response_stream_no_message_id_uses_send_response():
    ch, bot = _mk_channel_with_fake_bot()
    ch._delivery_mode = "stream"
    on_token = ch.create_on_token({"chat_id": "42"})  # never fired → no message_id
    await ch.finalize_response_stream(admitted("**hi**"), {"chat_id": "42"}, on_token)
    assert bot.edit_message_text.await_count == 0
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "hi" and kw["entities"][0].type == MessageEntity.BOLD


async def test_finalize_stream_error_path_stays_plain():
    # agent.py routes error text through finalize_stream (NOT _response); it must
    # never format even with rich enabled.
    ch, bot = _mk_channel_with_fake_bot()
    ch._delivery_mode = "stream"
    on_token = ch.create_on_token({"chat_id": "42"})
    await on_token("partial")
    await ch.finalize_stream(admitted("**err**"), {"chat_id": "42"}, on_token)
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "**err**"
    assert "entities" not in kw


async def test_stream_first_send_over_utf16_limit_is_skipped():
    """#305 (Sol r1): the FIRST stream send needs the same UTF-16 guard as
    the edits — an oversized first payload would be rejected by the Bot API
    on every callback; it is skipped and finalize splits instead."""
    ch, bot = _mk_channel_with_fake_bot()
    ch._delivery_mode = "stream"
    on_token = ch.create_on_token({"chat_id": "42"})
    await on_token("\U0001F389" * 3000)  # 6000 UTF-16 units
    assert bot.send_message.await_count == 0
    # finalize falls back to the splitting send path (no message_id).
    await ch.finalize_response_stream(
        admitted("\U0001F389" * 3000), {"chat_id": "42"}, on_token)
    assert bot.send_message.await_count >= 2  # split into UTF-16-sized chunks
