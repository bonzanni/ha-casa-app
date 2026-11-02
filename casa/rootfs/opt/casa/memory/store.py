"""The connection owner of ``memory.db``.

Every access is one transaction on its own connection: :meth:`MemoryStore.
transaction` opens a connection, runs ``BEGIN IMMEDIATE`` (a write) or
``BEGIN`` (a read snapshot), commits or rolls back, and closes it. No handle
outlives its transaction, so nothing in a process keeps the file open between
uses.

The floor is Python 3.11 with SQLite 3.40, so there is no
``Connection.autocommit``: connections run with ``isolation_level=None`` and
every transaction is begun explicitly.

:meth:`MemoryStore.open` is the boot step. It creates the store directory
(0700), checks that SQLite has FTS5, and brings the file to the current schema
version in one transaction: a fresh file is migrated from version 0, an older
one forward. Only after that succeeds is the file switched to WAL, since a
journal-mode change cannot sit inside the transaction; WAL is a performance
setting, so if the switch fails the store stays available in its current
journal mode and a warning is logged. ``open`` reports the store unavailable
only when it has left it unchanged: any failure up to the migration's commit
leaves an existing file byte for byte as it was, removes a file (and
directory) this call created, and makes the store *unavailable*:
:attr:`MemoryStore.available` is false, the reason is kept, and
:meth:`MemoryStore.transaction` raises :class:`MemoryUnavailable`. ``open``
never raises, so Casa boots without memory rather than not at all.

**Before a forward migration** of an existing file, ``open`` calls its
*before_migration* argument with the file as it is (``casa-main`` passes the
automatic export, :mod:`memory.export`). If that raises, nothing is migrated
and the store is unavailable, so no migration runs without its export.

The restore builds its store with this same opener, under another name
(``restore-source/built.db``), at the export's schema version and in a
rollback journal (``journal_mode="delete"``): a single file with no sidecar
holding any of its rows. ``casa-main`` switches the installed file to WAL the
first time it opens it.

Everything here is blocking; async callers run it with ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from memory import registry
from memory.schema import MIGRATIONS

logger = logging.getLogger(__name__)

#: Seconds a connection waits on another's write lock before failing.
BUSY_TIMEOUT_S = 30.0


class MemoryUnavailable(RuntimeError):
    """The store could not be opened at boot; memory is off until it can."""


def schema_version() -> int:
    """The version this code writes: the highest migration it carries."""
    return max(MIGRATIONS)


def tables_at(version: int) -> set[str]:
    """The tables schema *version* has, derived from the migrations alone:
    their statements up to *version* applied in order to a scratch in-memory
    database, so a table created, renamed or dropped by any version is where
    the code puts it. No store is opened."""
    conn = sqlite3.connect(":memory:")
    try:
        for v in range(1, version + 1):
            for statement in MIGRATIONS[v]:
                conn.execute(statement)
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def fts5_available() -> bool:
    """Whether this SQLite can create and query an FTS5 table."""
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
        conn.execute("INSERT INTO probe (body) VALUES ('probe')")
        row = conn.execute(
            "SELECT count(*) FROM probe WHERE probe MATCH 'probe'").fetchone()
        return row[0] == 1
    except sqlite3.Error:
        return False
    finally:
        conn.close()


class MemoryStore:
    """``memory.db`` under *directory* (``/data/memory`` in production)."""

    def __init__(self, directory: str = registry.MEMORY_DIR, *,
                 name: str = registry.MEMORY_DB_NAME) -> None:
        self.directory = directory
        self.name = name
        self.path = os.path.join(directory, name)
        self._unavailable_reason: str | None = "not opened"

    def as_found(self) -> "MemoryStore":
        """The same file, usable as it is, with no boot check and no
        migration: what the automatic export reads before a migration."""
        found = MemoryStore(self.directory, name=self.name)
        found._unavailable_reason = None
        return found

    @property
    def available(self) -> bool:
        return self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def open(self, *, journal_mode: str = "wal", target: int | None = None,
             before_migration: Callable[["MemoryStore", int], object]
             | None = None) -> bool:
        """Boot check and migration. Returns :attr:`available`; never raises.

        *target* is the schema version to bring the file to (the current one
        by default; the restore builds at its export's version first).
        *journal_mode* ``"wal"`` is a performance setting whose failure is
        only logged; any other mode is part of the open, which fails without
        it. *before_migration* runs before a forward migration of an
        existing file, with that file as it is and its version."""
        try:
            self._open(journal_mode, schema_version() if target is None
                       else target, before_migration)
        except Exception as exc:  # noqa: BLE001 — any failure means unavailable
            self._unavailable_reason = f"{type(exc).__name__}: {exc}"
            logger.error(
                "memory store unavailable (%s): %s", self.path,
                self._unavailable_reason)
            return False
        self._unavailable_reason = None
        logger.info("memory store open (%s, schema v%d)", self.path,
                    schema_version() if target is None else target)
        return True

    def _open(self, journal_mode: str, target: int,
              before_migration: Callable[["MemoryStore", int], object]
              | None) -> None:
        if not fts5_available():
            raise MemoryUnavailable(
                f"SQLite {sqlite3.sqlite_version} has no FTS5")
        created_dir = not os.path.isdir(self.directory)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        created_file = not os.path.lexists(self.path)
        if before_migration is not None and not created_file:
            found = self._file_version()
            if found is not None and 0 < found < target:
                before_migration(self.as_found(), found)
        try:
            self._migrate(target)
        except BaseException:
            if created_file:
                for name in (self.path, *(self.path + s for s in
                                          registry.SQLITE_SIDECARS)):
                    try:
                        os.unlink(name)
                    except FileNotFoundError:
                        pass
            if created_dir:
                try:
                    os.rmdir(self.directory)
                except OSError:
                    pass
            raise
        if journal_mode != "wal":
            self._set_journal_mode(journal_mode)
            return
        # WAL is a performance setting, not a correctness one. Once the
        # migration has committed, the store is current and usable in any
        # journal mode, so a failure here never makes it unavailable.
        try:
            self._enable_wal()
        except Exception as exc:  # noqa: BLE001 — logged, store stays usable
            logger.warning(
                "memory store stays in its current journal mode (%s): %s",
                self.path, exc)

    def _file_version(self) -> int | None:
        """The existing file's ``user_version``, read without changing it;
        ``None`` when it cannot be read (the migration then reports why)."""
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                   timeout=BUSY_TIMEOUT_S)
        except sqlite3.Error:
            return None
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def _migrate(self, target: int) -> None:
        """Bring the file to *target* in one transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                if current > target:
                    raise MemoryUnavailable(
                        f"schema v{current} is newer than this Casa (v{target})")
                if current == 0:
                    tables = conn.execute(
                        "SELECT count(*) FROM sqlite_master").fetchone()[0]
                    if tables:
                        raise MemoryUnavailable(
                            "unversioned file that already holds a schema")
                for version in range(current + 1, target + 1):
                    for statement in MIGRATIONS[version]:
                        conn.execute(statement)
                if current != target:
                    # PRAGMA takes no bound parameter; ``target`` is an int.
                    conn.execute(f"PRAGMA user_version = {int(target)}")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            if current != target:
                logger.info(
                    "memory store migrated v%d -> v%d", current, target)
        finally:
            conn.close()

    def _enable_wal(self) -> None:
        """WAL is a property of the file and cannot change inside a
        transaction, so it is set once the schema is current."""
        self._set_journal_mode("wal")

    def _set_journal_mode(self, mode: str) -> None:
        conn = self._connect()
        try:
            # PRAGMA takes no bound parameter; *mode* is checked here.
            if mode not in ("wal", "delete"):
                raise ValueError(f"journal mode {mode!r}")
            got = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
        finally:
            conn.close()
        if str(got).lower() != mode:
            raise sqlite3.OperationalError(
                f"journal mode {got!r}, not {mode}")

    def _connect(self, busy_timeout: float = BUSY_TIMEOUT_S
                 ) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=busy_timeout, isolation_level=None)
        try:
            conn.execute("PRAGMA synchronous=FULL")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def transaction(
        self, *, write: bool = False, busy_timeout: float = BUSY_TIMEOUT_S,
    ) -> Iterator[sqlite3.Connection]:
        """One transaction on a connection of its own, closed on exit.

        ``write=True`` takes the write lock at once (``BEGIN IMMEDIATE``); a
        read is a deferred ``BEGIN``, a consistent snapshot from its first
        read. ``busy_timeout`` bounds the wait for another connection's lock;
        capture keeps it short, so a busy store spools instead of holding a
        turn. The block's exception rolls back and propagates.
        """
        if not self.available:
            raise MemoryUnavailable(self._unavailable_reason or "unavailable")
        conn = self._connect(busy_timeout)
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
