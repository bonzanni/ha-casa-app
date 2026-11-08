---
last_reviewed: 2026-11-02
---

# Memory lifecycle: sessions, the reset, and transcript retention

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The lifecycle of a resident's SDK session around memory: the freshness windows that decide
when a session stops being resumable, the explicit reset (`/new`) under its retirement
claim, and how long an SDK transcript is kept: run confirmation and retention. What a turn
captures into memory, and which text is eligible at all, is
[`architecture/memory-capture.md`](memory-capture.md); how the writer then files captured
events is [`architecture/memory-writer.md`](memory-writer.md). The operator-consented wipe,
which empties every transcript root along with the rest of memory, is
[`architecture/memory-wipe.md`](memory-wipe.md). The warm-client pool whose resume decision
the reset steers is [`architecture/sdk-client-pool.md`](sdk-client-pool.md).

## Mental model

**Memory does not depend on a session ending.** Casa's live capture is the only source of
memory ([`architecture/memory-capture.md`](memory-capture.md)): nothing is saved when a
session goes cold or is reset, and no transcript is read back. A session's lifecycle decides
only whether the next turn resumes it and when its transcript may be deleted.

**When a session goes cold is tunable.** The freshness windows that decide when a session
stops being resumable are environment-tunable (`FRESHNESS_VOICE_MINUTES`, default 30;
`FRESHNESS_TELEGRAM_HOURS`, default 12), `session_registry.freshness_window`. A turn after
the window starts a new session.

**A reset is announced before it runs.** `/new` (`session_registry.reset_channel`) holds
the key's write gate, places an in-memory *retirement claim* on
the key, closes the pool's warm client for it, and drops the pointer by the session id it
snapshotted: while the claim is live, both resume-decision sites (the pool's and the
per-turn bypass) steer a racing turn to a fresh session instead of the dying one, and the
registry refuses to re-register the dying session id. Claims are never persisted — a claim
surviving a restart would wrongly force fresh sessions forever — and they are a *set* per
key, so one owner ending its claim cannot strip another's protection. The reset then signals
the memory writer that a session ended.

## Contracts & invariants

**INV-MEM-013**: While a retirement claim is live on a key, no resume-decision site resumes the dying session, no registration re-arms its id, and one owner ending its claim never strips another owner's protection.

