"""Task 8 — non-response paths must NEVER format (byte-identical to pre-v0.70.0)."""
from __future__ import annotations

import pytest

from test_telegram_topic_stream import _mk_channel_with_fake_bot
from output_boundary_testing import admitted

pytestmark = pytest.mark.asyncio


async def test_plain_send_to_topic_never_formats():
    # permission/notice path — raw send_to_topic must stay literal markdown
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_to_topic(7, "**not bold** `raw`")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "**not bold** `raw`"
    assert "entities" not in kw


async def test_plain_send_never_formats():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send(admitted("**stars stay** `literal`"), {"chat_id": "42"})
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "**stars stay** `literal`"
    assert "entities" not in kw
