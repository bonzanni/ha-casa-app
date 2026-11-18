"""The one inline message a specialist's bot owns in a group it is not a
member of: the placeholder its guest query is answered with, the thinking
ticker that keeps it alive, and the single terminal edit that replaces it.

A fake bot records every ``answer_guest_query`` / ``edit_message_text`` /
``send_message`` call, and every raw ``editMessageText`` (the terminal edit,
a rich message) beside the plain edits, and raises a scripted sequence where
a test asks for it. The module's own ``_sleep`` and ``_now`` are patched to record waits
rather than sleeping — never ``asyncio.sleep`` itself, which is the shared
module attribute.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace as NS

import pytest
from telegram import MessageEntity, SentGuestMessage
from telegram.error import BadRequest, NetworkError, RetryAfter

import channels.guest_reply as guest_reply
from channels import DeliveryOutcome
from channels.guest_reply import GuestReply
from error_kinds import ErrorKind
from channels.tg_rich import RICH_BUDGET_BYTES, prepare
from guest_groups import THINKING_WORDS
from output_boundary import casa_text
from output_boundary_testing import admitted

pytestmark = [pytest.mark.asyncio]

NAME = "Judge"
WAITING = "I've asked you something in our chat."
ASKED_ELSEWHERE = "I've asked you something in our chat — I'll answer there."
DECLINE = "Not something I can help with here."
FAIL = "Something went wrong on my side — ask me again in a bit."


def shown(edit: dict) -> str:
    """The text an edit shows: a plain edit's ``text``, a rich edit's
    Markdown, or its plain blocks joined by blank lines."""
    if "text" in edit:
        return edit["text"]
    rich = edit["rich_message"]
    if "markdown" in rich:
        return rich["markdown"]
    return "\n\n".join(block["text"] for block in rich["blocks"])


class _Bot:
    """A Bot API stand-in. ``edits`` holds every edit of a message in order,
    plain (``edit_message_text``) or rich (a raw ``editMessageText``), and
    ``edits`` scripts what each successive one does: ``None`` succeeds, an
    exception is raised; once the script is spent every edit succeeds.
    ``rich`` holds every other raw call."""

    def __init__(self, edits=()):
        self.answers: list[dict] = []
        self.edits: list[dict] = []
        self.sent: list[dict] = []
        self.drafts: list[dict] = []
        self.rich: list[tuple[str, dict]] = []      # (endpoint, api_kwargs) of each raw send
        self.script = list(edits)
        self.answer_error: Exception | None = None
        self._n = 0

    async def answer_guest_query(self, guest_query_id, result):
        self.answers.append({"guest_query_id": guest_query_id, "result": result})
        if self.answer_error is not None:
            raise self.answer_error
        self._n += 1
        return SentGuestMessage(inline_message_id=f"inl-{self._n}")

    async def edit_message_text(self, **kw):
        return await self._edit(kw)

    async def _edit(self, kw: dict):
        self.edits.append(kw)
        if self.script:
            exc = self.script.pop(0)
            if exc is not None:
                raise exc
        return True

    async def send_message(self, **kw):
        self.sent.append(kw)
        return NS(message_id=len(self.sent))

    async def send_message_draft(self, **kw):
        self.drafts.append(kw)
        return True

    async def do_api_request(self, endpoint, *, api_kwargs, return_type=None):
        if endpoint == "editMessageText":
            return await self._edit(dict(api_kwargs))
        self.rich.append((endpoint, api_kwargs))
        return NS(message_id=len(self.rich))


class _SlowBot(_Bot):
    """A bot whose edits are issued at once (recorded in ``edits``, as
    Telegram receives them) but answered only when the test says so:
    ``in_flight`` counts edits issued and not yet answered, ``most_in_flight``
    the most there ever were at once. A cancelled caller abandons its wait,
    as an abandoned request does; the edit stays issued."""

    def __init__(self):
        super().__init__()
        self.in_flight = 0
        self.most_in_flight = 0
        self._pending: list[asyncio.Future] = []

    async def _edit(self, kw: dict):
        self.edits.append(kw)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        fut = asyncio.get_running_loop().create_future()
        self._pending.append(fut)
        try:
            await fut
        finally:
            self.in_flight -= 1
            if fut in self._pending:
                self._pending.remove(fut)
        return True

    def answer(self):
        """Answer the oldest edit still awaiting its response."""
        self._pending.pop(0).set_result(True)


async def _settle(n: int = 5):
    for _ in range(n):
        await asyncio.sleep(0)


def _reply(bot, *, key="q1", **kw) -> GuestReply:
    return GuestReply(bot, guest_query_id=f"gq-{key}", name=NAME, key=key, **kw)


async def _opened(bot, **kw) -> GuestReply:
    r = _reply(bot, **kw)
    assert await r.open() is True
    return r


def _patch_waits(monkeypatch):
    """Record every wait instead of sleeping; a fake clock advances by it."""
    waits: list[float] = []
    clock = {"t": 0.0}
    real_sleep = asyncio.sleep

    async def sleep(s):
        waits.append(s)
        clock["t"] += s
        await real_sleep(0)

    monkeypatch.setattr(guest_reply, "_sleep", sleep)
    monkeypatch.setattr(guest_reply, "_now", lambda: clock["t"])
    return waits


class _Ticks:
    """Drives the ticker by hand: the module's ``_sleep`` records its wait and
    blocks until the test grants one permit, so no real time elapses."""

    def __init__(self, monkeypatch):
        self.waits: list[float] = []
        self._permits = asyncio.Semaphore(0)
        monkeypatch.setattr(guest_reply, "_sleep", self._sleep)

    async def _sleep(self, s):
        self.waits.append(s)
        await self._permits.acquire()

    async def tick(self, n: int = 1):
        for _ in range(n):
            self._permits.release()
            for _ in range(5):          # the ticker wakes, edits, sleeps again
                await asyncio.sleep(0)


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name == "channels.guest_reply" and r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# open, ticker, waiting
# ---------------------------------------------------------------------------

async def test_open_answers_with_the_placeholder_and_keeps_the_inline_id():
    bot = _Bot()
    r = _reply(bot)
    assert await r.open() is True
    assert r.inline_message_id == "inl-1"
    assert len(bot.answers) == 1
    assert bot.answers[0]["guest_query_id"] == "gq-q1"
    content = bot.answers[0]["result"].input_message_content
    assert content.message_text == "Judge · thinking…"
    assert [(e.type, e.offset, e.length) for e in content.entities] == [
        (MessageEntity.BOLD, 0, 5)]
    assert r.meta == {}
    assert r.terminal is False


async def test_open_reports_a_refused_answer(caplog):
    bot = _Bot()
    bot.answer_error = BadRequest("query is too old")
    r = _reply(bot)
    with caplog.at_level(logging.WARNING):
        assert await r.open() is False
    assert r.inline_message_id is None
    assert len(_warnings(caplog)) == 1


async def test_open_final_answers_once_and_is_terminal(monkeypatch):
    """A reply whose first message is also its last (the busy line): the
    query is answered with the text itself, no ticker starts, and every
    terminal call afterwards returns the delivered outcome without an edit."""
    ticks = _Ticks(monkeypatch)
    bot = _Bot()
    r = _reply(bot)
    assert await r.open_final("Busy right now — ask me again in a moment.") is True
    assert [a["guest_query_id"] for a in bot.answers] == ["gq-q1"]
    content = bot.answers[0]["result"].input_message_content
    assert content.message_text == "Busy right now — ask me again in a moment."
    assert r.inline_message_id == "inl-1"
    assert r.terminal is True
    r.start_ticker()
    await ticks.tick(3)
    assert ticks.waits == []                       # no ticker ever slept
    assert await r.finish(admitted("late")) is DeliveryOutcome.DELIVERED
    assert await r.decline() is DeliveryOutcome.DELIVERED
    assert await r.fail() is DeliveryOutcome.DELIVERED
    await r.waiting()
    assert bot.edits == []
    assert len(bot.answers) == 1


async def test_open_final_refused_sets_nothing(caplog):
    bot = _Bot()
    bot.answer_error = BadRequest("query is too old")
    r = _reply(bot)
    with caplog.at_level(logging.WARNING):
        assert await r.open_final("Busy right now — ask me again in a moment.") is False
    assert r.inline_message_id is None and r.terminal is False
    assert len(_warnings(caplog)) == 1


async def test_the_ticker_rotates_words_and_stops_at_finish(monkeypatch):
    ticks = _Ticks(monkeypatch)
    bot = _Bot()
    r = await _opened(bot, tick_s=3.0)
    r.start_ticker()
    await ticks.tick(6)
    assert ticks.waits == [3.0] * 7              # six ticks made, the seventh pending
    texts = [shown(e) for e in bot.edits]
    assert len(texts) == 6
    words = [t[len("Judge · "):-1] for t in texts]
    assert words == list(THINKING_WORDS[1:]) + [THINKING_WORDS[0], THINKING_WORDS[1]]
    assert all(e["inline_message_id"] == "inl-1" for e in bot.edits)
    assert await r.finish(admitted("done")) is DeliveryOutcome.DELIVERED
    assert shown(bot.edits[-1]) == "done"
    n = len(bot.edits)
    await ticks.tick(3)
    assert len(bot.edits) == n
    assert ticks.waits == [3.0] * 7              # the cancelled ticker never slept again


async def test_the_ticker_stops_by_itself_after_twice_the_terminal_budget(monkeypatch):
    """A reply no turn ever finishes (its route lost) must not tick forever:
    after twice the terminal budget without a terminal the ticker ends on its
    own, and the message keeps the last word it showed."""
    ticks = _Ticks(monkeypatch)
    bot = _Bot()
    r = await _opened(bot, tick_s=3.0, terminal_budget_s=6.0)
    r.start_ticker()
    await ticks.tick(8)
    # ticks at 3, 6, 9 and 12 s edit; the one that would wake at 15 s > 12 s ends it
    assert len(bot.edits) == 4
    assert ticks.waits == [3.0] * 5
    assert r.terminal is False                   # nothing terminal happened
    await ticks.tick(3)
    assert len(bot.edits) == 4 and ticks.waits == [3.0] * 5
    assert await r.finish(admitted("late")) is DeliveryOutcome.DELIVERED


async def test_finish_is_the_only_terminal_edit():
    bot = _Bot()
    r = await _opened(bot)
    assert await r.finish(admitted("the answer")) is DeliveryOutcome.DELIVERED
    assert await r.decline() is DeliveryOutcome.DELIVERED
    assert await r.fail() is DeliveryOutcome.DELIVERED
    assert await r.asked_elsewhere() is DeliveryOutcome.DELIVERED
    assert [shown(e) for e in bot.edits] == ["the answer"]
    assert r.terminal is True


async def test_waiting_is_not_terminal(monkeypatch):
    ticks = _Ticks(monkeypatch)
    bot = _Bot()
    r = await _opened(bot)
    r.start_ticker()
    await ticks.tick(1)
    assert len(bot.edits) == 1
    await r.waiting()
    assert shown(bot.edits[-1]) == WAITING
    assert r.terminal is False
    n = len(bot.edits)
    await ticks.tick(3)
    assert len(bot.edits) == n            # the ticker stopped at waiting()
    assert await r.finish(admitted("final text")) is DeliveryOutcome.DELIVERED
    assert shown(bot.edits[-1]) == "final text"


async def test_waiting_after_the_terminal_edits_nothing():
    bot = _Bot()
    r = await _opened(bot)
    await r.decline()
    await r.waiting()
    assert [shown(e) for e in bot.edits] == [DECLINE]


# ---------------------------------------------------------------------------
# the terminal edit: retries, budget, outcomes
# ---------------------------------------------------------------------------

async def test_terminal_edit_waits_the_full_retry_after(monkeypatch):
    waits = _patch_waits(monkeypatch)
    bot = _Bot(edits=[RetryAfter(30), None])
    r = await _opened(bot)
    assert await r.finish(admitted("the answer")) is DeliveryOutcome.DELIVERED
    assert waits == [30.0]
    assert [shown(e) for e in bot.edits] == ["the answer", "the answer"]


async def test_network_errors_back_off_doubling(monkeypatch):
    waits = _patch_waits(monkeypatch)
    bot = _Bot(edits=[NetworkError("a"), NetworkError("b"), NetworkError("c"), None])
    r = await _opened(bot)
    assert await r.finish(admitted("the answer")) is DeliveryOutcome.DELIVERED
    assert waits == [1.0, 2.0, 4.0]
    assert len(bot.edits) == 4


async def test_terminal_budget_expiry_leaves_one_warning_and_no_more_edits(
        monkeypatch, caplog):
    waits = _patch_waits(monkeypatch)
    bot = _Bot(edits=[NetworkError("down")] * 50)
    r = await _opened(bot, terminal_budget_s=10.0)
    with caplog.at_level(logging.WARNING):
        assert await r.finish(admitted("the answer")) is DeliveryOutcome.NOT_DELIVERED
    # attempts at 0, 1, 3 and 7 s; the next wait (8 s) would pass the budget
    assert waits == [1.0, 2.0, 4.0]
    assert len(bot.edits) == 4
    assert len(_warnings(caplog)) == 1
    assert await r.fail() is DeliveryOutcome.NOT_DELIVERED
    assert len(bot.edits) == 4


async def test_not_modified_is_success_and_not_found_stops():
    bot = _Bot(edits=[BadRequest("Message is not modified")])
    r = await _opened(bot)
    assert await r.finish(admitted("same")) is DeliveryOutcome.DELIVERED
    assert len(bot.edits) == 1

    bot2 = _Bot(edits=[BadRequest("Message to edit not found")])
    r2 = await _opened(bot2)
    assert await r2.finish(admitted("**gone**")) is DeliveryOutcome.NOT_DELIVERED
    assert len(bot2.edits) == 1               # no plain retry of a vanished message
    assert r2.terminal is True


async def test_a_refused_rich_edit_is_made_again_as_plain_blocks():
    bot = _Bot(edits=[BadRequest("Rich_message_markdown_invalid")])
    r = await _opened(bot)
    assert await r.finish(admitted("the **answer**")) is DeliveryOutcome.DELIVERED
    assert [e["rich_message"] for e in bot.edits] == [
        {"markdown": "the **answer**"},
        {"blocks": [{"type": "paragraph", "text": "the **answer**"}]}]
    assert {e["inline_message_id"] for e in bot.edits} == {"inl-1"}


async def test_an_unexpected_edit_error_yields_not_delivered_and_stops_the_ticker(
        caplog):
    # the rich edit, its plain blocks and the plain-text last resort, all refused
    bot = _Bot(edits=[BadRequest("something else")] * 3)
    r = await _opened(bot, tick_s=0.01)
    r.start_ticker()
    with caplog.at_level(logging.WARNING):
        assert await r.finish(admitted("the **answer**")) is DeliveryOutcome.NOT_DELIVERED
    first, last = _warnings(caplog)
    assert "rich edit refused" in first.getMessage()
    assert "terminal edit failed" in last.getMessage()
    assert r._ticker is None or r._ticker.done()
    n = len(bot.edits)
    await asyncio.sleep(0.05)
    assert len(bot.edits) == n


async def test_a_non_telegram_exception_is_contained(caplog):
    bot = _Bot(edits=[ValueError("bad surrogate")])
    r = await _opened(bot)
    with caplog.at_level(logging.WARNING):
        assert await r.fail() is DeliveryOutcome.NOT_DELIVERED
    assert len(_warnings(caplog)) == 1


# ---------------------------------------------------------------------------
# the terminal edit under cancellation and concurrency
# ---------------------------------------------------------------------------

async def test_a_cancelled_caller_lets_the_terminal_land_before_the_cancellation_goes_on(
        monkeypatch):
    """A turn torn down while its terminal edit waits out a flood wait must
    not leave the placeholder: the edit lands (within its budget), then the
    caller's cancellation goes on."""
    waits: list[float] = []
    waiting, release = asyncio.Event(), asyncio.Event()

    async def sleep(s):
        waits.append(s)
        waiting.set()
        await release.wait()
    monkeypatch.setattr(guest_reply, "_sleep", sleep)
    bot = _Bot(edits=[RetryAfter(30), None])
    r = await _opened(bot)
    caller = asyncio.create_task(r.finish(admitted("the answer")))
    await asyncio.wait_for(waiting.wait(), timeout=5)
    caller.cancel()
    await _settle()
    assert not caller.done()                    # the cancellation waits for the edit
    assert [shown(e) for e in bot.edits] == ["the answer"]        # the rejected one
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert waits == [30.0]
    assert [shown(e) for e in bot.edits] == ["the answer", "the answer"]
    assert r.terminal is True
    assert await r.decline() is DeliveryOutcome.DELIVERED        # the landed edit's outcome
    assert len(bot.edits) == 2


