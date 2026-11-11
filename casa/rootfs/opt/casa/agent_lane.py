"""One lane and one fault per executing agent.

An agent's plugin work runs one use at a time: a stored-call tap, a plugin
continuation, and a delegation to that agent all take the agent's **lane**.
The lane is a lock with a short queue in front of it. A place in the queue is
reserved before any task exists and released once, on admission or on any
earlier exit, so a use reserved earlier is never overtaken by one that reached
the lane sooner. At most :data:`QUEUE_MAX` uses wait.

The lane is keyed by the agent alone, never by a chat: the operator's private
chat has the same id on every bot, and one agent's plugin processes are one
set whichever chat asked for them.

An agent whose pinned run could not be confirmed terminated is **faulted**:
every later use of its lane is refused at once, and its plugin work is not
admitted, until Casa restarts (a restart is what clears it, so nothing here
persists). A run that is still unwinding past its runner's teardown bound
refuses the lane the same way, but only until it has ended, and it is not a
fault (no health row).
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import time
from typing import Any, Callable

QUEUE_MAX = 3
UNWINDING = "an earlier run is still unwinding"


class LaneBusy(Exception):
    """A use admitted by the lane found the agent's concurrency permit held by
    work outside the lane, or waited past its deadline; nothing ran."""


class LaneFaulted(Exception):
    """A use admitted by the lane found the agent faulted; nothing ran."""


def faulted_line(label: str) -> str:
    """The operator's line for an agent that is out of service until restart."""
    return f"{label} is out of service until Casa restarts."


@dataclasses.dataclass(frozen=True)
class Fault:
    """Why an agent is faulted: the pinned run whose termination stayed
    unconfirmed at its deadline, the plugin it ran, and when."""
    run_id: str
    plugin: str
    since: float
    reason: str


