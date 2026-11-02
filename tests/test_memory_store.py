"""The memory store's connection owner, boot check and migration runner.

Every test runs against a real SQLite file in a temporary directory: the
store's guarantees are about what is on disk and which descriptors are open,
so a model of either would pin the model.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from memory import schema
from memory import store as store_mod
from memory.store import MemoryStore, MemoryUnavailable

CODE_ROOT = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"


def _open_fds_on(path: str) -> list[str]:
    """Every descriptor of this process that points at *path* or a sidecar."""
    hits = []
    fd_dir = "/proc/self/fd"
    for fd in os.listdir(fd_dir):
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue
        if target.startswith(path):
            hits.append(target)
    return hits


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _raw(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


@pytest.fixture
def opened(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.open() is True, store.unavailable_reason
    return store


def test_fresh_store_creates_schema_v1_in_wal(opened: MemoryStore) -> None:
    assert stat.S_IMODE(os.stat(opened.directory).st_mode) == 0o700
    conn = _raw(opened.path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()
    assert tables == {
        "events", "objects", "revisions", "dispositions", "jobs",
        "correction_marks", "session_views", "engagement_summaries",
        "backfill_run", "meta",
    }
    # A second open of a current store migrates nothing and stays available.
    again = MemoryStore(opened.directory)
    assert again.open() is True


def test_connection_per_transaction_no_handle_survives(
    opened: MemoryStore,
) -> None:
    canary = secrets.token_hex(8)
    seen: list[sqlite3.Connection] = []
    with opened.transaction(write=True) as conn:
        seen.append(conn)
        # The probe below must be able to see a live handle, or its silence
        # after the block proves nothing.
        assert _open_fds_on(opened.path), "fd probe cannot see an open store"
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                     ("probe", json.dumps(canary)))
    with opened.transaction() as conn:
        seen.append(conn)
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'probe'").fetchone()
        assert json.loads(row[0]) == canary
    with pytest.raises(RuntimeError, match="boom"):
        with opened.transaction(write=True) as conn:
            seen.append(conn)
            conn.execute("INSERT INTO meta (key, value) VALUES ('gone', '1')")
            raise RuntimeError("boom")

    assert len({id(c) for c in seen}) == 3, "a connection was reused"
    for conn in seen:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    assert _open_fds_on(opened.path) == []
    assert not any(isinstance(v, sqlite3.Connection)
                   for v in vars(opened).values())
    # The failed block rolled back.
    check = _raw(opened.path)
    try:
        assert check.execute(
            "SELECT count(*) FROM meta WHERE key = 'gone'").fetchone()[0] == 0
    finally:
        check.close()


def test_failed_migration_keeps_the_old_file_and_reports_unavailable(
    opened: MemoryStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = secrets.token_hex(8)
    with opened.transaction(write=True) as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                     ("canary", json.dumps(canary)))
    before = _sha256(opened.path)

    # Version 2 creates a table, then fails part-way through.
    monkeypatch.setattr(store_mod, "MIGRATIONS", {
        1: schema.V1,
        2: ("CREATE TABLE added_in_v2 (x TEXT) STRICT",
            "THIS IS NOT A STATEMENT"),
    })
    later = MemoryStore(opened.directory)
    assert later.open() is False
    assert later.available is False
    assert later.unavailable_reason
    with pytest.raises(MemoryUnavailable):
        with later.transaction():
            pass

    assert _sha256(opened.path) == before
    conn = _raw(opened.path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'added_in_v2'"
        ).fetchone()[0] == 0
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'canary'").fetchone()
        assert json.loads(row[0]) == canary
    finally:
        conn.close()


def test_unopenable_files_report_unavailable_and_stay_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A file that is not a database at all.
    junk_dir = tmp_path / "junk"
    junk_dir.mkdir()
    junk = junk_dir / "memory.db"
    junk.write_bytes(b"not a database " + secrets.token_hex(16).encode())
    before = _sha256(str(junk))
    store = MemoryStore(str(junk_dir))
    assert store.open() is False
    assert _sha256(str(junk)) == before

    # A store written by a newer Casa: never downgraded.
    newer = MemoryStore(str(tmp_path / "newer"))
    assert newer.open() is True
    conn = _raw(newer.path)
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    before = _sha256(newer.path)
    again = MemoryStore(newer.directory)
    assert again.open() is False
    assert "newer" in (again.unavailable_reason or "")
    assert _sha256(newer.path) == before


def test_fts5_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert store_mod.fts5_available() is True

    monkeypatch.setattr(store_mod, "fts5_available", lambda: False)
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.open() is False
    assert "FTS5" in (store.unavailable_reason or "")
    # The check runs before anything is created.
    assert not (tmp_path / "memory").exists()


def test_memory_package_imports_no_casa_main_module() -> None:
    """The container-start maintenance step imports ``memory`` while no Casa
    process runs, so the package may import only its declared ports. Run in a
    fresh interpreter: this test process has long since imported everything."""
    probe = (
        "import importlib, os, pkgutil, sys\n"
        f"root = {str(CODE_ROOT)!r}\n"
        "sys.path.insert(0, root)\n"
        "import memory\n"
        "for m in pkgutil.walk_packages(memory.__path__, 'memory.'):\n"
        "    importlib.import_module(m.name)\n"
        "own = sorted(n for n, m in list(sys.modules.items())\n"
        "             if (getattr(m, '__file__', None) or '').startswith(root))\n"
        "print('\\n'.join(own))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        env=env, cwd="/", check=True,
    ).stdout.split()
    assert "memory.store" in out and "memory.registry" in out, out
    allowed_ports = {"config", "timekeeping"}
    foreign = [m for m in out
               if m != "memory" and not m.startswith("memory.")
               and m not in allowed_ports]
    assert foreign == [], f"memory imports Casa modules: {foreign}"
