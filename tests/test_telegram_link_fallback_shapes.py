"""#831 — the shapes of the link-target plain fallback.

The accepted red case (`test_telegram_link_fallback.py`) pins the defect itself
on all four multi-page senders. These pin the parts of the fix a single
rejected page cannot show: the reconstruction helper, the overflow shape, the
first-chunk-only kwargs rule, the delivery latch ordering, and the paths the
change must NOT reach.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import MessageEntity
from telegram.error import BadRequest, TimedOut

from test_telegram_topic_stream import _mk_channel_with_fake_bot

# asyncio mode is AUTO (pytest.ini); the sync helper tests below take no mark.

URL = "https://example.test/target"
OTHER_URL = "https://example.test/second"
TAIL = "x" * 4096


def _link(offset: int, length: int, url: str = URL) -> MessageEntity:
    return MessageEntity(
        type=MessageEntity.TEXT_LINK, offset=offset, length=length, url=url)


# ---------------------------------------------------------------- the helper


def test_helper_reattaches_two_destinations_in_display_order():
    from channels.tg_richtext import plain_with_link_targets, render

    display, entities = render(
        f"see [one]({URL}) and [two]({OTHER_URL}) now")
    assert display == "see one and two now"
    assert plain_with_link_targets(display, entities) == (
        f"see one ({URL}) and two ({OTHER_URL}) now")


def test_helper_converts_utf16_offsets_across_an_astral_character():
    from channels.tg_richtext import plain_with_link_targets, render

    display, entities = render(f"\U0001F600 [go]({URL}) end")
    # The entity offset is in UTF-16 units (the emoji counts 2); a helper that
    # read it as a Python index would insert after "g".
    assert entities[0].offset == 3
    assert plain_with_link_targets(display, entities) == (
        f"\U0001F600 go ({URL}) end")


def test_helper_leaves_non_link_entities_alone():
    from channels.tg_richtext import plain_with_link_targets, render

    display, entities = render("**bold** and `code`")
    assert plain_with_link_targets(display, entities) == display


def test_helper_does_not_repeat_a_url_already_visible_in_its_label():
    from channels.tg_richtext import plain_with_link_targets, render

    display, entities = render(f"[{URL}]({URL})")
    assert display == URL
    assert plain_with_link_targets(display, entities) == URL


def test_helper_degrades_to_the_display_on_unusable_entities():
    from channels.tg_richtext import plain_with_link_targets

    display = "some text"
    assert plain_with_link_targets(display, None) == display
    assert plain_with_link_targets(display, []) == display
    assert plain_with_link_targets(display, [_link(0, 500)]) == display
    assert plain_with_link_targets(display, [object()]) == display


def test_helper_ignores_a_non_link_entity_that_carries_a_url():
    """The TEXT_LINK filter is a guard of its own, not a consequence of the
    url check: an entity of another type carrying a url must add nothing."""
    from channels.tg_richtext import plain_with_link_targets

    display = "word and more"
    bold_with_url = SimpleNamespace(
        type=MessageEntity.BOLD, offset=0, length=4, url=URL)
    assert plain_with_link_targets(display, [bold_with_url]) == display


def test_helper_never_raises_on_an_entity_whose_url_is_not_text():
    """The containment has a firing: `url in display[...]` raises TypeError on
    a non-string url, and a fallback path that raised would lose the turn."""
    from channels.tg_richtext import plain_with_link_targets

    display = "word and more"
    bad = SimpleNamespace(
        type=MessageEntity.TEXT_LINK, offset=0, length=4, url=12345)
    assert plain_with_link_targets(display, [bad]) == display


def test_missing_link_targets_lists_each_destination_once_in_order():
    from channels.tg_richtext import missing_link_targets, render

    display, entities = render(
        f"[a]({URL}) [b]({OTHER_URL}) [c]({URL})")
    assert missing_link_targets(display, entities) == [URL, OTHER_URL]


# ------------------------------------------------------------ the two shapes


def test_fallback_is_one_chunk_when_the_reconstruction_fits():
    from channels.tg_richtext import render

    ch, _ = _mk_channel_with_fake_bot()
    display, entities = render(f"[label]({URL})")
    assert ch._plain_fallback_chunks(display, entities) == [f"label ({URL})"]


def test_fallback_over_the_budget_keeps_the_display_and_appends_targets():
    from text_util import utf16_len

    ch, _ = _mk_channel_with_fake_bot()
    display = "L" * 4090
    chunks = ch._plain_fallback_chunks(display, [_link(0, 4090)])
    # Chunk 0 is the display byte-for-byte — today's payload, unchanged — and
    # the destination follows whole: splitting the inline form would have cut
    # the URL across two messages, because the plain splitter cuts at the last
    # newline before the limit and otherwise hard-cuts.
    assert chunks == [display, URL]
    assert all(utf16_len(c) <= 4096 for c in chunks)
    assert sum(c.count(URL) for c in chunks) == 1


def test_a_destination_longer_than_one_message_is_dropped_not_fragmented():
    """`_split_message` hard-cuts a line it cannot fit, so a url longer than a
    whole message would arrive as unusable pieces. It is omitted instead."""
    from text_util import utf16_len

    ch, _ = _mk_channel_with_fake_bot()
    huge = "https://example.test/" + "z" * 5000
    display = "L" * 4090
    chunks = ch._plain_fallback_chunks(
        display, [_link(0, 4090, url=huge)])
    assert chunks == [display]
    assert all(utf16_len(c) <= 4096 for c in chunks)


def test_a_deliverable_destination_survives_beside_an_undeliverable_one():
    ch, _ = _mk_channel_with_fake_bot()
    huge = "https://example.test/" + "z" * 5000
    display = "AB" + "L" * 4088
    chunks = ch._plain_fallback_chunks(display, [
        _link(0, 1, url=huge),
        _link(1, 1, url=URL),
    ])
    assert chunks == [display, URL]


async def test_send_response_overflowing_fallback_sends_display_then_target():
    ch, bot = _mk_channel_with_fake_bot()
    sent: list[str] = []

    async def _send(**kwargs):
        sent.append(kwargs["text"])
        if kwargs.get("entities"):
            raise BadRequest("bad entity")
        return MagicMock(message_id=12345)

    bot.send_message = AsyncMock(side_effect=_send)
    label = "L" * 4090
    await ch.send_response(f"[{label}]({URL})\n\n{TAIL}", {"chat_id": "42"})

    assert sent == [label, label, URL, TAIL]


# ------------------------------------------------- kwargs, latch, and fences


async def test_topic_fallback_chunks_carry_first_page_kwargs_only():
    ch, bot = _mk_channel_with_fake_bot()
    calls: list[tuple[str, bool]] = []

    async def _send(**kwargs):
        calls.append((kwargs["text"], "reply_parameters" in kwargs))
        if kwargs.get("entities"):
            raise BadRequest("bad entity")
        return MagicMock(message_id=12345)

    bot.send_message = AsyncMock(side_effect=_send)
    label = "L" * 4090
    await ch.send_response_to_topic(
        42, f"[{label}]({URL})\n\n{TAIL}", reply_parameters="anchor")

    assert calls == [
        (label, True),    # the rejected rich page keeps the anchor
        (label, True),    # its first fallback chunk is that same message
        (URL, False),     # the destination is a follow-on message
        (TAIL, False),
    ]


async def test_head_chunk_landing_stamps_delivery_before_the_tail_raises():
    ch, bot = _mk_channel_with_fake_bot()
    sent: list[str] = []

    async def _send(**kwargs):
        sent.append(kwargs["text"])
        if kwargs.get("entities"):
            raise BadRequest("bad entity")
        if kwargs["text"] == URL:
            raise TimedOut("network")
        return MagicMock(message_id=12345)

    bot.send_message = AsyncMock(side_effect=_send)
    label = "L" * 4090
    context: dict = {"chat_id": "42"}
    with pytest.raises(TimedOut):
        await ch.send_response(f"[{label}]({URL})\n\n{TAIL}", context)

    # The head is on the operator's screen; a raising tail must not erase that.
    assert sent == [label, label, URL]
    assert context["_delivery_head_sent"] is True


async def test_accepted_entities_send_exactly_the_rendered_pages():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_response(f"[label]({URL})\n\n{TAIL}", {"chat_id": "42"})

    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts == ["label", TAIL]
    assert sum(t.count(URL) for t in texts) == 0


async def test_single_page_fallback_still_resends_the_authored_text():
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message.side_effect = [BadRequest("bad entity"), None]
    authored = f"[label]({URL})"
    await ch.send_response(authored, {"chat_id": "42"})

    assert [c.kwargs["text"] for c in bot.send_message.await_args_list] == [
        "label", authored]


async def test_post_ask_body_rich_still_makes_one_send_and_no_retry():
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message.side_effect = BadRequest("bad entity")
    with pytest.raises(BadRequest):
        await ch.post_ask_body_rich(42, f"[label]({URL})")
    assert bot.send_message.await_count == 1