class FaultRegistry:
    """The faulted agents, by role, until restart empties the registry."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self.now = now
        self._faults: dict[str, Fault] = {}

    def mark(self, role: str, run: str, plugin: str, *, reason: str = "",
             since: "float | None" = None) -> Fault:
        """Fault *role*: the run *run* of *plugin* could not be confirmed
        terminated. The fault stands until restart."""
        fault = Fault(run_id=str(run), plugin=str(plugin or ""),
                      since=self.now() if since is None else float(since),
                      reason=reason or f"run {run}: termination unconfirmed")
        self._faults[role] = fault
        return fault

    def set(self, role: str, fault: "Fault | None") -> None:
        """Record *fault* for *role* as it is, or clear it with ``None``."""
        if fault is None:
            self._faults.pop(role, None)
        else:
            self._faults[role] = fault

    def faulted(self, role: str) -> "Fault | None":
        """The fault of *role*, or ``None``."""
        return self._faults.get(role)

    def roles(self) -> list[str]:
        return list(self._faults)

    def issues(self) -> list:
        """The standing health report's rows: one per faulted agent, against
        the plugin its pinned run ran, the time in the detail. Recomputed on
        every regeneration, so a row stands exactly as long as its fault (a
        restart clears both)."""
        from plugin_registry import PluginIssue
        rows = []
        for role, fault in self._faults.items():
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(fault.since))
            rows.append(PluginIssue(
                name=fault.plugin or "casa", target=f"specialist:{role}", stage="verify",
                reason_code="desk_faulted", detail=f"since {when}"))
        return rows

    def clear(self) -> None:
        self._faults.clear()


class Lane:
    """The lane of one executing agent: the lock that serialises its uses and
    the queue in front of it, in reservation order."""

    def __init__(self, role: str, faults: FaultRegistry) -> None:
        self.role = role
        self._faults = faults
        self.lock = asyncio.Lock()
        # The inner task of a use that left while it was still unwinding past
        # the bounded runner's teardown bound: the lane is refused until it ends.
        self._unwinding: "asyncio.Task | None" = None
        self._queue: "collections.deque[Reservation]" = collections.deque()
        self._changed = asyncio.Event()
        self._extra_waiting = 0      # tests that simulate a full queue set ``waiting``

    @property
    def fault(self) -> "Fault | None":
        return self._faults.faulted(self.role)

    @property
    def faulted(self) -> "str | None":
        """The fault's reason, ``UNWINDING`` while an earlier run still
        unwinds, or ``None``: the one truth every use checks."""
        fault = self._faults.faulted(self.role)
        if fault is not None:
            return fault.reason
        if self._unwinding is not None and not self._unwinding.done():
            return UNWINDING
        return None

    @property
    def waiting(self) -> int:
        return len(self._queue) + self._extra_waiting

    @waiting.setter
    def waiting(self, value: int) -> None:
        self._extra_waiting = max(int(value) - len(self._queue), 0)

    def reserve(self) -> "Reservation | None":
        """One place in the queue, or ``None`` when ``QUEUE_MAX`` are already
        waiting. The reservation is released once: on admission, or on any
        exit before it (a cancel while waiting, a task cancelled before its
        coroutine started: the holder's done-callback)."""
        if self.waiting >= QUEUE_MAX:
            return None
        reservation = Reservation(self)
        self._queue.append(reservation)
        return reservation

    def hold_unwinding(self, runs: list) -> None:
        """Called under the lock as a use leaves, before its permit is
        released: a run the bounded runner gave up waiting for refuses the
        lane until it ends. Synchronous, so no later use can enter first."""
        for run in runs:
            if not run.done():
                self._unwinding = run

    def _unreserve(self, reservation: "Reservation") -> None:
        try:
            self._queue.remove(reservation)
        except ValueError:
            return
        self._changed.set()

    @contextlib.asynccontextmanager
    async def use(self, reservation: "Reservation | None", deadline: "float | None" = None):
        """One use of the lane: wait until *reservation* is at the head of the
        queue (an abandoned place ahead is skipped as it is released), take
        the lock, release the place, yield ``True``; the lock is released on
        every exit. With *deadline* (a ``time.monotonic`` instant) the wait
        ends there: the place is released and the use yields ``False``,
        holding nothing."""
        async def _admit() -> None:
            if reservation is not None:
                while self._queue and self._queue[0] is not reservation and not reservation.released:
                    self._changed.clear()
                    await self._changed.wait()
            await self.lock.acquire()

        if deadline is None:
            await _admit()
        else:
            try:
                await asyncio.wait_for(_admit(), max(deadline - time.monotonic(), 0.0))
            except asyncio.TimeoutError:
                if reservation is not None:
                    reservation.release()
                yield False
                return
        try:
            if reservation is not None:
                reservation.release()
            yield True
        finally:
            self.lock.release()


class Reservation:
    """A place in a lane's queue; ``release`` is idempotent."""

    def __init__(self, lane: Lane) -> None:
        self._lane = lane
        self._released = False

    def release(self, *_ignored: Any) -> None:
        if self._released:
            return
        self._released = True
        self._lane._unreserve(self)

    @property
    def released(self) -> bool:
        return self._released


class LaneRegistry:
    """The lanes, one per executing agent, created on first use."""

    def __init__(self, faults: FaultRegistry) -> None:
        self._faults = faults
        self._lanes: dict[str, Lane] = {}

    def get(self, role: str) -> Lane:
        """The lane of *role*, created on first use."""
        if not isinstance(role, str) or not role:
            raise ValueError(f"not an agent role: {role!r}")
        lane = self._lanes.get(role)
        if lane is None:
            lane = Lane(role, self._faults)
            self._lanes[role] = lane
        return lane

    def clear(self) -> None:
        self._lanes.clear()


FAULTS = FaultRegistry()
LANES = LaneRegistry(FAULTS)


def reset(now: Callable[[], float] = time.time) -> None:
    """Start from no lanes and no faults, with *now* as the faults' clock.
    The registries stay the same objects, so every holder of ``LANES`` or
    ``FAULTS`` sees the reset."""
    LANES.clear()
    FAULTS.clear()
    FAULTS.now = now
