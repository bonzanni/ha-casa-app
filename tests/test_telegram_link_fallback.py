"""A reply carrying a link reaches the operator with its destination, whatever
Telegram makes of the formatting.

The agent's Markdown goes out as written in one rich message, so a link's
destination travels inside it. When Telegram refuses the message, the resend
is the same text unformatted, and the destination is still in it. These pin
the destination's presence in what was ACTUALLY sent to the Bot API —
counts and payloads, never statuses.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from telegram.error import BadRequest

from telegram_channel_double import _mk_channel_with_fake_bot
from output_boundary_testing import admitted

pytestmark = pytest.mark.asyncio

LABEL = "DESTINATION"
URL = "https://example.test/target"
TAIL = "x" * 4096
AUTHORED = f"[{LABEL}]({URL})\n\n{TAIL}"


def _record_attempts(bot, *, refuse_markdown: bool):
    """Record every rich send as ``(endpoint, payload as JSON)``; refuse
    every Markdown payload when *refuse_markdown*."""
    attempts: list[tuple[str, str]] = []

    async def _call(endpoint, *, api_kwargs, return_type=None):
        attempts.append((endpoint, json.dumps(api_kwargs["rich_message"],
                                              ensure_ascii=False)))
        if refuse_markdown and "markdown" in api_kwargs["rich_message"]:
            raise BadRequest("Rich_message_parse_failed")
        return MagicMock(message_id=12345)

    bot.do_api_request.side_effect = _call
    return attempts


async def test_a_reply_with_a_link_is_one_message_carrying_its_destination():
    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_attempts(bot, refuse_markdown=False)

    await ch.send_response(admitted(AUTHORED), {"chat_id": "42"})

    assert (
        [endpoint for endpoint, _ in attempts],
        sum(payload.count(URL) for _, payload in attempts),
        sum(payload.count(LABEL) for _, payload in attempts),
    ) == (["sendRichMessage"], 1, 1)
    bot.send_message.assert_not_awaited()


async def test_a_refused_reply_resends_its_link_destination():
    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_attempts(bot, refuse_markdown=True)

    await ch.send_response(admitted(AUTHORED), {"chat_id": "42"})

    assert (
        len(attempts),
        URL in attempts[0][1],
        '"blocks"' in attempts[1][1],
        attempts[1][1].count(URL),
        attempts[1][1].count(LABEL),
    ) == (2, True, True, 1, 1)
    bot.send_message.assert_not_awaited()


CONVERSION_FAILURE_AUTHORED = f"\ud800[{LABEL}]({URL})\n\n{TAIL}"


async def test_send_response_delivers_the_target_when_conversion_fails():
    """A lone surrogate before the link cannot be encoded for Telegram. It is
    replaced, and the link goes out whole in the one message sent."""
    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_attempts(bot, refuse_markdown=False)

    await ch.send_response(admitted(CONVERSION_FAILURE_AUTHORED), {"chat_id": "42"})

    assert (
        len(attempts),
        sum(payload.count(URL) for _, payload in attempts),
        sum(payload.count(LABEL) for _, payload in attempts),
        sum(payload.count("\ud800") for _, payload in attempts),
        sum(payload.count("�") for _, payload in attempts),
    ) == (1, 1, 1, 0, 1)
