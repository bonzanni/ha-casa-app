"""Personality Task 11: recall telemetry + the NEW circuit breaker.

`recall_health.observed_recall` wraps a recall coroutine and records one
telemetry event per outcome (hits / zero_hits / unavailable), re-raising
`RecallUnavailable` after recording. `RecallCircuitBreaker` is the NEW breaker
for the four previously-unbreakered call sites; it is DISTINCT from agent.py's
own `_RecallBreaker`. Monotonic time is injected — never patch asyncio.sleep.
"""
from __future__ import annotations

import asyncio

import pytest

from personality_types import RecallHit
from semantic_memory import RecallUnavailable

pytestmark = [pytest.mark.unit]


def _hit(text: str = "fact") -> RecallHit:
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=(), provenance=None, backend_id="b1", document_id=None,
        chunk_id=None, source_fact_ids=None, metadata=None, context=None,
        score=None,
    )


# ---------------------------------------------------------------------------
# observed_recall telemetry
# ---------------------------------------------------------------------------


async def test_observed_recall_records_hits() -> None:
    from recall_health import RecallTelemetry, observed_recall

    tel = RecallTelemetry()

    async def _op():
        return (_hit(), _hit())

    hits = await observed_recall(path="direct_tool", telemetry=tel, operation=_op)
    assert len(hits) == 2
    events = tel.snapshot()
    assert len(events) == 1
    assert events[0].path == "direct_tool"
    assert events[0].outcome == "hits"
    assert events[0].hit_count == 2


async def test_observed_recall_records_zero_hits() -> None:
    from recall_health import RecallTelemetry, observed_recall

    tel = RecallTelemetry()

    async def _op():
        return ()

    hits = await observed_recall(path="delegated", telemetry=tel, operation=_op)
    assert hits == ()
    events = tel.snapshot()
    assert events[0].outcome == "zero_hits"
    assert events[0].hit_count == 0


async def test_observed_recall_records_and_reraises_unavailable() -> None:
    from recall_health import RecallTelemetry, observed_recall

    tel = RecallTelemetry()

    async def _op():
        raise RecallUnavailable("http_504")

    with pytest.raises(RecallUnavailable):
        await observed_recall(path="query_engager", telemetry=tel, operation=_op)
    events = tel.snapshot()
    assert events[0].outcome == "unavailable"
    assert events[0].reason == "http_504"
    assert events[0].path == "query_engager"


# ---------------------------------------------------------------------------
# RecallCircuitBreaker — injected monotonic time
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_breaker_opens_after_threshold_failures() -> None:
    from recall_health import RecallCircuitBreaker

    clock = _Clock()
    br = RecallCircuitBreaker(failure_threshold=3, recovery_seconds=30.0, monotonic=clock)
    assert br.state == "closed"
    for _ in range(3):
        assert br.try_acquire() is True
        br.failure()
    assert br.state == "open"
    assert br.try_acquire() is False


def test_breaker_recovers_after_cooldown_and_success_closes() -> None:
    from recall_health import RecallCircuitBreaker

    clock = _Clock()
    br = RecallCircuitBreaker(failure_threshold=2, recovery_seconds=30.0, monotonic=clock)
    for _ in range(2):
        br.try_acquire()
        br.failure()
    assert br.state == "open"
    clock.advance(31.0)
    assert br.state == "half_open"
    # Exactly one probe admitted in half-open.
    assert br.try_acquire() is True
    assert br.try_acquire() is False
    br.success()
    assert br.state == "closed"


def test_breaker_half_open_failure_reopens() -> None:
    from recall_health import RecallCircuitBreaker

    clock = _Clock()
    br = RecallCircuitBreaker(failure_threshold=2, recovery_seconds=30.0, monotonic=clock)
    for _ in range(2):
        br.try_acquire()
        br.failure()
    clock.advance(31.0)
    assert br.state == "half_open"
    assert br.try_acquire() is True
    br.failure()          # probe failed
    assert br.state == "open"
    assert br.try_acquire() is False


