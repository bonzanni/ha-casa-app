---
last_reviewed: 2026-07-31
---

# Persistent state

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What this application writes to disk, how it treats a file that is missing or corrupt, and
what that means for recovery. It deliberately does not enumerate every file — that list
changes faster than a document can track, and the modules that own each artifact are the
authority. What is stable, and what this document is for, is the *pattern*.

## Mental model

**Two roots with different meanings.** One is mapped and operator-visible, holding
configuration and installed content. The other is application-private runtime state:
registries, ledgers, secrets, workspaces, queues and reports.

**Operator-visible does not mean version-controlled.** Only a whitelist of the mapped root is
tracked — agents, policies, bindings, schema, and specific registry files. Personas,
receipts, installed stores, staging areas, agent homes and the environment file are all
present, all operator-visible, and none of them tracked. If you are relying on git history to
recover something there, check the whitelist first.

**A missing file is usually a valid empty state, not an error.** Most loaders treat absence
as "nothing yet" and carry on. That is why a fresh install and a wiped state directory both
boot cleanly.

**Corrupt-file policy is not uniform, and that inconsistency is the thing to know.** Across
the state files there are at least four different behaviours: rename the bad file aside and
start empty; back it up under a different suffix and start empty; **overwrite it in place
with an empty value, preserving no copy**; and **raise, with no recovery at all**. Two of
those lose data, and one of them stops the load. Before assuming a corrupt file is
recoverable, read that specific loader — the neighbouring one probably does something else.

