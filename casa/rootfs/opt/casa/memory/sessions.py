"""``session_views``: the view each session was served, and its base.

One row per session key (a resident conversation's channel key). It holds
no memory text: the ledger the session reads, the ``commit_no`` of the
snapshot its view was rendered from (``base_commit_no``), the clearance and
budget it was rendered for, the renderer's version, the hash of the block,
and the SDK session the view belongs to. A new session for the same key
replaces the row, unbound; once the SDK names the session the row is bound
to that session id (:func:`bind`), and only a session whose id the row is
bound to may resume on it. An in-casa engagement has no row: its base is
derived from its start time (:func:`memory.note.for_engagement`).

The correction note (:mod:`memory.note`) reads ``base_commit_no`` to tell
which committed corrections the session's frozen view does not reflect; a
resumed session's view is rendered again from it (:func:`memory.view.serve`).

Every function takes an open connection, inside the caller's transaction.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_FIELDS = ("session_key", "ledger", "base_commit_no", "clearance", "budget",
           "renderer_version", "block_hash", "sdk_session_id")


@dataclass(frozen=True)
class SessionView:
    session_key: str
    ledger: str | None
    base_commit_no: int
    clearance: str
    budget: int
    renderer_version: str
    block_hash: str
    sdk_session_id: str | None = None


def get(conn: sqlite3.Connection, session_key: str) -> SessionView | None:
    row = conn.execute(
        f"SELECT {', '.join(_FIELDS)} FROM session_views "
        "WHERE session_key = ?", (session_key,)).fetchone()
    return None if row is None else SessionView(*row)


def put(conn: sqlite3.Connection, view: SessionView) -> None:
    conn.execute(
        f"INSERT INTO session_views ({', '.join(_FIELDS)}) VALUES "
        f"({', '.join('?' for _ in _FIELDS)}) ON CONFLICT(session_key) DO "
        "UPDATE SET " + ", ".join(f"{f} = excluded.{f}" for f in _FIELDS[1:]),
        tuple(getattr(view, f) for f in _FIELDS))


def bind(conn: sqlite3.Connection, session_key: str, *, base_commit_no: int,
         budget: int, block_hash: str, sdk_session_id: str) -> bool:
    """Bind the row recorded for this view to the SDK session it was served
    to. Only the row with exactly that base, budget and hash, unbound or
    already bound to that session, is bound. Returns whether it is now
    bound."""
    return conn.execute(
        "UPDATE session_views SET sdk_session_id = ? WHERE session_key = ? "
        "AND base_commit_no = ? AND budget = ? AND block_hash = ? "
        "AND (sdk_session_id IS NULL OR sdk_session_id = ?)",
        (sdk_session_id, session_key, base_commit_no, budget, block_hash,
         sdk_session_id)).rowcount == 1
