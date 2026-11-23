"""An agent's reply, and any text given to ``send``, goes out as a Telegram
rich message carrying the agent's Markdown."""
from __future__ import annotations

import pytest
from telegram.error import BadRequest, TimedOut

from channels import DeliveryOutcome
from channels.tg_rich import plain_blocks, prepare
from telegram_channel_double import _mk_channel_with_fake_bot
from output_boundary_testing import admitted

pytestmark = pytest.mark.asyncio


def _rich_calls(bot) -> list[dict]:
    calls = bot.do_api_request.call_args_list
    assert all(c.args[0] == "sendRichMessage" for c in calls)
    return [c.kwargs["api_kwargs"] for c in calls]


async def test_send_response_sends_the_agents_markdown_as_one_rich_message():
    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    assert await ch.send_response(admitted("**hi**"), ctx) is DeliveryOutcome.DELIVERED
    (kw,) = _rich_calls(bot)
    assert kw["rich_message"] == {"markdown": prepare("**hi**")}
    assert kw["chat_id"] == 42
    bot.send_message.assert_not_awaited()
    assert ctx["_delivery_head_sent"] is True


async def test_send_response_plain_text_is_a_rich_message_too():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_response(admitted("just text"), {"chat_id": "42"})
    (kw,) = _rich_calls(bot)
    assert kw["rich_message"] == {"markdown": "just text"}
    bot.send_message.assert_not_awaited()


async def test_send_response_badrequest_resends_the_text_unformatted():
    ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = [BadRequest("bad markdown"), None]
    await ch.send_response(admitted("**hi**"), {"chat_id": "42"})
    first, second = _rich_calls(bot)
    assert first["rich_message"] == {"markdown": prepare("**hi**")}
    assert second["rich_message"] == {"blocks": plain_blocks("**hi**")}


async def test_send_response_timedout_makes_exactly_one_attempt():
    ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = TimedOut("network")
    with pytest.raises(TimedOut):
        await ch.send_response(admitted("**hi**"), {"chat_id": "42"})
    assert bot.do_api_request.await_count == 1  # no duplicate on non-BadRequest


async def test_send_response_long_reply_is_one_message():
    ch, bot = _mk_channel_with_fake_bot()
    reply = "\n".join(f"line {i:04d} " + "x" * 80 for i in range(100))
    assert len(reply) > 9_000
    await ch.send_response(admitted(reply), {"chat_id": "42"})
    (kw,) = _rich_calls(bot)
    assert kw["rich_message"] == {"markdown": prepare(reply)}


async def test_send_response_blank_reply_sends_nothing():
    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    assert await ch.send_response(admitted("  \n "), ctx) is DeliveryOutcome.NOT_DELIVERED
    bot.do_api_request.assert_not_awaited()
    assert "_delivery_head_sent" not in ctx


async def test_send_tool_text_is_rich_and_keeps_long_link():
    ch, bot = _mk_channel_with_fake_bot()
    url = "https://example.com/invoices/INV-913"
    text = "a" * 4080 + " " + url
    ctx = {"chat_id": "42"}
    assert await ch.send(admitted(text), ctx) is DeliveryOutcome.DELIVERED
    (kw,) = _rich_calls(bot)
    assert url in kw["rich_message"]["markdown"]
    assert kw["rich_message"] == {"markdown": prepare(text)}
    bot.send_message.assert_not_awaited()
    assert ctx["_delivery_head_sent"] is True


async def test_send_of_blank_text_is_not_delivered():
    ch, bot = _mk_channel_with_fake_bot()
    assert await ch.send(admitted(" \n\n "), {"chat_id": "42"}) is DeliveryOutcome.NOT_DELIVERED
    bot.do_api_request.assert_not_awaited()


async def test_send_while_the_channel_is_down_sends_nothing():
    ch, bot = _mk_channel_with_fake_bot()
    ch._app = None
    assert await ch.send(admitted("hi"), {"chat_id": "42"}) is DeliveryOutcome.NOT_DELIVERED
    bot.do_api_request.assert_not_awaited()


# ---------------------------------------------------------------------------
# A task's start post and a job's question
# ---------------------------------------------------------------------------