async def test_turn_finished_cancelled_during_its_edit_still_lands_it():
    bot = _SlowBot()
    ch = _channel(bot)
    _, ctx = await _bound(ch, bot, key="d")
    ctx["_turn_scope"] = NS(detached_ask=False)
    teardown = asyncio.create_task(ch.turn_finished(ctx))
    await _settle()
    assert bot.in_flight == 1
    teardown.cancel()
    await _settle()
    assert not teardown.done() and "d" in ch._guest_replies     # the binding waits too
    bot.answer()
    with pytest.raises(asyncio.CancelledError):
        await teardown
    assert [shown(e) for e in bot.edits] == [DECLINE]
    assert "d" not in ch._guest_replies


async def test_turn_finished_cancelled_repeatedly_during_its_edit_still_lands_it():
    """A second cancellation arriving while the first one waits for the edit
    must not let the caller go early: however often it is cancelled, the
    binding stays until the one edit has landed."""
    bot = _SlowBot()
    ch = _channel(bot)
    reply, ctx = await _bound(ch, bot, key="d")
    ctx["_turn_scope"] = NS(detached_ask=False)
    teardown = asyncio.create_task(ch.turn_finished(ctx))
    await _settle()
    assert bot.in_flight == 1
    for _ in range(3):
        teardown.cancel()
        await _settle()
        assert not teardown.done() and "d" in ch._guest_replies
    bot.answer()
    with pytest.raises(asyncio.CancelledError):
        await teardown
    assert [shown(e) for e in bot.edits] == [DECLINE]
    assert reply._terminal_task.done() and not reply._terminal_task.cancelled()
    assert "d" not in ch._guest_replies


