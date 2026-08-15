# casa/rootfs/opt/casa/session_gate.py
"""#578/#579: the module-level, path-independent authority over a session key.

v0.66.0 made the SDK client pool the authority for three questions: who may run
a turn on session key K, how to join and close K's in-flight client, and what
flushes K's transcript. Only the last is genuinely the pool's. The first two are
properties of the KEY, which outlives any pool instance and is reachable by
paths that never enter a pool at all — a scheduled turn, a webhook one-shot, a
``PoolUnavailable`` fallback, or an ordinary turn on the far side of a reload
that replaced the pool underneath it. Every such path was uncovered:

- #579: a reload builds a fresh Agent with its OWN pool while a turn still runs
  on the old one, so two clients could resume one session id. The pool entry
  lock serializes within one pool; across a reload there are two.
- #578: a memory wipe joined in-flight turns through the pool's reset listener,
  so a turn the pool never owned was not joined, and re-armed the session
  pointer after the wipe reported completion.

Both are answered by authorities that live at module scope, where a reload
cannot replace them and no turn path can route around them:

- :func:`session_write_gate` — per-``channel_key`` serialization, held across
  the SDK attempt AND the registry publish. Introduced by #573 for scheduled
  turns only, in :mod:`agent`; now unconditional and moved here so
  :mod:`memory_wipe` and :mod:`session_saver` can import it without the
  lazy-import cycle those modules already work around.
- :class:`TurnAdmission` — process-wide, over turns. A retirement takes the
  exclusive side and drains every in-flight turn regardless of path. This is
  what covers a FIRST-EVER turn on a key: it holds admission before it has any
  registry entry, so enumerating the registry cannot see it but the drain can.

Lock order, global and mandatory:

    TurnAdmission -> session_write_gate -> RetainFence -> pool entry lock

Never the reverse. An earlier revision of this design had a retirement take the
RetainFence before a session gate while ``/new`` took them the other way round,
which is a permanent resident deadlock.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from rw_barrier import RwBarrier

# How long a retirement waits for in-flight turns before giving up. Generous:
# the slowest legitimate turn is a full SDK exchange. A turn that outlasts it
# makes the retirement report failure with nothing deleted, which is the
# fail-closed half of the trade — the alternative is a wipe that claims success
# while a live turn still holds a pre-wipe transcript.
_ADMISSION_DRAIN_TIMEOUT_S = 60.0


# #573: one writer per session key.
#
# Refcounted rather than a plain dict-of-locks: the key space is per-boot
# unbounded, and an entry that nothing holds is deleted rather than retained.
# The bookkeeping below contains no awaits, so a cancelled waiter cannot
# interleave inside it and strand an entry.
_SESSION_GATES: "dict[str, list]" = {}


@asynccontextmanager
async def session_write_gate(channel_key: str):
    entry = _SESSION_GATES.get(channel_key)
    if entry is None:
        entry = [asyncio.Lock(), 0]
        _SESSION_GATES[channel_key] = entry
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] <= 0 and _SESSION_GATES.get(channel_key) is entry:
            del _SESSION_GATES[channel_key]


class AdmissionTimeout(Exception):
    """In-flight turns did not drain; the caller must NOT proceed to destroy
    anything. Translated by each caller into its own report."""


class TurnAdmission:
    """Process-wide readers/writer barrier over turns.

    Turns hold the shared side (:meth:`admitted`) across their SDK attempt and
    registry publish; a retirement holds the exclusive side
    (:meth:`exclusive`), which refuses new turns and drains the admitted ones.

    Not a per-key lock and not a global turn lock: admitted turns overlap
    freely with each other. It exists solely so a retirement can establish
    "no turn is running, and none can start" — the state the pool's reset
    listener could only ever establish for the turns the pool itself owned.
    """

    def __init__(self, drain_timeout_s: float = _ADMISSION_DRAIN_TIMEOUT_S) -> None:
        self._barrier = RwBarrier()
        self._drain_timeout_s = drain_timeout_s

    @asynccontextmanager
    async def admitted(self):
        """``async with TURN_ADMISSION.admitted():`` — the turn side."""
        await self._barrier.wait_for_exclusive_clear()
        # No await between the wait above and the increment below: a
        # cancellation in that gap would leave a participant no drain can see.
        self._barrier.enter_shared()
        try:
            yield
        finally:
            self._barrier.exit_shared()

    @asynccontextmanager
    async def exclusive(self):
        """``async with TURN_ADMISSION.exclusive():`` — the retirement side.

        Raises :class:`AdmissionTimeout` when in-flight turns do not drain,
        with exclusivity released and nothing changed.
        """
        try:
            await self._barrier.acquire_exclusive(
                drain_timeout_s=self._drain_timeout_s,
            )
        except (asyncio.TimeoutError, TimeoutError):
            raise AdmissionTimeout(
                "in-flight turns did not drain within "
                f"{self._drain_timeout_s:.0f}s; nothing was deleted"
            ) from None
        try:
            yield
        finally:
            self._barrier.release_exclusive()


# The process-wide instance (module singleton, like memory_wipe.FENCE): every
# turn and every retirement share exactly this one.
TURN_ADMISSION = TurnAdmission()