async def test_task_start_post_is_one_silent_rich_message_quoting_the_request():
    ch, bot = _mk_channel_with_fake_bot()
    mid = await ch.post_task_start(42, "Ellen", admitted("Pay **rent** by Monday"), post=None)
    assert mid == 12345
    (kw,) = _rich_calls(bot)
    assert kw == {
        "chat_id": 42, "disable_notification": True,
        "rich_message": {"markdown": "**Ellen** asked:\n> Pay **rent** by Monday"}}
    bot.send_message.assert_not_awaited()


async def test_task_start_post_quotes_every_line_and_keeps_its_formatting():
    """A Markdown block quote: every line of the request behind ``>``, a blank
    line as ``>`` alone, so its paragraphs and formatting stay inside it."""
    ch, bot = _mk_channel_with_fake_bot()
    brief = "Pay **rent** by Monday.\n\nThen `check` the [bank](https://e.example/b)."
    await ch.post_task_start(42, "Ellen", admitted(brief), post=None)
    (kw,) = _rich_calls(bot)
    markdown = kw["rich_message"]["markdown"]
    assert markdown == ("**Ellen** asked:\n> Pay **rent** by Monday.\n>\n"
                        "> Then `check` the [bank](https://e.example/b).")
    assert "<blockquote" not in markdown and "\\*" not in markdown


async def test_a_task_too_long_for_one_message_is_clipped_to_fit():
    from channels.tg_rich import RICH_BUDGET_BYTES
    ch, bot = _mk_channel_with_fake_bot()
    task = "💶 " * 12_000                         # 60,000 bytes
    await ch.post_task_start(42, "Ellen", admitted(task), post=None)
    (kw,) = _rich_calls(bot)
    markdown = kw["rich_message"]["markdown"]
    assert len(markdown.encode()) <= RICH_BUDGET_BYTES
    assert markdown.startswith("**Ellen** asked:\n> 💶 ")
    assert markdown.endswith("…")
    assert markdown.count("\n") == 1                # one quoted line, clipped


async def test_a_task_of_many_lines_is_clipped_to_one_message():
    """#1662: every kept line gains its quote prefix, so the clip must fit the
    quoted form — a request of 800 short lines still arrives as ONE post."""
    from channels.tg_rich import RICH_BUDGET_BYTES, prepare
    ch, bot = _mk_channel_with_fake_bot()
    task = "\n".join(f"{i:04d} " + "x" * 45 for i in range(800))
    await ch.post_task_start(42, "Ellen", admitted(task), post=None)
    (kw,) = _rich_calls(bot)
    markdown = kw["rich_message"]["markdown"]
    assert len(prepare(markdown).encode()) <= RICH_BUDGET_BYTES
    assert markdown.startswith("**Ellen** asked:\n> 0000 x")
    assert markdown.endswith("…")


@pytest.mark.parametrize("task", [
    "\n" * 20_000 + "x",                 # mostly blank lines: the room runs out
    "\n".join("y" * 40_000),             # 40,000 one-character lines
    "\n".join("y" * 200_000),            # 200,000 one-character lines
], ids=["blank-lines", "one-char-lines", "200k-one-char-lines"])
async def test_a_degenerate_long_task_is_clipped_to_one_message_promptly(task):
    """#1662: a request whose quote prefixes outweigh its text still yields one
    post, and the clip, which runs on the event loop, ends. Run in a thread so
    a hang fails instead of stalling; the limit is wide because it is there to
    catch a clip that never ends, not to time a slow shared runner."""
    import threading
    from channels.telegram import TelegramChannel
    from channels.tg_rich import RICH_BUDGET_BYTES, prepare
    out = []
    worker = threading.Thread(target=lambda: out.append(
        TelegramChannel._task_start_markdown("Ellen", task)), daemon=True)
    worker.start()
    worker.join(timeout=120)
    assert out, "clipping the start post did not finish within 120 s"
    (markdown,) = out
    assert len(prepare(markdown).encode()) <= RICH_BUDGET_BYTES
    assert markdown.startswith("**Ellen** asked:\n>")
    assert markdown.endswith("…")


