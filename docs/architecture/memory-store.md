---
last_reviewed: 2026-11-02
---

# Memory store: `memory.db`, its connection owner, and the registry of everything that holds memory

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The SQLite store under `/data/memory`, the one way code opens it, what happens at boot when
it cannot be opened, and the registry that lists every place memory lives, inside the store
and outside it. What fills the store and what reads it are documented with those mechanisms;
the recall path in service today is [`memory.md`](memory.md).

## Mental model

**One file, one transaction per connection.** `memory.db` lives in `/data/memory/`, a
directory created at mode 0700 and kept there by the private-state inventory
([`persistent-state.md`](persistent-state.md), INV-STATE-005). The file runs in WAL mode.
`MemoryStore.transaction()` is the only way in: it opens a connection, begins the
transaction explicitly (`BEGIN IMMEDIATE` for a write, a deferred `BEGIN` for a read, which
is a consistent snapshot from its first read), commits or rolls back, and closes the
connection. No handle outlives its transaction, so between uses nothing in the process holds
the file open. The code is blocking; async callers run it in a thread.

**Boot opens the store once.** `MemoryStore.open()` runs in `casa_core.main()` right after
the long-term memory backend is built. It checks that SQLite can create and query an FTS5
table, creates the directory, and brings the file to the current schema version in a single
transaction. A fresh file counts as version 0, so creating a store and migrating one are
the same loop over `memory/schema.py`'s `MIGRATIONS`. The schema version is SQLite's
`user_version`.

**Unavailable, never empty.** If any step fails, the transaction rolls back and the file is
left as it was. The store is then *unavailable*: `available` is false, the reason is kept,
and every `transaction()` raises `MemoryUnavailable`. `open()` never raises, so Casa boots
without memory rather than not at all. A file written by a newer Casa is not downgraded; it
is reported unavailable too.

**The registry is the list the whole-memory operations walk.** `memory/registry.py` holds
`REGISTRY`, one `Entry` per store: its name, its kind, its paths (glob patterns, or table
names for a table row), and three rules — what an export does with it, what a restore does
after its wipe, and what the wipe does. Export, restore and the wipe are loops over this
list, so a store missing from it is one they would silently skip. The registry covers:

- the tables of `memory.db` and the file with its SQLite sidecars;
- the rest of `/data/memory`: the capture spool, the search index, local exports, a staged
  restore source, legacy transcripts awaiting backfill, and the maintenance intent;
- the SDK transcript roots: the resident home's `.claude/projects`, each engagement
  workspace's `.home/.claude/projects`, and `/root/.claude/projects` when it is a real
  directory;
- the session pointers in `/data/sessions.json`;
- served copies of memory-derived text: the explanation records in `/data/explanations/`,
  and each engagement's executor-memory block (its control-directory cache and the
  `CLAUDE.md` it is rendered into).

A row outside `/data/memory` also names the code that writes it and the staging-file
pattern of the atomic-write helper that code uses. A crash between a staging file's write
and its rename leaves that file behind, so the wipe needs its name. The session-pointer
writer names its staging file after its target (`.sessions.json.<random>.tmp`) for this
reason; the shared atomic writer's default name, `.<random>.tmp`, names nothing.

The registry imports nothing from the running Casa process, and neither does the rest of
the `memory` package, so it can be used while that process is not running.

## Contracts & invariants

**INV-MEM-025**: Every store that holds memory is a registry row with an export, a restore and a wipe rule; a test fails when a fresh store has a table or a `/data/memory` path no row declares, when a named producer of memory-derived text persists through a call site or is configured with a path no row covers, when a launcher configures a transcript root no row covers, or when a producer's atomic-write helper stages under a name its row does not declare.

The store pin opens a real store and reads `sqlite_master` and the directory, once with a
transaction open so the WAL sidecars are present. The producer pin reads code:

- **Persisting call sites.** It starts from each producer a row names and follows the input
  that carries the memory text, through assignments and into functions of the same module
  it is passed to, to every call that writes it. Each site it finds must be mapped to the
  production path it writes, and that path must lie in the producer's own row. A new site
  fails the pin until it is mapped.
- **Configured paths.** Every call that constructs a class producer is evaluated statically
  for the path it is given, or its default.
- **Transcript roots.** Every launcher is scanned: each `HOME` or `CLAUDE_CONFIG_DIR` a
  shell script sets or publishes to the container environment, each literal
  `.claude/projects` path, and each one Python code sets for a launch. A value the scan
  cannot resolve fails the pin rather than passing.
- **Staging names.** These are measured, not read: the test runs each staging producer and
  records the name of the file it renames into place, which is the name a crash at that
  instant would leave behind.

What it does not cover: the scan follows memory text only within a producer's own module,
so a producer that hands its text to another module's writer needs that writer named as a
producer too. The producer list is named by hand; a new producer of memory-derived text
joins it in the change that adds it. Copies of `sessions.json` that the corrupt-file
quarantine sets aside, and staging files the shared writer left before its name carried a
target, are not attributed to the row: they hold session ids, not conversation text.

## Failure behavior

**SQLite without FTS5.** The image build fails (a `RUN` step in `casa/Dockerfile` creates
and queries an FTS5 table). On a running system the boot check reports the store
unavailable before creating anything.

**A migration fails part-way.** The transaction rolls back, the file keeps its old schema
and content byte for byte, and the store is unavailable. The error is logged with the
reason.

**The file is not a database, or is from a newer schema.** Unavailable; the file is not
touched.

**A transaction's block raises.** The transaction rolls back, the connection closes, and
the exception propagates.

## Extension points

**A new table** goes into a new schema version in `memory/schema.py` (a released version's
statements never change) and into a registry row in the same change; the store pin fails
otherwise.

**A new file or directory under `/data/memory`**, or a new place outside it that holds
memory-derived text, gets a registry row with all three rules. A new producer of such text
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
- [`architecture/memory-wipe.md`](../architecture/memory-wipe.md)
<!-- END SOURCEMAP -->
