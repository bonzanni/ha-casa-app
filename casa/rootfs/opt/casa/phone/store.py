"""The connection owner of ``phone.db``, and the queries over it.

Connection discipline follows :mod:`contacts.store`: every access is one
transaction on its own connection (``BEGIN IMMEDIATE`` for a write, ``BEGIN``
for a read), closed on exit. :meth:`PhoneStore.open` creates the directory
(0700) and brings the file to the current schema version in one transaction,
then switches it to WAL. It never raises: on failure the store is unavailable,
its reason is kept, and :meth:`PhoneStore.transaction` raises
:class:`PhoneUnavailable`. A file written by a newer Casa is refused and left
as it is.

The functions below take an open connection, so a caller composes them inside
one transaction. Everything here is blocking; async callers run it with
``asyncio.to_thread``.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from phone.schema import MIGRATIONS

logger = logging.getLogger(__name__)

PHONE_DIR = "/data/phone"
PHONE_DB_NAME = "phone.db"

#: Seconds a connection waits on another's write lock before failing.
BUSY_TIMEOUT_S = 30.0

_SIDECARS = ("-wal", "-shm", "-journal")


class PhoneUnavailable(RuntimeError):
    """The store could not be opened; the phone line is off until it can."""


def schema_version() -> int:
    """The version this code writes: the highest migration it carries."""
    return max(MIGRATIONS)


class PhoneStore:
    """``phone.db`` under *directory* (``/data/phone`` in production)."""

    def __init__(self, directory: str = PHONE_DIR) -> None:
        self.directory = directory
        self.path = os.path.join(directory, PHONE_DB_NAME)
        self._unavailable_reason: str | None = "not opened"

    @property
    def available(self) -> bool:
        return self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def open(self) -> bool:
        """Create or migrate the file. Returns :attr:`available`; never
        raises."""
        try:
            self._open()
        except Exception as exc:  # noqa: BLE001 — any failure means unavailable
            self._unavailable_reason = f"{type(exc).__name__}: {exc}"
            logger.error("phone store unavailable (%s): %s", self.path,
                         self._unavailable_reason)
            return False
        self._unavailable_reason = None
        logger.info("phone store open (%s, schema v%d)", self.path,
                    schema_version())
        return True

    def _open(self) -> None:
        created_dir = not os.path.isdir(self.directory)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        created_file = not os.path.lexists(self.path)
        try:
            self._migrate(schema_version())
        except BaseException:
            if created_file:
                for name in (self.path, *(self.path + s for s in _SIDECARS)):
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
        # WAL is a performance setting: once the schema is current the store
        # is usable in any journal mode, so a failure here is only logged.
        try:
            conn = self._connect()
            try:
                got = conn.execute("PRAGMA journal_mode=wal").fetchone()[0]
            finally:
                conn.close()
            if str(got).lower() != "wal":
                raise sqlite3.OperationalError(f"journal mode {got!r}")
        except Exception as exc:  # noqa: BLE001 — logged, store stays usable
            logger.warning(
                "phone store stays in its current journal mode (%s): %s",
                self.path, exc)

    def _migrate(self, target: int) -> None:
        """Bring the file to *target* in one transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                if current > target:
                    raise PhoneUnavailable(
                        f"schema v{current} is newer than this Casa (v{target})")
                if current == 0:
                    tables = conn.execute(
                        "SELECT count(*) FROM sqlite_master").fetchone()[0]
                    if tables:
                        raise PhoneUnavailable(
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
                logger.info("phone store migrated v%d -> v%d",
                            current, target)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def transaction(self, *, write: bool = False
                    ) -> Iterator[sqlite3.Connection]:
        """One transaction on a connection of its own, closed on exit.

        ``write=True`` takes the write lock at once (``BEGIN IMMEDIATE``); a
        read is a deferred ``BEGIN``. The block's exception rolls back and
        propagates."""
        if not self.available:
            raise PhoneUnavailable(
                self._unavailable_reason or "unavailable")
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


# --- settings and voices ---------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?",
                       (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value))


def voices(conn: sqlite3.Connection) -> dict[str, str]:
    """The stored voice of each language."""
    return dict(conn.execute("SELECT language, voice FROM voices"))


def set_voice(conn: sqlite3.Connection, language: str, voice: str,
              description: str = "") -> None:
    conn.execute(
        "INSERT INTO voices (language, voice, description) VALUES (?, ?, ?) "
        "ON CONFLICT (language) DO UPDATE SET voice = excluded.voice, "
        "description = excluded.description",
        (language, voice, description))


