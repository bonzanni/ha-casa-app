"""Task 8 — non-response paths must NEVER format (byte-identical to pre-v0.70.0)."""
from __future__ import annotations

import pytest

from telegram_channel_double import _mk_channel_with_fake_bot
from output_boundary_testing import admitted

pytestmark = pytest.mark.asyncio


async def test_plain_send_never_formats():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send(admitted("**stars stay** `literal`"), {"chat_id": "42"})
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "**stars stay** `literal`"
    assert "entities" not in kw
