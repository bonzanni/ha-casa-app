# casa/rootfs/opt/casa/recall_health.py
"""Recall telemetry + circuit breaker for the structured-recall call sites
(personality Task 11, GH #200).

``observed_recall`` wraps a typed-recall coroutine, gates it through a
per-``path`` :class:`RecallCircuitBreaker`, and records exactly one telemetry
event per outcome (hits / zero_hits / unavailable). An OPEN breaker fast-fails
BEFORE the wrapped coroutine is invoked by raising ``RecallUnavailable`` with
reason ``"circuit_open"`` — never a fabricated zero-hit — and a genuine
``RecallUnavailable`` from the operation itself is re-raised after recording,
so the three-outcome discipline is preserved end to end either way.

``RecallCircuitBreaker`` is a NEW breaker for the call sites that have no
breaker today (direct-tool, specialist-archive, query_engager,
executor-archive). It is VERIFIED distinct from — and never a replacement
for — ``agent.py``'s existing
per-``Agent`` ``_RecallBreaker``, which continues to gate the pre-turn
automatic recall exactly as it does today. Monotonic time is injected so tests
drive recovery without patching ``asyncio.sleep``.

Breakers are cached one-per-``path`` in a process-wide registry (module-level
``_PATH_BREAKERS``) so failures on one path never trip another.
``reset_recall_breakers()`` clears that registry — a test-only seam so
per-process breaker state never leaks across tests; call it from an autouse
fixture scoped to the tests that exercise it, never repo-wide.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from personality_types import RecallHit
from semantic_memory import RecallUnavailable

RECALL_BREAKER_FAILURE_THRESHOLD = 3
RECALL_BREAKER_RECOVERY_SECONDS = 30.0

# Process-wide telemetry sink cap. This is a continuously-running daemon —
# an unbounded list here would leak forever (one event per recall). Bounded
# to a few thousand: enough for a useful recent-history snapshot, small
# enough to never matter memory-wise (see the pytest-OOM scar tissue in
# CLAUDE.md re: unbounded process-wide state).
RECALL_TELEMETRY_MAX_EVENTS = 4096

# #205: "specialist_archive" split out of the generic "delegated" default so a
# specialist's memory read is distinguishable in telemetry and gets its own
# breaker. "delegated" remains the function default (and the label for any
# future caller that does not name a path), but has no production call site of
# its own today — every live caller names one.
RecallPath = Literal[
    "delegated", "direct_tool", "query_engager", "executor_archive",
    "specialist_archive",
]


@dataclass(frozen=True, slots=True)
class RecallTelemetryEvent:
    path: RecallPath
    outcome: Literal["hits", "zero_hits", "unavailable"]
    reason: str
    latency_ms: int
    hit_count: int


class RecallTelemetry:
    """Append-only in-memory sink of recall outcomes (no gating behaviour)."""

    def __init__(self) -> None:
        self._events: deque[RecallTelemetryEvent] = deque(maxlen=RECALL_TELEMETRY_MAX_EVENTS)

    def record(self, event: RecallTelemetryEvent) -> None:
        self._events.append(event)

    def snapshot(self) -> tuple[RecallTelemetryEvent, ...]:
        return tuple(self._events)


_DEFAULT_TELEMETRY = RecallTelemetry()


def default_telemetry() -> RecallTelemetry:
    """The process-wide telemetry sink the migrated call sites record to."""
    return _DEFAULT_TELEMETRY


async def observed_recall(
    *, path: RecallPath, telemetry: RecallTelemetry,
    operation: Callable[[], Awaitable[tuple[RecallHit, ...]]],
) -> tuple[RecallHit, ...]:
    """Run ``operation`` (a typed recall) through the per-``path`` circuit
    breaker, record one telemetry event, and return its hits.

    If the ``path`` breaker is OPEN, ``operation`` is never invoked: this
    fast-fails by raising ``RecallUnavailable("circuit_open")`` — the SAME
    typed unavailable outcome a live backend failure produces, never a
    fabricated zero-hit. Otherwise a ``RecallUnavailable`` (incl.
    ``RecallProtocolError``) from ``operation`` itself is recorded as
    ``outcome=unavailable``, counted as a breaker failure, and RE-RAISED —
    never swallowed into a zero-hit — so callers keep the
    unavailable-vs-zero-hit distinction. Any other outcome (hits OR zero
    hits — a genuine empty result is success) counts as a breaker success."""
    breaker = _breaker_for(path)
    started = time.monotonic()
    if not breaker.try_acquire():
        telemetry.record(RecallTelemetryEvent(
            path, "unavailable", "circuit_open",
            int((time.monotonic() - started) * 1000), 0,
        ))
        raise RecallUnavailable("circuit_open")
    try:
        hits = await operation()
    except RecallUnavailable as exc:
        breaker.failure()
        telemetry.record(RecallTelemetryEvent(
            path, "unavailable", exc.reason,
            int((time.monotonic() - started) * 1000), 0,
        ))
        raise
    except BaseException:
        # GH #356: cancellation (or a non-RecallUnavailable bug exception)
        # while awaiting a HALF-OPEN probe used to leave `_probe_in_flight`
        # set forever — every later call fast-failed `circuit_open` until
        # restart. Abandon the probe: clear the flag WITHOUT counting a
        # failure (a cancelled probe says nothing about backend health) and
        # without a telemetry event (no outcome occurred — the three-outcome
        # discipline stays intact).
        breaker.abandon()
        raise
    breaker.success()
    telemetry.record(RecallTelemetryEvent(
        path, "hits" if hits else "zero_hits", "ok",
        int((time.monotonic() - started) * 1000), len(hits),
    ))
    return hits


class RecallCircuitBreaker:
    """A NEW breaker for the call sites (direct-tool, specialist-archive,
    query_engager, executor-archive) that have no breaker today — VERIFIED
    distinct from, and never a replacement for, ``agent.py``'s existing
    per-``Agent`` ``_RecallBreaker``, which continues to gate the pre-turn
    automatic recall exactly as it does today."""

    def __init__(
        self, *, failure_threshold: int = RECALL_BREAKER_FAILURE_THRESHOLD,
        recovery_seconds: float = RECALL_BREAKER_RECOVERY_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._now = monotonic
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        if self._opened_at is None:
            return "closed"
        return "open" if self._now() - self._opened_at < self._recovery else "half_open"

    def try_acquire(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open" or self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def abandon(self) -> None:
        """Release a half-open probe slot with NO outcome recorded (GH #356).

        Called when the probe was cancelled (or died on a non-recall bug
        exception) before producing evidence either way: the flag is cleared
        so the next caller can probe, failures are not incremented, and an
        open breaker stays open on its existing clock. In the closed state
        the flag is already False — harmless no-op."""
        self._probe_in_flight = False

    def failure(self) -> None:
        was_half_open = self.state == "half_open"
        self._probe_in_flight = False
        self._failures += 1
        if was_half_open or self._failures >= self._threshold:
            self._opened_at = self._now()


# Process-wide, one breaker per RecallPath — a failing path (e.g. Hindsight
# down) must never trip an unrelated path. Lazily populated with the standard
# threshold/recovery policy (no per-path override — the same policy for every
# call site; do not invent new policy here).
_PATH_BREAKERS: dict[str, RecallCircuitBreaker] = {}


def _breaker_for(path: str) -> RecallCircuitBreaker:
    breaker = _PATH_BREAKERS.get(path)
    if breaker is None:
        breaker = RecallCircuitBreaker()
        _PATH_BREAKERS[path] = breaker
    return breaker


def reset_recall_breakers() -> None:
    """Test-only seam: drop all per-path breaker state.

    ``RecallCircuitBreaker`` instances are cached process-wide in
    ``_PATH_BREAKERS`` (keyed by path), so without this a breaker OPENed by
    one test would fast-fail an unrelated test's calls on the same path. Call
    from an autouse fixture scoped to the test file(s) that exercise gating —
    never repo-wide."""
    _PATH_BREAKERS.clear()
