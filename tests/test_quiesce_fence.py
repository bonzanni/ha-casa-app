"""#599 — the per-engagement lifecycle fence.

Both design reviewers reproduced the same hole from different entry points: a
start already in flight resumed AFTER the terminal transition committed and after
the ladder observed the uid set empty, and put a fresh CLI back under that uid.
Terminal status blocks turn admission (INV-ENG-009); it did not block work
already in flight. These are those reproductions as red cases.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from drivers import s6_rc
from drivers.claude_code_driver import ClaudeCodeDriver
from engagement_uids import UID_BASE

pytestmark = [pytest.mark.asyncio]

UID = UID_BASE + 21


def _driver(tmp_path, registry=None) -> ClaudeCodeDriver:
    async def send(topic_id, text):
        return None
    return ClaudeCodeDriver(
        engagements_root=str(tmp_path), send_to_topic=send,
        casa_framework_mcp_url="http://unused", registry=registry)


class FakeRegistry:
    """Just enough registry for the fence: ``get`` and the obligation clear."""

    def __init__(self, rec):
        self.rec = rec
        self.cleared: list[str] = []

    def get(self, eid):
        return self.rec if eid == self.rec.id else None

    async def clear_quiesce_pending(self, eid):
        self.cleared.append(eid)


def _rec(status="active"):
    return SimpleNamespace(id="f" * 32, status=status, allocated_uid=UID,
                           topic_id=5, quiesce_pending=True)


# --- the reproduced hole ----------------------------------------------------

async def test_a_start_that_resumes_after_the_terminal_commit_is_refused(tmp_path):
    """Sol's reproduction: pause inside the launch, commit the terminal
    transition, release — the s6 up-transition must NOT happen."""
    rec = _rec()
    reg = FakeRegistry(rec)
    drv = _driver(tmp_path, reg)
    started: list[str] = []

    async def fake_start(*, engagement_id):
        started.append(engagement_id)
    s6_rc_start = s6_rc.start_service
    s6_rc.start_service = fake_start
    try:
        # The engagement goes terminal while the launch is in flight.
        rec.status = "cancelled"
        assert await drv._start_service_fenced(rec) is False
        assert started == [], "a terminal record was started anyway"
    finally:
        s6_rc.start_service = s6_rc_start


async def test_a_live_record_still_starts(tmp_path):
    rec = _rec()
    reg = FakeRegistry(rec)
    drv = _driver(tmp_path, reg)
    started: list[str] = []

    async def fake_start(*, engagement_id):
        started.append(engagement_id)
    s6_rc_start = s6_rc.start_service
    s6_rc.start_service = fake_start
    try:
        assert await drv._start_service_fenced(rec) is True
        assert started == [rec.id]
    finally:
        s6_rc.start_service = s6_rc_start


async def test_the_ladder_and_a_start_are_serialised_by_the_fence(tmp_path):
    """Whichever side wins is correct — but they must never interleave."""
    rec = _rec()
    reg = FakeRegistry(rec)
    drv = _driver(tmp_path, reg)
    order: list[str] = []
    release = asyncio.Event()

    async def slow_latch(*, engagement_id):
        order.append("ladder-enter")
        await release.wait()

    async def wanted(*, engagement_id):
        return True

    async def fake_start(*, engagement_id):
        order.append("start")

    import engagement_quiesce
    real = engagement_quiesce.quiesce_engagement

    async def ladder(rec_):
        async with drv._lifecycle_lock(rec_.id):
            await slow_latch(engagement_id=rec_.id)
            order.append("ladder-exit")

    s6_rc_start = s6_rc.start_service
    s6_rc.start_service = fake_start
    try:
        lad = asyncio.ensure_future(ladder(rec))
        await asyncio.sleep(0)
        starter = asyncio.ensure_future(drv._start_service_fenced(rec))
        await asyncio.sleep(0)
        assert order == ["ladder-enter"], "the start jumped the fence"
        release.set()
        await lad
        await starter
        assert order == ["ladder-enter", "ladder-exit", "start"]
    finally:
        s6_rc.start_service = s6_rc_start


# --- the reproduced deadlock ------------------------------------------------

async def test_the_ladder_never_needs_the_compile_lock(tmp_path):
    """Terra's reproduction: ``start()`` holds ``_compile_lock`` around its whole
    body and then wants the fence, so a ladder that held the fence and wanted the
    compile lock would deadlock. The ladder must complete with the compile lock
    held by somebody else — which is only true if it never asks for it."""
    rec = _rec()
    reg = FakeRegistry(rec)
    drv = _driver(tmp_path, reg)

    import engagement_quiesce
    real = engagement_quiesce.quiesce_engagement

    async def fake_ladder(**kw):
        return engagement_quiesce.QuiesceResult(extinct=True, latched=True)
    engagement_quiesce.quiesce_engagement = fake_ladder
    try:
        async with s6_rc._compile_lock:          # somebody else is compiling
            out = await asyncio.wait_for(drv.quiesce(rec), timeout=2.0)
        assert out is True
        assert reg.cleared == [rec.id]
    finally:
        engagement_quiesce.quiesce_engagement = real


async def test_the_fence_is_retired_by_its_holder(tmp_path):
    """Both reviewers, re-review: a fence still held when ``cancel`` runs was
    retained forever — normal finalization calls ``cancel`` exactly once, so
    "the next teardown retires it" was untrue, leaking one Lock per overrun.

    Red case: remove the retirement in ``quiesce`` and the entry survives.
    """
    rec = _rec()
    reg = FakeRegistry(rec)
    drv = _driver(tmp_path, reg)

    import engagement_quiesce
    real = engagement_quiesce.quiesce_engagement

    async def fake_ladder(**kw):
        return engagement_quiesce.QuiesceResult(extinct=True, latched=True)
    engagement_quiesce.quiesce_engagement = fake_ladder
    try:
        assert await drv.quiesce(rec) is True
        assert rec.id not in drv._lifecycle_locks, "the fence leaked"
    finally:
        engagement_quiesce.quiesce_engagement = real


async def test_a_fence_a_new_holder_took_is_not_dropped(tmp_path):
    """Identity check: retiring must never drop a lock somebody else now holds."""
    rec = _rec()
    drv = _driver(tmp_path, FakeRegistry(rec))
    original = drv._lifecycle_lock(rec.id)

    import engagement_quiesce
    real = engagement_quiesce.quiesce_engagement

    async def fake_ladder(**kw):
        # Simulate the map being re-keyed to a DIFFERENT lock, held by another
        # caller, while this ladder ran.
        replacement = asyncio.Lock()
        await replacement.acquire()
        drv._lifecycle_locks[rec.id] = replacement
        return engagement_quiesce.QuiesceResult(extinct=True, latched=True)
    engagement_quiesce.quiesce_engagement = fake_ladder
    try:
        await drv.quiesce(rec)
        assert drv._lifecycle_locks.get(rec.id) is not original
        assert drv._lifecycle_locks.get(rec.id) is not None, (
            "a fence held by another caller was dropped underneath it")
    finally:
        engagement_quiesce.quiesce_engagement = real


async def test_the_obligation_is_cleared_only_on_an_observed_extinction(tmp_path):
    rec = _rec()
    reg = FakeRegistry(rec)
    drv = _driver(tmp_path, reg)

    import engagement_quiesce
    real = engagement_quiesce.quiesce_engagement

    async def unverified(**kw):
        return engagement_quiesce.QuiesceResult(
            extinct=False, reason="survivor", survivors=(123,))
    engagement_quiesce.quiesce_engagement = unverified
    try:
        assert await drv.quiesce(rec) is False
        assert reg.cleared == [], "an unverified extinction discharged the debt"
    finally:
        engagement_quiesce.quiesce_engagement = real


async def test_a_raising_ladder_never_propagates_into_the_transition(tmp_path):
    rec = _rec()
    drv = _driver(tmp_path, FakeRegistry(rec))

    import engagement_quiesce
    real = engagement_quiesce.quiesce_engagement

    async def boom(**kw):
        raise RuntimeError("ladder exploded")
    engagement_quiesce.quiesce_engagement = boom
    try:
        assert await drv.quiesce(rec) is False
    finally:
        engagement_quiesce.quiesce_engagement = real
