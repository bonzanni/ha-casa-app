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

#: The ledgers Casa maintains. ``private`` always; a lower ledger joins when
#: a reader of its clearance is served one.
MAINTAINED_LEDGERS: tuple[str, ...] = ("private",)

#: The clearance ladder, lowest first.
TIERS: tuple[str, ...] = ("public", "friends", "family", "private")


def rank(tier: str) -> int:
    return TIERS.index(tier)


def current_commit_no(conn: sqlite3.Connection) -> int:
    """The highest ``commit_no`` the store's rows carry (0 when none)."""
    return conn.execute(
        "SELECT max("
        " coalesce((SELECT max(commit_no) FROM dispositions), 0),"
        " coalesce((SELECT max(commit_no) FROM revisions), 0),"
        " coalesce((SELECT max(mark_commit_no) FROM correction_marks), 0))"
    ).fetchone()[0]


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
    """The maintained ledgers *seq* is pending in."""
    have = {r[0] for r in conn.execute(
        "SELECT ledger FROM pending WHERE seq = ?", (seq,))}
    return [ledger for ledger in MAINTAINED_LEDGERS if ledger in have]


def pending_corrections(conn: sqlite3.Connection, ledger: str) -> list[int]:
    """Marked events pending in *ledger*: its pending corrections."""
    return [r[0] for r in conn.execute(
        "SELECT p.seq FROM pending AS p JOIN correction_marks AS m "
        "ON m.seq = p.seq WHERE p.ledger = ? ORDER BY m.mark_no", (ledger,))]


def pending_times(conn: sqlite3.Connection, ledger: str) -> list[str]:
    """``recorded_at`` of every event pending in *ledger* (when the store
    took it, so the writer could first see it), oldest first."""
    return [r[0] for r in conn.execute(
        "SELECT e.recorded_at FROM pending AS p JOIN events AS e "
        "ON e.seq = p.seq WHERE p.ledger = ? ORDER BY e.recorded_at",
        (ledger,))]
