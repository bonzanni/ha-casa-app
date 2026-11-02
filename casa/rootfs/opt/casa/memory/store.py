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
one forward. Any failure leaves the file as it was and the store
*unavailable*: :attr:`MemoryStore.available` is false, the reason is kept, and
:meth:`MemoryStore.transaction` raises :class:`MemoryUnavailable`. ``open``
never raises, so Casa boots without memory rather than not at all.

Everything here is blocking; async callers run it with ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
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

    def __init__(self, directory: str = registry.MEMORY_DIR) -> None:
        self.directory = directory
        self.path = os.path.join(directory, registry.MEMORY_DB_NAME)
        self._unavailable_reason: str | None = "not opened"

    @property
    def available(self) -> bool:
        return self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def open(self) -> bool:
        """Boot check and migration. Returns :attr:`available`; never raises."""
        try:
            self._open()
        except Exception as exc:  # noqa: BLE001 — any failure means unavailable
            self._unavailable_reason = f"{type(exc).__name__}: {exc}"
            logger.error(
                "memory store unavailable (%s): %s", self.path,
                self._unavailable_reason)
            return False
        self._unavailable_reason = None
        logger.info(
            "memory store open (%s, schema v%d)", self.path, schema_version())
        return True

    def _open(self) -> None:
        if not fts5_available():
            raise MemoryUnavailable(
                f"SQLite {sqlite3.sqlite_version} has no FTS5")
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        target = schema_version()
        conn = self._connect()
        try:
            # WAL is a property of the file; it cannot change inside a
            # transaction, so it is set before the migration begins.
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise MemoryUnavailable(f"journal mode {mode!r}, not WAL")
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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        try:
            conn.execute("PRAGMA synchronous=FULL")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """One transaction on a connection of its own, closed on exit.

        ``write=True`` takes the write lock at once (``BEGIN IMMEDIATE``); a
        read is a deferred ``BEGIN``, a consistent snapshot from its first
        read. The block's exception rolls back and propagates.
        """
        if not self.available:
            raise MemoryUnavailable(self._unavailable_reason or "unavailable")
        conn = self._connect()
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
