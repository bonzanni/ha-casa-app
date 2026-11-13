"""The channel end of the output boundary (#1038): the model-text
methods of ``TelegramChannel`` accept an ``Admitted`` and refuse a bare string
through each method's OWN failure contract, making zero Bot API calls.

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
from telegram_channel_double import _mk_channel_with_fake_bot

pytestmark = [pytest.mark.asyncio]

LINE = "(Ellen hasn't opened “invoice.pdf”.)"


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
