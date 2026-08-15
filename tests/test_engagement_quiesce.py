"""#599 — the bounded, observed uid quiesce ladder.

Every test drives the ladder with an injected ``sleep``/``monotonic`` and a fake
``/proc`` tree, so nothing here touches a real process or the shared
``asyncio.sleep`` attribute (memory-cage rule).
"""
import pytest

from engagement_quiesce import (
    QuiesceResult, kill_uid_until_empty, live_pids_for_uid, quiesce_engagement,
)
from engagement_uids import UID_BASE, UNALLOCATED_UID

UID = UID_BASE + 7


class FakeProc:
    """A writable /proc: ``pids`` maps pid -> (ruid, state)."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "proc"
        self.root.mkdir()

    def set(self, pids: dict[int, tuple[int, str]]):
        for child in self.root.iterdir():
            if child.is_dir():
                for f in child.iterdir():
                    f.unlink()
                child.rmdir()
        for pid, (ruid, state) in pids.items():
            d = self.root / str(pid)
            d.mkdir()
            (d / "status").write_text(
                f"Name:\tclaude\nState:\t{state} (whatever)\n"
                f"Uid:\t{ruid}\t{ruid}\t{ruid}\t{ruid}\n")

    @property
    def path(self) -> str:
        return str(self.root)


class RecordingSignaller:
    """pidfd seams that record (pid, sig) and optionally kill the fake process."""

    def __init__(self, proc: FakeProc, *, dies=True, alive=None):
        self.proc = proc
        self.sent: list[tuple[int, int]] = []
        self.dies = dies
        self.alive = dict(alive or {})

    def open(self, pid, flags):
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        return 1000 + pid

    def send(self, fd, sig, info):
        pid = fd - 1000
        self.sent.append((pid, sig))
        if self.dies:
            self.alive.pop(pid, None)
            self.proc.set({p: v for p, v in self.alive.items()})

    def close(self, fd):
        pass


def _clock():
    """Monotonic that advances 0.01 per call — deadlines are reached, not slept."""
    t = {"now": 0.0}

    def now():
        t["now"] += 0.01
        return t["now"]
    return now


async def _nosleep(_):
    return None


# --- enumeration ------------------------------------------------------------

def test_enumeration_selects_only_the_engagement_uid(tmp_path):
    proc = FakeProc(tmp_path)
    proc.set({1: (0, "S"), 42: (UID, "R"), 43: (UID + 1, "R")})
    assert live_pids_for_uid(UID, proc_root=proc.path) == [42]


def test_a_zombie_counts_as_extinct(tmp_path):
    """Red case: treating Z as live would turn a harmless transient into a
    false NOT_VERIFIED. A zombie cannot execute, fork or write."""
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "Z")})
    assert live_pids_for_uid(UID, proc_root=proc.path) == []


def test_a_stopped_process_is_not_inert(tmp_path):
    """T is NOT extinct — a stopped process may be continued and run again."""
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "T")})
    assert live_pids_for_uid(UID, proc_root=proc.path) == [42]


def test_an_unallocated_uid_enumerates_nothing(tmp_path):
    proc = FakeProc(tmp_path)
    proc.set({42: (0, "R")})
    assert live_pids_for_uid(UNALLOCATED_UID, proc_root=proc.path) == []


# --- the kill loop ----------------------------------------------------------

async def test_kill_loop_reports_extinct_only_after_observing_empty(tmp_path):
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "R")})
    sigr = RecordingSignaller(proc, alive={42: (UID, "R")})
    res = await kill_uid_until_empty(
        UID, proc_root=proc.path, sleep=_nosleep, monotonic=_clock(),
        pidfd_open=sigr.open, pidfd_send=sigr.send, close=sigr.close)
    assert res.extinct and res
    assert sigr.sent and sigr.sent[0][0] == 42


async def test_a_child_that_appears_only_in_the_second_pass_is_killed(tmp_path):
    """The convergence claim: a member may fork after the snapshot and before
    the signal lands; the next pass enumerates and kills that child."""
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "R")})
    sigr = RecordingSignaller(proc, alive={42: (UID, "R")})
    original_send = sigr.send
    forked = {"done": False}

    def send_then_fork(fd, sig, info):
        original_send(fd, sig, info)
        if not forked["done"]:      # 42 dies, but it left a child behind
            forked["done"] = True
            sigr.alive[99] = (UID, "R")
            proc.set({99: (UID, "R")})
    sigr.send = send_then_fork

    res = await kill_uid_until_empty(
        UID, proc_root=proc.path, sleep=_nosleep, monotonic=_clock(),
        pidfd_open=sigr.open, pidfd_send=send_then_fork, close=sigr.close)
    assert res.extinct
    assert [p for p, _ in sigr.sent] == [42, 99]
    assert res.passes >= 2


async def test_a_survivor_is_reported_not_claimed(tmp_path):
    """Red case: a member that never dies must produce NOT_VERIFIED naming the
    pid — never a silent success."""
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "R")})
    sigr = RecordingSignaller(proc, dies=False, alive={42: (UID, "R")})
    res = await kill_uid_until_empty(
        UID, proc_root=proc.path, deadline_s=0.05, sleep=_nosleep,
        monotonic=_clock(), pidfd_open=sigr.open, pidfd_send=sigr.send,
        close=sigr.close)
    assert not res.extinct and not res
    assert res.survivors == (42,)


async def test_missing_pidfd_primitives_signal_nothing(tmp_path):
    """A never-reused uid does not make a numeric kill safe: the pid can be
    recycled for an unrelated process between the read and the signal."""
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "R")})
    res = await kill_uid_until_empty(
        UID, proc_root=proc.path, sleep=_nosleep, monotonic=_clock(),
        pidfd_open=None, pidfd_send=None)
    assert not res.extinct
    assert res.reason == "pidfd unavailable"
    assert live_pids_for_uid(UID, proc_root=proc.path) == [42]


async def test_a_recycled_pid_is_never_signalled(tmp_path):
    """The pinned instance's uid is re-verified AFTER the pidfd is opened; a pid
    that now belongs to another uid must not be signalled."""
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "R")})
    sigr = RecordingSignaller(proc, alive={42: (UID, "R")})

    def open_then_recycle(pid, flags):
        fd = sigr.open(pid, flags)
        proc.set({42: (0, "R")})      # pid reused by root between read + signal
        return fd

    res = await kill_uid_until_empty(
        UID, proc_root=proc.path, deadline_s=0.05, sleep=_nosleep,
        monotonic=_clock(), pidfd_open=open_then_recycle,
        pidfd_send=sigr.send, close=sigr.close)
    assert sigr.sent == []
    assert res.extinct        # the uid set is genuinely empty now


async def test_an_unreadable_proc_is_never_an_extinction(tmp_path, monkeypatch):
    """Sol, diff review — the weakest line in the first cut: ``os.listdir``
    failing returned an empty list, which the loop read as a verified
    extinction. An observation failure is not proof of emptiness.

    Red case: with the scan's ``ok`` flag removed this returns extinct=True.
    """
    import os as _os
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "R")})

    def boom(path):
        raise PermissionError("cannot read /proc")
    monkeypatch.setattr(_os, "listdir", boom)

    res = await kill_uid_until_empty(
        UID, proc_root=proc.path, deadline_s=0.05, sleep=_nosleep,
        monotonic=_clock(), pidfd_open=lambda *a: 1, pidfd_send=lambda *a: None,
        close=lambda fd: None)
    assert not res.extinct
    assert "enumerate" in res.reason


async def test_an_unreadable_live_pid_blocks_the_extinction_claim(tmp_path):
    """A pid whose status cannot be read but whose /proc entry still exists is a
    process we failed to observe — not one that is gone."""
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "R")})
    (proc.root / "42" / "status").write_text("garbage without a Uid line\n")

    res = await kill_uid_until_empty(
        UID, proc_root=proc.path, deadline_s=0.05, sleep=_nosleep,
        monotonic=_clock(), pidfd_open=lambda *a: 1, pidfd_send=lambda *a: None,
        close=lambda fd: None)
    assert not res.extinct


async def test_a_zombie_parent_does_not_certify_the_pass_that_hides_its_child(
        tmp_path, monkeypatch):
    """Sol, diff review — reproduced: a parent forks a child and becomes ``Z``
    between the directory snapshot and the reads. The child EXISTS but is absent
    from that snapshot, and the parent is inert, so a single "no live pids" scan
    would certify extinction in the very pass whose successor finds the child.

    Modelled the way it actually happens: the child is in /proc all along and the
    FIRST snapshot simply misses it. Red case: with one clear scan sufficient,
    the ladder returns extinct and never signals 99.
    """
    import os as _os
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "Z"), 99: (UID, "R")})

    real_listdir = _os.listdir
    calls = {"n": 0}

    def hiding_listdir(path):
        calls["n"] += 1
        entries = real_listdir(path)
        if calls["n"] == 1:            # the first snapshot misses the child
            return [e for e in entries if e != "99"]
        return entries
    monkeypatch.setattr(_os, "listdir", hiding_listdir)

    sigr = RecordingSignaller(proc, alive={99: (UID, "R")})
    res = await kill_uid_until_empty(
        UID, proc_root=proc.path, deadline_s=0.5, sleep=_nosleep,
        monotonic=_clock(), pidfd_open=sigr.open, pidfd_send=sigr.send,
        close=sigr.close)
    assert [p for p, _ in sigr.sent] == [99], (
        "the hidden child was never signalled — a single clear scan certified "
        "extinction")
    assert res.extinct


async def test_a_lingering_zombie_does_not_block_a_real_extinction(tmp_path):
    """Sol, re-review: requiring "no trace at all" held a real extinction hostage
    to whenever init reaps a zombie, and contradicted INV-CONT-006's published
    "zombies count as extinct"."""
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "Z")})
    res = await kill_uid_until_empty(
        UID, proc_root=proc.path, deadline_s=0.5, sleep=_nosleep,
        monotonic=_clock(), pidfd_open=lambda *a: 1,
        pidfd_send=lambda *a: None, close=lambda fd: None)
    assert res.extinct, "a zombie that will never be reaped blocked extinction"


# --- the full ladder --------------------------------------------------------

def _latch(calls, ok=True):
    async def latch_down(*, engagement_id):
        calls.append(("latch", engagement_id))
    async def wanted_down(*, engagement_id):
        calls.append(("probe", engagement_id))
        return ok
    return latch_down, wanted_down


async def test_ladder_latches_before_it_kills(tmp_path):
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "R")})
    sigr = RecordingSignaller(proc, alive={42: (UID, "R")})
    calls: list = []
    latch_down, wanted_down = _latch(calls)

    async def latch_recording(*, engagement_id):
        await latch_down(engagement_id=engagement_id)

    res = await quiesce_engagement(
        engagement_id="e" * 32, uid=UID, latch_down=latch_recording,
        wanted_down=wanted_down, proc_root=proc.path, sleep=_nosleep,
        monotonic=_clock(), pidfd_open=sigr.open, pidfd_send=sigr.send,
        close=sigr.close)
    assert res.extinct and res.latched
    assert calls[0][0] == "latch"


async def test_an_unverified_latch_is_never_extinct_even_when_empty(tmp_path):
    """Red case — the load-bearing one. The kill loop empties the set, but s6 is
    still wanted-up and may put a fresh CLI back, so this is NOT an extinction.
    Asserting the OUTCOME, not the call order (a round-1 finding: the earlier
    plan's test proved only that the latch was called)."""
    proc = FakeProc(tmp_path)
    proc.set({42: (UID, "R")})
    sigr = RecordingSignaller(proc, alive={42: (UID, "R")})
    calls: list = []
    latch_down, wanted_down = _latch(calls, ok=False)

    res = await quiesce_engagement(
        engagement_id="e" * 32, uid=UID, latch_down=latch_down,
        wanted_down=wanted_down, proc_root=proc.path, latch_attempts=2,
        sleep=_nosleep, monotonic=_clock(), pidfd_open=sigr.open,
        pidfd_send=sigr.send, close=sigr.close)
    assert live_pids_for_uid(UID, proc_root=proc.path) == []   # it DID empty
    assert not res.extinct and not res.latched
    assert "wanted-down" in res.reason


async def test_a_raising_latch_command_does_not_raise_out_of_the_ladder(tmp_path):
    """A ladder that raised inside a terminal transition would wedge the funnel
    it exists to protect."""
    proc = FakeProc(tmp_path)
    proc.set({})

    async def latch_down(*, engagement_id):
        raise OSError("s6-svc missing")

    async def wanted_down(*, engagement_id):
        raise OSError("s6-svstat missing")

    res = await quiesce_engagement(
        engagement_id="e" * 32, uid=UID, latch_down=latch_down,
        wanted_down=wanted_down, proc_root=proc.path, latch_attempts=1,
        sleep=_nosleep, monotonic=_clock())
    assert not res.extinct and not res.latched


async def test_an_unallocated_uid_is_reported_not_signalled(tmp_path):
    proc = FakeProc(tmp_path)
    calls: list = []
    latch_down, wanted_down = _latch(calls)
    res = await quiesce_engagement(
        engagement_id="e" * 32, uid=UNALLOCATED_UID, latch_down=latch_down,
        wanted_down=wanted_down, proc_root=proc.path, sleep=_nosleep,
        monotonic=_clock())
    assert not res.extinct
    assert calls == []            # specialist/in-casa records are never touched


def test_result_is_falsy_unless_extinct():
    assert not QuiesceResult(extinct=False, reason="x")
    assert QuiesceResult(extinct=True)
