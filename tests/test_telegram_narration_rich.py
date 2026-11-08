"""R2a — the rich topic SEND primitive.

Text posted into an engagement topic through ``send_to_topic_rich`` renders
markdown (``**bold**``, ``` `code` ```, fenced-pre) as Telegram
``MessageEntity`` spans instead of leaking literal markers, and falls back to
the original text, verbatim, when Telegram refuses the spans.
"""

from __future__ import annotations

import pytest
from telegram import MessageEntity
from telegram.error import BadRequest

from test_telegram_topic_stream import _mk_channel_with_fake_bot

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# send_to_topic_rich — the narration SEND primitive
# --------------------------------------------------------------------------

async def test_send_rich_bold_sends_entities():
    ch, bot = _mk_channel_with_fake_bot()
    mid = await ch.send_to_topic_rich(42, "**hi**")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "hi"
    assert kw["message_thread_id"] == 42
    assert kw["entities"][0].type == MessageEntity.BOLD
    assert mid == 12345


async def test_send_rich_inline_code_sends_entities():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_to_topic_rich(42, "see `config.yaml` now")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "see config.yaml now"
    assert kw["entities"][0].type == MessageEntity.CODE


async def test_send_rich_fenced_pre_sends_entities():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_to_topic_rich(42, "```python\nx = 1\n```")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "x = 1"
    assert kw["entities"][0].type == MessageEntity.PRE


async def test_send_rich_plain_sends_raw_no_entities():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_to_topic_rich(42, "just text")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "just text"
    assert "entities" not in kw


async def test_send_rich_badrequest_falls_back_to_plain_original_once():
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message.side_effect = [BadRequest("bad entity"), bot.send_message.return_value]
    await ch.send_to_topic_rich(42, "**hi**")
    assert bot.send_message.await_count == 2
    last = bot.send_message.await_args_list[-1].kwargs
    assert last["text"] == "**hi**"  # ORIGINAL, verbatim
    assert "entities" not in last
