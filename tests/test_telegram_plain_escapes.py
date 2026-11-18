"""#1330: a message with no formatting span shows its text as the rich path
would, with the markdown escapes consumed.

``render()`` returns ``(display, None)`` for a span-less text, and every
single-message sender used to send the AUTHORED text in that branch, so a
plugin card escaped for markdown (``CUWVSRB8\\-0007``) showed its backslashes
while the same card with one bold word did not. ``plain_text`` is the one
place that decides what such a send shows; these tests pin every sender that
takes the branch, and pin what stays authored: a text whose spans could not
be sent (over the budgets). Text cards, DM questions and their edits go as
rich messages instead, whose Markdown Telegram parses itself: their escapes
travel as written.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from channels.tg_rich import plain_blocks
from channels.tg_richtext import plain_text, render
from output_boundary_testing import admitted
from telegram_channel_double import _mk_channel_with_fake_bot, rich_edits, rich_markdown, rich_sent

pytestmark = pytest.mark.asyncio

ESCAPED = "📊 Q3\nchose CUWVSRB8\\-0007 \\(1 Oct\\)"
SHOWN = "📊 Q3\nchose CUWVSRB8-0007 (1 Oct)"
# As sent: the line break kept by a backslash, since Telegram joins a soft one.
SENT = "📊 Q3\\\nchose CUWVSRB8\\-0007 \\(1 Oct\\)"


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

async def test_plain_text_consumes_the_escapes_of_a_span_less_text():
    assert render(ESCAPED) == (SHOWN, None)
    assert plain_text(ESCAPED) == SHOWN


async def test_plain_text_returns_the_same_object_when_nothing_changes():
    text = "C:\\Users\\x has no escape"   # a backslash before a letter is literal
    assert plain_text(text) is text
    adm = admitted("no escapes here")
    assert plain_text(adm) is adm


async def test_plain_text_keeps_the_authored_text_when_spans_could_not_be_sent():
    # 101 bold spans: over the entity budget, so render() sends nothing rich.
    # The authored text is what carries the formatting (and any address).
    text = " ".join("**b**" for _ in range(101)) + " x\\-y"
    assert render(text)[1] is None
    assert plain_text(text) is text


# ---------------------------------------------------------------------------
# Every single-message sender's no-entities branch
# ---------------------------------------------------------------------------

async def test_a_plain_photo_card_caption_shows_no_backslash():
    """A photo card's caption cannot be rich: sent without entities, it shows
    the display, never the authored backslashes."""
    ch, bot = _mk_channel_with_fake_bot()
    ch._record_post = MagicMock()
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=9))
    await ch.deliver_operator_proposal(100, ESCAPED, ["Yes", "No"], "rid1",
                                       photo=(b"\x89PNG", "p.png"))
    kw = bot.send_photo.await_args.kwargs
    assert kw["caption"] == SHOWN
    assert kw["caption_entities"] is None
    assert kw["reply_markup"] is not None


async def test_a_text_card_leaves_its_escapes_to_the_rich_messages_markdown():
    ch, bot = _mk_channel_with_fake_bot()
    ch._record_post = MagicMock()
    await ch.deliver_operator_proposal(100, ESCAPED, ["Yes", "No"], "rid1")
    assert rich_markdown(bot) == [SENT]
    bot.send_message.assert_not_awaited()


async def test_a_dm_keyboard_body_leaves_its_escapes_to_the_rich_messages_markdown():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.post_dm_keyboard(chat_id=100, request_id="r1",
                              text=admitted(ESCAPED), options=["Ok"])
    assert rich_markdown(bot) == [SENT]
    bot.send_message.assert_not_awaited()


async def test_a_dm_edit_leaves_its_escapes_to_the_rich_messages_markdown():
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.edit_dm_message(100, 5, ESCAPED) is True
    assert [e["rich_message"] for e in rich_edits(bot)] == [{"markdown": SENT}]
    bot.edit_message_text.assert_not_awaited()


async def test_a_reply_leaves_its_escapes_to_the_rich_messages_markdown():
    """A reply goes as a rich message: Telegram's own Markdown consumes the
    escapes, so they travel as written."""
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_response(admitted(ESCAPED), {"chat_id": "42"})
    assert rich_markdown(bot) == [SENT]
    bot.send_message.assert_not_awaited()


async def test_a_refused_card_still_retries_with_the_authored_text():
    ch, bot = _mk_channel_with_fake_bot()
    ch._record_post = MagicMock()
    text = "**Pair** CUWVSRB8\\-0007?"
    bot.do_api_request = AsyncMock(side_effect=[BadRequest("Rich_message_parse_failed"),
                                                MagicMock(message_id=9)])
    assert await ch.deliver_operator_proposal(100, text, ["Yes"], "rid1") == 9
    # resent unformatted: the authored text as plain blocks
    assert rich_sent(bot)[-1]["rich_message"] == {"blocks": plain_blocks(text)}


async def test_a_rich_card_is_unchanged():
    """A formatted card's Markdown, escapes included, goes as written."""
    ch, bot = _mk_channel_with_fake_bot()
    ch._record_post = MagicMock()
    await ch.deliver_operator_proposal(100, "**Pair** CUWVSRB8\\-0007?", ["Yes"], "rid1")
    assert rich_markdown(bot) == ["**Pair** CUWVSRB8\\-0007?"]
