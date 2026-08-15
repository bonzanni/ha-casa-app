"""#599 — the durable uid-quiesce obligation and its discharge owner.

The obligation exists because a casa death between the terminal commit and the
kill would otherwise lose the work silently, and because a record that goes
terminal through ``mark_error`` needs an in-process owner too — not only boot
recovery (both established by design review, with reproductions).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from engagement_registry import EngagementRecord, EngagementRegistry
from engagement_uids import UID_BASE, UNALLOCATED_UID

pytestmark = [pytest.mark.asyncio]

UID = UID_BASE + 11


def _rec(**kw) -> EngagementRecord:
    base = dict(
        id="a" * 32, kind="executor", role_or_type="dev", driver="claude_code",
        status="active", topic_id=7, started_at=1000.0, last_user_turn_ts=1000.0,
        last_idle_reminder_ts=0.0, completed_at=None, sdk_session_id=None,
        origin={"role": "assistant", "channel": "telegram"}, task="t",
        allocated_uid=UID,
    )
    base.update(kw)
    return EngagementRecord(**base)


async def _registry(tmp_path, rec=None, owner=None) -> EngagementRegistry:
    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    if rec is not None:
        reg._records[rec.id] = rec
    if owner is not None:
        reg.set_quiesce_owner(owner)
    return reg


# --- who owes it ------------------------------------------------------------

async def test_a_terminal_claude_code_record_owes_a_quiesce(tmp_path):
    rec = _rec()
    reg = await _registry(tmp_path, rec)
    assert await reg.try_transition_terminal(rec.id, "completed", strict=True)
    assert rec.quiesce_pending is True


async def test_a_specialist_record_owes_nothing(tmp_path):
    """No OS identity of its own — the sentinel uid is the whole test."""
    rec = _rec(driver="in_casa", allocated_uid=UNALLOCATED_UID)
    reg = await _registry(tmp_path, rec)
    assert await reg.try_transition_terminal(rec.id, "completed", strict=True)
    assert rec.quiesce_pending is False


async def test_mark_error_owes_and_schedules_in_process(tmp_path):
    """Sol, design round 3: a launch that fails after start_service marks the
    record errored and immediately aborts the topic for the operator. Without an
    in-process owner the service kept writing until a restart."""
    seen: list[str] = []

    async def owner(record):
        seen.append(record.id)
        return True

    rec = _rec()
    reg = await _registry(tmp_path, rec, owner=owner)
    assert await reg.mark_error(rec.id, "launch_failed", "boom") is True
    assert rec.quiesce_pending is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == [rec.id]


async def test_a_rolled_back_transition_owes_nothing(tmp_path):
    """Red case: a strict persist failure leaves the record LIVE, and a live
    record must not owe a kill of its own running processes."""
    rec = _rec()
    reg = await _registry(tmp_path, rec)

    async def boom(*a, **k):
        reg._last_tombstone_ok = False
        raise OSError("disk gone")
    reg._write_tombstone_locked = boom

    with pytest.raises(OSError):
        await reg.try_transition_terminal(rec.id, "completed", strict=True)
    assert rec.status == "active"
    assert rec.quiesce_pending is False


async def test_a_settled_write_then_cancellation_still_schedules(tmp_path):
    """Sol, diff review: ``_write_tombstone_locked`` can SETTLE the durable write
    and then re-raise a cancellation. The scheduling line after it was skipped,
    leaving a record durably terminal and owing a kill with nothing running it
    until a restart.

    Red case: with the ``finally:`` removed, ``seen`` is empty here.
    """
    seen: list[str] = []

    async def owner(record):
        seen.append(record.id)
        return True

    rec = _rec()
    reg = await _registry(tmp_path, rec, owner=owner)
    real_write = reg._write_tombstone_locked

    async def settle_then_cancel(*a, **k):
        await real_write(*a, **k)          # the write COMMITS...
        raise asyncio.CancelledError()     # ...and then cancellation lands

    reg._write_tombstone_locked = settle_then_cancel
    with pytest.raises(asyncio.CancelledError):
        await reg.mark_error(rec.id, "launch_failed", "boom")
    assert rec.quiesce_pending is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == [rec.id], "the durable obligation had no in-process owner"


# --- durability -------------------------------------------------------------

async def test_the_obligation_survives_a_reload(tmp_path):
    rec = _rec()
    reg = await _registry(tmp_path, rec)
    await reg.try_transition_terminal(rec.id, "cancelled", strict=True)

    reloaded = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    await reloaded.load()
    again = reloaded._records[rec.id]
    assert again.quiesce_pending is True
    assert again.allocated_uid == UID
    assert [r.id for r in reloaded.records_owing_quiesce()] == [rec.id]


async def test_an_owing_record_is_exempt_from_terminal_expiry(tmp_path):
    """Sol, design round 3 — the finding neither the plan nor Terra had. Terminal
    records are dropped after the retention window; dropping one that still owes
    a quiesce would delete the obligation AND the uid recovery needs, so an
    unkillable survivor would become invisible at 30 days."""
    old = time.time() - 400 * 86400
    owing = _rec(id="b" * 32, status="cancelled", completed_at=old,
                 quiesce_pending=True)
    discharged = _rec(id="c" * 32, status="cancelled", completed_at=old,
                      quiesce_pending=False)
    reg = await _registry(tmp_path)
    reg._records[owing.id] = owing
    reg._records[discharged.id] = discharged
    await reg._write_tombstone_locked()

    rows = json.loads(Path(tmp_path / "engagements.json").read_text())
    ids = {r["id"] for r in rows} if isinstance(rows, list) else {
        r["id"] for r in rows.get("engagements", [])}
    assert owing.id in ids, "an outstanding obligation was expired away"
    assert discharged.id not in ids, "expiry stopped working for ordinary records"


async def test_clearing_is_exclusive_to_an_observed_extinction(tmp_path):
    rec = _rec(status="cancelled", completed_at=time.time(),
               quiesce_pending=True)
    reg = await _registry(tmp_path, rec)
    await reg.clear_quiesce_pending(rec.id)
    assert rec.quiesce_pending is False
    assert reg.records_owing_quiesce() == []


# --- the discharge owner ----------------------------------------------------

async def test_await_quiesce_returns_the_ladder_outcome(tmp_path):
    """Including the fast case: a ladder that finishes before the funnel asks
    has already retired its task, so the durable flag has to answer — reading
    "not extinct" for a successful extinction would be simply wrong."""
    async def owner(record):
        await reg.clear_quiesce_pending(record.id)
        return True
    rec = _rec()
    reg = await _registry(tmp_path, rec, owner=owner)
    await reg.try_transition_terminal(rec.id, "completed", strict=True)
    assert await reg.await_quiesce(rec.id, timeout=1.0) is True
    # ...and again once the task has retired entirely.
    await asyncio.sleep(0)
    assert await reg.await_quiesce(rec.id, timeout=1.0) is True


async def test_an_outstanding_obligation_with_no_task_is_not_extinct(tmp_path):
    """No owner wired: the obligation stands, so the honest answer is False."""
    rec = _rec()
    reg = await _registry(tmp_path, rec)
    await reg.try_transition_terminal(rec.id, "completed", strict=True)
    assert await reg.await_quiesce(rec.id, timeout=0.01) is False


async def test_await_quiesce_is_bounded_and_the_caller_continues(tmp_path):
    """A ladder that overruns must not wedge the funnel behind it."""
    started = asyncio.Event()

    async def slow_owner(record):
        started.set()
        await asyncio.sleep(30)
        return True

    rec = _rec()
    reg = await _registry(tmp_path, rec, owner=slow_owner)
    await reg.try_transition_terminal(rec.id, "completed", strict=True)
    await started.wait()
    assert await reg.await_quiesce(rec.id, timeout=0.01) is False
    task = reg._quiesce_tasks.get(rec.id)
    assert task is not None and not task.done()   # still running, not cancelled
    task.cancel()


async def test_an_outer_cancellation_does_not_cancel_the_ladder(tmp_path):
    """The task is registry-owned and shielded: cancelling the funnel that waits
    on it must not cancel containment."""
    ran_to_completion = asyncio.Event()
    release = asyncio.Event()

    async def owner(record):
        await release.wait()
        ran_to_completion.set()
        return True

    rec = _rec()
    reg = await _registry(tmp_path, rec, owner=owner)
    await reg.try_transition_terminal(rec.id, "completed", strict=True)

    waiter = asyncio.ensure_future(reg.await_quiesce(rec.id, timeout=30))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await asyncio.wait_for(ran_to_completion.wait(), timeout=1.0)


async def test_two_ladders_are_never_started_for_one_record(tmp_path):
    runs: list[str] = []

    async def owner(record):
        runs.append(record.id)
        await asyncio.sleep(0.05)
        return True

    rec = _rec()
    reg = await _registry(tmp_path, rec, owner=owner)
    await reg.try_transition_terminal(rec.id, "completed", strict=True)
    # A second terminal attempt loses the race and must not schedule again.
    await reg.try_transition_terminal(rec.id, "cancelled", strict=True)
    reg._schedule_quiesce_locked(rec)
    await asyncio.sleep(0)
    assert runs == [rec.id]
    t = reg._quiesce_tasks.get(rec.id)
    if t is not None:
        await asyncio.wait_for(t, timeout=1.0)


async def test_no_owner_configured_still_records_the_obligation(tmp_path):
    """With no owner wired, the durable obligation is what boot recovery reads."""
    rec = _rec()
    reg = await _registry(tmp_path, rec)
    await reg.try_transition_terminal(rec.id, "completed", strict=True)
    assert rec.quiesce_pending is True
    assert reg._quiesce_tasks == {}
