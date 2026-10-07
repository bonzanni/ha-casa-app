"""#1330: a message with no formatting span shows its text as the rich path
would, with the markdown escapes consumed.

``render()`` returns ``(display, None)`` for a span-less text, and every
single-message sender used to send the AUTHORED text in that branch, so a
plugin card escaped for markdown (``CUWVSRB8\\-0007``) showed its backslashes
while the same card with one bold word did not. ``plain_text`` is the one
place that decides what such a send shows; these tests pin every sender that
takes the branch, and pin what stays authored: a text whose spans could not
be sent (over the budgets) and the retry after Telegram refuses entities.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from channels.tg_richtext import plain_text, render
from output_boundary_testing import admitted
from test_telegram_topic_stream import _mk_channel_with_fake_bot

pytestmark = pytest.mark.asyncio

ESCAPED = "📊 Q3\nchose CUWVSRB8\\-0007 \\(1 Oct\\)"
SHOWN = "📊 Q3\nchose CUWVSRB8-0007 (1 Oct)"


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


async def test_a_span_less_table_shows_its_rich_display_with_every_cell():
    # Terra x1: a separator-less table with no span re-spaces its rows from
    # the parsed cells. The plain send shows that display, as the rich path
    # does, and no cell content is lost.
    text = "|  " + "x" * 30 + "   |  " + "y" * 30 + "  |\n|  a  |  b\\-c  |\n|d|e|"
    display, entities = render(text)
    assert entities is None
    assert plain_text(text) == display
    assert display.split() and "".join(display.split()) == "".join(
        text.replace("\\-", "-").split())


async def test_a_span_less_display_over_one_message_keeps_the_authored_text():
    # Astra x1: a span-less table's display is padded LONGER than its text;
    # sent as display it would exceed one message and land nothing.
    text = "\n".join(["|a|b|c|d|e|f|g|h|i|j|"] * 100) + "\nx\\-y"
    display, entities = render(text)
    assert entities is None
    assert len(text) <= 4096 < len(display)
    assert plain_text(text) is text


async def test_plain_text_keeps_the_authored_text_when_spans_could_not_be_sent():
    # 101 bold spans: over the entity budget, so render() sends nothing rich.
    # The authored text is what carries the formatting (and any address).
    text = " ".join("**b**" for _ in range(101)) + " x\\-y"
    assert render(text)[1] is None
    assert plain_text(text) is text


# ---------------------------------------------------------------------------
# Every single-message sender's no-entities branch
# ---------------------------------------------------------------------------

async def test_a_plain_proposal_card_shows_no_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    ch._record_post = MagicMock()
    await ch.deliver_operator_proposal(100, ESCAPED, ["Yes", "No"], "rid1")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == SHOWN
    assert "entities" not in kw
    assert kw["reply_markup"] is not None


async def test_a_plain_dm_keyboard_body_shows_no_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.post_dm_keyboard(chat_id=100, request_id="r1",
                              text=admitted(ESCAPED), options=["Ok"])
    assert bot.send_message.await_args.kwargs["text"] == SHOWN


async def test_a_plain_dm_edit_shows_no_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.edit_dm_message(100, 5, ESCAPED) is True
    assert bot.edit_message_text.await_args.kwargs["text"] == SHOWN


async def test_a_plain_topic_post_with_markup_shows_no_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_topic_message_markup(42, ESCAPED, None)
    assert bot.send_message.await_args.kwargs["text"] == SHOWN


async def test_a_plain_topic_edit_with_markup_shows_no_backslash():
    from channels.output_sequencer import MARKUP_EMPTY
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.edit_topic_message_markup(42, 7, ESCAPED, MARKUP_EMPTY) is True
    assert bot.edit_message_text.await_args.kwargs["text"] == SHOWN


async def test_a_plain_narration_edit_shows_no_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.edit_topic_message_rich(42, 7, ESCAPED) is True
    assert bot.edit_message_text.await_args.kwargs["text"] == SHOWN


async def test_a_plain_narration_post_shows_no_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_to_topic_rich(42, ESCAPED)
    assert bot.send_message.await_args.kwargs["text"] == SHOWN


async def test_a_plain_ask_body_shows_no_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.post_ask_body_rich(42, ESCAPED)
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.kwargs["text"] == SHOWN


async def test_a_plain_reply_shows_no_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_response(admitted(ESCAPED), {"chat_id": "42"})
    assert bot.send_message.await_args.kwargs["text"] == SHOWN


async def test_a_plain_streamed_reply_finalizes_without_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("partial")
    await ch.finalize_response_stream(admitted(ESCAPED), ctx, on_token)
    assert bot.edit_message_text.await_args.kwargs["text"] == SHOWN


async def test_a_plain_topic_stream_finalizes_without_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    handle = ch.create_topic_stream(topic_id=42)
    await handle.emit("partial")
    await handle.finalize(ESCAPED)
    assert bot.edit_message_text.await_args.kwargs["text"] == SHOWN


async def test_a_plain_topic_stream_with_no_emit_posts_without_backslash():
    ch, bot = _mk_channel_with_fake_bot()
    handle = ch.create_topic_stream(topic_id=42)
    await handle.finalize(ESCAPED)
    assert bot.send_message.await_args.kwargs["text"] == SHOWN


# ---------------------------------------------------------------------------
# What stays authored
# ---------------------------------------------------------------------------

async def test_a_refused_card_still_retries_with_the_authored_text():
    ch, bot = _mk_channel_with_fake_bot()
    ch._record_post = MagicMock()
    text = "**Pair** CUWVSRB8\\-0007?"
    bot.send_message = AsyncMock(side_effect=[BadRequest("bad entity"),
                                              MagicMock(message_id=9)])
    assert await ch.deliver_operator_proposal(100, text, ["Yes"], "rid1") == 9
    assert bot.send_message.await_args_list[-1].kwargs["text"] == text


async def test_a_rich_card_is_unchanged():
    ch, bot = _mk_channel_with_fake_bot()
    ch._record_post = MagicMock()
    await ch.deliver_operator_proposal(100, "**Pair** CUWVSRB8\\-0007?", ["Yes"], "rid1")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "Pair CUWVSRB8-0007?"
    assert kw["entities"]
