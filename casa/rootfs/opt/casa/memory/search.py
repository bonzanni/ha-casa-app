"""``search.db``: the BM25 index of the verbatim store, and the search
behind ``search_memory``.

``search.db`` is derived. It holds one FTS5 table with every event's text
under its ``seq`` and its frozen local day, and the highest ``seq`` it has
indexed. It is never exported or restored: the wipe deletes it, and
:func:`ensure` rebuilds a missing or unreadable one from ``events`` with no
model call, at boot and on first use.

``events`` only grows. A ``seq`` is assigned inside the store's single write
transaction and an event's text never changes, so the index catches up by
``seq``: :func:`catch_up` adds every event above its high-water mark, under
the index's own write lock, before each search reads it.

The index holds no tier and is never what a hit shows. A tier can rise when
the private job classifies an event, so every candidate the index ranks is
read back from ``events`` in the search's own read snapshot, and kept only
when the reader's clearance reads its tier, before anything is paged; each
hit's window neighbours are filtered the same way. A hit's text is the
store's, so an index that outlived its store could only rank wrongly; it
could never show text the store no longer holds.

The query is split into its words mechanically (letters and digits); the
index ranks events by BM25 over any of them. Nothing here judges meaning.

Blocking; async callers use ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from memory import browse, pending, registry
from memory.store import BUSY_TIMEOUT_S as BOOT_BUSY_TIMEOUT_S
from memory.store import MemoryStore
from memory.view import BUSY_TIMEOUT_S as TURN_BUSY_TIMEOUT_S

logger = logging.getLogger(__name__)

SEARCH_DB_NAME = "search.db"
#: Bumped whenever the index's layout changes; an index of another version
#: is rebuilt.
SCHEMA_VERSION = 1
#: Hits per page.
PAGE_SIZE = 5
#: Characters of a hit's text shown; the day's browse shows it whole.
HIT_CHARS = 2000
#: Readable neighbours shown before and after a hit, and their length.
WINDOW = 2
WINDOW_CHARS = 300
#: Words of a query used; more add little to a BM25 ranking.
MAX_TERMS = 32
#: Events read from the store per batch while catching up.
CATCH_UP_BATCH = 1000
#: Candidates read from the index per batch while filtering.
CANDIDATE_BATCH = 200

_WORD = re.compile(r"\w+", re.UNICODE)
CUT = " … (cut; browse_memory_day shows it whole)"


def path_for(store: MemoryStore) -> str:
    return os.path.join(store.directory, SEARCH_DB_NAME)


def _connect(path: str, busy_timeout: float) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=busy_timeout, isolation_level=None)


def _remove(path: str) -> None:
    for name in (path, *(path + s for s in registry.SQLITE_SIDECARS)):
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _current(conn: sqlite3.Connection) -> bool:
    """Whether *conn*'s file is an index of this layout."""
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        return False
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {"docs", "indexed"} <= names


