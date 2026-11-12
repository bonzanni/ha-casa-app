"""Approval cards are posted with entities computed by the caller and edited
as literal text: neither path may run the markdown renderer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import MessageEntity
from telegram.error import BadRequest

import channels.telegram as tg
from output_boundary_testing import admitted
from telegram_channel_double import _mk_channel_with_fake_bot


def _boom(*_a, **_k):
    raise AssertionError("render() must not run on the explicit-entity path")


@pytest.mark.asyncio
async def test_explicit_entities_are_sent_as_given_and_render_is_not_called(monkeypatch):
    monkeypatch.setattr(tg, "render", _boom)
    ch, bot = _mk_channel_with_fake_bot()
    ents = [MessageEntity(type=MessageEntity.BOLD, offset=0, length=4)]
    mid = await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("**ab** ok"),
        options=["Yes", "No"], entities=ents,
    )
    assert mid == 12345
    assert bot.send_message.await_count == 1
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "**ab** ok"           # markers are never parsed
    assert kw["entities"] == ents
    assert kw["reply_markup"].inline_keyboard[0][0].text == "Yes"


@pytest.mark.asyncio
async def test_explicit_entities_text_has_lone_surrogates_replaced_one_for_one(monkeypatch):
    monkeypatch.setattr(tg, "render", _boom)
    ch, bot = _mk_channel_with_fake_bot()
    ents = [MessageEntity(type=MessageEntity.BOLD, offset=2, length=2)]
    await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("a\ud800bc"),
        options=["Ok"], entities=ents,
    )
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "a�bc"
    assert kw["entities"] == ents


@pytest.mark.asyncio
async def test_entity_bad_request_retries_the_same_text_plain(monkeypatch):
    monkeypatch.setattr(tg, "render", _boom)
    monkeypatch.setattr(tg, "_plain", _boom)
    ch, bot = _mk_channel_with_fake_bot()
    ok = MagicMock(message_id=7)
    bot.send_message = AsyncMock(side_effect=[BadRequest("bad entity"), ok])
    ents = [MessageEntity(type=MessageEntity.BOLD, offset=0, length=2)]
    mid = await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("a\\_b **x**"),
        options=["Ok"], entities=ents,
    )
    assert mid == 7
    first, second = bot.send_message.await_args_list
    assert first.kwargs["entities"] == ents
    assert second.kwargs["text"] == "a\\_b **x**"   # escapes untouched
    assert "entities" not in second.kwargs
    assert "reply_markup" in second.kwargs


@pytest.mark.asyncio
async def test_explicit_entities_other_failure_returns_none_without_retry(monkeypatch):
    monkeypatch.setattr(tg, "render", _boom)
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("down"))
    mid = await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("x"), options=["Ok"],
        entities=[],
    )
    assert mid is None
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_explicit_entities_still_refuse_unadmitted_text(monkeypatch):
    monkeypatch.setattr(tg, "render", _boom)
    ch, bot = _mk_channel_with_fake_bot()
    mid = await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text="bare string", options=["Ok"],
        entities=[],
    )
    assert mid is None
    assert bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_literal_edit_sends_markers_literally(monkeypatch):
    monkeypatch.setattr(tg, "render", _boom)
    monkeypatch.setattr(tg, "_plain", _boom)
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.edit_dm_message(
        100, 5, "✅ Approved — **Ellen**", literal=True) is True
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "✅ Approved — **Ellen**"
    assert "entities" not in kw
    assert kw["reply_markup"].inline_keyboard == []   # buttons dropped as usual


@pytest.mark.asyncio
async def test_literal_edit_replaces_lone_surrogates(monkeypatch):
    monkeypatch.setattr(tg, "render", _boom)
    ch, bot = _mk_channel_with_fake_bot()
    await ch.edit_dm_message(100, 5, "a\udc00b", literal=True)
    assert bot.edit_message_text.await_args.kwargs["text"] == "a�b"


@pytest.mark.asyncio
async def test_literal_edit_caption_variant_and_not_modified(monkeypatch):
    monkeypatch.setattr(tg, "render", _boom)
    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_caption = AsyncMock()
    assert await ch.edit_dm_message(100, 5, "**x**", caption=True, literal=True) is True
    kw = bot.edit_message_caption.await_args.kwargs
    assert kw["caption"] == "**x**"
    assert "caption_entities" not in kw
    bot.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    assert await ch.edit_dm_message(100, 5, "**x**", literal=True) is True
    assert bot.edit_message_text.await_count == 1


@pytest.mark.asyncio
async def test_literal_edit_keeps_buttons(monkeypatch):
    monkeypatch.setattr(tg, "render", _boom)
    ch, bot = _mk_channel_with_fake_bot()
    await ch.edit_dm_message(100, 5, "x", literal=True, buttons=[("Stop", "cb|1")])
    markup = bot.edit_message_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "cb|1"


@pytest.mark.asyncio
async def test_ask_user_posts_still_render():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("**Install** it?"),
        options=["Yes"],
    )
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "Install it?"
    assert kw["entities"][0].type == MessageEntity.BOLD
    await ch.edit_dm_message(100, 5, "**done**")
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "done"
    assert kw["entities"][0].type == MessageEntity.BOLD
