"""v0.109.0 — close the remaining rich-text delivery gaps (G1-G5).

Operator-reported symptom: literal ``**`` and unstructured tables in the
assistant's DMs and engagement topics. Root causes (Sol+Terra design round):

* G1: ``post_dm_keyboard`` / ``edit_dm_message`` sent/edited plain — the
  assistant's dominant DM ask surface never rendered.
* G2/G5: engagement completion summaries posted plain for in_casa
  engagements, and raw-fallback (no pagination) over the render caps.
* G3: engagement notices posted plain on the non-driver fallback paths.
* Table detector: separator-less bordered tables stayed proportional.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest

from channels.tg_richtext import render
from test_telegram_topic_stream import _mk_channel_with_fake_bot
from output_boundary_testing import admitted


# ===========================================================================
# G1 · post_dm_keyboard renders the body
# ===========================================================================

@pytest.mark.asyncio
async def test_post_dm_keyboard_renders_entities():
    ch, bot = _mk_channel_with_fake_bot()
    mid = await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("**Install** the plugin?"),
        options=["Yes", "No"],
    )
    assert mid == 12345
    assert bot.send_message.await_count == 1
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "Install the plugin?"
    assert kw["entities"][0].type == MessageEntity.BOLD
    assert isinstance(kw["reply_markup"], InlineKeyboardMarkup)
    # Buttons untouched by rendering.
    assert kw["reply_markup"].inline_keyboard[0][0].text == "Yes"


@pytest.mark.asyncio
async def test_post_dm_keyboard_plain_body_sends_raw():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("No markup here"),
        options=["Ok"],
    )
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "No markup here"
    assert "entities" not in kw


@pytest.mark.asyncio
async def test_post_dm_keyboard_badrequest_retries_plain_original():
    ch, bot = _mk_channel_with_fake_bot()
    ok = MagicMock(message_id=7)
    bot.send_message = AsyncMock(side_effect=[BadRequest("bad entity"), ok])
    mid = await ch.post_dm_keyboard(
        chat_id=100, request_id="r1", text=admitted("**b**"), options=["Ok"],
    )
    assert mid == 7
    assert bot.send_message.await_count == 2
    first, second = bot.send_message.await_args_list
    assert "entities" in first.kwargs
    assert second.kwargs["text"] == "**b**"           # ORIGINAL raw text
    assert "entities" not in second.kwargs
    assert "reply_markup" in second.kwargs            # keyboard preserved


@pytest.mark.asyncio
async def test_edit_dm_message_renders_entities():
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.edit_dm_message(100, 5, "**done**") is True
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "done"
    assert kw["entities"][0].type == MessageEntity.BOLD


@pytest.mark.asyncio
async def test_edit_dm_message_badrequest_retries_edit_plain():
    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_text = AsyncMock(side_effect=[BadRequest("bad entity"), None])
    assert await ch.edit_dm_message(100, 5, "**done**") is True
    assert bot.edit_message_text.await_count == 2     # same-message edit, never a send
    assert bot.send_message.await_count == 0
    second = bot.edit_message_text.await_args_list[1]
    assert second.kwargs["text"] == "**done**"        # ORIGINAL raw text
    assert "entities" not in second.kwargs


@pytest.mark.asyncio
async def test_edit_dm_message_not_modified_tolerated():
    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_text = AsyncMock(
        side_effect=BadRequest("Message is not modified"))
    assert await ch.edit_dm_message(100, 5, "**same**") is True
    assert bot.edit_message_text.await_count == 1


# ===========================================================================
# G3 · engagement-notice fallback renders
# ===========================================================================

@pytest.mark.asyncio
async def test_post_engagement_notice_fallback_rich():
    ch, bot = _mk_channel_with_fake_bot()
    ch._driver_post_notice = None
    rec = MagicMock(topic_id=42)
    await ch._post_engagement_notice(rec, "**resumed**")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "resumed"
    assert kw["entities"][0].type == MessageEntity.BOLD


# ===========================================================================
# G5 · the completion summary uses the paged rich sender when the channel has one
# ===========================================================================

async def _finalize_with_channel(tmp_path, tch):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=42,
    )
    cm = MagicMock()
    cm.get.return_value = tch
    init_tools(
        channel_manager=cm, bus=MagicMock(notify=AsyncMock()),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    await _finalize_engagement(
        rec, outcome="completed", text="**summary**",
        next_steps=[], driver=None,
    )


@pytest.mark.asyncio
async def test_completion_summary_uses_paged_rich_sender(tmp_path):
    tch = MagicMock()
    tch.send_to_topic = AsyncMock(return_value=1)
    tch.send_response_to_topic = AsyncMock(return_value=33)
    tch.close_topic = AsyncMock()
    await _finalize_with_channel(tmp_path, tch)
    tch.send_response_to_topic.assert_awaited_once()
    topic, text = tch.send_response_to_topic.await_args.args
    assert topic == 42
    assert "**summary**" in text
    assert not any("**summary**" in str(c.args)
                   for c in tch.send_to_topic.await_args_list)


@pytest.mark.asyncio
async def test_completion_summary_without_paged_sender_falls_back(tmp_path):
    tch = MagicMock()
    del tch.send_response_to_topic   # a channel without the paged rich sender
    tch.send_to_topic = AsyncMock(return_value=11)
    tch.close_topic = AsyncMock()
    await _finalize_with_channel(tmp_path, tch)
    assert any(c.args[0] == 42 and "**summary**" in c.args[1]
               for c in tch.send_to_topic.await_args_list)


# ===========================================================================
# Table detector · narrow separator-less acceptance
# ===========================================================================

def _pre_spans(text: str):
    display, entities = render(text)
    return display, [e for e in (entities or []) if e.type == MessageEntity.PRE]


def test_separatorless_bordered_table_becomes_pre():
    txt = "| a | b |\n| c | d |\n| e | f |"
    display, pres = _pre_spans(txt)
    assert len(pres) == 1
    # Content fidelity (Sol r2): EVERY row survives verbatim inside the PRE
    # span — the emission has no separator-row special-casing to lose row 2.
    pre = pres[0]
    covered = display[pre.offset:pre.offset + pre.length]
    assert covered == "| a | b |\n| c | d |\n| e | f |"


def test_separatored_table_content_fidelity():
    # v3 (#506): reflowed fidelity — every CELL survives; the separator row
    # is syntax and is dropped, cells are padded to their column width.
    txt = "| h1 | h2 |\n|---|---|\n| a | b |"
    display, pres = _pre_spans(txt)
    assert len(pres) == 1
    pre = pres[0]
    covered = display[pre.offset:pre.offset + pre.length]
    assert covered == "| h1 | h2 |\n| a  | b  |"


def test_separatorless_two_rows_stay_literal():
    txt = "| a | b |\n| c | d |"
    _, pres = _pre_spans(txt)
    assert pres == []


def test_separatorless_ragged_columns_stay_literal():
    txt = "| a | b |\n| c |\n| e | f |"
    _, pres = _pre_spans(txt)
    assert pres == []


def test_separatorless_single_column_stays_literal():
    txt = "| a |\n| b |\n| c |"
    _, pres = _pre_spans(txt)
    assert pres == []


def test_separatored_table_still_pre():
    txt = "| h1 | h2 |\n|---|---|\n| a | b |"
    _, pres = _pre_spans(txt)
    assert len(pres) == 1


def test_table_with_markers_reflows_marker_free():
    # v3 (#506 mode-A fix): markers in cells no longer reject the block —
    # the table reflows to a PRE box with the markers stripped, never a
    # literal-pipe soup.
    txt = "| **a** | b |\n| c | d |\n| e | f |"
    display, entities = render(txt)
    assert any(e.type == MessageEntity.PRE for e in (entities or []))
    assert "**" not in display
