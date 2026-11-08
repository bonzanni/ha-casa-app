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
        "correction_marks", "session_views", "transcript_runs", "meta",
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


_FAILING_V2 = {
    1: schema.V1,
    # Version 2 creates a table, then fails part-way through.
    2: ("CREATE TABLE added_in_v2 (x TEXT) STRICT", "THIS IS NOT A STATEMENT"),
}


def _v1_store(directory: Path, journal_mode: str, canary: str) -> str:
    """A version-1 store in *journal_mode*, holding *canary*, closed."""
    store = MemoryStore(str(directory))
    assert store.open() is True, store.unavailable_reason
    conn = _raw(store.path)
    try:
        assert conn.execute(
            f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                     ("canary", json.dumps(canary)))
    finally:
        conn.close()
    return store.path


def test_failed_migration_keeps_the_old_file_and_reports_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = secrets.token_hex(8)
    # A store in WAL (what Casa writes) and one in a rollback journal (a
    # restored single-file store): the failure must not touch either.
    paths = {mode: _v1_store(tmp_path / mode, mode, canary)
             for mode in ("wal", "delete")}
    before = {mode: (_sha256(p), sorted(os.listdir(os.path.dirname(p))))
              for mode, p in paths.items()}

    monkeypatch.setattr(store_mod, "MIGRATIONS", _FAILING_V2)
    for mode, path in paths.items():
        later = MemoryStore(os.path.dirname(path))
        assert later.open() is False, mode
        assert later.available is False
        assert later.unavailable_reason
        with pytest.raises(MemoryUnavailable):
            with later.transaction():
                pass

        assert (_sha256(path), sorted(os.listdir(os.path.dirname(path)))) \
            == before[mode], mode
        conn = _raw(path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == mode
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
            assert conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'added_in_v2'"
            ).fetchone()[0] == 0
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'canary'").fetchone()
            assert json.loads(row[0]) == canary
        finally:
            conn.close()

    # Where there was no store, a failed creation leaves nothing behind.
    fresh = tmp_path / "fresh" / "memory"
    monkeypatch.setattr(store_mod, "MIGRATIONS", {
        1: (*schema.V1, "THIS IS NOT A STATEMENT")})
    assert MemoryStore(str(fresh)).open() is False
    assert not fresh.exists()


def test_wal_failure_after_a_committed_migration_keeps_the_store_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``open`` reports unavailable only when the store is unchanged. Once a
    migration has committed, a failed switch to WAL (a performance setting)
    leaves the store current and usable in its journal mode."""
    def refuse(self):
        raise sqlite3.OperationalError("journal mode change refused")

    # A v1 store in a rollback journal, migrated to v2 while WAL fails.
    canary = secrets.token_hex(8)
    path = _v1_store(tmp_path / "existing", "delete", canary)
    monkeypatch.setattr(store_mod, "MIGRATIONS", {
        1: schema.V1, 2: ("CREATE TABLE added_in_v2 (x TEXT) STRICT",)})
    monkeypatch.setattr(MemoryStore, "_enable_wal", refuse)
    later = MemoryStore(os.path.dirname(path))
    assert later.open() is True, later.unavailable_reason
    with later.transaction(write=True) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        conn.execute("INSERT INTO added_in_v2 (x) VALUES ('ok')")
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'canary'").fetchone()
        assert json.loads(row[0]) == canary

    # A store created while WAL fails is kept, too.
    fresh = MemoryStore(str(tmp_path / "fresh"))
    assert fresh.open() is True, fresh.unavailable_reason
    with fresh.transaction() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def _event_row(conn: sqlite3.Connection, text: str) -> None:
    conn.execute(
        "INSERT INTO events (source_key, occurred_at, tz, local_day, "
        "recorded_at, role, channel, speaker_kind, origin_kind, text, tier, "
        "tier_status, provenance, eligibility) VALUES (?, 't', 'UTC', "
        "'2026-11-02', 't', 'assistant', 'telegram', 'operator', 'operator', "
        "?, 'private', 'provisional', 'captured', 'eligible')",
        (f"event:{secrets.token_hex(8)}", text))


def test_a_forward_migration_runs_only_after_its_automatic_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-MEM-033: the export of the file as it is comes first; then the
    migration, in its transaction."""
    from memory import export
    canary = secrets.token_hex(8)
    v1 = MemoryStore(str(tmp_path / "memory"))
    assert v1.open() is True
    with v1.transaction(write=True) as conn:
        _event_row(conn, canary)
    monkeypatch.setattr(store_mod, "MIGRATIONS", {
        1: schema.V1, 2: ("CREATE TABLE added_in_v2 (x TEXT) STRICT",)})
    later = MemoryStore(v1.directory)
    assert later.open(before_migration=export.before_migration) is True
    [name] = os.listdir(export.exports_dir(later))
    assert name.startswith("premigration-v1-")
    manifest = export.read_manifest(os.path.join(export.exports_dir(later),
                                                 name))
    assert manifest["schema_version"] == 1
    assert manifest["counts"]["tables"]["events"] == 1
    with later.transaction() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    # A store already current takes no export.
    again = MemoryStore(v1.directory)
    assert again.open(before_migration=export.before_migration) is True
    assert len(os.listdir(export.exports_dir(later))) == 1


def test_a_failed_automatic_export_leaves_the_store_unmigrated_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-MEM-033: no migration runs without its export, and the store is
    then reported unavailable, never as an empty healthy one."""
    canary = secrets.token_hex(8)
    path = _v1_store(tmp_path / "memory", "wal", canary)
    before = _sha256(path)
    monkeypatch.setattr(store_mod, "MIGRATIONS", {
        1: schema.V1, 2: ("CREATE TABLE added_in_v2 (x TEXT) STRICT",)})

    def no_room(store, version):
        raise OSError("no space left on device")

    later = MemoryStore(os.path.dirname(path))
    assert later.open(before_migration=no_room) is False
    assert "no space left" in (later.unavailable_reason or "")
    with pytest.raises(MemoryUnavailable):
        with later.transaction():
            pass
    assert _sha256(path) == before
    conn = _raw(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
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
    # The declared ports, plus three stdlib-only leaf helpers the
    # container-start maintenance step writes and restarts through: the
    # atomic writer, the no-symlink workspace writer, and the Supervisor's
    # restart request.
    allowed_ports = {"config", "timekeeping", "atomic_io", "safe_fs",
                     "supervisor_restart"}
    foreign = [m for m in out
               if m != "memory" and not m.startswith("memory.")
               and m not in allowed_ports]
    assert foreign == [], f"memory imports Casa modules: {foreign}"
