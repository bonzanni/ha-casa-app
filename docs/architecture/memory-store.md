---
last_reviewed: 2026-11-08
---

# Memory store: `memory.db`, its connection owner, and the registry of everything that holds memory

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The SQLite store under `/data/memory`, the one way code opens it, what happens at boot when
it cannot be opened, and the registry that lists every place memory lives, inside the store
and outside it. What fills the store and what reads it are documented with those mechanisms,
mapped from [`memory.md`](memory.md).

## Mental model

**One file, one transaction per connection.** `memory.db` lives in `/data/memory/`, a
directory created at mode 0700 and kept there by the private-state inventory
([`persistent-state.md`](persistent-state.md), INV-STATE-005). The file runs in WAL mode.
`MemoryStore.transaction()` is the only way in: it opens a connection, begins the
transaction explicitly (`BEGIN IMMEDIATE` for a write, a deferred `BEGIN` for a read, which
is a consistent snapshot from its first read), commits or rolls back, and closes the
connection. No handle outlives its transaction, so between uses nothing in the process holds
the file open. A transaction waits a bounded time for another connection's lock; capture
keeps that bound short, so a busy store makes it spool rather than hold a turn
([`memory-capture.md`](memory-capture.md)). The code is blocking; async callers run it in a
thread.

**Boot opens the store once.** `MemoryStore.open()` runs in `casa_core.main()` once the
plugin environment is applied, as memory's first consumer at boot. It checks that SQLite can create and query an FTS5
table, creates the directory, and brings the file to the current schema version in a single
transaction. A fresh file counts as version 0, so creating a store and migrating one are
the same loop over `memory/schema.py`'s `MIGRATIONS`. The schema version is SQLite's
`user_version`. Besides its tables the schema holds one view, `pending`, which stores
nothing: it is the writer's pending predicate over events, dispositions and correction marks
([`memory-writer.md`](memory-writer.md)). Only then is the file switched to WAL, because a journal-mode change cannot
sit inside the transaction. WAL is a performance setting: if the switch fails, the store
stays available in its current journal mode and a warning is logged. Once the store is
open, the boot check rebuilds a derived file that is missing or unreadable: the search
index, built from `events` with no model call ([`memory-tools.md`](memory-tools.md)).

**A forward migration comes after an export.** `casa-main` passes the automatic export as
`open()`'s `before_migration`: an existing file older than the code is exported as it is
into `/data/memory/exports/` first, and only then migrated
([`memory-export-restore.md`](memory-export-restore.md)). The restore uses the same opener
for the store it builds: under another name (`restore-source/built.db`), at the export's
schema version (`target`) and in a rollback journal (`journal_mode="delete"`), where a
journal mode that cannot be set fails the open. `tables_at(version)` names the tables a
schema version has, by applying the migrations up to it to a scratch in-memory database;
the export and the restore use it to know which tables an export of that version holds.

**Unavailable, never empty, and only when unchanged.** `open()` reports the store
unavailable only when it has left it as it was. If any step up to the migration's commit
fails, the transaction rolls back and an existing file is left byte for byte as it was; a
file this boot was creating is removed. The store is then *unavailable*: `available` is
false, the reason is kept,
and every `transaction()` raises `MemoryUnavailable`. `open()` never raises, so Casa boots
without memory rather than not at all. A file written by a newer Casa is not downgraded; it
is reported unavailable too, and so is a file whose pre-migration export failed: it is left
unmigrated.

**The registry is the list the whole-memory operations walk.** `memory/registry.py` holds
`REGISTRY`, one `Entry` per store: its name, its kind, its paths (glob patterns, or table
names for a table row), and three rules — what an export does with it, what a restore does
after its wipe, and what the wipe does. Export, restore and the wipe are loops over this
list, so a store missing from it is one they would silently skip. The registry covers:

