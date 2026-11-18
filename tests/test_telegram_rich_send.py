"""Sending, editing and drafting Telegram rich messages (channels/tg_rich.py).

The fake bot's ``do_api_request`` stands in for the raw Bot API call; every
refusal is raised the way python-telegram-bot raises it, with the error name
rewritten (``Bad Request: RICH_MESSAGE_BLOCKS_TOO_MANY`` arrives as
``Rich_message_blocks_too_many``).
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters
from telegram.error import BadRequest, RetryAfter

from channels.tg_rich import (
    RICH_BUDGET_BYTES,
    edit_rich,
    plain_blocks,
    prepare,
    send_rich,
    send_rich_draft,
    split,
)
from telegram_channel_double import _mk_channel_with_fake_bot


def _calls(bot) -> list[tuple[str, dict]]:
    return [(c.args[0], c.kwargs["api_kwargs"]) for c in bot.do_api_request.call_args_list]


def _sent(message_id: int = 12345):
    return MagicMock(message_id=message_id)


def _keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Yes", callback_data="p:yes")]])


# ---------------------------------------------------------------------------
# send_rich
# ---------------------------------------------------------------------------


async def test_send_rich_calls_sendRichMessage_with_prepared_markdown():
    ch, bot = _mk_channel_with_fake_bot()
    text = "Called `mcp__casa__x` and mcp__casa__x."
    got = await ch._send_rich(100, text)
    (call,) = bot.do_api_request.call_args_list
    assert call.args[0] == "sendRichMessage"
    assert call.kwargs["return_type"] is Message
    assert call.kwargs["api_kwargs"] == {
        "chat_id": 100, "rich_message": {"markdown": prepare(text)}}
    assert got == [bot.do_api_request.return_value]


async def test_keyboard_on_last_part_quote_on_first():
    _ch, bot = _mk_channel_with_fake_bot()
    text = "a" * 20_000 + "\n\n" + "b" * 20_000
    assert len(split(text)) == 2
    reply = ReplyParameters(message_id=9, quote="a", allow_sending_without_reply=True)
    keyboard = _keyboard()
    recorded = []
    await send_rich(bot, 100, text, reply_markup=keyboard, reply_parameters=reply,
                    record=recorded.append, disable_notification=True,
                    message_thread_id=4)
    (e1, first), (e2, second) = _calls(bot)
    assert e1 == e2 == "sendRichMessage"
    assert first["reply_parameters"] == reply.to_dict()
    assert "reply_markup" not in first
    assert second["reply_markup"] == keyboard.to_dict()
    assert "reply_parameters" not in second
    for kw in (first, second):
        assert kw["disable_notification"] is True
        assert kw["message_thread_id"] == 4
    assert first["rich_message"]["markdown"].startswith("aaa")
    assert second["rich_message"]["markdown"].startswith("bbb")
    assert len(recorded) == 2


async def test_blank_text_sends_nothing():
    _ch, bot = _mk_channel_with_fake_bot()
    assert await send_rich(bot, 100, "  \n\n ") == []
    bot.do_api_request.assert_not_awaited()


async def test_syntax_refusal_resends_plain_blocks():
    _ch, bot = _mk_channel_with_fake_bot()
    text = "![x](tg://time?unix=1)\n\nsecond paragraph"
    bot.do_api_request.side_effect = [BadRequest("Rich_message_photo_url_invalid"), _sent()]
    recorded = []
    got = await send_rich(bot, 100, text, record=recorded.append)
    (_e1, first), (e2, second) = _calls(bot)
    assert "markdown" in first["rich_message"]
    assert e2 == "sendRichMessage"
    assert second["rich_message"] == {"blocks": plain_blocks(text)}
    assert "markdown" not in second["rich_message"]
    assert len(got) == len(recorded) == 1


async def test_block_limit_refusal_on_short_text_splits_by_paragraphs():
    _ch, bot = _mk_channel_with_fake_bot()
    words = [f"w{i}" for i in range(501)]
    text = "\n\n".join(words)
    assert len(text.encode()) < 3_000 and len(split(text)) == 1
    delivered = []

    async def api(endpoint, *, api_kwargs, return_type):
        markdown = api_kwargs["rich_message"]["markdown"]
        if len(markdown.split("\n\n")) > 500:
            raise BadRequest("Rich_message_blocks_too_many")
        delivered.append(markdown)
        return _sent()

    bot.do_api_request.side_effect = api
    got = await send_rich(bot, 100, text)
    assert len(got) == len(delivered) == 2
    assert [w for part in delivered for w in part.split("\n\n")] == words


async def test_size_refusal_halving_keeps_quote_first_and_keyboard_last():
    _ch, bot = _mk_channel_with_fake_bot()
    text = "\n\n".join(f"p{i}" for i in range(8))
    reply = ReplyParameters(message_id=9)
    keyboard = _keyboard()

    async def api(endpoint, *, api_kwargs, return_type):
        if len(api_kwargs["rich_message"]["markdown"].split("\n\n")) > 4:
            raise BadRequest("Rich_message_too_large")
        return _sent()

    bot.do_api_request.side_effect = api
    await send_rich(bot, 100, text, reply_markup=keyboard, reply_parameters=reply)
    ok = _calls(bot)[1:]
    assert len(ok) == 2
    assert "reply_parameters" in ok[0][1] and "reply_markup" not in ok[0][1]
    assert "reply_markup" in ok[1][1] and "reply_parameters" not in ok[1][1]


async def test_size_refusal_at_every_depth_ends_in_plain_blocks():
    _ch, bot = _mk_channel_with_fake_bot()
    paragraphs = [f"p{i}" for i in range(32)]
    text = "\n\n".join(paragraphs)

    async def api(endpoint, *, api_kwargs, return_type):
        if "markdown" in api_kwargs["rich_message"]:
            raise BadRequest("Rich_message_text_too_long")
        return _sent()

    bot.do_api_request.side_effect = api
    got = await send_rich(bot, 100, text)
    calls = _calls(bot)
    markdown_calls = [kw for _e, kw in calls if "markdown" in kw["rich_message"]]
    block_calls = [kw for _e, kw in calls if "blocks" in kw["rich_message"]]
    # One send, then four levels of halving: 1 + 2 + 4 + 8 + 16 attempts.
    assert len(markdown_calls) == 31
    assert len(got) == len(block_calls) == 16
    assert [b["text"] for kw in block_calls for b in kw["rich_message"]["blocks"]] == paragraphs


async def test_single_paragraph_size_refusal_goes_as_plain_blocks():
    _ch, bot = _mk_channel_with_fake_bot()
    text = "one paragraph only"
    bot.do_api_request.side_effect = [BadRequest("Rich_message_blocks_too_many"), _sent()]
    await send_rich(bot, 100, text)
    (_e1, _first), (_e2, second) = _calls(bot)
    assert second["rich_message"] == {"blocks": plain_blocks(text)}


async def test_a_refusal_of_the_plain_blocks_propagates():
    _ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = BadRequest("Chat not found")
    with pytest.raises(BadRequest):
        await send_rich(bot, 100, "hello")
    assert len(_calls(bot)) == 2


@pytest.mark.parametrize("refusal", ["Quote_text_invalid", "Something else"],
                         ids=["quote", "other"])
async def test_rich_send_refused_quote_is_resent_without_it(refusal):
    _ch, bot = _mk_channel_with_fake_bot()

    async def api(endpoint, *, api_kwargs, return_type):
        if "quote" in api_kwargs.get("reply_parameters", {}):
            raise BadRequest(refusal)
        return _sent()

    bot.do_api_request.side_effect = api
    reply = ReplyParameters(message_id=9, quote="Tue", allow_sending_without_reply=True)
    got = await send_rich(bot, 100, "**Tue** works.", reply_parameters=reply)
    (_e1, first), (_e2, second) = _calls(bot)
    assert first["reply_parameters"]["quote"] == "Tue"
    assert second["reply_parameters"] == {"message_id": 9, "allow_sending_without_reply": True}
    assert second["rich_message"] == first["rich_message"] == {"markdown": "**Tue** works."}
    assert len(got) == 1


async def test_a_syntax_refusal_with_a_quote_keeps_the_quote_on_the_plain_blocks():
    _ch, bot = _mk_channel_with_fake_bot()

    async def api(endpoint, *, api_kwargs, return_type):
        if "markdown" in api_kwargs["rich_message"]:
            raise BadRequest("Rich_message_photo_url_invalid")
        return _sent()

    bot.do_api_request.side_effect = api
    reply = ReplyParameters(message_id=9, quote="Tue", allow_sending_without_reply=True)
    await send_rich(bot, 100, "Tue works.", reply_parameters=reply)
    calls = _calls(bot)
    assert [("quote" in kw["reply_parameters"], next(iter(kw["rich_message"])))
            for _e, kw in calls] == [(True, "markdown"), (False, "markdown"), (True, "blocks")]


async def test_a_size_refusal_with_a_quote_is_not_retried_without_it():
    _ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = [BadRequest("Rich_message_blocks_too_many"),
                                      _sent(), _sent()]
    reply = ReplyParameters(message_id=9, quote="a", allow_sending_without_reply=True)
    await send_rich(bot, 100, "a\n\nb", reply_parameters=reply)
    calls = _calls(bot)
    assert len(calls) == 3
    assert calls[1][1]["reply_parameters"]["quote"] == "a"
    assert calls[1][1]["rich_message"] == {"markdown": "a"}
    assert calls[2][1]["rich_message"] == {"markdown": "b"}


async def test_halving_inside_a_fence_reopens_it():
    _ch, bot = _mk_channel_with_fake_bot()
    text = "```py\nx = 1\n\ny = 2\n```"
    bot.do_api_request.side_effect = [BadRequest("Rich_message_blocks_too_many"),
                                      _sent(), _sent()]
    await send_rich(bot, 100, text)
    halves = [kw["rich_message"]["markdown"] for _e, kw in _calls(bot)[1:]]
    assert halves[0].startswith("```py\n") and halves[0].endswith("\n```")
    assert halves[1].startswith("```py\n") and halves[1].endswith("```")


# ---------------------------------------------------------------------------
# edit_rich
# ---------------------------------------------------------------------------


async def test_edit_rich_sends_editMessageText_with_rich_message():
    _ch, bot = _mk_channel_with_fake_bot()
    keyboard = _keyboard()
    got = await edit_rich(bot, "mcp__casa__x", chat_id=100, message_id=5,
                          reply_markup=keyboard)
    (call,) = bot.do_api_request.call_args_list
    assert call.args[0] == "editMessageText"
    assert call.kwargs["return_type"] is Message
    assert call.kwargs["api_kwargs"] == {
        "chat_id": 100, "message_id": 5, "reply_markup": keyboard.to_dict(),
        "rich_message": {"markdown": prepare("mcp__casa__x")}}
    assert got is bot.do_api_request.return_value


async def test_edit_rich_by_inline_message_id_names_no_chat():
    _ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.return_value = True
    assert await edit_rich(bot, "done", inline_message_id="AbC") is True
    (_e, kw) = _calls(bot)[0]
    assert kw == {"inline_message_id": "AbC", "rich_message": {"markdown": "done"}}


async def test_edit_rich_too_long_is_cut_with_an_ellipsis():
    _ch, bot = _mk_channel_with_fake_bot()
    text = "\n\n".join("è" * 1_000 for _ in range(40))
    await edit_rich(bot, text, chat_id=100, message_id=5)
    (_e, kw) = _calls(bot)[0]
    markdown = kw["rich_message"]["markdown"]
    assert markdown.endswith("…")
    assert len(markdown.encode("utf-8")) <= RICH_BUDGET_BYTES
    assert markdown == prepare(split(text, RICH_BUDGET_BYTES - 3)[0] + "…")


@pytest.mark.parametrize("refusal", ["Rich_message_photo_url_invalid",
                                     "Rich_message_blocks_too_many"], ids=["syntax", "size"])
async def test_edit_rich_any_refusal_falls_back_to_blocks(refusal):
    _ch, bot = _mk_channel_with_fake_bot()
    text = "first\n\nsecond"
    keyboard = _keyboard()
    bot.do_api_request.side_effect = [BadRequest(refusal), _sent(5)]
    got = await edit_rich(bot, text, chat_id=100, message_id=5, reply_markup=keyboard)
    (e1, first), (e2, second) = _calls(bot)
    assert e1 == e2 == "editMessageText"
    assert first["rich_message"] == {"markdown": text}
    assert second["rich_message"] == {"blocks": plain_blocks(text)}
    assert second["reply_markup"] == keyboard.to_dict()
    assert (second["chat_id"], second["message_id"]) == (100, 5)
    assert got.message_id == 5


async def test_edit_rich_not_modified_is_success():
    _ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = BadRequest(
        "Message is not modified: specified new message content and reply markup "
        "are exactly the same as a current content and reply markup of the message")
    assert await edit_rich(bot, "same", chat_id=100, message_id=5) is True
    assert len(_calls(bot)) == 1


async def test_edit_rich_refusal_of_the_plain_blocks_propagates():
    _ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = BadRequest("Rich_message_parse_failed")
    with pytest.raises(BadRequest):
        await edit_rich(bot, "x", chat_id=100, message_id=5)
    assert len(_calls(bot)) == 2


@pytest.mark.parametrize("refusal", ["Message can't be edited", "Message to edit not found"],
                         ids=["never-editable", "gone"])
async def test_edit_rich_a_message_that_cannot_be_edited_is_not_resent(refusal):
    _ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = BadRequest(refusal)
    with pytest.raises(BadRequest):
        await edit_rich(bot, "x", chat_id=100, message_id=5)
    assert len(_calls(bot)) == 1


# ---------------------------------------------------------------------------
# send_rich_draft
# ---------------------------------------------------------------------------


async def test_draft_uses_sendRichMessageDraft_with_stop_flags():
    _ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.return_value = True
    text = "Writing mcp__casa__x"
    assert await send_rich_draft(bot, 100, 7, text, can_stop=True, keep_on_stop=False) is None
    (call,) = bot.do_api_request.call_args_list
    assert call.args[0] == "sendRichMessageDraft"
    assert call.kwargs["api_kwargs"] == {
        "chat_id": 100, "draft_id": 7, "rich_message": {"markdown": prepare(text)},
        "can_stop": True, "keep_on_stop": False}


@pytest.mark.parametrize("wait", [3, timedelta(seconds=3)], ids=["seconds", "timedelta"])
async def test_draft_flood_wait_returns_the_seconds(wait):
    _ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = RetryAfter(wait)
    assert await send_rich_draft(bot, 100, 7, "x", can_stop=True, keep_on_stop=True) == 3.0


# ---------------------------------------------------------------------------
# The channel's methods pass its bot
# ---------------------------------------------------------------------------


async def test_channel_methods_pass_the_channel_bot():
    ch, bot = _mk_channel_with_fake_bot()
    recorded = []
    await ch._send_rich(100, "hi", record=recorded.append, disable_notification=True)
    await ch._edit_rich(100, 5, "edited")
    await ch._edit_rich(None, None, "inline", inline_message_id="AbC")
    bot.do_api_request.return_value = True
    await ch._send_rich_draft(100, 7, "draft", can_stop=True, keep_on_stop=True)
    assert [(e, kw.get("chat_id"), kw.get("message_id"), kw.get("inline_message_id"))
            for e, kw in _calls(bot)] == [
        ("sendRichMessage", 100, None, None),
        ("editMessageText", 100, 5, None),
        ("editMessageText", None, None, "AbC"),
        ("sendRichMessageDraft", 100, None, None)]
    assert len(recorded) == 1


# ---------------------------------------------------------------------------
# Every message that lands is recorded as it lands
# ---------------------------------------------------------------------------


async def test_a_landed_half_is_recorded_before_the_next_half_fails():
    from telegram.error import NetworkError
    _ch, bot = _mk_channel_with_fake_bot()
    text = "\n\n".join(f"p{i}" for i in range(4))
    first = _sent(42)
    bot.do_api_request.side_effect = [
        BadRequest("Rich_message_too_large"), first, NetworkError("boom")]
    recorded = []
    with pytest.raises(NetworkError):
        await send_rich(bot, 100, text, record=recorded.append)
    assert recorded == [first]


async def test_every_half_is_recorded_once_in_order():
    _ch, bot = _mk_channel_with_fake_bot()
    text = "\n\n".join(f"p{i}" for i in range(4))
    landed = [_sent(1), _sent(2)]
    bot.do_api_request.side_effect = [BadRequest("Rich_message_too_large"), *landed]
    recorded = []
    got = await send_rich(bot, 100, text, record=recorded.append)
    assert recorded == got == landed


# ---------------------------------------------------------------------------
# One rule for "the message itself cannot be edited"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("refusal", ["Message can't be edited", "Message to edit not found"],
                         ids=["never-editable", "gone"])
async def test_a_caption_edit_of_a_message_that_cannot_be_edited_is_tried_once(refusal):
    from unittest.mock import AsyncMock
    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_caption = AsyncMock(side_effect=BadRequest(refusal))
    assert await ch.edit_dm_message(100, 5, "**Paid** invoice", caption=True) is False
    assert bot.edit_message_caption.await_count == 1


def test_the_channel_and_the_guest_reply_share_the_message_refusal_rule():
    from channels import guest_reply, telegram, tg_rich
    assert tg_rich.is_message_refusal(BadRequest("Message can't be edited"))
    assert tg_rich.is_message_refusal(BadRequest("Message to edit not found"))
    assert not tg_rich.is_message_refusal(BadRequest("Rich_message_parse_failed"))
    assert guest_reply.is_message_refusal is tg_rich.is_message_refusal
    assert telegram.is_message_refusal is tg_rich.is_message_refusal
