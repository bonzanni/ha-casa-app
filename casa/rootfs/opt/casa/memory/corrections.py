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

After the transaction a correction job is queued for every maintained ledger
the event is now pending in; the writer runs it next. A writer job already
holding the event is rejected at its commit, because the event's mark is no
longer the one it read.

Blocking; async callers use ``asyncio.to_thread``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from memory import pending, writer
from memory.store import MemoryStore


@dataclass(frozen=True)
class MarkResult:
    #: Whether this call wrote a mark (``False``: the earlier one is still
    #: pending, or the event does not exist).
    marked: bool
    #: The ledgers a correction job was queued for.
    queued: tuple[str, ...] = ()
    found: bool = True


def mark(store: MemoryStore, seq: int) -> MarkResult:
    """Mark event *seq* as corrected and queue its correction jobs."""
    with store.transaction(write=True) as conn:
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
        writer.enqueue(store, ledger, writer.CORRECTION)
    if ledgers:
        writer.wake()
    return MarkResult(marked=True, queued=ledgers)
