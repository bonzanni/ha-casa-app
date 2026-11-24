"""The clock of a live voice session: the deadlines that end it, or settle
its pending work, without any model.

The clock only keeps time. The session tells it what happened (attached, a
side spoke, a decision could not be made, work became pending) and the clock
calls ``fire(which)`` when a deadline passes:

- ``first_speech``: attached, and the agent has not said anything yet;
- ``silence``: neither side has spoken for a while;
- ``undecided``: the decision for the latest agent line could not be made
  and neither side has spoken since;
- ``max``: the session has lasted as long as one may;
- ``pending``: accepted work has not finished.

``fire`` returns true when the session is over, which stops the clock. A
``fire`` that returns false must have moved the deadline it was called for
(``silence`` is moved by the speech that cancelled it, ``pending`` by
:meth:`Clock.settled`).
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from live_voice.session import Timing


class Clock:
    def __init__(self, timing: Timing,
                 fire: Callable[[str], Awaitable[bool]]) -> None:
        self._timing = timing
        self._fire = fire
        self.started = 0.0
        self._last_speech = 0.0
        self._attached: float | None = None
        self._agent_spoke = False
        self._undecided: float | None = None
        self._pending: float | None = None
        # Set when a deadline may have moved earlier than the one waited on.
        self._changed = asyncio.Event()

    def start(self) -> None:
        """The session begins now; nothing has been heard yet."""
        self.started = self._last_speech = time.monotonic()

    def attached(self) -> None:
        """The control channel is up for the first time."""
        if self._attached is None:
            self._attached = time.monotonic()
            self._changed.set()

    def heard(self, who: str) -> None:
        """A side spoke: the silence starts again, and a decision that could
        not be made no longer matters."""
        self._last_speech = time.monotonic()
        self._undecided = None
        if who == "agent":
            self._agent_spoke = True

    def undecided(self) -> None:
        """The decision for the latest agent line could not be made."""
        self._undecided = time.monotonic()
        self._changed.set()

    def pending(self) -> None:
        """Accepted work has not finished. While it is awaited, quiet is
        expected: the silence starts again and ``undecided`` is dropped, so
        only ``pending`` and ``max`` can come due before it settles."""
        if self._pending is None:
            self._pending = self._last_speech = time.monotonic()
            self._undecided = None
            self._changed.set()

    def settled(self) -> None:
        """The pending work was settled."""
        self._pending = None

    def _due(self) -> tuple[float, str]:
        timing = self._timing
        due = [(self.started + timing.max_call_s, "max"),
               (self._last_speech + timing.silence_s, "silence")]
        if self._attached is not None and not self._agent_spoke:
            due.append((self._attached + timing.first_speech_s, "first_speech"))
        if self._undecided is not None:
            due.append((self._undecided + timing.undecided_silence_s, "undecided"))
        if self._pending is not None:
            due.append((self._pending + timing.pending_s, "pending"))
        return min(due)

    async def run(self) -> None:
        """Fire each deadline as it passes, until one ends the session."""
        while True:
            self._changed.clear()
            when, which = self._due()
            wait = when - time.monotonic()
            if wait <= 0:
                if await self._fire(which):
                    return
                continue
            try:
                async with asyncio.timeout(wait):
                    await self._changed.wait()
            except TimeoutError:
                pass