@pytest.mark.parametrize("caller_cancelled_first", [False, True])
async def test_a_cancelled_terminal_edit_lets_its_caller_go_at_once(
        monkeypatch, caller_cancelled_first):
    """The edit's own task cancelled (the loop's shutdown sweep cancels every
    task) ends the wait: the caller leaves cancelled, at once, whether or not
    it was itself cancelled before."""
    waiting = asyncio.Event()

    async def sleep(s):
        waiting.set()
        await asyncio.Event().wait()            # a flood wait that never ends
    monkeypatch.setattr(guest_reply, "_sleep", sleep)
    bot = _Bot(edits=[RetryAfter(30), None])
    r = await _opened(bot)
    caller = asyncio.create_task(r.finish(admitted("the answer")))
    await asyncio.wait_for(waiting.wait(), timeout=5)
    if caller_cancelled_first:
        caller.cancel()
        await _settle()
        assert not caller.done()
    r._terminal_task.cancel()
    await _settle()
    assert caller.done() and caller.cancelled()
    assert [shown(e) for e in bot.edits] == ["the answer"]       # the rejected one only


async def test_two_terminal_calls_at_once_make_one_edit_and_share_its_outcome():
    bot = _SlowBot()
    r = await _opened(bot)
    finish = asyncio.create_task(r.finish(admitted("the answer")))
    decline = asyncio.create_task(r.decline())
    await _settle()
    assert [shown(e) for e in bot.edits] == ["the answer"]
    assert bot.in_flight == 1
    assert not decline.done()                   # the second waits for the first's edit
    bot.answer()
    assert await finish is DeliveryOutcome.DELIVERED
    assert await decline is DeliveryOutcome.DELIVERED
    assert [shown(e) for e in bot.edits] == ["the answer"]
    assert bot.most_in_flight == 1


