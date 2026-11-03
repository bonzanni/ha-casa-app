---
last_reviewed: 2026-11-02
---

# Memory export and restore: a verified copy of the store, and replacing memory with one

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The memory export (from the terminal, every night, and before a schema migration), its
format, and the restore that replaces all of memory with an export: the request in
`casa-main`, and the two phases the container-start step carries out. What the restore's
wipe erases is the wipe loop of [`memory-wipe.md`](memory-wipe.md); what each registry row
does in an export and a restore is the registry of [`memory-store.md`](memory-store.md).

## Mental model

**An export is the store's rows as files, verified before it is published**
(`memory.export.export`). One runs at a time; a second gets "an export is already
running". It first drains the capture spool through capture's own drain, for every
registry row whose export rule is `drain`: a drain that fails fails the export and nothing
is published, so no export ever carries a spool. Then, in one read transaction, every row
whose export rule is `yes` writes one `<table>.jsonl` per table, one JSON object per row in
`rowid` order (`events`, `correction_marks`, `objects`, `revisions`, `dispositions`,
`jobs`, `engagement_summaries`), plus `ledger.md`, the `private` ledger as text, and
a format note (FORMAT.md) describing the files. `manifest.json` comes last: the format and schema
versions, the row count of every exported table, events by tier and current objects by
ledger, the **obligations** — per ledger the events still pending and the pending
corrections, and the failed jobs, each as a count and a SHA-256 of the sorted ids, all read
from the one pending predicate ([`memory-writer.md`](memory-writer.md)) — and a SHA-256 of
every other file. The files are written in a hidden `.<name>.part` directory, fsynced,
read back and checked against the manifest, and published by renaming the directory.

Exports land in `/data/memory/exports/`, so a Home Assistant backup carries them:
`export-<time>` from `casactl memory-export` (`POST /admin/memory/export`, root-gated like
every admin route), `nightly-<time>` from the 03:00 scheduler job, of which the last seven
are kept, and `premigration-v<n>-<time>` before a schema migration. A wipe deletes them all.

**A forward schema migration runs only after an automatic export.** `casa-main` opens the
store with the export as its pre-migration step (`MemoryStore.open(before_migration=…)`):
an existing file older than the code is exported as it is, then migrated in one
transaction. That export copies exactly the exported tables its own schema version has
(the migration may be about to add one) and records that version; every other export
fails on a registered exported table the store lacks. It does not drain the spool, because the spool holds rows in the
code's newer shape, which the older schema may not take; the spool is drained after the
migration, as at every boot. If the export fails, nothing is migrated and memory is
unavailable ([`memory-store.md`](memory-store.md), INV-MEM-033).

**A restore replaces memory; it never merges.** It runs the whole wipe and then installs a
store built from the export, so no copy of memory that the export does not carry survives:
not a transcript, an explanation record, an executor-memory block, a staging file or a
local export. The only door is the terminal: `casactl memory-restore <export> [--yes]`
(`POST /admin/memory/restore`), where `<export>` is a directory or the name of one in
`exports/`.

- **The request** (`memory.restore.request_restore`, in `casa-main`). The export is
  validated at once: its manifest is one this Casa reads, every table it holds is
  registered with a restore rule, its schema is one this Casa can migrate, it holds exactly
  the exported tables its schema version has (one left out would be restored empty), with a
  file for each and no other table file, and every file matches its hash. The tables each
  version has are derived from the migrations alone (`memory.store.tables_at`). A
  pending wipe or restore refuses it too; neither is ever replaced. Any refusal answers
  "The restore was refused: <reason>. Nothing was scheduled." Without `--yes` the answer
  is the confirmation — "This replaces all current memory with this export's, including
  anything wiped since it was made. My saved conversations and memory backups go too, as in
  a wipe. Casa restarts to do it." — and nothing is scheduled. With it, the export is
  copied into `/data/memory/restore-source/` (it may sit in `exports/`, which the wipe
  deletes) and validated again there, the intent is written (kind `restore`, phase
  `staged`, the manifest's hash), "Restoring now. I'll restart and be back in a minute." is
  the reply, and the app restart is requested exactly as for a wipe. The window before the
  restart, the courtesy line ("Restarting to restore memory — send it again in a minute."),
  a failed restart request and an intent still waiting at boot all behave as for a wipe
  ([`memory-wipe.md`](memory-wipe.md)).
- **Phase `staged`, in the container-start step** (`memory.restore.build_phase`). After the
  step records its attempt, the staged export is validated again and must be the one the
  intent names. `restore-source/built.db` is built beside the current store, which is not
  touched: created at the export's schema version in a rollback journal, never WAL; every
  table loaded; then what was loaded is compared with the manifest — the counts, every
  table dumped again and hashed against its file, and the obligations. **Loaded must equal
  the manifest exactly.** Then every `running` job goes back to `queued`, the store is
  migrated forward when the export is older, and `meta` is written fresh. `built.db` is
  closed, checked to be in the rollback journal with no `-wal`, `-shm` or `-journal` beside
  it, fsynced and hashed, so the hash and any copy of it cover every loaded row; one atomic
  write then moves the intent to phase `wiping` with that hash.
- **Phase `wiping`** (`check_built`, the wipe loop, `install_phase`). `built.db` must match
  its hash. The whole wipe loop runs, keeping only the staged source and the intent. Each
  row's restore rule then applies: `memory.db` gets a copy of `built.db` (copy, fsync,
  hash check, rename over any leftover), the session pointers are reset, and every other
  row stays as the wipe left it. The step deletes the intent, then `restore-source/`.
  `casa-main` switches the installed file to WAL when it first opens it, rebuilds the
  search index from it at its boot check, and reports "Done. Memory is restored from the
  export of <time>." once.

