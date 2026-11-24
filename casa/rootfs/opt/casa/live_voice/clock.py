"""The clock of a live voice session: the deadlines that end it, or settle
its pending work, without any model.

The clock only keeps time. The session tells it what happened (attached, a
side spoke, a decision could not be made, work became pending) and the clock
calls ``fire(which)`` when a deadline passes:

- ``first_speech``: attached, and the agent has not said anything yet;
- ``check_in``: nobody has been speaking for a while since someone was last
  heard; the agent is asked to check on the other side;
- ``silence``: nobody has been speaking for a while since that check-in;
- ``undecided``: the decision for the latest agent line could not be made
  and nobody has been speaking for a while since;
- ``max``: the session has lasted as long as one may;
- ``pending``: accepted work has not finished.

Three of these mean "nobody has been speaking for a while": ``check_in``,
``silence`` and ``undecided``. Each is counted from :meth:`Clock._silent_since`
of the moment it began, which is never before the agent's latest line has
been played to its end: the text of a line arrives before its sound, and a
line still being heard is not silence. None of them keeps a start time of its
own that could lie inside a line, and when later text of the agent moves the
end of what is being played, all three move with it.

A silence has two steps and each comes once: ``check_in`` when it has lasted
``check_in_s``, then ``silence`` another ``silence_s`` later. Only the other
side's speech starts it again. What the agent says after the check-in (its
question whether someone is there) does not: it brings no second check-in,
and only keeps the closing ``silence`` from being counted before that line
has been played. From the check-in until the other side speaks ``undecided``
cannot come due, so nothing but ``silence`` and ``max`` ends a session that
was checked on. Before the agent has said anything there is no silence to
check on; ``first_speech`` covers that. While work is pending neither step
can come due, and the silence starts afresh when that work is settled.

``fire`` returns true when the session is over, which stops the clock. A
``fire`` that returns false must have moved the deadline it was called for
(``check_in`` by :meth:`Clock.checked_in`, ``silence`` by the speech that
cancelled it, ``pending`` by :meth:`Clock.settled`).
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
        #: When a side was last heard (or pending work was settled);
        #: ``None`` until someone has spoken.
        self._heard: float | None = None
        #: When the agent's latest line has been played to its end.
        self._plays_until = 0.0
        #: When the agent was asked to check on the other side.
        self._checked_in: float | None = None
        self._attached: float | None = None
        self._agent_spoke = False
        self._undecided: float | None = None
        self._pending: float | None = None
        # Set when a deadline may have moved, so the wait is taken up anew.
        self._changed = asyncio.Event()

    def start(self) -> None:
        """The session begins now; nothing has been heard yet."""
        self.started = time.monotonic()

    def attached(self) -> None:
        """The control channel is up for the first time."""
        if self._attached is None:
            self._attached = time.monotonic()
            self._changed.set()

    def heard(self, who: str, ends: float | None = None) -> None:
        """A side spoke; *ends* (a ``time.monotonic()`` value) is when an
        agent line will have been played. A decision that could not be made
        no longer matters. Only the other side's words end a check-in."""
        self._heard = time.monotonic()
        self._undecided = None
        if who == "agent":
            self._agent_spoke = True
            self._plays_until = ends or 0.0
        else:
            self._checked_in = None
        self._changed.set()

    def checked_in(self) -> None:
        """The agent was asked to check on the other side: the second step
        of the silence starts now."""
        self._checked_in = time.monotonic()
        self._undecided = None

    def undecided(self) -> None:
        """The decision for the latest agent line could not be made. After a
        check-in that changes nothing: the silence alone ends the session."""
        if self._checked_in is None:
            self._undecided = time.monotonic()
            self._changed.set()

    def pending(self) -> None:
        """Accepted work has not finished. While it is awaited, quiet is
        expected: ``undecided`` is dropped and neither step of the silence
        can come due, so only ``pending`` and ``max`` can before it
        settles."""
        if self._pending is None:
            self._pending = time.monotonic()
            self._undecided = None
            self._changed.set()

    def settled(self) -> None:
        """The pending work was settled; the silence starts afresh."""
        self._pending = None
        self._checked_in = None
        self._heard = time.monotonic()

    def _silent_since(self, moment: float) -> float:
        """The moment from which nobody is speaking, for a quiet that began
        at *moment*: never before the agent's latest line has been played."""
        return max(moment, self._plays_until)

    def _due(self) -> tuple[float, str]:
        timing = self._timing
        due = [(self.started + timing.max_call_s, "max")]
        if self._pending is not None:
            due.append((self._pending + timing.pending_s, "pending"))
        elif self._checked_in is not None:
            due.append((self._silent_since(self._checked_in)
                        + timing.silence_s, "silence"))
        elif self._heard is not None and self._agent_spoke:
            due.append((self._silent_since(self._heard)
                        + timing.check_in_s, "check_in"))
        if self._attached is not None and not self._agent_spoke:
            due.append((self._attached + timing.first_speech_s, "first_speech"))
        if self._undecided is not None:
            due.append((self._silent_since(self._undecided)
                        + timing.undecided_silence_s, "undecided"))
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
