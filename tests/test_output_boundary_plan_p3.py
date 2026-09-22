"""Plan round 3 findings on R1 (#1038), pinned before the fix — the fix being a
MECHANISM CUT (design §6.6 as amended): when a streamed reply's page-1 unit did
not land, the admission's lines go out ONCE as their own message before the
overflow pages, which then go exactly as they do when the head landed. Four
findings in three rounds had the same shape in the rule this replaces — a page
re-split around a prefix loses or cuts a destination — the last being Astra's:
page 1's own fallback destinations (`chunks0[1:]`) re-split at the budget.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from channels import DeliveryOutcome
from output_boundary import Admitted, IntentKind as K
from test_telegram_topic_stream import _mk_channel_with_fake_bot
from text_util import utf16_len

pytestmark = [pytest.mark.unit]

LINE = "Casa: Ellen answered without opening “invoice.pdf” in this turn."


def _admitted(text: str, *lines: str) -> Admitted:
    body = ("\n\n".join(lines) + "\n\n" + text) if lines else text
    return Admitted(body, scope_id="t1", kind=K.FINAL_REPLY, annotations=tuple(lines))


def _long_rich(n: int = 200) -> str:
    return "\n\n".join(f"**para {i}** the invoice is €412 due Friday" for i in range(n))


async def test_page_1_fallback_destinations_go_whole_after_a_failed_head():
    """Astra R1 p3 S1, with the real renderer: page 1 carries a 4,084-unit
    destination, its rich edit is refused, its plain edit fails — the
    destination then goes out as ONE message, after the line, never cut."""
    from telegram.error import BadRequest, TelegramError

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    url = "https://example.com/pay/" + "a" * 4060
    text = f"[Pay invoice]({url})\n\n" + _long_rich(200)
    bot.edit_message_text = AsyncMock(side_effect=[BadRequest("bad entities"), TelegramError("boom")])
    outcome = await ch.finalize_response_stream(_admitted(text, LINE), ctx, on_token)
    assert outcome is not DeliveryOutcome.DELIVERED
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts[0] == LINE
    assert url in texts, [utf16_len(t) for t in texts]           # whole, its own message
    assert all(t == url or ("a" * 200) not in t for t in texts)  # and never a fragment


async def test_a_failed_line_unit_is_logged_and_the_pages_still_go(caplog):
    """#1036 ruling 2: the model's words are never withheld — a line message
    that fails is logged, and the pages go out untouched after it."""
    from telegram.error import TelegramError

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    bot.send_message.side_effect = [TelegramError("flood")] + [MagicMock(message_id=7)] * 50
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("boom"))
    with caplog.at_level(logging.WARNING):
        outcome = await ch.finalize_response_stream(_admitted(_long_rich(400), LINE), ctx, on_token)
    assert outcome is not DeliveryOutcome.DELIVERED
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts[0] == LINE and len(texts) >= 2
    assert not any(t.startswith("Casa:") for t in texts[1:])
    assert "para 399 " in "\n".join(texts[1:])
    assert any("disclosure" in r.message for r in caplog.records)


async def test_a_failed_line_unit_on_the_plain_path_is_logged_and_the_chunks_still_go(caplog):
    from telegram.error import TelegramError
    from channels.telegram import _split_message

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    bot.send_message.side_effect = [TelegramError("flood")] + [MagicMock(message_id=7)] * 50
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("boom"))
    plain = "\n\n".join(f"para {i} the invoice is €412" for i in range(400))
    adm = _admitted(plain, LINE)
    with caplog.at_level(logging.WARNING):
        outcome = await ch.finalize_stream(adm, ctx, on_token)
    assert outcome is not DeliveryOutcome.DELIVERED
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts[0] == LINE
    assert texts[1:] == _split_message(str(adm))[1:]
    assert any("disclosure" in r.message for r in caplog.records)


async def test_an_unconfirmed_head_gets_the_line_message_too():
    """Terra design r5 S2, stated policy: a head edit whose acknowledgement was
    lost (`TimedOut` → UNKNOWN) may have applied — the line may already be on
    screen — and still gets the line message: a line shown twice costs less
    than claims shown with none."""
    from telegram.error import TimedOut

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock(side_effect=TimedOut())
    outcome = await ch.finalize_response_stream(_admitted(_long_rich(400), LINE), ctx, on_token)
    assert outcome is DeliveryOutcome.UNKNOWN
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts[0] == LINE and texts.count(LINE) == 1
    assert not any(t.startswith("Casa:") for t in texts[1:])


async def test_no_line_message_when_nothing_follows_a_failed_single_page_edit():
    """Astra design r5 5(b), the narrower rule: the line message exists to
    precede overflow sends; a failed single-page edit has none, so nothing is
    sent (the streamed text on screen already carried the line)."""
    from telegram.error import TelegramError

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("boom"))
    outcome = await ch.finalize_response_stream(_admitted("**short** and rich", LINE), ctx, on_token)
    assert outcome is not DeliveryOutcome.DELIVERED
    assert bot.send_message.await_count == 0