**Abandonment leaves memory as it was.** If anything in phase `staged` fails — a file no
longer matches, a row the store refuses, a count, a hash or an obligation that differs —
the outcome is recorded in the current store's `meta`, then the intent is deleted, then
`restore-source/`, and the step ends. `casa-main` boots normally on the untouched memory
and says once "The restore didn't happen (<reason>); memory is unchanged."

**Every crash re-runs to a clean run.** A kill before the `wiping` write leaves the current
memory untouched, and the next start builds again from the staged export (any `built.db`
and sidecars left are deleted first). A kill at or after it, right after the install
included, runs phase `wiping` again: the wipe removes a partial or complete install and
`built.db` is still there with its hash. A kill between deleting the intent and deleting
`restore-source/` leaves a source with no intent, which the next start deletes. Like a
wipe, an attempt cut off anywhere puts `casa-main` in notification-only mode until a
restart lets the step finish.

## Contracts & invariants

**INV-MEM-037**: A restore runs only in the container-start step and is a wipe plus an exact load of a single-file `built.db`: what is loaded equals the export's manifest (counts, every table's bytes, obligations) or the restore is abandoned with memory unchanged; `built.db` is hashed only in a rollback journal with no sidecar; a crash at any point re-runs to the same result as a clean run; and the intent is always deleted before `restore-source/`.

Pinned by a round trip through the real step, by canaries in every registry row (none
survives, and the store equals the export), by a staged export spoiled three ways (each
abandoned with the exported tables byte-identical and one report), by real SIGKILLs of the
step in the build, at every deletion of the wipe, right after the install and between the
last two deletions (on success and on abandonment), by a spool-only event that an export
must drain to carry, and by counters that keep their next values across a restore. What
it does not cover: an export edited by hand so that its files and its manifest agree is
restored as edited; the export is the operator's own copy.

## Failure behavior

**The spool cannot be drained.** The export fails ("the capture spool could not be stored
first"), nothing is published, and the spool stays on disk; the hour-old-spool notice
already tells the operator.

**The restore request is refused.** Nothing is staged and no intent is written; the line
names the reason.

**The build fails.** Abandoned: memory unchanged, one line. If the current store cannot
take the record (it is missing its table, or unreadable), the abandonment is only logged.

**Phase `wiping` fails** (`built.db` missing or not matching its hash, a section that cannot
be located, a row with no restore rule). The attempt records the error and keeps the
intent; `casa-main` starts in notification-only mode, and a restart retries the phase.

**A pre-migration export fails.** Memory is unavailable and the file is left unmigrated, as
it was.

## Extension points

**A new table** joins the registry with its rules. One with export `no` and restore `none`
(a record of runs, for example) needs nothing here: it is left out of the export, created
empty in the built store and wiped. One with export `yes` is exported and loaded by the
same loops; a restore rule the build or the install does not know stops the step rather
than being skipped. **A new schema version** is migrated after the automatic export; it
decides there whether that export should drain first.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory/export.py`
- `casa/rootfs/opt/casa/memory/restore.py`
- `casa/rootfs/opt/casa/internal_handlers.py::build_admin_memory_export_handler`
- `casa/rootfs/opt/casa/internal_handlers.py::build_admin_memory_restore_handler`

**Tests**
- `tests/test_memory_export_restore.py`
- `tests/test_memory_restore_crash.py`
- `tests/test_admin_memory_export_restore_routes.py`
- `tests/test_executor_memory_blank.py`

**Related**
- [`architecture/memory-store.md`](../architecture/memory-store.md)
- [`architecture/memory-wipe.md`](../architecture/memory-wipe.md)
- [`architecture/memory-capture.md`](../architecture/memory-capture.md)
- [`architecture/memory-writer.md`](../architecture/memory-writer.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