async def test_a_terminal_waits_for_an_edit_still_in_flight():
    """Never two edits of the message in flight at once: a terminal that
    starts while the waiting line's edit is still unanswered issues its own
    only once that one has returned, so the final text is what stays."""
    bot = _SlowBot()
    r = await _opened(bot)
    waiting = asyncio.create_task(r.waiting())
    await _settle()
    assert [shown(e) for e in bot.edits] == [WAITING] and bot.in_flight == 1
    finish = asyncio.create_task(r.finish(admitted("the answer")))
    await _settle()
    assert [shown(e) for e in bot.edits] == [WAITING]       # the final edit not issued yet
    assert bot.in_flight == 1
    bot.answer()                                            # the waiting edit returns
    await waiting
    await _settle()
    assert [shown(e) for e in bot.edits] == [WAITING, "the answer"]
    bot.answer()
    assert await finish is DeliveryOutcome.DELIVERED
    assert bot.most_in_flight == 1


async def test_a_terminal_during_a_tick_in_flight_ends_the_ticker_and_lands_last(monkeypatch):
    """A tick whose edit is still unanswered when the terminal starts: the
    ticker is gone by the time the terminal returns, the final text is the
    last edit issued, and the two were never in flight together."""
    ticks = _Ticks(monkeypatch)
    bot = _SlowBot()
    r = await _opened(bot, tick_s=3.0)
    r.start_ticker()
    ticker = r._ticker
    await ticks.tick()
    assert len(bot.edits) == 1 and bot.in_flight == 1       # the tick's edit, unanswered
    finish = asyncio.create_task(r.finish(admitted("the answer")))
    await _settle()
    assert ticker.done()
    assert [shown(e) for e in bot.edits][-1] == "the answer"
    assert bot.in_flight == 1 and bot.most_in_flight == 1
    bot.answer()
    assert await finish is DeliveryOutcome.DELIVERED
    assert ticker.done() and r._ticker is None
    n = len(bot.edits)
    await ticks.tick(3)
    assert len(bot.edits) == n