@pytest.mark.parametrize("task", [
    # whitespace-only lines shrink to ">" when quoted
    ("x" * 80 + "\n" + " " * 80 + "\n") * 200 + "END OF REQUEST",
    # CRLF line ends shrink in preparation
    "```\r\n" + ("x" * 60 + "\r\n") * 503 + "```\r\nEND OF REQUEST",
    # time links contract in preparation
    "\n".join(["[x](time:2026-11-16T09:00:00.123456+01:00)"] * 720),
], ids=["whitespace-lines", "crlf-fence", "time-links"])
async def test_a_long_request_that_fits_once_prepared_is_sent_whole(task):
    """#1662: clipping must never bite a request whose post fits: one whose raw
    size looks too big but shrinks when prepared is quoted whole, unclipped."""
    from channels.telegram import TelegramChannel
    from channels.tg_rich import RICH_BUDGET_BYTES, prepare
    markdown = TelegramChannel._task_start_markdown("Ellen", task)
    whole = "**Ellen** asked:\n" + "\n".join(
        f"> {line}" if line.strip() else ">" for line in task.split("\n"))
    same = markdown == whole               # no pytest diff of 30 kB strings
    assert same, f"clipped to {len(markdown)} of {len(whole)} characters"
    assert len(prepare(markdown).encode()) <= RICH_BUDGET_BYTES


async def test_a_long_task_whose_quoted_form_grows_still_shows_a_real_quote():
    """#1662: tab-indented lines of underscores grow when quoted and prepared;
    the clipped post is still one message, and with ample room it quotes a
    substantial part of the request, not the ellipsis alone."""
    from channels.tg_rich import RICH_BUDGET_BYTES
    ch, bot = _mk_channel_with_fake_bot()
    task = ("\t" + "_" * 80 + "\n") * 500
    await ch.post_task_start(42, "Ellen", admitted(task), post=None)
    (kw,) = _rich_calls(bot)
    markdown = kw["rich_message"]["markdown"]
    assert len(markdown.encode()) <= RICH_BUDGET_BYTES   # sent as prepared
    assert markdown.count("\n> \t") >= 20               # quoted request lines
    assert markdown.endswith("…")


async def test_a_task_start_post_that_fails_returns_none():
    ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = RuntimeError("telegram down")
    assert await ch.post_task_start(42, "Ellen", admitted("hi"), post=None) is None


def _job(chat=42):
    from types import SimpleNamespace
    return SimpleNamespace(id="job-1", delivery=SimpleNamespace(chat_id=chat), origin={})


async def test_job_question_is_a_rich_message_with_its_buttons():
    ch, bot = _mk_channel_with_fake_bot()
    body = "Finance asks: which **account**?"
    mid = await ch.post_job_question(_job(), "q1", admitted(body), ["a", "b"], ["A", "B"])
    assert mid == 12345
    (kw,) = _rich_calls(bot)
    assert kw["chat_id"] == 42
    assert kw["rich_message"] == {"markdown": prepare(body)}
    rows = kw["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in row] for row in rows] == [["A"], ["B"], ["Cancel job"]]
    bot.send_message.assert_not_awaited()


async def test_a_long_job_question_puts_its_buttons_on_the_last_message():
    from unittest.mock import MagicMock
    from channels.tg_rich import split
    ch, bot = _mk_channel_with_fake_bot()
    body = "a" * 20_000 + "\n\n" + "b" * 20_000
    assert len(split(body)) == 2
    bot.do_api_request.side_effect = [MagicMock(message_id=1), MagicMock(message_id=2)]
    mid = await ch.post_job_question(_job(), "q1", admitted(body), ["a"], ["A"])
    first, second = _rich_calls(bot)
    assert "reply_markup" not in first and "reply_markup" in second
    assert mid == 2


async def test_a_job_question_that_fails_returns_none():
    ch, bot = _mk_channel_with_fake_bot()
    bot.do_api_request.side_effect = RuntimeError("telegram down")
    assert await ch.post_job_question(_job(), "q1", admitted("Which?"), ["a"], ["A"]) is None
