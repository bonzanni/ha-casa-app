"""``session_views``: the view each session was served, and its base.

One row per session key (a resident conversation's channel key). It holds
no memory text: the ledger the session reads, the ``commit_no`` of the
snapshot its view was rendered from (``base_commit_no``), the clearance and
budget it was rendered for, the renderer's version and the hash of the
block. A new session for the same key replaces the row.

The correction note (:mod:`memory.note`) reads ``base_commit_no`` to tell
which committed corrections the session's frozen view does not reflect; a
resumed session's view is rendered again from it (:func:`memory.view.serve`).

Every function takes an open connection, inside the caller's transaction.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_FIELDS = ("session_key", "ledger", "base_commit_no", "clearance", "budget",
           "renderer_version", "block_hash")


@dataclass(frozen=True)
class SessionView:
    session_key: str
    ledger: str | None
    base_commit_no: int
    clearance: str
    budget: int
    renderer_version: str
    block_hash: str


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