# ---------------------------------------------------------------------------
# the answer as a rich message
# ---------------------------------------------------------------------------

async def test_guest_final_edit_is_rich_by_inline_message_id():
    bot = _Bot()
    r = await _opened(bot)
    assert await r.finish(admitted("## Rates\n\n| a | b |\n|---|---|\n| 1 | 2 |")) \
        is DeliveryOutcome.DELIVERED
    [edit] = bot.edits
    assert edit == {"inline_message_id": "inl-1", "rich_message": {
        "markdown": "## Rates\n\n| a | b |\n|---|---|\n| 1 | 2 |"}}
    assert bot.rich == []


async def test_guest_long_answer_not_cut_at_4096():
    bot = _Bot()
    r = await _opened(bot)
    source = ("word " * 1800).rstrip()
    assert len(source) > 8_900
    assert await r.finish(admitted(source)) is DeliveryOutcome.DELIVERED
    assert bot.edits[-1]["rich_message"] == {"markdown": source}


async def test_guest_multibyte_answer_cut_by_prepared_bytes():
    bot = _Bot()
    r = await _opened(bot)
    source = "\u00e8" * 20_000                         # 40 000 bytes
    assert await r.finish(admitted(source)) is DeliveryOutcome.DELIVERED
    [edit] = bot.edits
    markdown = edit["rich_message"]["markdown"]
    assert len(prepare(markdown).encode("utf-8")) <= RICH_BUDGET_BYTES
    assert markdown.endswith("…")
    assert markdown[:-1] == "\u00e8" * len(markdown[:-1])
    assert len(markdown.encode("utf-8")) > RICH_BUDGET_BYTES - 8