- the tables of `memory.db` and the file with its SQLite sidecars;
- the rest of `/data/memory`: the capture spool and the snapshot a drain stores from, the
  search index, local exports, a staged restore source, and the maintenance intent;
- the SDK transcript roots: the resident home's `.claude/projects`, and
  `/root/.claude/projects` when it is a real directory;
- the session pointers in `/data/sessions.json`;
- served copies of memory-derived text: the explanation records in `/data/explanations/`.

The `memory.db` row also names the served-view renderer as its producer: a session's view
is recorded there (in `session_views`, its hash and base, no text), and the text itself is
copied only into the explanation records. The search index row names its indexer, which copies each
event's text into `search.db`; the producer pin runs it like any other producer, and the
store it reads is the one file it opens outside its row. A row outside `/data/memory` also names the code
that writes it and the staging-file pattern of the atomic-write helper that code uses. A crash between a staging file's write
and its rename leaves that file behind, so the wipe needs its name. The session-pointer
writer names its staging file after its target (`.sessions.json.<random>.tmp`) for this
reason; the shared atomic writer's default name, `.<random>.tmp`, names nothing.

The registry imports nothing from the running Casa process, and neither does the rest of
the `memory` package beyond two stdlib-only helpers (`atomic_io` and
`supervisor_restart`), so it can be used while that process is not running: the
container-start step that carries out a wipe walks this list with no Casa process up
([`memory-wipe.md`](memory-wipe.md)). The maintenance intent's row also declares the
staging name its atomic write leaves on a crash.

## Contracts & invariants

**INV-MEM-033**: Memory that cannot be opened is reported unavailable, never as an empty healthy store: a missing FTS5, an unreadable or newer file, a failed migration or a failed pre-migration export leaves an existing file byte for byte as it was and makes every transaction raise `MemoryUnavailable`; no forward migration runs before its automatic export.

Pinned on real files in WAL and in a rollback journal, with a migration that fails part-way
and an export that fails before it. What it does not cover: how each consumer words the
outage to the operator or a model; the tools and the served view say it with their own
lines.

**INV-MEM-025**: Export, restore and the wipe walk one registry whose every row carries an export, a restore and a wipe rule; a test fails when a fresh store has a table or a `/data/memory` path no row declares, when a named producer of memory-derived text, run as the code configures it, writes from a memory-bearing call site to a path its row does not cover or writes any other file that neither its row nor its declared list of files carrying no memory covers, when a memory-bearing call site issues no write in any run, or when a launcher configures a transcript root no row covers.

The store pin opens a real store and reads `sqlite_master` and the directory, once with a
transaction open so the WAL sidecars are present, and once more after the producers that
write under `/data/memory` have run: a capture that commits and one that spools. The producer pin judges what it
observes, never a path it is told:

- **Every write, as it happens.** Each named producer runs in a sandbox that stands in for
  the filesystem root, with the configuration the code gives it — the paths its class is
  constructed with (each construction call evaluated statically, or its default), the
  module's own constants — and with
  its optional memory-bearing inputs filled, so a write behind a condition on one of them
  happens.
  An audit hook (`sys.addaudithook`) records each write the run issues at the moment it is
  issued: an `open` for writing or creating, a rename's destination, a new directory, link
  or symlink, a truncation, a SQLite connection. Each is recorded with its full path — an
  `os.open` relative to a directory descriptor is placed by the rename or removal that
  names it — and with the producer line that issued it. Nothing is inferred from what files
  hold afterwards, so a file written and then deleted, or holding an encoded copy, is seen.
- **Judged by where it lands.** A read of the producer's code follows the input that
  carries the memory text, through assignments and into functions of the same module it is
  passed to, to every call that writes it. A write issued from such a call site must land
  in the producer's row. A file it creates and then renames into place — the file a crash
  before the rename leaves behind — must match one of the row's staging patterns. Any other
  write the producer issues must land in its row or on a short, explicit list of files it
  writes that carry no memory.
