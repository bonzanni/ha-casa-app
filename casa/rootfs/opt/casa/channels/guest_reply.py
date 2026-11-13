"""The one inline message a specialist's bot owns in a group it is not a
member of.

When a bot is summoned in a group it does not belong to, Telegram lets it
answer the guest query exactly once, with one inline message, and edit that
message afterwards. A :class:`GuestReply` owns that message for one turn: it
answers the query at once with a placeholder ("**Name** · thinking…"), keeps
the placeholder alive with a slow ticker of thinking words while the agent
works, and then replaces it with exactly one terminal edit — the agent's
answer, a decline, a failure line, or a pointer to the chat where a question
was asked instead.

Everything that touches the message is serialized on one lock, the terminal
edit is attempted once per reply however many terminal methods are called,
and a terminal method never raises (``asyncio.CancelledError`` aside): the
ticker is always stopped and an outcome always established, so the caller
can treat the result like any other delivery outcome. The terminal edit
runs as the reply's own task: a caller cancelled while it waits (a turn torn
down during a flood wait), once or repeatedly, lets the edit land, or spend
its budget, before the cancellation goes on — the group is never left with
the placeholder.

Text length is Telegram's: 4096 UTF-16 units for the message. The source is
cut before rendering — the renderer drops every entity of a text whose
rendered form exceeds the limit, and a rendered display is never longer
than its source except for a table, whose padded cells can expand it — and
the rendered form is measured again afterwards, falling back to the plain
form when it does not fit.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from telegram import InlineQueryResultArticle, InputTextMessageContent
from telegram.error import BadRequest, NetworkError, RetryAfter

from channels import DeliveryOutcome
from channels.tg_richtext import plain_text, render
from guest_groups import (
    GUEST_TERMINAL_BUDGET_S, GUEST_TEXT_LIMIT, GUEST_TICK_S, THINKING_WORDS,
)
from text_util import utf16_len, utf16_prefix_end

logger = logging.getLogger(__name__)

# The waits and the clock, as module attributes so a test can record waits
# instead of sleeping without touching the shared ``asyncio.sleep``.
_sleep = asyncio.sleep
_now = time.monotonic

WAITING_LINE = "I've asked you something in our chat."
ASKED_ELSEWHERE_LINE = "I've asked you something in our chat — I'll answer there."
DECLINE_LINE = "Not something I can help with here."
FAIL_LINE = "Something went wrong on my side — ask me again in a bit."

# The first wait after a network error; each further one doubles it.
_NETWORK_BACKOFF_S = 1.0
# A cut at a whitespace keeps whole words, unless the last whitespace lies
# so early that it would discard more than a quarter of the message — then
# the cut lands at the limit itself.
_WHITESPACE_CUT_FLOOR = int(GUEST_TEXT_LIMIT * 0.75)


def _cut(text: str) -> str:
    """*text* shortened to fit one message with room for the ellipsis: cut
    at the last whitespace at or before ``GUEST_TEXT_LIMIT - 1`` UTF-16
    units when that keeps at least ``_WHITESPACE_CUT_FLOOR`` units, else at
    the limit itself (never inside a surrogate pair), with "…" appended. A
    text that fits is returned unchanged."""
    budget = GUEST_TEXT_LIMIT - 1
    if utf16_len(text) <= budget:
        return text
    end = utf16_prefix_end(text, 0, budget)
    cut = end
    for i in range(end - 1, -1, -1):
        if text[i].isspace():
            if utf16_len(text[:i]) >= _WHITESPACE_CUT_FLOOR:
                cut = i
            break
    return text[:cut].rstrip() + "…"


def _retry_after_seconds(exc: RetryAfter) -> float:
    ra = exc.retry_after
    return ra.total_seconds() if isinstance(ra, timedelta) else float(ra)


class GuestReply:
    """One guest query's inline message, from placeholder to terminal edit."""

    def __init__(
        self, bot: Any, *, guest_query_id: str, name: str, key: str,
        tick_s: float = GUEST_TICK_S,
        terminal_budget_s: float = GUEST_TERMINAL_BUDGET_S,
    ) -> None:
        self.bot = bot
        self.guest_query_id = guest_query_id
        self.name = name
        self.key = key
        self.tick_s = tick_s
        self.terminal_budget_s = terminal_budget_s
        self.inline_message_id: str | None = None
        # Facts about the summons the dispatcher records and the log reads
        # (who asked, in which group); nothing here depends on them.
        self.meta: dict[str, Any] = {}
        self._lane = asyncio.Lock()
        self._ticker: asyncio.Task | None = None
        # The terminal edit, once started: the reply's own task, so that a
        # caller's cancellation cannot abandon it (see ``_terminal``). A reply
        # opened final holds an already-completed future here instead.
        self._terminal_task: asyncio.Future | None = None
        self._done = False
        self._outcome: DeliveryOutcome | None = None

    # -- the placeholder and the ticker ------------------------------------

    def _line(self, word: str) -> tuple[str, Any]:
        return render(f"**{self.name}** · {word}…")

    async def open(self) -> bool:
        """Answer the guest query with the placeholder and keep the inline
        message id. False, logged, when Telegram refuses the answer."""
        return await self._answer(*self._line(THINKING_WORDS[0]))

    async def open_final(self, text: str) -> bool:
        """Answer the guest query with *text* as the reply's one and final
        message (the busy line): no placeholder, no ticker, no edit after it.
        The reply is terminal at once, its outcome delivered, so every later
        terminal call returns that outcome without touching the message.
        False, logged, with nothing set, when Telegram refuses the answer."""
        display, entities, _plain = self._forms(text)
        if not await self._answer(display, entities):
            return False
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        self._terminal_task = done
        self._done = True
        self._outcome = DeliveryOutcome.DELIVERED
        return True

    async def _answer(self, display: str, entities: Any) -> bool:
        """Answer the guest query with one inline message and keep its id."""
        result = InlineQueryResultArticle(
            id=uuid.uuid4().hex, title=self.name,
            input_message_content=InputTextMessageContent(display, entities=entities),
        )
        try:
            sent = await self.bot.answer_guest_query(self.guest_query_id, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the reply simply does not open
            logger.warning("guest reply for %s not opened: %s: %s",
                           self.name, type(exc).__name__, exc)
            return False
        self.inline_message_id = sent.inline_message_id
        return True

    def start_ticker(self) -> None:
        """Rotate the thinking word every ``tick_s`` until a terminal edit or
        :meth:`waiting` stops it."""
        if self._ticker is None and not self._done and self.inline_message_id:
            self._ticker = asyncio.get_running_loop().create_task(self._tick())

    async def _tick(self) -> None:
        i = 0
        while True:
            await _sleep(self.tick_s)
            i += 1
            # A reply no turn ever finishes must not tick forever: past twice
            # the terminal budget the ticker ends on its own.
            if i * self.tick_s > 2 * self.terminal_budget_s:
                logger.info("guest reply ticker for %s ended: no terminal edit within "
                            "%.0f s", self.name, 2 * self.terminal_budget_s)
                return
            async with self._lane:
                if self._done:
                    return
                display, entities = self._line(THINKING_WORDS[i % len(THINKING_WORDS)])
                try:
                    await self.bot.edit_message_text(
                        inline_message_id=self.inline_message_id,
                        text=display, entities=entities)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — the ticker is cosmetic
                    logger.info("guest reply ticker for %s stopped: %s: %s",
                                self.name, type(exc).__name__, exc)
                    return

    async def _stop_ticker(self) -> None:
        task, self._ticker = self._ticker, None
        if task is None or task.done():
            return
        task.cancel()
        # wait() reports the cancelled task without raising its cancellation;
        # our own cancellation still propagates from it
        await asyncio.wait({task})

    async def waiting(self) -> None:
        """The agent asked its question elsewhere and is waiting: stop the
        ticker and say so, once. Not terminal — the answer still follows."""
        await self._stop_ticker()
        async with self._lane:
            if self._done:
                return
            try:
                await self.bot.edit_message_text(
                    inline_message_id=self.inline_message_id, text=WAITING_LINE)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — informational edit
                logger.info("guest reply waiting line for %s not shown: %s: %s",
                            self.name, type(exc).__name__, exc)

    # -- the terminal edit ---------------------------------------------------

    @property
    def terminal(self) -> bool:
        return self._done

    async def finish(self, text: str) -> DeliveryOutcome:
        """The agent's answer, rendered, as the one terminal edit."""
        return await self._terminal(text)

    async def decline(self) -> DeliveryOutcome:
        return await self._terminal(DECLINE_LINE)

    async def fail(self) -> DeliveryOutcome:
        return await self._terminal(FAIL_LINE)

    async def asked_elsewhere(self) -> DeliveryOutcome:
        return await self._terminal(ASKED_ELSEWHERE_LINE)

    async def _terminal(self, text: str) -> DeliveryOutcome:
        """One terminal edit per reply: the first call makes it, every later
        call waits for it and returns its outcome. Never raises except on
        cancellation, and then only once the edit has landed or spent its
        budget: the edit runs as the reply's own task, out of reach of a
        caller cancelled mid-wait (a turn torn down during a flood wait), so
        the caller can remove its binding knowing the group's message is
        final. However often the caller is cancelled meanwhile, it keeps
        waiting — the budget bounds the wait — and the cancellation goes on
        once the edit is done. Only the edit's own task being cancelled (the
        loop shutting down) ends the wait early."""
        if not self._done:
            self._done = True
            self._outcome = DeliveryOutcome.NOT_DELIVERED
            self._terminal_task = asyncio.get_running_loop().create_task(
                self._deliver_terminal(text))
        task = self._terminal_task
        interrupted: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.done():
                    raise
                if interrupted is None:
                    interrupted = exc
        if interrupted is not None:
            raise interrupted
        return self._outcome or DeliveryOutcome.NOT_DELIVERED

    async def _deliver_terminal(self, text: str) -> None:
        """The terminal edit itself: the ticker stopped, then the edit on the
        lane, its outcome recorded (an edit that raises records a failure)."""
        await self._stop_ticker()
        async with self._lane:
            try:
                self._outcome = await self._edit_final(text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the outcome stands in for the raise
                logger.warning("guest reply for %s: terminal edit failed: %s: %s",
                               self.name, type(exc).__name__, exc)

    def _forms(self, text: str) -> tuple[str, Any, str]:
        """``(display, entities, plain)``: the rendered form of the cut source
        when it fits, else no entities; and the plain form, cut the same way,
        for the fallback edit."""
        plain = _cut(plain_text(text))
        display, entities = render(_cut(text))
        if entities is None or utf16_len(display) > GUEST_TEXT_LIMIT:
            return plain, None, plain
        shown = utf16_len(display)
        entities = [e for e in entities if e.offset + e.length <= shown]
        return display, entities, plain

    async def _edit_final(self, text: str) -> DeliveryOutcome:
        display, entities, plain = self._forms(text)
        started = _now()
        backoff = _NETWORK_BACKOFF_S
        rich = entities is not None
        while True:
            try:
                if rich:
                    try:
                        await self.bot.edit_message_text(
                            inline_message_id=self.inline_message_id,
                            text=display, entities=entities)
                        return DeliveryOutcome.DELIVERED
                    except BadRequest as exc:
                        low = str(exc).lower()
                        if "not modified" in low:
                            return DeliveryOutcome.DELIVERED
                        if "message to edit not found" in low:
                            return DeliveryOutcome.NOT_DELIVERED
                        # an entity refusal: the same edit, plain
                        logger.info("guest reply for %s: rich edit refused (%s); "
                                    "editing plain", self.name, exc)
                        rich = False
                await self.bot.edit_message_text(
                    inline_message_id=self.inline_message_id, text=plain)
                return DeliveryOutcome.DELIVERED
            except RetryAfter as exc:
                wait = _retry_after_seconds(exc)
            except BadRequest as exc:
                low = str(exc).lower()
                if "not modified" in low:
                    return DeliveryOutcome.DELIVERED
                if "message to edit not found" in low:
                    return DeliveryOutcome.NOT_DELIVERED
                raise
            except NetworkError:
                wait, backoff = backoff, backoff * 2
            if _now() - started + wait > self.terminal_budget_s:
                logger.warning("guest reply for %s: terminal edit not made within "
                               "%.0f s", self.name, self.terminal_budget_s)
                return DeliveryOutcome.NOT_DELIVERED
            await _sleep(wait)
