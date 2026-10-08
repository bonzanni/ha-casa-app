"""#1350: one step log per in-process executor turn (a configurator topic).

A configurator turn runs several tools between the words it writes, and the
topic used to sit silent for 30–55 s at a stretch. This line records the
turn's steps as they start and is closed by the same turn:

- the first tool use posts ``🔧 This turn so far: 1 step — latest: <activity> (at 0s)``;
- later steps edit it to the newest count, activity and time;
- the turn's end edits it once more to ``☑ This turn: N steps in <elapsed>``,
  or to ``✖ This turn stopped after N steps`` when the turn raised.

Every text states something that has already happened when it is written, so
it stays true whatever follows: a close that fails or never runs leaves a
true "so far … latest …" record, never a claim about the present (#1350 d1/d2:
lines claiming current state went false on reachable paths). Nothing is
persisted. Sends and edits run in one background task this object owns —
coalesced (the newest wanted text wins) and spaced — so the turn's message
loop never waits on Telegram, and each transport call is bounded.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from drivers.summary_controller import format_elapsed

logger = logging.getLogger(__name__)

_SPACING_S = 3.0      # at least this long between two writes of the line
_IO_TIMEOUT_S = 3.0   # bound on each Telegram call and on the close's drain


# #1350 x1: closes run here, never awaited by the turn — a hung Telegram must
# not delay the turn, and a cancellation landing while the log closes must not
# become the turn's outcome. Strong references until each close finishes.
_closing: set[asyncio.Task] = set()


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _steps(n: int) -> str:
    return f"{n} step" if n == 1 else f"{n} steps"


class TurnProgressLine:
    """The progress line of one turn. ``send(text)`` posts it and returns the
    message id (or None); ``edit(message_id, text)`` edits it."""

    def __init__(
        self, *,
        send: Callable[[str], Awaitable[int | None]],
        edit: Callable[[int, str], Awaitable[bool]],
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
        spacing_s: float = _SPACING_S,
        io_timeout_s: float = _IO_TIMEOUT_S,
    ) -> None:
        self._send = send
        self._edit = edit
        self._now = now
        self._sleep = sleep
        self._spacing_s = spacing_s
        self._io_timeout_s = io_timeout_s
        self._started = now()
        self._count = 0
        self._wanted: str | None = None
        self._shown: str | None = None
        self._message_id: int | None = None
        self._post_failed = False
        self._last_write: float | None = None
        self._pump: asyncio.Task | None = None
        self._elapsed_at_end: float | None = None

    def step(self, activity: str) -> None:
        """A tool use began. Never awaits."""
        self._count += 1
        elapsed = format_elapsed(self._now() - self._started)
        self._wanted = (f"🔧 This turn so far: {_steps(self._count)} — "
                        f"latest: {activity} (at {elapsed})")
        if self._pump is None or self._pump.done():
            self._pump = asyncio.ensure_future(self._run())

    async def _write(self, text: str) -> None:
        try:
            if self._message_id is None:
                mid = await asyncio.wait_for(self._send(text), self._io_timeout_s)
                if mid is None:
                    self._post_failed = True
                    return
                self._message_id = mid
            else:
                await asyncio.wait_for(
                    self._edit(self._message_id, text), self._io_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — progress is never the turn's fault
            if self._message_id is None:
                self._post_failed = True
            logger.info("turn progress line write failed: %s", type(exc).__name__)
            return
        finally:
            self._last_write = self._now()
        self._shown = text

    async def _run(self) -> None:
        while (self._wanted is not None and self._wanted != self._shown
               and not self._post_failed):
            if self._last_write is not None:
                wait = self._spacing_s - (self._now() - self._last_write)
                if wait > 0:
                    await self._sleep(wait)
            text = self._wanted
            await self._write(text)
            if self._shown != text and self._message_id is not None:
                return  # an edit failed: leave it to the next step or the close

    def finish(self, *, ok: bool) -> None:
        """The turn ended: close the log in the background. Never awaits, never
        raises; a turn with no tool use posted nothing and spawns nothing."""
        if self._count == 0:
            return
        # the turn's duration is its own, not its log's delivery delay (x2)
        self._elapsed_at_end = self._now() - self._started
        task = asyncio.ensure_future(self.close(ok=ok))
        _closing.add(task)
        task.add_done_callback(_closing.discard)

    async def close(self, *, ok: bool) -> None:
        """Drains the pending write (bounded), waits out the spacing, then
        writes the closing text once (bounded)."""
        if self._elapsed_at_end is None:
            self._elapsed_at_end = self._now() - self._started
        pump = self._pump
        if pump is not None and not pump.done():
            try:
                await asyncio.wait_for(asyncio.shield(pump), self._io_timeout_s)
            except asyncio.CancelledError:
                pump.cancel()
                raise
            except Exception:  # noqa: BLE001 — a timeout included
                pump.cancel()
                # settle it, so the spacing below counts its last write (x2)
                await asyncio.gather(pump, return_exceptions=True)
        if self._message_id is None:
            return
        if self._last_write is not None:
            wait = self._spacing_s - (self._now() - self._last_write)
            if wait > 0:
                await self._sleep(wait)
        if ok:
            text = (f"☑ This turn: {_steps(self._count)} in "
                    f"{format_elapsed(self._elapsed_at_end)}")
        else:
            text = f"✖ This turn stopped after {_steps(self._count)}"
        try:
            await asyncio.wait_for(
                self._edit(self._message_id, text), self._io_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("turn progress line close failed: %s", type(exc).__name__)
