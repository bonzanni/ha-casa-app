"""#599 — the s6 seams the quiesce ladder stands on.

``wanted_down`` is the latch-verification probe (deliberately narrower than the
tri-state classifier), ``latch_down`` is the durable down latch, and
``start_service`` must be cancellation-COMPLETE so a cancelled start cannot bring
the service up after the fence that guarded it has been released.
"""
import asyncio
import subprocess

import pytest

from drivers import s6_rc


class FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


# --- wanted_down ------------------------------------------------------------

async def test_wanted_down_true_only_on_an_affirmative_false(monkeypatch, tmp_path):
    scandir = tmp_path / "engagement-x"
    scandir.mkdir()
    monkeypatch.setattr(s6_rc, "_service_scandir", lambda eid: str(scandir))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: FakeCompleted(stdout="false\n"))
    assert await s6_rc.wanted_down(engagement_id="x") is True


async def test_wanted_down_is_false_while_s6_still_wants_it_up(monkeypatch, tmp_path):
    """The load-bearing case: the leader is alive (so the tri-state probe would
    say "up" whatever the wanted state is), and s6 still wants it up."""
    scandir = tmp_path / "engagement-x"
    scandir.mkdir()
    monkeypatch.setattr(s6_rc, "_service_scandir", lambda eid: str(scandir))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: FakeCompleted(stdout="true\n"))
    assert await s6_rc.wanted_down(engagement_id="x") is False


async def test_a_failed_query_is_not_proof_of_wanted_down(monkeypatch, tmp_path):
    scandir = tmp_path / "engagement-x"
    scandir.mkdir()
    monkeypatch.setattr(s6_rc, "_service_scandir", lambda eid: str(scandir))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: FakeCompleted(stdout="", returncode=1))
    assert await s6_rc.wanted_down(engagement_id="x") is False


async def test_an_absent_scandir_is_wanted_down(monkeypatch, tmp_path):
    monkeypatch.setattr(s6_rc, "_service_scandir",
                        lambda eid: str(tmp_path / "gone"))
    assert await s6_rc.wanted_down(engagement_id="x") is True


async def test_wanted_down_asks_for_wantedup_only(monkeypatch, tmp_path):
    """It must not reuse the up,wantedup pair the tri-state classifier reads —
    that classifier answers a different question."""
    scandir = tmp_path / "engagement-x"
    scandir.mkdir()
    monkeypatch.setattr(s6_rc, "_service_scandir", lambda eid: str(scandir))
    seen = []

    def run(cmd, *a, **k):
        seen.append(cmd)
        return FakeCompleted(stdout="false\n")
    monkeypatch.setattr(subprocess, "run", run)
    await s6_rc.wanted_down(engagement_id="x")
    assert seen[0][:3] == ["s6-svstat", "-o", "wantedup"]


# --- latch_down -------------------------------------------------------------

async def test_latch_down_uses_the_durable_capital_D(monkeypatch, tmp_path):
    """Capital -D writes ./down, so the latch survives an s6-supervise restart.
    Lowercase -d would not, and s6-rc -d change is the unbounded path."""
    monkeypatch.setattr(s6_rc, "_service_scandir",
                        lambda eid: str(tmp_path / "sv"))
    seen = []

    def run(cmd, *a, **k):
        seen.append(cmd)
        return FakeCompleted()
    monkeypatch.setattr(subprocess, "run", run)
    await s6_rc.latch_down(engagement_id="x")
    assert seen == [["s6-svc", "-D", str(tmp_path / "sv")]]


# --- start_service cancellation-completeness --------------------------------

async def test_a_cancelled_start_drains_its_worker_before_unwinding(monkeypatch):
    """#599 (Sol, reproduced): a bare ``await asyncio.to_thread`` lets the
    cancelled caller release its fence while ``s6-rc -u change`` is still
    running, so the service can come UP after the ladder observed the uid empty.

    Red case: with the shield removed this test fails — the cancellation
    propagates while ``worker_finished`` is still False.
    """
    import threading

    started = asyncio.Event()
    release = threading.Event()     # bounded: the worker can never outlive the test
    finished = threading.Event()

    def blocking_run(cmd, *a, **k):
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=5)
        finished.set()
        return FakeCompleted()

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(subprocess, "run", blocking_run)

    task = asyncio.ensure_future(s6_rc.start_service(engagement_id="x"))
    try:
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)      # let the cancellation reach the coroutine
        # The whole point: the up-transition must still be in flight AND the
        # caller must still be inside start_service (holding its fence).
        unwound_early = task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not unwound_early, (
            "cancellation unwound past the fence while s6-rc -u change was "
            "still running — the service can come up after the ladder ran")
        assert finished.is_set(), "the s6-rc worker was abandoned mid-transition"
    finally:
        release.set()               # never leave the worker spinning
        if not task.done():
            task.cancel()


async def test_repeated_cancellation_still_drains_the_worker(monkeypatch):
    """Terra, diff review: a SECOND cancellation landing on the drain used to be
    swallowed, and the function then unwound with ``s6-rc -u change`` still
    running — the same hole one level deeper. Red case: with the drain written as
    a single retry instead of a loop, this fails."""
    import threading

    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_run(cmd, *a, **k):
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=5)
        finished.set()
        return FakeCompleted()

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(subprocess, "run", blocking_run)

    task = asyncio.ensure_future(s6_rc.start_service(engagement_id="x"))
    try:
        await started.wait()
        for _ in range(4):          # repeated cancellations, as a busy loop would
            task.cancel()
            await asyncio.sleep(0)
        unwound_early = task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not unwound_early, (
            "a repeated cancellation unwound past the fence with the "
            "up-transition still in flight")
        assert finished.is_set()
    finally:
        release.set()
        if not task.done():
            task.cancel()


async def test_an_uncancelled_start_still_raises_a_failed_transition(monkeypatch):
    def failing_run(cmd, *a, **k):
        raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(subprocess.CalledProcessError):
        await s6_rc.start_service(engagement_id="x")
