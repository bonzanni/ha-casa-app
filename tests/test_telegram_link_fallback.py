"""#831 red case — a rejected multi-page rich page must still deliver its link
destinations.

All four multi-page rich senders hand `render_paged`'s marker-free DISPLAY to
their plain fallback, so when Telegram rejects a page's entities the retry
carries the label alone and a `TEXT_LINK`'s destination reaches nobody, while
the turn still reports DELIVERED. These pin the destination's presence in what
was ACTUALLY sent to the Bot API — counts and payloads, never statuses.

Specified by sol (red-case SPECIFY round, cluster U attempt 2). The payload
assertions pin the PROPERTY the acceptor asked for — the destination present
exactly once in what the retry actually sent — rather than one implementation's
formatting of it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import MessageEntity
from telegram.error import BadRequest

from test_telegram_topic_stream import _mk_channel_with_fake_bot

pytestmark = pytest.mark.asyncio

LABEL = "DESTINATION"
URL = "https://example.test/target"
TAIL = "x" * 4096
AUTHORED = f"[{LABEL}]({URL})\n\n{TAIL}"


def _record_attempts(bot):
    """Record every Bot API text call; reject any call carrying a TEXT_LINK.

    Returns the list of `(method_name, text, entity_urls)` tuples.
    """
    attempts: list[tuple[str, str, list[str]]] = []

    def _method(name):
        async def _call(**kwargs):
            ents = list(kwargs.get("entities") or [])
            attempts.append((
                name,
                kwargs.get("text"),
                [e.url for e in ents if getattr(e, "url", None)],
            ))
            if any(e.type == MessageEntity.TEXT_LINK for e in ents):
                raise BadRequest("bad entity")
            return MagicMock(message_id=12345)
        return _call

    bot.send_message = AsyncMock(side_effect=_method("send_message"))
    bot.edit_message_text = AsyncMock(side_effect=_method("edit_message_text"))
    return attempts


def _assert_pagination_premise():
    """The input must paginate to exactly the two pages the red cases assume."""
    from channels.tg_richtext import render_paged

    pages = render_paged(AUTHORED)
    assert [(text, [(e.type, e.url) for e in (ents or [])])
            for text, ents in pages] == [
        (LABEL, [(MessageEntity.TEXT_LINK, URL)]),
        (TAIL, []),
    ]


async def test_send_response_multipage_entity_fallback_keeps_link_target():
    _assert_pagination_premise()
    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_attempts(bot)

    await ch.send_response(AUTHORED, {"chat_id": "42"})

    assert (
        len(attempts),
        sum(text.count(URL) for _, text, _ in attempts),
        sum(text.count(LABEL) for _, text, _ in attempts),
        [(name, urls) for name, _, urls in attempts],
        attempts[0][1],
        URL in attempts[1][1],
        attempts[2][1],
    ) == (
        3,
        1,
        2,
        [
            ("send_message", [URL]),
            ("send_message", []),
            ("send_message", []),
        ],
        LABEL,
        True,
        TAIL,
    )


async def test_finalize_response_stream_multipage_entity_fallback_keeps_link_target():
    _assert_pagination_premise()
    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_attempts(bot)
    ch._delivery_mode = "stream"
    on_token = ch.create_on_token({"chat_id": "42"})
    await on_token("partial")  # establishes message_id 12345
    attempts.clear()

    await ch.finalize_response_stream(AUTHORED, {"chat_id": "42"}, on_token)

    assert (
        len(attempts),
        sum(text.count(URL) for _, text, _ in attempts),
        sum(text.count(LABEL) for _, text, _ in attempts),
        [(name, urls) for name, _, urls in attempts],
        attempts[0][1],
        URL in attempts[1][1],
        attempts[2][1],
    ) == (
        3,
        1,
        2,
        [
            ("edit_message_text", [URL]),
            ("edit_message_text", []),
            ("send_message", []),
        ],
        LABEL,
        True,
        TAIL,
    )


async def test_send_response_to_topic_multipage_entity_fallback_keeps_link_target():
    _assert_pagination_premise()
    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_attempts(bot)

    await ch.send_response_to_topic(42, AUTHORED)

    assert (
        len(attempts),
        sum(text.count(URL) for _, text, _ in attempts),
        sum(text.count(LABEL) for _, text, _ in attempts),
        [(name, urls) for name, _, urls in attempts],
        attempts[0][1],
        URL in attempts[1][1],
        attempts[2][1],
    ) == (
        3,
        1,
        2,
        [
            ("send_message", [URL]),
            ("send_message", []),
            ("send_message", []),
        ],
        LABEL,
        True,
        TAIL,
    )


async def test_topic_stream_finalize_multipage_entity_fallback_keeps_link_target():
    _assert_pagination_premise()
    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_attempts(bot)
    handle = ch.create_topic_stream(topic_id=42)
    await handle.emit("partial")  # establishes message_id 12345
    attempts.clear()

    await handle.finalize(AUTHORED)

    assert (
        len(attempts),
        sum(text.count(URL) for _, text, _ in attempts),
        sum(text.count(LABEL) for _, text, _ in attempts),
        [(name, urls) for name, _, urls in attempts],
        attempts[0][1],
        URL in attempts[1][1],
        attempts[2][1],
    ) == (
        3,
        1,
        2,
        [
            ("edit_message_text", [URL]),
            ("edit_message_text", []),
            ("send_message", []),
        ],
        LABEL,
        True,
        TAIL,
    )
