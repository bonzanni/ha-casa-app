"""DDL of ``contacts.db``, by schema version.

``MIGRATIONS[n]`` is the ordered list of statements that take a store from
version ``n - 1`` to version ``n``; a fresh file is version 0, so creating a
store and migrating one are the same loop (:mod:`contacts.store`). A schema
change that existing stores must follow appends a new version.

``people`` and ``numbers`` mirror each address-book source, keyed by the
source's name, and are rewritten by every sync of that source. ``marks`` is
what Casa itself holds about a person (priority, relation, language), one row
per origin; it does not reference ``people``, so a mark can outlive its
person's absence from a sync. ``note_reads`` holds, per person, the hash of
the note last settled into ``note``-origin marks (read by a model, or emptied
and cleared) taken together with the note reader's prompt version
(:func:`contacts.store.settled_hash`), so a note that was not read is tried
again, and every note is read once more after the prompt changes.
``sync_state`` is per source.

Tables are ``STRICT``. JSON-valued columns hold arrays as text. Times are
ISO-8601 text.
"""
from __future__ import annotations

V1: tuple[str, ...] = (
    """
    CREATE TABLE people (
        source TEXT NOT NULL,
        uid TEXT NOT NULL,
        name TEXT NOT NULL,
        first_name TEXT NOT NULL,
        org TEXT NOT NULL,
        note TEXT NOT NULL,
        note_hash TEXT NOT NULL,
        groups TEXT NOT NULL,
        related TEXT NOT NULL,
        PRIMARY KEY (source, uid)
    ) STRICT
    """,
    """
    CREATE TABLE numbers (
        e164 TEXT NOT NULL,
        source TEXT NOT NULL,
        uid TEXT NOT NULL,
        PRIMARY KEY (e164, source, uid),
        FOREIGN KEY (source, uid) REFERENCES people ON DELETE CASCADE
    ) STRICT
    """,
    "CREATE INDEX numbers_person ON numbers (source, uid)",
    """
    CREATE TABLE marks (
        source TEXT NOT NULL,
        uid TEXT NOT NULL,
        field TEXT NOT NULL CHECK (field IN ('priority', 'relation', 'language')),
        origin TEXT NOT NULL CHECK (origin IN ('operator', 'group', 'note')),
        value TEXT NOT NULL,
        set_at TEXT NOT NULL,
        PRIMARY KEY (source, uid, field, origin)
    ) STRICT
    """,
    """
    CREATE TABLE note_reads (
        source TEXT NOT NULL,
        uid TEXT NOT NULL,
        note_hash TEXT NOT NULL,
        PRIMARY KEY (source, uid)
    ) STRICT
    """,
    """
    CREATE TABLE sync_state (
        source TEXT PRIMARY KEY,
        last_ok TEXT,
        last_error TEXT NOT NULL DEFAULT '',
        failing INTEGER NOT NULL DEFAULT 0
    ) STRICT
    """,
)

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: V1,
}