async def test_guest_501_row_table_falls_back_to_plain_blocks():
    bot = _Bot(edits=[BadRequest("Rich_message_blocks_too_many")])
    r = await _opened(bot)
    source = "| n | v |\n|---|---|\n" + "".join(f"| {i} | x |\n" for i in range(501))
    assert await r.finish(admitted(source)) is DeliveryOutcome.DELIVERED
    first, second = bot.edits
    assert first["rich_message"] == {"markdown": source}
    blocks = second["rich_message"]["blocks"]
    assert blocks == [{"type": "paragraph", "text": source}]
    assert second["inline_message_id"] == "inl-1"


class _RichRefusingBot(_Bot):
    """Every rich edit is refused; the plain edits land."""

    async def do_api_request(self, endpoint, *, api_kwargs, return_type=None):
        if endpoint == "editMessageText":
            self.edits.append(dict(api_kwargs))
            raise BadRequest("Rich_message_invalid")
        return await super().do_api_request(endpoint, api_kwargs=api_kwargs,
                                            return_type=return_type)


async def test_guest_final_edit_falls_back_to_plain_text_when_rich_is_refused(caplog):
    bot = _RichRefusingBot()
    r = await _opened(bot)
    with caplog.at_level(logging.WARNING):
        assert await r.finish(admitted("The rate is **1.08** today.")) \
            is DeliveryOutcome.DELIVERED
    # today's plain edit: a text with spans keeps its markers, the words all there
    plain = [e for e in bot.edits if "text" in e]
    assert plain == [{"inline_message_id": "inl-1", "text": "The rate is **1.08** today."}]
    assert [e["rich_message"] for e in bot.edits if "rich_message" in e] == [
        {"markdown": "The rate is **1.08** today."},
        {"blocks": [{"type": "paragraph", "text": "The rate is **1.08** today."}]}]
    assert sum("rich edit refused" in r.getMessage() for r in _warnings(caplog)) == 1


