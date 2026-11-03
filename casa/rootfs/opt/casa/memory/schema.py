"""DDL of ``memory.db``, by schema version.

``MIGRATIONS[n]`` is the ordered list of statements that take a store from
version ``n - 1`` to version ``n``; a fresh file is version 0, so creating a
store and migrating one are the same loop (:mod:`memory.store`). A released
version's statements never change: a schema change appends a new version.

Every table here is registered in :mod:`memory.registry` with its export,
restore and wipe rule; the store pin in ``tests/test_memory_registry.py``
fails on a table that is not (INV-MEM-025). The one view, ``pending``, stores
nothing: it is the pending predicate over ``events``, ``dispositions`` and
``correction_marks`` (:mod:`memory.pending`).

Tables are ``STRICT`` (SQLite 3.37+). Tiers are the clearance ladder
``public < friends < family < private``. JSON-valued columns hold arrays or
objects as text. Times are ISO-8601 text.
"""
from __future__ import annotations

_TIERS = "('public', 'friends', 'family', 'private')"
#: A tier column's rank on the ladder, as SQL.
_RANK = ("(CASE {col} WHEN 'public' THEN 0 WHEN 'friends' THEN 1 "
         "WHEN 'family' THEN 2 ELSE 3 END)")

V1: tuple[str, ...] = (
    # The verbatim store. ``seq`` is assigned by the store and never reused
    # (AUTOINCREMENT); ``source_key`` is the occurrence identity.
    f"""
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        source_key TEXT NOT NULL UNIQUE,
        occurred_at TEXT NOT NULL,
        tz TEXT NOT NULL,
        local_day TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        session_id TEXT,
        role TEXT NOT NULL,
        channel TEXT NOT NULL,
        speaker_kind TEXT NOT NULL CHECK (speaker_kind IN
            ('operator', 'resident', 'specialist', 'casa', 'third_party')),
        speaker_id TEXT,
        speaker_name TEXT,
        origin_kind TEXT NOT NULL CHECK (origin_kind IN
            ('operator', 'scheduled', 'callback', 'notification',
             'delegated_result', 'system')),
        scheduled INTEGER NOT NULL DEFAULT 0 CHECK (scheduled IN (0, 1)),
        reply_to TEXT,
        provider_message_id TEXT,
        text TEXT NOT NULL,
        tier TEXT NOT NULL CHECK (tier IN {_TIERS}),
        tier_status TEXT NOT NULL CHECK (tier_status IN
            ('provisional', 'classified')),
        origin_tier TEXT CHECK (origin_tier IN {_TIERS}),
        provenance TEXT NOT NULL CHECK (provenance IN
            ('told', 'captured')),
        eligibility TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX events_local_day ON events (local_day)",
    # The writer's context window: a session's events in the order said.
    "CREATE INDEX events_session_said ON events (session_id, occurred_at, seq)",
    # One row per object per ledger: its current state. ``ledger`` is the
    # maintained clearance the object belongs to, and so its tier.
    f"""
    CREATE TABLE objects (
        ledger TEXT NOT NULL CHECK (ledger IN {_TIERS}),
        object_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('entry', 'group', 'topic')),
        entry_kind TEXT CHECK (entry_kind IN ('fact', 'errand', 'episode')),
        errand_state TEXT CHECK (errand_state IN ('open', 'done', 'dropped')),
        topic TEXT,
        subject TEXT,
        body TEXT,
        line TEXT,
        cues TEXT NOT NULL DEFAULT '[]',
        parent TEXT,
        origin TEXT,
        source_seqs TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL CHECK (status IN ('current', 'retired')),
        PRIMARY KEY (ledger, object_id)
    ) STRICT
    """,
    # Every state an object has had, with the commit that produced it.
    # ``commit_no`` is derived from these rows, never stored elsewhere.
    f"""
    CREATE TABLE revisions (
        ledger TEXT NOT NULL CHECK (ledger IN {_TIERS}),
        object_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('entry', 'group', 'topic')),
        entry_kind TEXT CHECK (entry_kind IN ('fact', 'errand', 'episode')),
        errand_state TEXT CHECK (errand_state IN ('open', 'done', 'dropped')),
        topic TEXT,
        subject TEXT,
        body TEXT,
        line TEXT,
        cues TEXT NOT NULL DEFAULT '[]',
        parent TEXT,
        origin TEXT,
        source_seqs TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL CHECK (status IN ('current', 'retired')),
        op TEXT NOT NULL,
        reason TEXT,
        job_id TEXT,
        committed_at TEXT NOT NULL,
        commit_no INTEGER NOT NULL,
        PRIMARY KEY (ledger, object_id, revision)
    ) STRICT
    """,
    "CREATE INDEX revisions_commit_no ON revisions (commit_no)",
    # Per event and ledger, every outcome a writer commit recorded; an older
    # terminal one stays as history.
    f"""
    CREATE TABLE dispositions (
        id INTEGER PRIMARY KEY,
        seq INTEGER NOT NULL,
        ledger TEXT NOT NULL CHECK (ledger IN {_TIERS}),
        job_id TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN
            ('patched', 'no_change', 'deferred')),
        commit_no INTEGER NOT NULL,
        committed_at TEXT
    ) STRICT
    """,
    "CREATE INDEX dispositions_seq_ledger ON dispositions (seq, ledger)",
    "CREATE INDEX dispositions_commit_no ON dispositions (commit_no)",
    f"""
    CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY,
        ledger TEXT NOT NULL CHECK (ledger IN {_TIERS}),
        kind TEXT NOT NULL,
        input_seqs TEXT NOT NULL DEFAULT '[]',
        base_revisions TEXT NOT NULL DEFAULT '{{}}',
        mark_seen TEXT NOT NULL DEFAULT '{{}}',
        state TEXT NOT NULL CHECK (state IN
            ('queued', 'running', 'committed', 'failed')),
        boot_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    ) STRICT
    """,
    # ``mark_no`` is derived from these rows, never stored elsewhere.
    """
    CREATE TABLE correction_marks (
        seq INTEGER PRIMARY KEY,
        mark_no INTEGER NOT NULL,
        marked_at TEXT NOT NULL,
        mark_commit_no INTEGER NOT NULL
    ) STRICT
    """,
    "CREATE INDEX correction_marks_mark_no ON correction_marks (mark_no)",
    "CREATE INDEX correction_marks_mark_commit_no "
    "ON correction_marks (mark_commit_no)",
    # The one pending predicate (INV-MEM-027), for every ledger: an event is
    # pending in L while it is eligible for L (its tier at or below L, and
    # classified unless L is ``private``) and has no terminal disposition in
    # L committed after its latest correction mark. ``deferred`` is not
    # terminal. Nothing else records whether an event was handled.
    f"""
    CREATE VIEW pending (seq, ledger) AS
    WITH ledgers (ledger, rank) AS (
        VALUES ('public', 0), ('friends', 1), ('family', 2), ('private', 3)
    )
    SELECT e.seq, l.ledger
    FROM events AS e
    JOIN ledgers AS l
      ON {_RANK.format(col="e.tier")} <= l.rank
     AND (e.tier_status = 'classified' OR l.ledger = 'private')
    WHERE NOT EXISTS (
        SELECT 1 FROM dispositions AS d
        WHERE d.seq = e.seq AND d.ledger = l.ledger
          AND d.outcome IN ('patched', 'no_change')
          AND d.commit_no > coalesce(
              (SELECT m.mark_commit_no FROM correction_marks AS m
               WHERE m.seq = e.seq), 0)
    )
    """,
    f"""
    CREATE TABLE session_views (
        session_key TEXT PRIMARY KEY,
        ledger TEXT CHECK (ledger IN {_TIERS}),
        base_commit_no INTEGER NOT NULL,
        clearance TEXT NOT NULL CHECK (clearance IN {_TIERS}),
        budget INTEGER NOT NULL,
        renderer_version TEXT NOT NULL,
        block_hash TEXT NOT NULL,
        sdk_session_id TEXT
    ) STRICT
    """,
    # A finalized engagement's summaries. ``source_key`` is the occurrence
    # identity capture mints for each (so a spool drain re-run adds one row);
    # ``procedural_epoch`` is the executor epoch digest, empty when none.
    f"""
    CREATE TABLE engagement_summaries (
        id INTEGER PRIMARY KEY,
        source_key TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL,
        procedural_epoch TEXT NOT NULL DEFAULT '',
        tier TEXT NOT NULL CHECK (tier IN {_TIERS}),
        text TEXT NOT NULL,
        engagement_id TEXT NOT NULL,
        at TEXT NOT NULL
    ) STRICT
    """,
    # Every run that can capture, registered before its first capture and
    # confirmed once each capture it made is durable (memory/transcript_runs.py).
    # Ids and times only, never content. ``directory`` is the run's working
    # directory, the key a deleter resolves the transcript folder from.
    """
    CREATE TABLE transcript_runs (
        run_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN
            ('resident', 'engagement', 'delegation', 'summary')),
        role TEXT NOT NULL,
        directory TEXT,
        session_id TEXT,
        boot_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        confirmed_at TEXT,
        ended_at TEXT
    ) STRICT
    """,
    "CREATE INDEX transcript_runs_session ON transcript_runs (session_id)",
    "CREATE INDEX transcript_runs_directory ON transcript_runs (directory)",
    # Content-free operational markers (the last maintenance outcome).
    # Values are JSON.
    """
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT
    """,
)

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: V1,
}