**Atomicity is a property of individual writers, not of the system.** There are helpers that
write to a temporary file, flush and fsync it, replace, then fsync the containing directory —
the last step makes the *rename itself* durable across a power crash, so a later write cannot
survive while an earlier one's directory entry vanished. The directory fsync is best-effort
(content durability and the caller's success signal never depend on it). Writers that use the
helpers are atomic; writers that do not are not, and several ordinary paths do a direct
write. Some repair paths — including one that rewrites a corrupt file — are themselves
non-atomic.

**Some state is produced by things this application does not control.** Engagement workspaces
and plugin outbox directories receive content from child processes and plugins. There is no
closed inventory of what appears there.

## Contracts & invariants

**INV-STATE-001**: Only an explicit whitelist of the mapped configuration root is admitted into version control.

Enforced by the ignore file written and reconciled at boot. Being under that root implies
visibility, not history.

What it does not cover: ignore rules gate *admission*, not eviction. A path that is already
tracked — from before a whitelist change, or force-added — stays tracked and keeps recording
history until something removes it from the index; nothing here does.

**INV-STATE-002**: A missing state file is a valid empty state for most loaders; a corrupt one is handled per-loader and inconsistently.

Stated as an invariant because the *inconsistency* is what a reader must carry. There is no
single policy to rely on.

**INV-STATE-003**: Atomic replacement is applied by specific writers that opt into it, not by a storage layer.

What it does not cover: direct writes elsewhere, and repair paths that rewrite a damaged file
without the same protection.

**INV-STATE-004**: Where a registry commits to disk before publishing in memory, the on-disk state leads.

This is the pattern that keeps a process from believing something that was never persisted.
The job registry is the genuine example: it builds a detached candidate snapshot, writes it,
and only then assigns it to live memory. The engagement registry's *strict* terminal
transition achieves a related guarantee by the opposite means — it mutates the shared record
provisionally, writes, and rolls the mutation back on failure — so a lock-free reader can
observe the provisional state during the write there; what is guaranteed is restoration, not
disk-first visibility.

What it does not cover: the pattern is opt-in per write path, and the engagement registry's
legacy direct status mutators write their tombstone non-strictly — a failed write there is
swallowed with a warning, leaving memory terminal while disk is not. Where disk-leads
matters, check which of the three shapes the path you are on actually has.

**INV-STATE-005**: Private runtime state has one declared inventory of paths and modes, is repaired to it on every boot before any privilege-dropped engagement can start, and a bearer-credential path that cannot be made root-only refuses such an engagement rather than running one that could read it.

The inventory is the only thing that decides which paths are private; it names each path, its
mode, and what is exposed if the entry regresses. Individual files are tightened under the
private root, which itself stays traversable; directories are tightened at the top only,
because without execute permission for others nothing beneath is reachable by name, so the
pass never recurses. Repair runs after boot replay has confirmed every existing engagement
service down, so nothing privilege-dropped is alive while modes move, and before that
function's fast-path return, so a boot with no records still repairs. Repair, not the write
sites, is what fixes an already-deployed install — its files already exist world-readable, and
an atomic write with no explicit mode preserves an existing file's mode, so a store whose next
write is days away would otherwise stay exposed. The credential subset is re-read from the
filesystem at each point that can start such an engagement, rather than latched once at boot.

What it does not cover: the two roots themselves stay traversable, because a dropped uid
reaches its own workspace and its assigned artifacts through them — this is a per-path mode
boundary, not a filesystem namespace boundary. Non-credential repair failures are logged and
stop nothing, an already-open file descriptor is unaffected by a mode change, and one report
file is deliberately left world-readable because a shipped executor recipe reads it. The
default mode of the shared atomic writer stays world-readable on purpose: the same helper
writes the artifacts a dropped engagement must load, so private call sites opt in explicitly.

## Failure behavior

**A state file is absent.** Almost always treated as empty, and often created on first write.

**Session pointers are actively reaped, and webhook sessions never survive a boot.** A
six-hour sweep removes expired entries and any with malformed or missing activity
timestamps, hard-deleting their SDK transcripts best-effort — a stored pointer is not
indefinitely durable. With one exception, and it is not storage hygiene: an entry that
names a transcript on a bank-writable channel is held back however stale it is, because a
successful retain would have removed the entry, so one that is still here is a conversation
that never reached long-term memory and whose transcript is its only copy
(INV-MEM-017, `architecture/memory-lifecycle.md`). Such an entry stays until the retain
succeeds or the operator resets or wipes it, and each sweep says so. The TTLs are environment-tunable: `SESSION_TTL_DAYS` (default 30)
and the much shorter `WEBHOOK_SESSION_TTL_DAYS` (default 1). And a boot-time purge drops *every* webhook-scoped session
unconditionally, so webhook conversation continuity deliberately does not survive a
restart even though the registry file does.

A session entry may also carry two advisory resume-fault fields — the fault-streak state
of the turn loop's INV-TURN-008 (`architecture/turn-loop.md`), persisted with the entry
and restored on write failure so disk and memory never disagree about the streak. Absent
or malformed fields read as no streak, and fields naming a sid the entry no longer holds
are inert.

**A state file is corrupt.** Depends entirely on which file. Some are preserved under a
different name before being replaced; at least one is overwritten with no copy retained; at
least one raises rather than recovering. Check the loader.

**A write fails in an atomic writer.** It raises to its caller rather than leaving a partial
file. The temporary file is what absorbs the failure.

**A write fails in a direct writer.** There is no general guarantee. A partially written file
is possible, and it becomes the next run's corrupt-file case.

## Extension points

**A new state file** needs five decisions made explicitly, because none is inherited: where
it is created, what a missing file means, what a corrupt one means, whether the write is
atomic, and — if it is under the mapped root — whether it should be tracked. Adding a file
there does not make it version-controlled.

**Anything that must not be lost** should use the atomic helpers and should decide its
corrupt-file policy deliberately. Preserving the damaged copy costs a rename and is the
difference between a diagnosable failure and a silent one.

**What survives an uninstall** is decided by the host from the volume mapping, not by
application code. This repository does not determine it, so do not infer retention from what
the code writes.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/atomic_io.py::atomic_write_json`
- `casa/rootfs/opt/casa/atomic_io.py::atomic_write_text`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry`
- `casa/rootfs/opt/casa/topic_ledger.py`
- `casa/rootfs/opt/casa/explanation_store.py`
- `casa/rootfs/opt/casa/config_git.py::init_repo`
- `casa/rootfs/opt/casa/private_state.py::enforce`
- `casa/rootfs/opt/casa/private_state.py::credential_modes_ok`

**Tests**
- `tests/test_atomic_io.py`
- `tests/test_session_registry.py`
- `tests/test_explanation_store_concurrency.py`
- `tests/test_private_state.py`
- `tests/test_private_state_refusal.py`
- `tests/test_private_state_write_sites.py`
- `tests/test_private_state_dropped_uid.py`
- `tests/test_boot_replay.py`

**Related**
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/memory.md`](../architecture/memory.md)
<!-- END SOURCEMAP -->