def test_telemetry_buffer_is_capped_and_retains_most_recent() -> None:
    from recall_health import RECALL_TELEMETRY_MAX_EVENTS, RecallTelemetry, RecallTelemetryEvent

    tel = RecallTelemetry()
    total = RECALL_TELEMETRY_MAX_EVENTS + 100
    for i in range(total):
        tel.record(RecallTelemetryEvent(
            "direct_tool", "hits", "ok", 0, i,
        ))
    events = tel.snapshot()
    assert len(events) == RECALL_TELEMETRY_MAX_EVENTS
    # The oldest 100 (hit_count 0..99) were evicted; the retained window is
    # the most-recent RECALL_TELEMETRY_MAX_EVENTS, i.e. hit_count 100..total-1.
    assert events[0].hit_count == 100
    assert events[-1].hit_count == total - 1


def test_breaker_success_resets_failure_count() -> None:
    from recall_health import RecallCircuitBreaker

    clock = _Clock()
    br = RecallCircuitBreaker(failure_threshold=3, recovery_seconds=30.0, monotonic=clock)
    br.try_acquire(); br.failure()
    br.try_acquire(); br.failure()
    br.try_acquire(); br.success()   # resets
    br.try_acquire(); br.failure()
    br.try_acquire(); br.failure()
    assert br.state == "closed"      # only 2 consecutive since the reset


# ---------------------------------------------------------------------------
# GH #356: a cancelled (or bug-exception) half-open probe must not wedge the
# breaker — before the fix `_probe_in_flight` stayed True forever and every
# later call fast-failed `circuit_open` until restart.
# ---------------------------------------------------------------------------


def test_breaker_abandon_releases_probe_without_outcome() -> None:
    from recall_health import RecallCircuitBreaker

    clock = {"now": 0.0}
    br = RecallCircuitBreaker(
        failure_threshold=2, recovery_seconds=10.0, monotonic=lambda: clock["now"],
    )
    br.failure(); br.failure()
    assert br.state == "open"
    clock["now"] += 11.0
    assert br.state == "half_open"
    assert br.try_acquire() is True          # probe slot taken
    assert br.try_acquire() is False         # probe in flight
    br.abandon()
    # Slot released, no failure counted, breaker not re-opened: the NEXT
    # caller can probe immediately (still half_open on the same clock).
    assert br.state == "half_open"
    assert br.try_acquire() is True


@pytest.mark.parametrize("exc_type", [asyncio.CancelledError, RuntimeError])
async def test_cancelled_half_open_probe_does_not_wedge(exc_type) -> None:
    """Red case for GH #356: drive the per-path breaker to half-open, cancel
    the probe mid-await, then verify the path is probe-able again instead of
    fast-failing circuit_open forever."""
    import recall_health as rh
    from recall_health import RecallTelemetry, observed_recall

    rh.reset_recall_breakers()
    try:
        clock = {"now": 0.0}
        # Install a clock-driven breaker for the path under test.
        br = rh.RecallCircuitBreaker(monotonic=lambda: clock["now"])
        rh._PATH_BREAKERS["direct_tool"] = br
        tel = RecallTelemetry()

        async def _down():
            raise RecallUnavailable("backend_down")

        for _ in range(rh.RECALL_BREAKER_FAILURE_THRESHOLD):
            with pytest.raises(RecallUnavailable):
                await observed_recall(
                    path="direct_tool", telemetry=tel, operation=_down)
        assert br.state == "open"
        clock["now"] += rh.RECALL_BREAKER_RECOVERY_SECONDS + 1
        assert br.state == "half_open"

        async def _interrupted():
            raise exc_type("probe interrupted")

        with pytest.raises(exc_type):
            await observed_recall(
                path="direct_tool", telemetry=tel, operation=_interrupted)

        # The probe slot is free again: a healthy probe now closes the breaker.
        async def _ok():
            return (_hit(),)

        hits = await observed_recall(
            path="direct_tool", telemetry=tel, operation=_ok)
        assert hits and br.state == "closed"
        # The interruption produced NO telemetry outcome (no fabricated
        # unavailable/zero-hit): threshold failures + the final hit only.
        outcomes = [e.outcome for e in tel.snapshot()]
        assert outcomes == ["unavailable"] * rh.RECALL_BREAKER_FAILURE_THRESHOLD + ["hits"]
    finally:
        rh.reset_recall_breakers()
