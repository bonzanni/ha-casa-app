"""DDL of ``phone.db``, by schema version.

``MIGRATIONS[n]`` takes a store from version ``n - 1`` to ``n``; a fresh file
is version 0, so creating and migrating are the same loop (:mod:`phone.store`).

``settings`` holds the model choices, ``voices`` one voice per language,
``grants`` the level at which each caller class may use an ability (only rows
that differ from the defaults need exist). ``calls`` is one row per call;
``actions`` and ``record`` are JSON text. ``transcripts`` holds a call's words
until ``expires_at``, apart from the call so the call outlives them.

Tables are ``STRICT``. Times are seconds since the epoch.
"""
from __future__ import annotations

V1: tuple[str, ...] = (
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE voices (
        language TEXT PRIMARY KEY,
        voice TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT ''
    ) STRICT
    """,
    """
    CREATE TABLE grants (
        caller_class TEXT NOT NULL
            CHECK (caller_class IN ('priority', 'known', 'unknown')),
        ability TEXT NOT NULL,
        level TEXT NOT NULL
            CHECK (level IN ('off', 'emergency', 'urgent', 'open')),
        PRIMARY KEY (caller_class, ability)
    ) STRICT
    """,
    """
    CREATE TABLE calls (
        id TEXT PRIMARY KEY,
        session_id TEXT UNIQUE NOT NULL,
        started_at REAL,
        ended_at REAL,
        number TEXT,
        name TEXT,
        caller_class TEXT,
        language TEXT,
        returned INTEGER,
        outcome TEXT,
        actions TEXT,
        record TEXT,
        delivered INTEGER NOT NULL DEFAULT 0
    ) STRICT
    """,
    """
    CREATE TABLE transcripts (
        call_id TEXT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
        text TEXT NOT NULL,
        expires_at REAL NOT NULL
    ) STRICT
    """,
)

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: V1,
}
