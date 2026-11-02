"""The correction note: every correction a session's frozen view does not
reflect, on every turn of every memory role.

A session's view is frozen at its start (:mod:`memory.view`), and a
correction can arrive in another conversation at any time. So each turn's
prompt carries, read in that turn's own snapshot of the store:

* (a) every correction pending in the session's ledger *L* (a marked event
  that is still pending there) that the turn's clearance can read;
* (b) every correction committed in *L* after the session's
  ``base_commit_no`` (a marked event with a terminal disposition in *L*
  committed after both its mark and that base).

A delegated run is a session of one turn: its view and note are read in
one snapshot (:func:`with_view`), so (b) is empty by construction.

Each is the operator's own words, verbatim, with the local time he said
them. The note is the same one rule on every turn, not a one-off: so
compaction, a reconnect or a resume cannot drop it, and nothing records that
a correction was "shown". An in-casa engagement is served no view, but its
session is recorded at its first turn with that turn's ``commit_no`` as its
base (:func:`for_session_without_view`), so it is owed (a) and (b) like any
other. A resident session whose view could not be recorded (the store was
unavailable) gets (a) only; its next reconnect starts it afresh.

Blocking; async callers use ``asyncio.to_thread``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from memory import pending, sessions
from memory.store import MemoryStore
from memory import view as view_mod
from memory.view import BUSY_TIMEOUT_S, View, ledger_for

HEAD = ("[Corrections he made that your memory above does not show yet. "
        "They override it.]")


@dataclass(frozen=True)
class Note:
    """The note for one turn; ``text`` is ``""`` when nothing is owed."""

    text: str
    ledger: str | None = None
    base_commit_no: int | None = None


def _local(occurred_at: str, tz: str) -> str:
    try:
        at = datetime.fromisoformat(occurred_at).astimezone(ZoneInfo(tz))
    except (ValueError, KeyError, TypeError):
        return occurred_at[:16].replace("T", " ")
    return at.strftime("%Y-%m-%d %H:%M")


def corrections(conn: sqlite3.Connection, *, ledger: str, clearance: str,
                base_commit_no: int | None) -> list[tuple[str, str]]:
    """``(local time, his words)`` of every correction the note owes, oldest
    first: (a), and (b) when *base_commit_no* is known."""
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT e.seq, e.occurred_at, e.tz, e.text, e.tier "
        "FROM pending AS p JOIN correction_marks AS m ON m.seq = p.seq "
        "JOIN events AS e ON e.seq = p.seq WHERE p.ledger = ?", (ledger,))}
    if base_commit_no is not None:
        rows.update({r[0]: r[1:] for r in conn.execute(
            "SELECT e.seq, e.occurred_at, e.tz, e.text, e.tier "
            "FROM correction_marks AS m JOIN events AS e ON e.seq = m.seq "
            "WHERE EXISTS (SELECT 1 FROM dispositions AS d "
            "WHERE d.seq = m.seq AND d.ledger = ? "
            "AND d.outcome IN ('patched', 'no_change') "
            "AND d.commit_no > m.mark_commit_no AND d.commit_no > ?)",
            (ledger, base_commit_no))})
    readable = pending.rank(clearance)
    out = []
    for seq in sorted(rows, key=lambda s: (rows[s][0], s)):
        occurred_at, tz, text, tier = rows[seq]
        if pending.rank(tier) <= readable:
            out.append((_local(occurred_at, tz), text))
    return out


def render(items: list[tuple[str, str]]) -> str:
    if not items:
        return ""
    lines = [f"- {when}, his words: \"{text}\"" for when, text in items]
    return ("<pending_corrections>\n" + HEAD + "\n" + "\n".join(lines)
            + "\n</pending_corrections>")


def _base(row, ledger: str, clearance: str) -> int | None:
    """The session's base, when its record is for this ledger and reader."""
    if row is not None and row.ledger == ledger and row.clearance == clearance:
        return row.base_commit_no
    return None


def for_turn(store: MemoryStore, *, clearance: str,
             session_key: str | None = None) -> Note:
    """The note for one turn of a session at *clearance*, read in one
    snapshot: the session's recorded view (by *session_key*) and the
    corrections. ``Note("")`` when the clearance reads no ledger. Raises
    when the store is unavailable or fails."""
    ledger = ledger_for(clearance)
    if ledger is None:
        return Note("")
    with store.transaction(busy_timeout=BUSY_TIMEOUT_S) as conn:
        row = sessions.get(conn, session_key) if session_key else None
        base = _base(row, ledger, clearance)
        items = corrections(conn, ledger=ledger, clearance=clearance,
                            base_commit_no=base)
    return Note(render(items), ledger=ledger, base_commit_no=base)


def for_session_without_view(store: MemoryStore, *, session_key: str,
                             clearance: str, budget: int) -> Note:
    """The note for one turn of a session served no view (an in-casa
    engagement). Its first turn records the session with the current
    ``commit_no`` as its base, in the same write transaction the note is
    read in; every later turn is owed what is pending plus what was
    committed after that base, like any session. A record for another
    ledger or reader is replaced, so a clearance change starts a new base.
    Raises when the store is unavailable or fails."""
    ledger = ledger_for(clearance)
    if ledger is None:
        return Note("")
    with store.transaction(busy_timeout=BUSY_TIMEOUT_S) as conn:
        base = _base(sessions.get(conn, session_key), ledger, clearance)
        if base is not None:
            items = corrections(conn, ledger=ledger, clearance=clearance,
                                base_commit_no=base)
            return Note(render(items), ledger=ledger, base_commit_no=base)
    with store.transaction(write=True, busy_timeout=BUSY_TIMEOUT_S) as conn:
        base = _base(sessions.get(conn, session_key), ledger, clearance)
        if base is None:
            base = pending.current_commit_no(conn)
            sessions.put(conn, sessions.SessionView(
                session_key=session_key, ledger=ledger, base_commit_no=base,
                clearance=clearance, budget=max(budget, 0),
                renderer_version=view_mod.RENDERER_VERSION,
                block_hash=view_mod.block_hash("")))
        items = corrections(conn, ledger=ledger, clearance=clearance,
                            base_commit_no=base)
    return Note(render(items), ledger=ledger, base_commit_no=base)


def with_view(store: MemoryStore, *, clearance: str,
              budget: int) -> tuple[View | None, Note]:
    """A one-turn session's view and note (a delegated run), rendered in one
    snapshot. Nothing is recorded: the run has no later turn, so its note
    owes only the corrections still pending. ``(None, Note(""))`` when the
    clearance reads no ledger. Raises when the store is unavailable or
    fails."""
    ledger = ledger_for(clearance)
    if ledger is None:
        return None, Note("")
    with store.transaction(busy_timeout=BUSY_TIMEOUT_S) as conn:
        view = view_mod.render(conn, ledger, budget) if budget > 0 else None
        items = corrections(conn, ledger=ledger, clearance=clearance,
                            base_commit_no=None)
    return view, Note(render(items), ledger=ledger,
                      base_commit_no=view.commit_no if view else None)
