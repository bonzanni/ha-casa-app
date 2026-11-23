"""The connection owner of ``contacts.db``, and the queries over it.

The store is a read-only mirror of each address-book source (``people`` and
``numbers``, rewritten whole by every sync of that source) plus the marks Casa
owns about a person: priority, relation and language, one row per origin.

Connection discipline follows :mod:`memory.store`: every access is one
transaction on its own connection (``BEGIN IMMEDIATE`` for a write, ``BEGIN``
for a read), closed on exit. :meth:`ContactsStore.open` creates the directory
(0700) and brings the file to the current schema version in one transaction,
then switches it to WAL. It never raises: on failure the store is unavailable,
its reason is kept, and :meth:`ContactsStore.transaction` raises
:class:`ContactsUnavailable`. A file written by a newer Casa is refused and
left as it is.

The functions below take an open connection, so a caller composes them inside
one transaction. Everything here is blocking; async callers run it with
``asyncio.to_thread``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from contacts import numbers as phone_numbers
from contacts.schema import MIGRATIONS

logger = logging.getLogger(__name__)

CONTACTS_DIR = "/data/contacts"
CONTACTS_DB_NAME = "contacts.db"

#: Seconds a connection waits on another's write lock before failing.
BUSY_TIMEOUT_S = 30.0

_SIDECARS = ("-wal", "-shm", "-journal")
_FIELDS = ("priority", "relation", "language")
#: Origins, strongest first: the effective mark is the strongest row.
_ORIGINS = ("operator", "group", "note")
_PRIORITY_ORIGINS = ("operator", "group")


class ContactsUnavailable(RuntimeError):
    """The store could not be opened; contacts are off until it can."""


def schema_version() -> int:
    """The version this code writes: the highest migration it carries."""
    return max(MIGRATIONS)


@dataclass(frozen=True)
class Person:
    source: str
    uid: str
    name: str
    first_name: str
    org: str
    note: str
    numbers: tuple[str, ...]
    groups: tuple[str, ...]
    related: tuple[str, ...]


@dataclass(frozen=True)
class Marks:
    """The effective marks of one person; ``""`` when a field is unset."""
    priority: bool
    priority_origin: str | None
    relation: str
    language: str


class ContactsStore:
    """``contacts.db`` under *directory* (``/data/contacts`` in production)."""

    def __init__(self, directory: str = CONTACTS_DIR) -> None:
        self.directory = directory
        self.path = os.path.join(directory, CONTACTS_DB_NAME)
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
            logger.error("contacts store unavailable (%s): %s", self.path,
                         self._unavailable_reason)
            return False
        self._unavailable_reason = None
        logger.info("contacts store open (%s, schema v%d)", self.path,
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
                "contacts store stays in its current journal mode (%s): %s",
                self.path, exc)

    def _migrate(self, target: int) -> None:
        """Bring the file to *target* in one transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                if current > target:
                    raise ContactsUnavailable(
                        f"schema v{current} is newer than this Casa (v{target})")
                if current == 0:
                    tables = conn.execute(
                        "SELECT count(*) FROM sqlite_master").fetchone()[0]
                    if tables:
                        raise ContactsUnavailable(
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
                logger.info("contacts store migrated v%d -> v%d",
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
            raise ContactsUnavailable(
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


# --- the mirror -------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def note_hash(note: str) -> str:
    """The hash a note is stored and settled under."""
    return hashlib.sha256(note.encode("utf-8")).hexdigest()


def replace_source(conn: sqlite3.Connection, source: str,
                   people: list[Person]) -> dict:
    """Rewrite *source*'s people and numbers to exactly *people*.

    Other sources are untouched. A person no longer present loses its
    ``group``- and ``note``-origin marks and its ``note_reads`` row;
    ``operator``-origin marks are never
    deleted here, so they apply again if the same uid returns. A uid given
    twice keeps its last occurrence. ``note_changed`` lists the uids that are
    new or whose note differs, in the order given."""
    incoming: dict[str, Person] = {}
    for person in people:
        incoming[person.uid] = person
    before = dict(conn.execute(
        "SELECT uid, note_hash FROM people WHERE source = ?", (source,)))

    conn.execute("DELETE FROM numbers WHERE source = ?", (source,))
    conn.execute("DELETE FROM people WHERE source = ?", (source,))
    note_changed: list[str] = []
    for uid, p in incoming.items():
        digest = note_hash(p.note)
        if before.get(uid) != digest:
            note_changed.append(uid)
        conn.execute(
            "INSERT INTO people (source, uid, name, first_name, org, note, "
            "note_hash, groups, related) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, uid, p.name, p.first_name, p.org, p.note, digest,
             json.dumps(list(p.groups)), json.dumps(list(p.related))))
        conn.executemany(
            "INSERT OR IGNORE INTO numbers (e164, source, uid) "
            "VALUES (?, ?, ?)",
            [(n, source, uid) for n in p.numbers])

    gone = [uid for uid in before if uid not in incoming]
    marked = [r[0] for r in conn.execute(
        "SELECT DISTINCT uid FROM marks WHERE source = ? "
        "AND origin != 'operator'", (source,))]
    conn.executemany(
        "DELETE FROM marks WHERE source = ? AND uid = ? "
        "AND origin != 'operator'",
        [(source, uid) for uid in marked if uid not in incoming])
    settled = [r[0] for r in conn.execute(
        "SELECT uid FROM note_reads WHERE source = ?", (source,))]
    conn.executemany(
        "DELETE FROM note_reads WHERE source = ? AND uid = ?",
        [(source, uid) for uid in settled if uid not in incoming])
    return {"added": sum(1 for uid in incoming if uid not in before),
            "removed": len(gone),
            "kept": sum(1 for uid in incoming if uid in before),
            "note_changed": note_changed}


def count(conn: sqlite3.Connection, source: str | None = None) -> int:
    """How many people the mirror holds, for one source or all."""
    if source is None:
        return conn.execute("SELECT count(*) FROM people").fetchone()[0]
    return conn.execute("SELECT count(*) FROM people WHERE source = ?",
                        (source,)).fetchone()[0]


def notes_to_settle(conn: sqlite3.Connection, source: str) -> list[Person]:
    """*source*'s people whose note differs from the one last settled (no
    ``note_reads`` row counts as different), by uid. A blank note with no
    row and no ``note``-origin mark is left out: there is nothing to do."""
    rows = conn.execute(
        f"SELECT {_PEOPLE_COLUMNS_P}, r.note_hash IS NOT NULL, EXISTS ("
        "SELECT 1 FROM marks AS m WHERE m.source = p.source "
        "AND m.uid = p.uid AND m.origin = 'note') "
        "FROM people AS p LEFT JOIN note_reads AS r "
        "ON r.source = p.source AND r.uid = p.uid "
        "WHERE p.source = ? AND r.note_hash IS NOT p.note_hash "
        "ORDER BY p.uid", (source,)).fetchall()
    return [_person(conn, row[:-2]) for row in rows
            if row[5].strip() or row[-2] or row[-1]]


def settle_note(conn: sqlite3.Connection, source: str, uid: str,
                note_hash: str) -> None:
    """Record *note_hash* as the note settled into *uid*'s note marks."""
    conn.execute(
        "INSERT INTO note_reads (source, uid, note_hash) VALUES (?, ?, ?) "
        "ON CONFLICT (source, uid) DO UPDATE SET "
        "note_hash = excluded.note_hash", (source, uid, note_hash))


# --- marks ------------------------------------------------------------------

def _check_mark(field: str, origin: str) -> None:
    if field not in _FIELDS:
        raise ValueError(f"unknown mark field {field!r}")
    if origin not in _ORIGINS:
        raise ValueError(f"unknown mark origin {origin!r}")
    if field == "priority" and origin not in _PRIORITY_ORIGINS:
        raise ValueError(f"priority cannot come from {origin!r}")


def set_mark(conn: sqlite3.Connection, source: str, uid: str, field: str,
             value: str, origin: str) -> None:
    """Set *field* for one person from *origin*, replacing that origin's
    earlier value. Priority comes only from the operator or the group."""
    _check_mark(field, origin)
    conn.execute(
        "INSERT INTO marks (source, uid, field, origin, value, set_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (source, uid, field, origin) "
        "DO UPDATE SET value = excluded.value, set_at = excluded.set_at",
        (source, uid, field, origin, value, _now()))


def clear_mark(conn: sqlite3.Connection, source: str, uid: str, field: str,
               origin: str) -> None:
    """Remove *origin*'s row for *field*; a weaker origin's row, if any,
    becomes effective."""
    _check_mark(field, origin)
    conn.execute(
        "DELETE FROM marks WHERE source = ? AND uid = ? AND field = ? "
        "AND origin = ?", (source, uid, field, origin))


def marks_for(conn: sqlite3.Connection, source: str, uid: str) -> Marks:
    """The effective marks: per field, the row with the strongest origin
    (operator > group > note). Priority is on when that row's value is "1",
    so an operator "0" overrides the group's "1"."""
    strongest: dict[str, tuple[str, str]] = {}
    for field, origin, value in conn.execute(
            "SELECT field, origin, value FROM marks "
            "WHERE source = ? AND uid = ?", (source, uid)):
        held = strongest.get(field)
        if held is None or _ORIGINS.index(origin) < _ORIGINS.index(held[0]):
            strongest[field] = (origin, value)
    priority = strongest.get("priority")
    return Marks(
        priority=priority is not None and priority[1] == "1",
        priority_origin=priority[0] if priority else None,
        relation=strongest.get("relation", ("", ""))[1],
        language=strongest.get("language", ("", ""))[1])


# --- lookup -----------------------------------------------------------------

_COLUMNS = ("source", "uid", "name", "first_name", "org", "note", "groups",
            "related")
_PEOPLE_COLUMNS = ", ".join(_COLUMNS)
_PEOPLE_COLUMNS_P = ", ".join("p." + c for c in _COLUMNS)


def _person(conn: sqlite3.Connection, row: tuple) -> Person:
    source, uid, name, first_name, org, note, groups, related = row
    nums = tuple(r[0] for r in conn.execute(
        "SELECT e164 FROM numbers WHERE source = ? AND uid = ? "
        "ORDER BY e164", (source, uid)))
    return Person(source=source, uid=uid, name=name, first_name=first_name,
                  org=org, note=note, numbers=nums,
                  groups=tuple(json.loads(groups)),
                  related=tuple(json.loads(related)))


def _ordered(conn: sqlite3.Connection, people: list[Person]) -> list[Person]:
    """Effective priority first, then name; source and uid break ties so the
    order is deterministic."""
    return sorted(people, key=lambda p: (
        not marks_for(conn, p.source, p.uid).priority,
        p.name.casefold(), p.source, p.uid))


def by_number(conn: sqlite3.Connection, e164: str) -> list[Person]:
    """Everyone holding exactly *e164*, priority first, then by name."""
    rows = conn.execute(
        f"SELECT {_PEOPLE_COLUMNS_P} "
        "FROM numbers AS n JOIN people AS p "
        "ON p.source = n.source AND p.uid = n.uid WHERE n.e164 = ?",
        (e164,)).fetchall()
    return _ordered(conn, [_person(conn, r) for r in rows])


def find(conn: sqlite3.Connection, query: str, *, default_region: str,
         limit: int = 10) -> list[Person]:
    """People whose name or org contains *query* (case-insensitive), plus
    those holding exactly the number *query* reads as, priority first, then
    by name; at most *limit*."""
    needle = query.strip().casefold()
    if not needle:
        return []
    e164 = phone_numbers.to_e164(query, default_region)
    with_number: set[tuple[str, str]] = set()
    if e164 is not None:
        with_number = {(r[0], r[1]) for r in conn.execute(
            "SELECT source, uid FROM numbers WHERE e164 = ?", (e164,))}
    hits = [
        _person(conn, row) for row in conn.execute(
            f"SELECT {_PEOPLE_COLUMNS} FROM people")
        if needle in row[2].casefold() or needle in row[4].casefold()
        or (row[0], row[1]) in with_number]
    return _ordered(conn, hits)[:limit]


# --- sync state -------------------------------------------------------------

def sync_state(conn: sqlite3.Connection, source: str) -> dict:
    """``last_ok`` (time or None), ``last_error`` and ``failing`` of a
    source; a source never synced has none of them."""
    row = conn.execute(
        "SELECT last_ok, last_error, failing FROM sync_state "
        "WHERE source = ?", (source,)).fetchone()
    if row is None:
        return {"last_ok": None, "last_error": "", "failing": False}
    return {"last_ok": row[0], "last_error": row[1], "failing": bool(row[2])}


def record_sync(conn: sqlite3.Connection, source: str, *, ok: bool,
                error: str = "") -> bool:
    """Record a sync outcome. Returns True only for the first failure since
    the last success: the one the operator should be told about."""
    was_failing = sync_state(conn, source)["failing"]
    if ok:
        conn.execute(
            "INSERT INTO sync_state (source, last_ok, last_error, failing) "
            "VALUES (?, ?, '', 0) ON CONFLICT (source) DO UPDATE SET "
            "last_ok = excluded.last_ok, last_error = '', failing = 0",
            (source, _now()))
        return False
    conn.execute(
        "INSERT INTO sync_state (source, last_error, failing) "
        "VALUES (?, ?, 1) ON CONFLICT (source) DO UPDATE SET "
        "last_error = excluded.last_error, failing = 1",
        (source, error))
    return not was_failing
