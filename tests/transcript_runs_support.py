"""Shared helpers for the run-confirmation and transcript-retention tests.

Every test runs against a real SQLite store wired into the live seams, and
real transcript files laid out the way the SDK's own folder lookup resolves
them under a temporary ``CLAUDE_CONFIG_DIR``. The kill helpers run a child
Python process this test started and SIGKILL it by its own pid at a named
point; nothing else is ever signalled.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_agent_sdk._internal.sessions import _canonicalize_path, _get_project_dir

from memory import transcript_runs
from memory.store import MemoryStore

TESTS_DIR = Path(__file__).resolve().parent
CODE_ROOT = TESTS_DIR.parent / "casa" / "rootfs" / "opt" / "casa"

#: Prepended to every child script: the import paths, the store wired into
#: the live seams, and ``stop()``, which writes the marker and waits to be
#: killed.
CHILD_PRELUDE = r"""
import asyncio, os, sys, time
store_dir, marker = sys.argv[1], sys.argv[2]
sys.path.insert(0, sys.argv[3])
sys.path.insert(0, sys.argv[4])
from memory import capture
from memory.store import MemoryStore
store = MemoryStore(store_dir)
assert store.open(), store.unavailable_reason
capture.configure(store=store, notify=None)

def stop():
    open(marker, "w").close()
    time.sleep(300)
"""


def new_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.open() is True, store.unavailable_reason
    return store


def rows(store: MemoryStore) -> list[dict]:
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM transcript_runs ORDER BY started_at")]
    finally:
        conn.close()


def events(store: MemoryStore) -> list[dict]:
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY seq")]
    finally:
        conn.close()


def summaries(store: MemoryStore) -> list[dict]:
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM engagement_summaries ORDER BY id")]
    finally:
        conn.close()


def unresolved(store: MemoryStore) -> int:
    with store.transaction() as conn:
        return transcript_runs.unresolved_count(conn)


def project(cwd: str) -> Path:
    """The folder the SDK resolves *cwd*'s sessions to (``CLAUDE_CONFIG_DIR``
    must already be set)."""
    return _get_project_dir(_canonicalize_path(cwd))


def write_transcript(cwd: str, sid: str, body: str = '{"type":"user"}\n') -> Path:
    folder = project(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text(body, encoding="utf-8")
    return path


def after_retention(days_more: int = 1) -> datetime:
    return datetime.now(timezone.utc) + transcript_runs.retention() + timedelta(
        days=days_more)


def kill_child(script: str, store: MemoryStore, tmp_path: Path, *args: str,
               env: dict | None = None, timeout: float = 60.0) -> None:
    """Run ``CHILD_PRELUDE + script`` in a child process this test owns and
    SIGKILL it by its pid once it has written the marker (``stop()``)."""
    marker = tmp_path / f"stopped-{time.monotonic_ns()}"
    child_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    child_env.update(env or {})
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD_PRELUDE + script, store.directory,
         str(marker), str(CODE_ROOT), str(TESTS_DIR), *args],
        env=child_env, cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + timeout
        while not marker.exists():
            if proc.poll() is not None:
                raise AssertionError(
                    f"child exited early ({proc.returncode}): "
                    f"{proc.stderr.read().decode(errors='replace')[-3000:]}")
            assert time.monotonic() < deadline, "child never reached its stop"
            time.sleep(0.02)
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        if proc.poll() is None and not marker.exists():
            os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()
    assert proc.returncode == -signal.SIGKILL
