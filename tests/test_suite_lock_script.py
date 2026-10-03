"""scripts/suite-lock.sh: the unit suite's lock names its holder, and a timeout
says so.

A hung suite in another worktree held /tmp/casa-suite.lock for over an hour.
`flock -w 1800` then expired with exit 1, and `make test-unit` (scripts/gate.sh
step 6/7) printed only `make: *** Error 1`. The wrapper names the holder when it
starts to wait and again when the wait expires, and a timeout exits 75.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "suite-lock.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None or shutil.which("lslocks") is None,
    reason="needs util-linux flock and lslocks")


def _held(lock: Path) -> subprocess.Popen:
    holder = subprocess.Popen(["flock", str(lock), "sleep", "30"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        probe = subprocess.run(["flock", "-n", str(lock), "true"])
        if probe.returncode != 0:
            return holder
        time.sleep(0.05)
    holder.kill()
    raise AssertionError("the holder never took the lock")


def test_a_timeout_exits_75_and_names_the_holder(tmp_path):
    lock = tmp_path / "suite.lock"
    holder = _held(lock)
    try:
        r = subprocess.run(["bash", str(SCRIPT), str(lock), "1", "true"],
                           capture_output=True, text=True, timeout=30)
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode == 75, (r.returncode, r.stderr)
    assert "is held — waiting up to 1 s" in r.stderr
    assert "suite lock timeout after 1 s" in r.stderr
    # named twice: when the wait starts and when it expires
    assert r.stderr.count(f"holder pid {holder.pid},") == 2, r.stderr


def test_a_free_lock_runs_the_command_and_returns_its_status(tmp_path):
    lock = tmp_path / "suite.lock"
    r = subprocess.run(["bash", str(SCRIPT), str(lock), "1", "sh", "-c", "exit 3"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 3
    assert r.stderr == ""


def test_the_command_runs_while_holding_the_lock(tmp_path):
    lock = tmp_path / "suite.lock"
    r = subprocess.run(
        ["bash", str(SCRIPT), str(lock), "1", "sh", "-c",
         f"flock -n {lock} true && echo free || echo held"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert r.stdout.strip() == "held"


def test_two_wrappers_contend_and_the_waiter_names_the_live_holder(tmp_path):
    """The real contention: one wrapper running a command, a second waiting on
    it. The holder `lslocks` names must be a LIVE process the waiter can
    describe — not `holder unknown` (diff r1, Astra S2: the first wrapper's
    lock was taken by a `flock -n` that had already exited)."""
    lock = tmp_path / "suite.lock"
    first = subprocess.Popen(["bash", str(SCRIPT), str(lock), "60", "sleep", "30"])
    try:
        deadline = time.monotonic() + 10
        while subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0:
            assert time.monotonic() < deadline, "the first wrapper never took the lock"
            time.sleep(0.05)
        r = subprocess.run(["bash", str(SCRIPT), str(lock), "1", "true"],
                           capture_output=True, text=True, timeout=30)
    finally:
        first.kill()
        first.wait()
    assert r.returncode == 75, r.stderr
    assert "holder unknown" not in r.stderr, r.stderr
    assert r.stderr.count("holder pid ") == 2, r.stderr
    assert "sleep 30" in r.stderr, r.stderr
