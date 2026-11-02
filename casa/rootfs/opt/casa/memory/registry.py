"""M1: the one enumerated list of everything that holds memory.

Each :class:`Entry` declares where a piece of memory lives and what each of
the three whole-memory operations does with it: export, restore (applied
after a restore has run the whole wipe) and wipe. Those operations are loops
over :data:`REGISTRY`, so a store that is not listed here is one they miss.

The list covers the store under ``/data/memory`` and the memory that lives
outside it: SDK transcripts, session pointers, and served copies of
memory-derived text. A row outside ``/data/memory`` names the code that
writes it (``producer``, as ``<module path>:<qualname>`` relative to the code
root) and the staging-file pattern of the atomic-write helper that code uses,
so a crash between a staging file's write and its rename leaves nothing the
wipe does not know to delete.

Two pinning tests keep the list complete (INV-MEM-025, in
``tests/test_memory_registry.py``): the store pin fails on any table or path
of a fresh store that no row declares, and the producer pin fails on any
persisting call site of a named producer, transcript root a launcher
configures, or staging pattern, that no row declares.

Paths are the production paths; :func:`under` places one beneath another
root (a test's temporary directory, or the container-start maintenance step's
view). This module imports nothing from the running Casa process, so it can be
used before that process starts.
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass

MEMORY_DIR = "/data/memory"
MEMORY_DB_NAME = "memory.db"
MEMORY_DB = f"{MEMORY_DIR}/{MEMORY_DB_NAME}"
#: Where exports are published: the nightly ones, the admin ones and the one
#: taken before a schema migration.
EXPORTS_DIR = f"{MEMORY_DIR}/exports"
#: The staged export a pending restore reads, and the store it builds there.
RESTORE_SOURCE = f"{MEMORY_DIR}/restore-source"
ENGAGEMENTS_ROOT = "/data/engagements"
ENGAGEMENT_CONTROL_ROOT = "/data/engagement-ctl"
RESIDENT_HOME = "/config/cc-home"

#: The files SQLite keeps beside a database while it is open or recovering.
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


class Kind(str, enum.Enum):
    #: Tables inside ``memory.db``; the entry's ``paths`` name the tables.
    TABLE = "table"
    FILE = "file"
    DIRECTORY = "directory"
    #: Roots under which the SDK writes its session transcripts.
    TRANSCRIPTS = "transcripts"
    #: The session-pointer store.
    POINTERS = "pointers"
    #: Copies of memory-derived text served outside the store.
    SERVED_COPY = "served_copy"


class Export(str, enum.Enum):
    YES = "yes"
    NO = "no"
    #: Exported through its tables' rows (``memory.db`` itself).
    TABLES = "tables"
    #: Drained into ``events`` first; an export that cannot drain it fails.
    DRAIN = "drain"


class Restore(str, enum.Enum):
    LOAD = "load"
    #: Loaded, with every ``running`` job set back to ``queued``.
    LOAD_REQUEUE = "load_requeue"
    #: The built store is installed as ``memory.db``.
    INSTALL = "install"
    NONE = "none"
    FRESH = "fresh"
    REBUILD = "rebuild"
    RESET = "reset"
    #: The staged export a pending restore reads; kept until it completes.
    SOURCE = "source"


class Wipe(str, enum.Enum):
    DELETE = "delete"
    #: Deleted, then rebuilt empty.
    DELETE_REBUILD = "delete_rebuild"
    FRESH = "fresh"
    RESET = "reset"
    #: Kept while a restore intent owns it; deleted otherwise.
    KEEP_WHILE_INTENT = "keep_while_intent"
    #: Every block emptied and its workspace section blanked; staging deleted.
    EMPTY_BLOCK = "empty_block"
    #: The intent itself, removed when the operation completes.
    REMOVE_LAST = "remove_last"


@dataclass(frozen=True)
class Entry:
    """One registered store. ``paths`` are glob patterns (``*`` stands for an
    engagement id), except for :attr:`Kind.TABLE`, where they are table
    names in ``memory.db``."""

    name: str
    kind: Kind
    paths: tuple[str, ...]
    export: Export
    restore: Restore
    wipe: Wipe
    producer: tuple[str, ...] = ()
    staging_patterns: tuple[str, ...] = ()


def _with_sidecars(path: str) -> tuple[str, ...]:
    return (path, *(path + s for s in SQLITE_SIDECARS))


REGISTRY: tuple[Entry, ...] = (
    # --- memory.db and its tables ------------------------------------------
    # The served-view renderer records each session's view here (its hash
    # and base, in ``session_views``); the text itself is served into the
    # prompt and copied only into the explanation records below.
    Entry("memory.db", Kind.FILE, _with_sidecars(MEMORY_DB),
          Export.TABLES, Restore.INSTALL, Wipe.DELETE,
          producer=("memory/view.py:serve",)),
    Entry("events", Kind.TABLE, ("events", "correction_marks"),
          Export.YES, Restore.LOAD, Wipe.DELETE),
    Entry("ledgers", Kind.TABLE, ("objects", "revisions"),
          Export.YES, Restore.LOAD, Wipe.DELETE),
    Entry("writer state", Kind.TABLE, ("dispositions", "jobs"),
          Export.YES, Restore.LOAD_REQUEUE, Wipe.DELETE),
    Entry("engagement summaries", Kind.TABLE, ("engagement_summaries",),
          Export.YES, Restore.LOAD, Wipe.DELETE),
    Entry("session views", Kind.TABLE, ("session_views",),
          Export.NO, Restore.NONE, Wipe.DELETE),
    Entry("meta", Kind.TABLE, ("meta",),
          Export.NO, Restore.FRESH, Wipe.FRESH),
    Entry("backfill run", Kind.TABLE, ("backfill_run",),
          Export.NO, Restore.NONE, Wipe.DELETE),
    # --- the rest of /data/memory -------------------------------------------
    # The capture spool, and the snapshot a drain renames it to and stores.
    Entry("spool", Kind.FILE, (f"{MEMORY_DIR}/spool.jsonl",
                               f"{MEMORY_DIR}/spool.draining.jsonl"),
          Export.DRAIN, Restore.NONE, Wipe.DELETE),
    # The BM25 index of ``events``, derived: rebuilt from the store when
    # missing (memory/search.py).
    Entry("search index", Kind.FILE, _with_sidecars(f"{MEMORY_DIR}/search.db"),
          Export.NO, Restore.REBUILD, Wipe.DELETE_REBUILD,
          producer=("memory/search.py:catch_up",)),
    Entry("local exports", Kind.DIRECTORY, (EXPORTS_DIR,),
          Export.NO, Restore.NONE, Wipe.DELETE),
    Entry("restore source", Kind.DIRECTORY, (RESTORE_SOURCE,),
          Export.NO, Restore.SOURCE, Wipe.KEEP_WHILE_INTENT),
    Entry("legacy transcripts", Kind.DIRECTORY,
          (f"{MEMORY_DIR}/legacy-transcripts",),
          Export.NO, Restore.NONE, Wipe.DELETE),
    Entry("maintenance intent", Kind.FILE, (f"{MEMORY_DIR}/maintenance.json",),
          Export.NO, Restore.NONE, Wipe.REMOVE_LAST,
          staging_patterns=(f"{MEMORY_DIR}/.maintenance.json.*.tmp",)),
    # --- memory outside /data/memory -----------------------------------------
    Entry("transcripts", Kind.TRANSCRIPTS,
          (f"{RESIDENT_HOME}/.claude/projects",
           f"{ENGAGEMENTS_ROOT}/*/.home/.claude/projects"),
          Export.NO, Restore.NONE, Wipe.DELETE),
    # setup-configs.sh moves a real directory here into the resident home and
    # leaves a symlink; only a real directory holds transcripts of its own.
    Entry("root transcripts", Kind.TRANSCRIPTS, ("/root/.claude/projects",),
          Export.NO, Restore.NONE, Wipe.DELETE),
    Entry("session pointers", Kind.POINTERS, ("/data/sessions.json",),
          Export.NO, Restore.RESET, Wipe.RESET,
          producer=("session_registry.py:SessionRegistry._write",),
          staging_patterns=("/data/.sessions.json.*.tmp",)),
    Entry("explanations", Kind.SERVED_COPY, ("/data/explanations",),
          Export.NO, Restore.NONE, Wipe.DELETE,
          producer=("explanation_store.py:ExplanationStore.record",),
          staging_patterns=("/data/explanations/*.tmp",)),
    Entry("executor memory", Kind.SERVED_COPY,
          (f"{ENGAGEMENT_CONTROL_ROOT}/*/.executor_memory",
           f"{ENGAGEMENTS_ROOT}/*/CLAUDE.md"),
          Export.NO, Restore.NONE, Wipe.EMPTY_BLOCK,
          producer=("drivers/workspace.py:provision_workspace",
                    "drivers/workspace.py:refresh_claude_md"),
          staging_patterns=(f"{ENGAGEMENTS_ROOT}/*/.CLAUDE.md.casa-tmp.*",)),
)


def under(root: str, declared: str) -> str:
    """*declared* (absolute) placed beneath *root*; ``"/"`` is production."""
    if root in ("", "/"):
        return declared
    return os.path.join(root, declared.lstrip("/"))