Enforced in three places that have to agree: the pool's decision block and the per-turn
bypass both consult the claim in the same no-await block as their registry read and force
the decision to fresh; the registry's `register` refuses a session id any live claim names
as dying, so an in-flight turn that resumed it before the claim landed cannot re-arm the
pointer; and claims are a per-key set of opaque tokens. The reset snapshots and claims in one
no-await block *before* the pool close, so a steered-fresh turn registering mid-close keeps
its new session (the removal carries the snapshot's id); a reset that finds a session-less
slot re-derives once after the close, because an in-flight pre-reset turn may publish a
session in exactly that window.

What it does not cover: claims are in-memory, so a restart clears them. A claim's lifetime
is also shorter than the turn it defends against — it is released when the reset returns,
so a turn still running at that moment publishes freely.

**INV-MEM-031**: No SDK transcript is deleted while a run registered in it is unconfirmed: every run that can capture — an eligible resident turn, an engagement turn of a memory-writing specialist, every delegated run, an engagement's finalize summaries — registers its row (with the working directory its sessions run in) before its first capture and is confirmed only once every capture it made returned a durable receipt (a spool line is never removed by a drain that did not store it); while a row is unconfirmed no transcript in its directory is deleted, whatever session it names; no transcript's content is ever read into memory; and once every run in it is confirmed a transcript is deleted no sooner than `TRANSCRIPT_RETENTION_DAYS` (default 30) after its session can no longer resume.

Casa's live capture ([`architecture/memory-capture.md`](memory-capture.md)) is the only
source of memory; a transcript is kept as a debug copy and as the record that a run's capture
is still owed. `memory/transcript_runs.py` owns the `transcript_runs` table in `memory.db`
(ids and times, no content): `Run.start` registers a run with this process's id, `Run.name`
records the session each stream message comes from (for visibility: no deletion depends on
it), every capture is awaited through `Run.kept`, and `Run.close` in
the run's outermost `finally` confirms it when each capture was kept — a cancelled or failed
run included, since the live rules capture nothing more for it. Registration and naming run on threads of their own, with a
capture's two bounds: they wait at most 50 ms on another writer's lock (`LOCK_WAIT_S`, the
store's busy timeout), and the turn waits at most 500 ms for them at all (`STALL_GUARD_S`, a
guard against a starved thread or a stalled disk, not a latency target). A registration that fails or times out is logged and the
run goes on: with no crash its captures are durable and nothing is lost, and only a crash on
top of it lets its transcript follow normal retention (a row that commits after the turn
stopped waiting is adopted by the run, and one whose run was cancelled while waiting is
confirmed, since nothing was captured). **The gate is one rule:** while a row is unconfirmed, nothing in its
directory is deleted — no single session's transcript and not the folder — because which
session a row names is no evidence of which sessions the run touched (a resumed run names
its session late, a naming write can fail, a run can move from one session to another). A
row whose directory is unknown holds the session it names. Confirmation and a delegated run's end are written off the run's path, in order, on one thread of their own, never on the thread pool the turns' bounded writes use. An engagement's finalize decides once, before its terminal flip, whether its summaries are written; the summary run's registration and the captures both read that decision. A row left unconfirmed by an
earlier process is *unresolved*: the size meter counts it (`unresolved_runs`), its
transcript is kept until a wipe, and nothing resolves or reads it. A crash between a reply
and its capture therefore loses that reply from memory, by design.

Every deleter asks the gate (`transcript_runs.blocked`) at its delete, outside any registry
lock, and removes the rows that named what it deleted. The resident time-to-live sweep
removes a pointer and its transcript only once both its time-to-live and the session's
freshness window plus the retention have passed, so a session is never removed while it can
resume, and keeps the pointer of a transcript the gate holds so a later sweep deletes it; it
checks, evicts and deletes each entry under that session's write gate, the one its turns
hold, so no run of the session starts in between; a pointer whose activity time is unusable is kept when it names a transcript. A
blocked transcript is kept when its pointer goes. The engagement transcripts are
INV-ENG-027's, in
[`architecture/engagement-transcript-reaping.md`](engagement-transcript-reaping.md); the
delegated sessions are INV-ENG-028's, in the same document.
Rows a deleter never reaches (a resident session a later one replaced) are dropped by the
delegated retention pass once confirmed for the retention: a confirmed row blocks nothing.

What it does not cover: a transcript no pointer, record or row names is beyond every deleter
and stays — a resident session a later session replaced, and a webhook one-shot whose
pointer the boot purge dropped. A run whose registration failed and that then crashed
before its capture leaves a transcript that normal retention deletes. The CLI's own cleanup
knows nothing of this gate; INV-MEM-021 is what keeps it off.

**INV-MEM-021**: No CLI launch Casa makes through the SDK loads the user settings source or passes `cleanupPeriodDays` on its settings flag, and the utility one-shots load no settings source at all, so the pinned CLI's own age-based transcript cleanup is never enabled by a Casa launch through the SDK.

When the CLI enables that cleanup it walks the whole projects root and
deletes every transcript older than `cleanupPeriodDays` (30 days unless
set), a transcript INV-MEM-031 keeps included. The pinned CLI enables it
only when the user settings source is loaded or a loaded source sets
`cleanupPeriodDays`. Residents, specialists, `in_casa` executors, plugin
jobs and the restricted webhook pin their sources without the user source; the five
utility one-shots (the tier classifier, `query_engager`'s
synthesis, run admission's reading, the browsing step check's `_ask_check` and the memory writer's model call) pin none, since they have no project of their own and a project
file could carry the key. `cleanupPeriodDays` is never put in the shared
settings flag: there it would enable the cleanup for every launch.

What it does not cover: a
`cleanupPeriodDays` written into a project settings file a project-sourced
launch loads (Casa writes none), a
managed-policy settings source, and a CLI version whose gate differs. It
chooses no retention policy: a transcript nothing in Casa names is kept,
not cleaned.

## Failure behavior

**A reset finds no session.** There is nothing to drop; the pool close still runs and the
reset returns.

**A reset step fails.** The claim is released in a `finally` on every exit path, so a failed
removal cannot leave the key steering fresh forever.

**The session registry file is corrupt at boot.** An unparseable file is renamed aside and
the process starts with an empty registry; a parseable file with structurally corrupt
individual entries quarantines just those entries and keeps the rest. Affected session
pointers are lost; the app comes up.

## Extension points

**A new retirement caller** (anything that ends a conversation's pointer) should claim the
key first and release in a `finally`, like the reset does — the claim is what keeps racing
turns off the dying session.

**A new deleter of transcripts** asks the run-confirmation gate (`transcript_runs.blocked`)
at its delete (INV-MEM-031).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/session_registry.py::reset_channel`
- `casa/rootfs/opt/casa/session_registry.py::freshness_window`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry.begin_retirement`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry.end_retirement`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry.retirement_pending`
- `casa/rootfs/opt/casa/session_sweeper.py::SessionSweeper._sweep_once`
- `casa/rootfs/opt/casa/memory/transcript_runs.py`

**Tests**
- `tests/test_retirement_claims.py`
- `tests/test_reset_channel_retirement.py`
- `tests/test_session_registry_reset_hook.py`
- `tests/test_session_sweeper.py`
- `tests/test_transcript_confirmation.py`
- `tests/test_transcript_retention.py`
- `tests/test_transcript_gate_uncertainty.py`

**Related**
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/memory-capture.md`](../architecture/memory-capture.md)
- [`architecture/memory-labelling.md`](../architecture/memory-labelling.md)
- [`architecture/memory-wipe.md`](../architecture/memory-wipe.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