# --- grants -----------------------------------------------------------------

def grants(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """The stored grants only; defaults are the caller's to apply."""
    return {(c, a): lvl for c, a, lvl in conn.execute(
        "SELECT caller_class, ability, level FROM grants")}


def set_grant(conn: sqlite3.Connection, caller_class: str, ability: str,
              level: str) -> None:
    """Raises :class:`sqlite3.IntegrityError` for an unknown class or level."""
    conn.execute(
        "INSERT INTO grants (caller_class, ability, level) VALUES (?, ?, ?) "
        "ON CONFLICT (caller_class, ability) DO UPDATE SET "
        "level = excluded.level", (caller_class, ability, level))


# --- calls ------------------------------------------------------------------

_CALL_COLUMNS = ("id", "session_id", "started_at", "ended_at", "number",
                 "name", "caller_class", "language", "returned", "outcome",
                 "actions", "record", "delivered")
_SELECT_CALL = f"SELECT {', '.join(_CALL_COLUMNS)} FROM calls"


def _call(row: tuple) -> dict:
    call = dict(zip(_CALL_COLUMNS, row))
    for key in ("actions", "record"):
        if call[key] is not None:
            call[key] = json.loads(call[key])
    return call


def insert_call(conn: sqlite3.Connection, row: dict) -> None:
    """Insert a call from the columns *row* names; the others stay unset."""
    unknown = set(row) - set(_CALL_COLUMNS)
    if unknown:
        raise ValueError(f"unknown call columns {sorted(unknown)}")
    values = {k: (json.dumps(v) if k in ("actions", "record") else v)
              for k, v in row.items()}
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO calls ({names}) VALUES ({marks})",
                 tuple(values.values()))


def finish_call(conn: sqlite3.Connection, call_id: str, *, ended_at: float,
                outcome: str, actions: object, record: object) -> bool:
    """True when the call exists and was updated."""
    return conn.execute(
        "UPDATE calls SET ended_at = ?, outcome = ?, actions = ?, "
        "record = ? WHERE id = ?",
        (ended_at, outcome, json.dumps(actions), json.dumps(record),
         call_id)).rowcount > 0


def set_record(conn: sqlite3.Connection, call_id: str, record: object) -> bool:
    """Store the call's record and nothing else; True when the call exists."""
    return conn.execute("UPDATE calls SET record = ? WHERE id = ?",
                        (json.dumps(record), call_id)).rowcount > 0


def mark_delivered(conn: sqlite3.Connection, call_id: str) -> bool:
    """True when the call exists and was marked."""
    return conn.execute("UPDATE calls SET delivered = 1 WHERE id = ?",
                        (call_id,)).rowcount > 0


def get_call(conn: sqlite3.Connection, call_id: str) -> dict | None:
    row = conn.execute(_SELECT_CALL + " WHERE id = ?", (call_id,)).fetchone()
    return _call(row) if row else None


def recent_calls(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """The latest calls, newest start first."""
    return [_call(r) for r in conn.execute(
        _SELECT_CALL + " ORDER BY started_at DESC, id DESC LIMIT ?",
        (limit,))]


def undelivered(conn: sqlite3.Connection) -> list[dict]:
    """Calls not yet reported to the operator, oldest first."""
    return [_call(r) for r in conn.execute(
        _SELECT_CALL + " WHERE delivered = 0 ORDER BY started_at, id")]


# --- transcripts ------------------------------------------------------------

def put_transcript(conn: sqlite3.Connection, call_id: str, text: str,
                   expires_at: float) -> None:
    conn.execute(
        "INSERT INTO transcripts (call_id, text, expires_at) "
        "VALUES (?, ?, ?) ON CONFLICT (call_id) DO UPDATE SET "
        "text = excluded.text, expires_at = excluded.expires_at",
        (call_id, text, expires_at))


def transcript(conn: sqlite3.Connection, call_id: str) -> str | None:
    row = conn.execute("SELECT text FROM transcripts WHERE call_id = ?",
                       (call_id,)).fetchone()
    return row[0] if row else None


def purge_transcripts(conn: sqlite3.Connection, now: float) -> int:
    """Delete the transcripts expired at *now*; returns how many."""
    return conn.execute("DELETE FROM transcripts WHERE expires_at <= ?",
                        (now,)).rowcount