- **Reach.** Every memory-bearing call site must issue at least one write during the runs; a
  site no run triggers fails the pin until a run triggers it.
- **Transcript roots.** Every launcher is scanned: each `HOME` or `CLAUDE_CONFIG_DIR` a
  shell script sets or publishes to the container environment, each literal
  `.claude/projects` path, and each one Python code sets for a launch. A value the scan
  cannot resolve fails the pin rather than passing.

What it does not cover: code written to evade the test — a write to a hard-coded absolute
path outside the producer's configuration, a write through `ctypes` or a subprocess — is
out of its reach. The pin catches memory written to a new or moved location; two things are
left to code review. One is memory placed inside a file a producer legitimately writes for
another purpose — anything on the no-memory list, such as an engagement's
`.claude/settings.json` — since the pin judges where a write lands, not what it holds. The
other is a write that happens only under inputs the runs do not produce. The reading of memory-bearing call sites follows the text only within a
producer's own module, so memory routed through another module's writer or hidden in a
container is judged against the no-memory list instead of the row alone; such a writer
needs naming as a producer. The producer list, a way to run each producer, and the
no-memory list are written by hand, in the change that adds a producer. Copies of
`sessions.json` that the corrupt-file quarantine sets aside, and staging files the shared
writer left before its name carried a target, are not attributed to the row: they hold
session ids, not conversation text.

## Failure behavior

**SQLite without FTS5.** The image build fails (a `RUN` step in `casa/Dockerfile` creates
and queries an FTS5 table). On a running system the boot check reports the store
unavailable before creating anything.

**A migration fails part-way.** The transaction rolls back, the file keeps its old schema,
content and journal mode byte for byte, and the store is unavailable; a store this boot was
creating is removed rather than left empty. The file is switched to WAL only after the
schema is current. The error is logged with the reason.

**The switch to WAL fails after a migration has committed.** The store is current, so it
stays available in the journal mode it has; a warning is logged and the next boot tries
again.

**The file is not a database, or is from a newer schema.** Unavailable; the file is not
touched.

**The export before a migration fails.** Unavailable; the file keeps its old schema, and
the next boot tries the export again.

**A transaction's block raises.** The transaction rolls back, the connection closes, and
the exception propagates.

## Extension points

**A new table** goes into a new schema version in `memory/schema.py` and into a registry row
in the same change; the store pin fails otherwise. **A table no code reads any more** is
removed from its version in place, with its registry row: a store created earlier keeps
it, unread and unexported, until a wipe or a restore replaces the file.

**A new file or directory under `/data/memory`**, or a new place outside it that holds
memory-derived text, gets a registry row with all three rules; a producer writing under
`/data/memory` is run by the store pin, so the path it really writes is the one judged. A new producer of such text
is named in its row's `producer`, its memory-bearing input is declared in the producer pin's
table, and each persisting site the pin then reports is mapped to its path. A producer that
writes through an atomic helper declares the helper's staging pattern, and the pin runs it
to check the name.

**A new launcher** that sets `HOME` or `CLAUDE_CONFIG_DIR` for the Claude CLI configures a
transcript root, which needs a row.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory/__init__.py`
- `casa/rootfs/opt/casa/memory/store.py::MemoryStore`
- `casa/rootfs/opt/casa/memory/store.py::MemoryUnavailable`
- `casa/rootfs/opt/casa/memory/store.py::fts5_available`
- `casa/rootfs/opt/casa/memory/schema.py`
- `casa/rootfs/opt/casa/memory/registry.py`

**Tests**
- `tests/test_memory_store.py`
- `tests/test_memory_registry.py`

**Related**
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/memory-capture.md`](../architecture/memory-capture.md)
- [`architecture/memory-wipe.md`](../architecture/memory-wipe.md)
- [`architecture/memory-export-restore.md`](../architecture/memory-export-restore.md)
<!-- END SOURCEMAP -->