async def test_the_plain_last_resort_is_cut_at_4096_units():
    bot = _RichRefusingBot()
    r = await _opened(bot)
    assert await r.finish(admitted("\U0001F389" * 3000)) is DeliveryOutcome.DELIVERED
    [plain] = [e for e in bot.edits if "text" in e]
    assert plain["text"] == "\U0001F389" * 2047 + "…"


async def test_a_message_that_cannot_be_edited_gets_no_plain_last_resort():
    bot = _Bot(edits=[BadRequest("Message can't be edited")])
    r = await _opened(bot)
    assert await r.finish(admitted("the answer")) is DeliveryOutcome.NOT_DELIVERED
    assert len(bot.edits) == 1 and "text" not in bot.edits[0]


async def test_two_live_replies_do_not_share_state():
    bot = _Bot()
    a = await _opened(bot, key="a")
    b = await _opened(bot, key="b")
    assert (a.inline_message_id, b.inline_message_id) == ("inl-1", "inl-2")
    await a.finish(admitted("for a"))
    assert b.terminal is False
    await b.finish(admitted("for b"))
    assert [(e["inline_message_id"], shown(e)) for e in bot.edits] == [
        ("inl-1", "for a"), ("inl-2", "for b")]


# ---------------------------------------------------------------------------
# the endpoint seams
# ---------------------------------------------------------------------------

def _channel(bot):
    from unittest.mock import MagicMock
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot_token="222:t", chat_id="100", default_agent="finance",
                         bot=bot, is_casa=False, known_bot_id=222)
    ch._app = MagicMock(bot=bot)
    return ch


async def _bound(ch, bot, key="q1", chat_id="100"):
    reply = await _opened(bot, key=key)
    ch._guest_replies[key] = reply
    return reply, {"chat_id": chat_id, "_tg_bot": 222, "_guest_query": key, "cid": "c1"}


