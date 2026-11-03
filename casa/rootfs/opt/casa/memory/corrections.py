"""Correction marking: the operator says a stored fact is wrong.

:func:`mark` marks the operator's originating event in one ``BEGIN
IMMEDIATE`` transaction. A mark records the next global ``mark_no`` and, as
``mark_commit_no``, the global ``commit_no`` read inside that same
transaction, so SQLite's single writer orders the mark against every writer
commit. A terminal disposition counts only when it was committed after the
mark (:mod:`memory.pending`), so the mark re-opens an event every ledger had
already filed, whether that commit landed just before the mark or long ago.

Marking is idempotent: while the event's mark is still pending in a
maintained ledger, a second mark (a retried tool call) changes nothing and
queues nothing. Once every ledger has filed the event after its mark, a new
mark replaces the old one.

In the same transaction a correction job is queued for every maintained
ledger the event is now pending in, so a mark and its jobs are stored
together or not at all; the writer runs them next. A writer job already
holding the event is rejected at its commit, because the event's mark is no
longer the one it read.

:func:`mark_operator_event` is ``correct_memory``'s entry point: it marks the
operator message a turn opened with, found by the occurrence key its capture
stored it under. It marks only a message already in the store; one still in
the spool is left to the background drain, and the caller answers without
the mark.

Blocking; async callers use ``asyncio.to_thread``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from memory import pending, view, writer
from memory.store import MemoryStore


@dataclass(frozen=True)
class MarkResult:
    #: Whether this call wrote a mark (``False``: the earlier one is still
    #: pending, or the event does not exist).
    marked: bool
    #: The ledgers a correction job was queued for.
    queued: tuple[str, ...] = ()
    found: bool = True


def mark(store: MemoryStore, seq: int, *,
         busy_timeout: float | None = None) -> MarkResult:
    """Mark event *seq* as corrected and queue its correction jobs.
    *busy_timeout* bounds the wait for the store's write lock (the store's
    default when ``None``); past it, ``sqlite3.OperationalError`` is
    raised and nothing is written."""
    bound = {} if busy_timeout is None else {"busy_timeout": busy_timeout}
    with store.transaction(write=True, **bound) as conn:
        if conn.execute("SELECT 1 FROM events WHERE seq = ?",
                        (seq,)).fetchone() is None:
            return MarkResult(marked=False, found=False)
        already = conn.execute(
            "SELECT 1 FROM correction_marks WHERE seq = ?", (seq,)
        ).fetchone() is not None
        if already and pending.pending_in(conn, seq):
            return MarkResult(marked=False)
        conn.execute(
            "INSERT INTO correction_marks (seq, mark_no, marked_at, "
            "mark_commit_no) VALUES (?, ?, ?, ?) ON CONFLICT(seq) DO UPDATE "
            "SET mark_no = excluded.mark_no, marked_at = excluded.marked_at, "
            "mark_commit_no = excluded.mark_commit_no",
            (seq, pending.next_mark_no(conn),
             datetime.now(timezone.utc).isoformat(timespec="microseconds"),
             pending.current_commit_no(conn)))
        ledgers = tuple(pending.pending_in(conn, seq))
        for ledger in ledgers:
            writer._enqueue(conn, ledger, writer.CORRECTION)
    if ledgers:
        writer.wake()
    return MarkResult(marked=True, queued=ledgers)


#: Seconds ``correct_memory`` waits for the store's lock: the turn-path bound
#: (``memory.view.BUSY_TIMEOUT_S``), so a tool call never holds a turn behind
#: a writer; a busy store answers "could not mark" instead.
TOOL_BUSY_TIMEOUT_S = view.BUSY_TIMEOUT_S


def _operator_seq(store: MemoryStore, source_key: str) -> int | None:
    with store.transaction(busy_timeout=TOOL_BUSY_TIMEOUT_S) as conn:
        row = conn.execute(
            "SELECT seq FROM events WHERE source_key = ? "
            "AND speaker_kind = 'operator'", (source_key,)).fetchone()
    return None if row is None else row[0]


def mark_operator_event(store: MemoryStore, source_key: str) -> MarkResult:
    """Mark the operator message stored under *source_key*. ``found`` is
    false when no operator message is stored under that key — one still in
    the spool included: nothing here drains it, since a drain can take as
    long as its backlog and waits on spool writes. Every store wait is
    bounded by :data:`TOOL_BUSY_TIMEOUT_S`; a store busy past it raises
    ``sqlite3.OperationalError`` and nothing is marked."""
    seq = _operator_seq(store, source_key)
    if seq is None:
        return MarkResult(marked=False, found=False)
    return mark(store, seq, busy_timeout=TOOL_BUSY_TIMEOUT_S)
