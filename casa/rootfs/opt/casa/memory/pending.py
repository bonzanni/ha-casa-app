"""The one pending predicate, and the two counters it is ordered by.

``pending(e, L)`` holds while event *e* is eligible for ledger *L* (its tier
at or below *L*, and classified unless *L* is ``private``) and has no
terminal disposition in *L* committed after its latest correction mark. A
disposition is terminal when it is ``patched`` or ``no_change``; ``deferred``
is not. So a correction mark re-opens an event every ledger had filed, and an
unmarked event is pending until its first terminal disposition.

The predicate is the SQL view ``pending(seq, ledger)`` (:mod:`memory.schema`),
and every consumer reads it: writer scheduling and input assembly, correction
marking, the meter, and later the correction note and the export and restore
checks. No other record of whether an event was handled exists
(INV-MEM-027).

The two counters are never stored. Inside the writing transaction, the next
``commit_no`` is one more than the highest ``commit_no`` any disposition or
revision carries and any ``mark_commit_no`` a correction mark carries; the
next ``mark_no`` is one more than the highest ``mark_no``. A store holding the
rows therefore holds its counters, and a fresh store starts both at 1.

Every function takes an open connection, inside the caller's transaction.
"""
from __future__ import annotations

import sqlite3

from memory import ledgers

#: The clearance ladder, lowest first.
TIERS: tuple[str, ...] = ("public", "friends", "family", "private")


def rank(tier: str) -> int:
    return TIERS.index(tier)


#: The range of an event's ``seq``: SQLite's signed 64-bit integer. A value
#: outside it names no event and cannot even be bound to a query.
SEQ_MIN, SEQ_MAX = -2 ** 63, 2 ** 63 - 1


def is_seq(value) -> bool:
    """Whether *value* could be an event's ``seq``: an ``int``, not a
    ``bool``, within SQLite's integer range."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and SEQ_MIN <= value <= SEQ_MAX)


def current_commit_no(conn: sqlite3.Connection) -> int:
    """The highest ``commit_no`` the store's rows carry (0 when none)."""
    return conn.execute(
        "SELECT max("
        " coalesce((SELECT max(commit_no) FROM dispositions), 0),"
        " coalesce((SELECT max(commit_no) FROM revisions), 0),"
        " coalesce((SELECT max(mark_commit_no) FROM correction_marks), 0))"
    ).fetchone()[0]


def commit_no_at(conn: sqlite3.Connection, at: str) -> int:
    """The newest ``commit_no`` committed at or before *at* (an ISO-8601 UTC
    time in the form the writer stamps), 0 when none: the state of the
    ledgers at that moment. Every commit stamps its time on its revisions
    and its dispositions."""
    return conn.execute(
        "SELECT max("
        " coalesce((SELECT max(commit_no) FROM revisions"
        "           WHERE committed_at <= ?), 0),"
        " coalesce((SELECT max(commit_no) FROM dispositions"
        "           WHERE committed_at <= ?), 0))", (at, at)).fetchone()[0]


def next_commit_no(conn: sqlite3.Connection) -> int:
    """The ``commit_no`` a commit in this write transaction takes."""
    return current_commit_no(conn) + 1


def next_mark_no(conn: sqlite3.Connection) -> int:
    """The ``mark_no`` a mark in this write transaction takes."""
    return conn.execute(
        "SELECT coalesce(max(mark_no), 0) + 1 FROM correction_marks"
    ).fetchone()[0]


def pending_seqs(conn: sqlite3.Connection, ledger: str) -> list[int]:
    """Every event pending in *ledger*, oldest first."""
    return [r[0] for r in conn.execute(
        "SELECT seq FROM pending WHERE ledger = ? ORDER BY seq", (ledger,))]


def is_pending(conn: sqlite3.Connection, seq: int, ledger: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM pending WHERE seq = ? AND ledger = ?",
        (seq, ledger)).fetchone() is not None


def pending_in(conn: sqlite3.Connection, seq: int) -> list[str]:
    """The maintained ledgers *seq* is pending in (:mod:`memory.ledgers`),
    ``private`` first."""
    have = {r[0] for r in conn.execute(
        "SELECT ledger FROM pending WHERE seq = ?", (seq,))}
    return [ledger for ledger in ledgers.maintained() if ledger in have]


def pending_corrections(conn: sqlite3.Connection, ledger: str) -> list[int]:
    """Marked events pending in *ledger*: its pending corrections."""
    return [r[0] for r in conn.execute(
        "SELECT p.seq FROM pending AS p JOIN correction_marks AS m "
        "ON m.seq = p.seq WHERE p.ledger = ? ORDER BY m.mark_no", (ledger,))]


def pending_arrivals(conn: sqlite3.Connection,
                     ledger: str) -> list[tuple[int, str]]:
    """``(seq, recorded_at)`` of every event pending in *ledger*, in ``seq``
    order: the order the store's single writer committed them in, and when
    the store took each, so the writer could first see it."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT e.seq, e.recorded_at FROM pending AS p JOIN events AS e "
        "ON e.seq = p.seq WHERE p.ledger = ? ORDER BY e.seq", (ledger,))]
