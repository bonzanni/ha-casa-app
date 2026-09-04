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


# ---------------------------------------------------------------------------
# #834 red case — a page whose entities cannot be EXPRESSED, no BadRequest.
#
# Specified by sol (red-case SPECIFY round, cluster L attempt 1). The #831
# cases above key on Telegram REJECTING valid entities; here the conversion
# itself fails, so no entities are ever attached, nothing raises BadRequest,
# and `_plain_fallback_chunks` is never reached. Its call count is asserted at
# zero precisely so a green result cannot be credited to the existing fallback.
# ---------------------------------------------------------------------------

CONVERSION_FAILURE_AUTHORED = f"\ud800[{LABEL}]({URL})\n\n{TAIL}"


def _record_without_rejecting(bot):
    """Record every Bot API text call and NEVER raise — the point of #834."""
    attempts: list[tuple[str, str, list[str]]] = []

    def _method(name):
        async def _call(**kwargs):
            ents = list(kwargs.get("entities") or [])
            attempts.append((
                name,
                kwargs.get("text"),
                [e.url for e in ents if getattr(e, "url", None)],
            ))
            return MagicMock(message_id=12345)
        return _call

    bot.send_message = AsyncMock(side_effect=_method("send_message"))
    bot.edit_message_text = AsyncMock(side_effect=_method("edit_message_text"))
    return attempts


def _observe_fallback(ch):
    """Record URL counts entering and leaving every #831 fallback call.

    A zero call count is NOT assertable — both stream finalizers compute
    `chunks0` unconditionally for a multi-page plan (`telegram.py:4636-4638`,
    `:5117-5119`), whatever the renderer returned. What the acceptor's
    objection actually protects is that the #831 fallback must not be what
    delivers the destination, and `(urls in, urls out, entities)` says exactly
    that: a fallback-created url reads `(0, 1, 0)`, a renderer-created one
    `(1, 1, 0)`.
    """
    calls: list[tuple[int, int, int]] = []
    inner = ch._plain_fallback_chunks

    def _wrapped(display, entities):
        chunks = inner(display, entities)
        calls.append((
            display.count(URL),
            sum(chunk.count(URL) for chunk in chunks),
            len(entities or ()),
        ))
        return chunks

    ch._plain_fallback_chunks = _wrapped
    return calls


def _summarise(attempts, fallback_calls):
    return (
        len(attempts),
        sum(text.count(URL) for _, text, _ in attempts),
        sum(text.count(LABEL) for _, text, _ in attempts),
        sum(bool(urls) for _, _, urls in attempts),
        sum(text.count("\ud800") for _, text, _ in attempts),
        sum(text.count("\ufffd") for _, text, _ in attempts),
        [name for name, _, _ in attempts],
        [text for _, text, _ in attempts],
        fallback_calls,
    )


async def test_all_four_senders_deliver_the_target_when_conversion_fails():
    from channels.tg_richtext import parse_markdown

    first = f"\ufffd{LABEL} ({URL})"

    display, spans = parse_markdown(CONVERSION_FAILURE_AUTHORED)
    assert (display, spans) == (
        f"\ud800{LABEL}\n\n{TAIL}",
        [(1, 1 + len(LABEL), f"link:{URL}")],
    )

    entity = MessageEntity(
        type=MessageEntity.TEXT_LINK,
        offset=1,
        length=len(LABEL),
        url=URL,
    )
    with pytest.raises(UnicodeEncodeError):
        MessageEntity.adjust_message_entities_to_utf_16(
            f"\ud800{LABEL}", [entity]
        )

    summaries: dict[str, tuple] = {}

    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_without_rejecting(bot)
    fallbacks = _observe_fallback(ch)
    await ch.send_response(CONVERSION_FAILURE_AUTHORED, {"chat_id": "42"})
    summaries["send_response"] = _summarise(attempts, fallbacks)

    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_without_rejecting(bot)
    fallbacks = _observe_fallback(ch)
    ch._delivery_mode = "stream"
    on_token = ch.create_on_token({"chat_id": "42"})
    await on_token("partial")  # establishes message_id 12345
    attempts.clear()
    await ch.finalize_response_stream(
        CONVERSION_FAILURE_AUTHORED, {"chat_id": "42"}, on_token
    )
    summaries["finalize_response_stream"] = _summarise(attempts, fallbacks)

    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_without_rejecting(bot)
    fallbacks = _observe_fallback(ch)
    await ch.send_response_to_topic(42, CONVERSION_FAILURE_AUTHORED)
    summaries["send_response_to_topic"] = _summarise(attempts, fallbacks)

    ch, bot = _mk_channel_with_fake_bot()
    attempts = _record_without_rejecting(bot)
    fallbacks = _observe_fallback(ch)
    handle = ch.create_topic_stream(topic_id=42)
    await handle.emit("partial")  # establishes message_id 12345
    attempts.clear()
    await handle.finalize(CONVERSION_FAILURE_AUTHORED)
    summaries["TopicStreamHandle.finalize"] = _summarise(attempts, fallbacks)

    assert summaries == {
        "send_response": (
            2, 1, 1, 0, 0, 1,
            ["send_message", "send_message"],
            [first, TAIL],
            [],
        ),
        "finalize_response_stream": (
            2, 1, 1, 0, 0, 1,
            ["edit_message_text", "send_message"],
            [first, TAIL],
            [(1, 1, 0)],
        ),
        "send_response_to_topic": (
            2, 1, 1, 0, 0, 1,
            ["send_message", "send_message"],
            [first, TAIL],
            [],
        ),
        "TopicStreamHandle.finalize": (
            2, 1, 1, 0, 0, 1,
            ["edit_message_text", "send_message"],
            [first, TAIL],
            [(1, 1, 0)],
        ),
    }
