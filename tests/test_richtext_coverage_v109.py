"""v0.109.0 — close the remaining rich-text delivery gaps (G1-G5).

Operator-reported symptom: literal ``**`` and unstructured tables in the
assistant's DMs. Root causes:

* G1: ``post_dm_keyboard`` / ``edit_dm_message`` sent/edited plain — the
  assistant's dominant DM ask surface never rendered. Both now send the body
  as a rich message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest

from channels.tg_rich import plain_blocks
from telegram_channel_double import _mk_channel_with_fake_bot, rich_edits, rich_sent
from output_boundary_testing import admitted


# ===========================================================================
# G1 · post_dm_keyboard / edit_dm_message send the body as a rich message
# ===========================================================================

@pytest.mark.asyncio
async def test_post_dm_keyboard_sends_the_body_as_a_rich_message():
    ch, bot = _mk_channel_with_fake_bot()
    mid = await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("**Install** the plugin?"),
        options=["Yes", "No"],
    )
    assert mid == 12345
    (kw,) = rich_sent(bot)
    assert kw["chat_id"] == 100
    assert kw["rich_message"] == {"markdown": "**Install** the plugin?"}
    # Buttons untouched, in the keyboard's JSON form.
    rows = kw["reply_markup"]["inline_keyboard"]
    assert [row[0]["text"] for row in rows] == ["Yes", "No"]
    assert [row[0]["callback_data"] for row in rows] == [
        "v1|resident_ask|r1|0", "v1|resident_ask|r1|1"]
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_dm_keyboard_refused_body_is_resent_unformatted_with_its_keyboard():
    ch, bot = _mk_channel_with_fake_bot()
    ok = MagicMock(message_id=7)
    bot.do_api_request = AsyncMock(side_effect=[BadRequest("Rich_message_parse_failed"), ok])
    mid = await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("**b**"), options=["Ok"],
    )
    assert mid == 7
    first, second = rich_sent(bot)
    assert first["rich_message"] == {"markdown": "**b**"}
    assert second["rich_message"] == {"blocks": plain_blocks("**b**")}   # the ORIGINAL text
    assert second["reply_markup"] == first["reply_markup"]                # keyboard preserved


@pytest.mark.asyncio
async def test_edit_dm_message_edits_into_a_rich_message():
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.edit_dm_message(100, 5, "**done**") is True
    (kw,) = rich_edits(bot)
    assert (kw["chat_id"], kw["message_id"]) == (100, 5)
    assert kw["rich_message"] == {"markdown": "**done**"}
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_dm_message_refused_edit_is_retried_unformatted():
    ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request = AsyncMock(side_effect=[BadRequest("Rich_message_parse_failed"), True])
    assert await ch.edit_dm_message(100, 5, "**done**") is True
    first, second = rich_edits(bot)                    # same-message edit, never a send
    assert rich_sent(bot) == [] and bot.send_message.await_count == 0
    assert second["message_id"] == first["message_id"] == 5
    assert second["rich_message"] == {"blocks": plain_blocks("**done**")}   # ORIGINAL text


@pytest.mark.asyncio
async def test_edit_dm_message_not_modified_tolerated():
    ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request = AsyncMock(
        side_effect=BadRequest("Message is not modified"))
    assert await ch.edit_dm_message(100, 5, "**same**") is True
    assert bot.do_api_request.await_count == 1


@pytest.mark.asyncio
async def test_settle_edit_on_pre_upgrade_plain_card_uses_rich_edit():
    """Message 5 is a card an earlier version posted as plain text with
    entities. Its settle edit is the same rich edit as any card's:
    ``editMessageText`` with ``rich_message``, the keyboard cleared, and no
    plain ``edit_message_text``."""
    from channels.tg_rich import prepare
    ch, bot = _mk_channel_with_fake_bot()
    settled = "**Install** the plugin?\n☑ Yes"
    assert await ch.edit_dm_message(100, 5, settled) is True
    (call,) = bot.do_api_request.await_args_list
    assert call.args[0] == "editMessageText"
    assert call.kwargs["api_kwargs"] == {
        "chat_id": 100, "message_id": 5, "reply_markup": InlineKeyboardMarkup([]).to_dict(),
        "rich_message": {"markdown": prepare(settled)}}
    bot.edit_message_text.assert_not_awaited()