async def test_guest_finish_routes_text_and_errors():
    bot = _Bot()
    ch = _channel(bot)
    _, ctx = await _bound(ch, bot, key="q1")
    out = await ch.guest_finish(admitted("the **answer**"), None, ctx)
    assert out is DeliveryOutcome.DELIVERED
    assert bot.edits[-1]["rich_message"] == {"markdown": "the **answer**"}
    assert "q1" not in ch._guest_replies

    _, ctx2 = await _bound(ch, bot, key="q2")
    out = await ch.guest_finish(casa_text("Rate limited by the API."), ErrorKind.RATE_LIMIT, ctx2)
    assert out is DeliveryOutcome.DELIVERED
    assert shown(bot.edits[-1]) == FAIL
    assert bot.edits[-1]["inline_message_id"] == "inl-2"
    assert "q2" not in ch._guest_replies


async def test_guest_finish_refuses_unadmitted_text_and_fails_the_reply(caplog):
    bot = _Bot()
    ch = _channel(bot)
    _, ctx = await _bound(ch, bot)
    with caplog.at_level(logging.ERROR):
        out = await ch.guest_finish("bare model text", None, ctx)
    assert out is DeliveryOutcome.DELIVERED
    assert [shown(e) for e in bot.edits] == [FAIL]
    assert any("unadmitted model text refused: guest_finish" in r.message
               for r in caplog.records)


async def test_guest_finish_without_a_bound_reply_is_not_delivered():
    bot = _Bot()
    ch = _channel(bot)
    ctx = {"chat_id": "100", "_tg_bot": 222, "_guest_query": "missing"}
    assert await ch.guest_finish(admitted("x"), None, ctx) is DeliveryOutcome.NOT_DELIVERED
    assert bot.edits == [] and bot.sent == []


async def test_terminal_outcomes_propagate():
    bot = _Bot(edits=[BadRequest("Message to edit not found")])
    ch = _channel(bot)
    _, ctx = await _bound(ch, bot, key="gone")
    assert await ch.guest_finish(admitted("x"), None, ctx) is DeliveryOutcome.NOT_DELIVERED
    assert "gone" not in ch._guest_replies
    _, ctx2 = await _bound(ch, bot, key="ok")
    assert await ch.guest_finish(admitted("y"), None, ctx2) is DeliveryOutcome.DELIVERED


async def test_turn_finished_declines_or_says_ill_answer_there():
    bot = _Bot()
    ch = _channel(bot)
    _, ctx = await _bound(ch, bot, key="d")
    ctx["_turn_scope"] = NS(detached_ask=False)
    await ch.turn_finished(ctx)
    assert shown(bot.edits[-1]) == DECLINE
    assert "d" not in ch._guest_replies

    _, ctx2 = await _bound(ch, bot, key="a")
    ctx2["_turn_scope"] = NS(detached_ask=True)
    await ch.turn_finished(ctx2)
    assert shown(bot.edits[-1]) == ASKED_ELSEWHERE
    assert "a" not in ch._guest_replies

    # a scope without the attribute (built before it existed) declines
    _, ctx3 = await _bound(ch, bot, key="n")
    ctx3["_turn_scope"] = NS()
    await ch.turn_finished(ctx3)
    assert shown(bot.edits[-1]) == DECLINE


async def test_sends_after_the_terminal_go_to_the_dm():
    bot = _Bot()
    ch = _channel(bot)
    _, ctx = await _bound(ch, bot, chat_id="100")
    await ch.guest_finish(admitted("the answer"), None, ctx)
    n = len(bot.edits)
    assert await ch.send(casa_text("Casa's own line"), ctx) is DeliveryOutcome.DELIVERED
    assert len(bot.edits) == n
    assert bot.sent == []
    assert [(e, kw["chat_id"], kw["rich_message"]) for e, kw in bot.rich] == [
        ("sendRichMessage", 100, {"markdown": "Casa's own line"})]


async def test_create_on_token_is_none_for_a_bound_turn():
    bot = _Bot()
    ch = _channel(bot)
    _, ctx = await _bound(ch, bot)
    assert ch.create_on_token(ctx) is None
    assert callable(ch.create_on_token({"chat_id": "100", "_tg_bot": 222}))
    # after the terminal the key is gone: an ordinary DM turn streams again
    await ch.guest_finish(admitted("x"), None, ctx)
    assert callable(ch.create_on_token(ctx))