def _create(conn: sqlite3.Connection) -> None:
    """Create the index's tables, unless a concurrent caller did first. The
    file is judged inside the write lock, in one consistent state: a file
    that holds anything but an index of this layout is refused."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _current(conn):
            conn.execute("COMMIT")
            return
        if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
            raise sqlite3.DatabaseError("not an index of this layout")
        conn.execute(
            "CREATE VIRTUAL TABLE docs USING fts5(text, local_day UNINDEXED, "
            "tokenize = 'unicode61 remove_diacritics 2')")
        conn.execute("CREATE TABLE indexed (id INTEGER PRIMARY KEY "
                     "CHECK (id = 1), through INTEGER NOT NULL)")
        conn.execute("INSERT INTO indexed (id, through) VALUES (1, 0)")
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _open(store: MemoryStore, busy_timeout: float) -> sqlite3.Connection:
    """A connection to an index of this layout, created when missing."""
    path = path_for(store)
    conn = _connect(path, busy_timeout)
    try:
        if not _current(conn):
            # Read outside a transaction, this can see a creation half
            # committed; _create judges again under the write lock.
            _create(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def catch_up(store: MemoryStore, *,
             busy_timeout: float = TURN_BUSY_TIMEOUT_S) -> int:
    """Index every event above the high-water mark, in one transaction of
    the index. Every wait on the index's or the store's lock is bounded by
    *busy_timeout* (the turn-path bound, unless boot passes its own).
    Returns the number of events added. Raises when the store or the index
    cannot be read, or is locked past the bound."""
    conn = _open(store, busy_timeout)
    added = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            through = conn.execute(
                "SELECT through FROM indexed WHERE id = 1").fetchone()[0]
            while True:
                with store.transaction(busy_timeout=busy_timeout) as mconn:
                    rows = mconn.execute(
                        "SELECT seq, text, local_day FROM events "
                        "WHERE seq > ? ORDER BY seq LIMIT ?",
                        (through, CATCH_UP_BATCH)).fetchall()
                if not rows:
                    break
                conn.executemany(
                    "INSERT INTO docs (rowid, text, local_day) "
                    "VALUES (?, ?, ?)", rows)
                added += len(rows)
                through = rows[-1][0]
            conn.execute("UPDATE indexed SET through = ? WHERE id = 1",
                         (through,))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return added


def rebuild(store: MemoryStore) -> int:
    """Delete the index and build it afresh from ``events``."""
    _remove(path_for(store))
    return catch_up(store, busy_timeout=BOOT_BUSY_TIMEOUT_S)


def ensure(store: MemoryStore) -> bool:
    """The boot check: an index that is missing, of another layout or
    unreadable is rebuilt, and a present one is brought current. Returns
    whether it was rebuilt. Never raises: a store that is unavailable has
    no index to build, and a failure leaves search to try again on first
    use."""
    if not store.available:
        return False
    path = path_for(store)
    rebuilt = False
    try:
        if os.path.lexists(path):
            try:
                conn = _connect(path, BOOT_BUSY_TIMEOUT_S)
                try:
                    usable = _current(conn) and conn.execute(
                        "PRAGMA quick_check").fetchone()[0] == "ok"
                finally:
                    conn.close()
            except sqlite3.DatabaseError:
                usable = False
            if not usable:
                logger.warning("memory search index %s is unreadable; "
                               "rebuilt", path)
                _remove(path)
        rebuilt = not os.path.lexists(path)
        added = catch_up(store, busy_timeout=BOOT_BUSY_TIMEOUT_S)
        if rebuilt:
            logger.info("memory search index built (%d events)", added)
    except Exception:  # noqa: BLE001 — search retries on first use
        logger.warning("memory search index not ready", exc_info=True)
    return rebuilt


def terms(query: str) -> list[str]:
    """The words of *query*, in order, each once."""
    seen: list[str] = []
    for word in _WORD.findall((query or "").lower()):
        if word not in seen:
            seen.append(word)
    return seen[:MAX_TERMS]


def match_expression(words: list[str]) -> str:
    """An FTS5 query that matches an event holding any of *words*."""
    return " OR ".join(f'"{w}"' for w in words)


@dataclass(frozen=True)
class Found:
    """One page of a search. ``more`` says a later page holds more hits."""

    hits: list[dict] = field(default_factory=list)
    page: int = 1
    more: bool = False


def _hit(event: dict, before: list[dict], after: list[dict],
         reader_role: str | None) -> dict:
    at = browse.local_time(event)
    hit = {"date": event["local_day"],
           "weekday": date.fromisoformat(event["local_day"]).strftime("%A"),
           "time": at.strftime("%H:%M") if at else "",
           "speaker": browse.speaker(event, reader_role),
           "text": browse.clip(event["text"], HIT_CHARS, CUT)}
    if event["scheduled"]:
        hit["scheduled"] = True
    if before:
        hit["before"] = [browse.line(e, reader_role, limit=WINDOW_CHARS)
                         for e in before]
    if after:
        hit["after"] = [browse.line(e, reader_role, limit=WINDOW_CHARS)
                        for e in after]
    return hit


_SELECT = f"SELECT {', '.join('e.' + c for c in browse.EVENT_COLUMNS)} " \
          "FROM events AS e"


def _neighbours(conn: sqlite3.Connection, event: dict, readable: int,
                *, earlier: bool) -> list[dict]:
    """Up to :data:`WINDOW` readable events next to *event* in its role's
    conversation on its day."""
    op, order = ("<", "DESC") if earlier else (">", "ASC")
    found = browse.rows(
        conn, f"{_SELECT} WHERE e.role = ? AND e.channel = ? "
        f"AND e.local_day = ? AND e.seq {op} ? AND {browse.TIER_RANK} <= ? "
        f"ORDER BY e.seq {order} LIMIT ?",
        (event["role"], event["channel"], event["local_day"], event["seq"],
         readable, WINDOW))
    return list(reversed(found)) if earlier else found


def search(store: MemoryStore, query: str, *, clearance: str,
           first_day: date | None = None, last_day: date | None = None,
           page: int = 1, reader_role: str | None = None) -> Found:
    """Page *page* of the events readable at *clearance* that hold any word
    of *query*, best match first, optionally only those said between
    *first_day* and *last_day* (local days, inclusive). Raises
    ``ValueError`` for a query with no words, and when the store or the
    index fails."""
    words = terms(query)
    if not words:
        raise ValueError("the query has no words")
    catch_up(store)
    readable = pending.rank(clearance)
    sql = "SELECT rowid FROM docs WHERE docs MATCH ?"
    args: list = [match_expression(words)]
    if first_day is not None:
        sql += " AND local_day >= ?"
        args.append(first_day.isoformat())
    if last_day is not None:
        sql += " AND local_day <= ?"
        args.append(last_day.isoformat())
    sql += " ORDER BY rank"
    wanted = max(page, 1) * PAGE_SIZE + 1
    kept: list[dict] = []
    index = _open(store, TURN_BUSY_TIMEOUT_S)
    try:
        with store.transaction(busy_timeout=TURN_BUSY_TIMEOUT_S) as conn:
            cursor = index.execute(sql, args)
            while len(kept) < wanted:
                seqs = [r[0] for r in cursor.fetchmany(CANDIDATE_BATCH)]
                if not seqs:
                    break
                by_seq = {e["seq"]: e for e in browse.rows(
                    conn, f"{_SELECT} WHERE e.seq IN "
                    f"({', '.join('?' for _ in seqs)}) "
                    f"AND {browse.TIER_RANK} <= ?",
                    (*seqs, readable))}
                kept.extend(by_seq[s] for s in seqs if s in by_seq)
            shown = kept[(max(page, 1) - 1) * PAGE_SIZE:][:PAGE_SIZE]
            hits = [_hit(e, _neighbours(conn, e, readable, earlier=True),
                         _neighbours(conn, e, readable, earlier=False),
                         reader_role) for e in shown]
    finally:
        index.close()
    return Found(hits=hits, page=page, more=len(kept) >= wanted)
