"""The channel end of the output boundary (#1038 §4.1, §6.6): the six
model-text methods of ``TelegramChannel`` accept an ``Admitted`` and refuse a
bare string through each method's OWN failure contract, making zero Bot API
calls; and when a streamed reply's page-1 unit did not land, the admission's
lines go out once, as their own message, before the overflow pages — which then
go exactly as they do when the head landed (plan round 3: the mechanism that
prefixed every unit was cut after four findings in it).

Real ``TelegramChannel`` with the recording bot double the delivery-outcome
tests use; assertions are on Bot API calls and outcomes, never on a method
merely returning.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from channels import DeliveryOutcome
from output_boundary import Admitted, IntentKind as K, UnadmittedText, casa_text
from test_telegram_topic_stream import _mk_channel_with_fake_bot

pytestmark = [pytest.mark.asyncio]

LINE = "Casa: Ellen answered without opening “invoice.pdf” in this turn."


def _admitted(text: str, *lines: str) -> Admitted:
    body = ("\n\n".join(lines) + "\n\n" + text) if lines else text
    return Admitted(body, scope_id="t1", kind=K.FINAL_REPLY, annotations=tuple(lines))


# ---------------------------------------------------------------------------
# Refusal, through each method's own contract (INV-OUT-001)
# ---------------------------------------------------------------------------

async def test_send_refuses_a_bare_string_with_zero_bot_calls(caplog):
    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    with caplog.at_level(logging.ERROR):
        assert await ch.send("hi", ctx) is DeliveryOutcome.NOT_DELIVERED
    assert bot.send_message.await_count == 0
    assert "_delivery_head_sent" not in ctx
    assert any("unadmitted model text refused: send" in r.message for r in caplog.records)


async def test_send_response_refuses_a_bare_string():
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.send_response("**hi**", {"chat_id": "42"}) is DeliveryOutcome.NOT_DELIVERED
    assert bot.send_message.await_count == 0


async def test_finalize_stream_refuses_a_bare_string():
    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("partial")
    assert await ch.finalize_stream("final", ctx, on_token) is DeliveryOutcome.NOT_DELIVERED
    assert bot.edit_message_text.await_count == 0


async def test_finalize_response_stream_refuses_a_bare_string():
    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("partial")
    assert await ch.finalize_response_stream("**final**", ctx, on_token) is DeliveryOutcome.NOT_DELIVERED
    assert bot.edit_message_text.await_count == 0
    assert bot.send_message.await_count == 1   # the streamed partial only


async def test_post_dm_keyboard_refuses_a_bare_string_with_none():
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.post_dm_keyboard(chat_id=42, request_id="r", text="Pick one",
                                     options=["a", "b"]) is None
    assert bot.send_message.await_count == 0


async def test_send_media_refuses_a_bare_string_caption_by_raising():
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_document = AsyncMock()
    with pytest.raises(UnadmittedText):
        await ch.send_media(b"%PDF-1.4", "document", "x.pdf", {"chat_id": "42"},
                            caption="the invoice")
    assert bot.send_document.await_count == 0


async def test_send_media_without_a_caption_needs_no_admission():
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_document = AsyncMock()
    await ch.send_media(b"%PDF-1.4", "document", "x.pdf", {"chat_id": "42"})
    assert bot.send_document.await_count == 1


# ---------------------------------------------------------------------------
# Admitted text is delivered as before
# ---------------------------------------------------------------------------

async def test_casa_text_is_delivered_by_send():
    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    assert await ch.send(casa_text("Rate limited."), ctx) is DeliveryOutcome.DELIVERED
    assert bot.send_message.await_args.kwargs["text"] == "Rate limited."


async def test_an_annotated_reply_is_delivered_with_the_line_at_its_head():
    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    assert await ch.send_response(_admitted("It's **€412**.", LINE), ctx) is DeliveryOutcome.DELIVERED
    sent = bot.send_message.await_args.kwargs["text"]
    assert sent.startswith(LINE)


async def test_post_dm_keyboard_posts_an_admitted_body():
    ch, bot = _mk_channel_with_fake_bot()
    body = Admitted("Which one?\n\n1. a\n2. b", scope_id="t1", kind=K.KEYBOARD)
    assert await ch.post_dm_keyboard(chat_id=42, request_id="r", text=body,
                                     options=["a", "b"]) == 12345
    assert bot.send_message.await_count == 1


# ---------------------------------------------------------------------------
# The overflow rule (§6.6)
# ---------------------------------------------------------------------------

def _long_rich(n: int = 400) -> str:
    return "\n\n".join(f"**para {i}** the invoice is €412 due Friday" for i in range(n))


async def test_the_line_goes_out_once_before_the_pages_when_the_page_1_edit_failed():
    from telegram.error import TelegramError

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")                      # streamed message established
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("boom"))
    outcome = await ch.finalize_response_stream(_admitted(_long_rich(), LINE), ctx, on_token)
    assert outcome is not DeliveryOutcome.DELIVERED
    calls = bot.send_message.await_args_list
    assert len(calls) >= 2
    assert calls[0].kwargs["text"] == LINE             # the line: once, its own message
    assert not any(c.kwargs["text"].startswith("Casa:") for c in calls[1:])
    assert all(c.kwargs.get("entities") for c in calls[1:])   # the pages: rich, untouched


async def test_overflow_pages_carry_no_line_when_page_1_landed():
    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    outcome = await ch.finalize_response_stream(_admitted(_long_rich(), LINE), ctx, on_token)
    assert outcome is DeliveryOutcome.DELIVERED
    assert bot.send_message.await_count >= 1
    assert not any(c.kwargs["text"].startswith(LINE) for c in bot.send_message.await_args_list)


async def test_the_line_goes_out_once_before_the_chunks_when_the_head_edit_failed():
    from telegram.error import TelegramError
    from channels.telegram import _split_message

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("boom"))
    plain = "\n\n".join(f"para {i} the invoice is €412" for i in range(400))
    adm = _admitted(plain, LINE)
    outcome = await ch.finalize_stream(adm, ctx, on_token)
    assert outcome is not DeliveryOutcome.DELIVERED
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts[0] == LINE                             # the line: once, its own message
    assert texts[1:] == _split_message(str(adm))[1:]    # the chunks: exactly the landed-head ones


async def test_a_not_modified_head_counts_as_landed_so_overflow_is_clean():
    from telegram.error import TelegramError

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("Message is not modified"))
    outcome = await ch.finalize_response_stream(_admitted(_long_rich(), LINE), ctx, on_token)
    assert outcome is DeliveryOutcome.DELIVERED
    assert not any(c.kwargs["text"].startswith(LINE) for c in bot.send_message.await_args_list)
