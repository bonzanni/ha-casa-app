"""AgentSpawnLimiter (#283) — occupancy-debt cap on agent-spawned engagements.

Design round 3/4 (2026-08-13): the limiter is an occupancy counter, not a
fixed permit pool. ``try_acquire`` refuses at the cap; ``restore`` (boot
reconciliation) increments unconditionally so pre-fix overloads become debt
that must drain before new acquisition succeeds. Tokens release idempotently.
"""
import specialist_limits
from specialist_limits import AgentSpawnLimiter


def test_acquire_under_cap_returns_token_and_counts():
    lim = AgentSpawnLimiter(max_spawns=3)
    t1 = lim.try_acquire()
    t2 = lim.try_acquire()
    assert t1 is not None and t2 is not None
    assert lim.occupancy == 2


def test_acquire_at_cap_refuses():
    lim = AgentSpawnLimiter(max_spawns=2)
    assert lim.try_acquire() is not None
    assert lim.try_acquire() is not None
    assert lim.try_acquire() is None
    assert lim.occupancy == 2


def test_release_frees_slot_and_is_idempotent():
    lim = AgentSpawnLimiter(max_spawns=1)
    tok = lim.try_acquire()
    assert lim.try_acquire() is None
    tok.release()
    assert lim.occupancy == 0
    # Idempotent: a second release must not go negative or free a phantom slot.
    tok.release()
    assert lim.occupancy == 0
    t2 = lim.try_acquire()
    assert t2 is not None
    assert lim.try_acquire() is None


def test_restore_exceeds_cap_as_debt_and_blocks_acquisition():
    lim = AgentSpawnLimiter(max_spawns=3)
    tokens = [lim.restore() for _ in range(4)]  # pre-fix overload: 4 live marked
    assert lim.occupancy == 4
    # Debt: no new acquisition while occupancy >= cap.
    assert lim.try_acquire() is None
    tokens[0].release()
    assert lim.occupancy == 3
    # Still at cap (3 live marked records remain) — the r3 Terra sequence:
    # one reap must NOT admit a new spawn while three marked stay live.
    assert lim.try_acquire() is None
    tokens[1].release()
    assert lim.occupancy == 2
    assert lim.try_acquire() is not None


def test_restore_token_release_idempotent():
    lim = AgentSpawnLimiter(max_spawns=1)
    tok = lim.restore()
    tok.release()
    tok.release()
    assert lim.occupancy == 0


def test_invalid_cap_rejected():
    import pytest
    with pytest.raises(ValueError):
        AgentSpawnLimiter(max_spawns=0)
