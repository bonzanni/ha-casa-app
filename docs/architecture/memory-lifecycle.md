---
last_reviewed: 2026-11-02
---

# Memory lifecycle: retention, reset, and the retry spool

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a conversation becomes long-term memory and how it stops being one: the
freshness windows that make a session save-eligible, the claim-and-guard
protocol that retains it exactly once, the explicit reset, the durable
retry spool, and how long an SDK transcript is kept: run confirmation and
retention. Which text may be stored at all — write-trust, the completeness
of a delegated answer, the specialists whose output is kept out — and Casa's
per-turn capture into the memory store are
[`architecture/memory-capture.md`](memory-capture.md); how the memory store's
writer then files captured events into its ledgers is
[`architecture/memory-writer.md`](memory-writer.md). How each item is labelled on the way in — its sensitivity tier,
its speaker provenance, and the content addressing that deduplicates it — is
[`architecture/memory-labelling.md`](memory-labelling.md). The
operator-consented wipe that removes long-term memory and leaves conversation
transcripts on disk — its two doors, its consent posture, the fence every writer passes through and what it leaves behind — is
[`architecture/memory-wipe.md`](memory-wipe.md). What happens to a stored fact
on the way back out — rendering and what a caller may claim from a result — is
[`architecture/memory.md`](memory.md), and who may read it back is
[`architecture/memory-scoping.md`](memory-scoping.md); the warm-client pool
whose resume decision this lifecycle steers is
[`architecture/sdk-client-pool.md`](sdk-client-pool.md).

## Mental model

**When a session goes cold is tunable, and retention deduplicates.** The
freshness windows that decide when a session stops being resumable and
becomes save-eligible are environment-tunable (`FRESHNESS_VOICE_MINUTES`,
default 30; `FRESHNESS_TELEGRAM_HOURS`, default 12). Retained facts are
content-addressed, so the same speaker saying the same thing across sessions
collapses to one stored document — and agent-side deduplication ignores
persona version, so a persona upgrade does not mint duplicate memories.

**A registry key names a conversation slot, not a session.** A new turn can
re-register the slot at any suspension point, so every step of the save
protocol carries the session id its caller judged, and — where the id cannot
distinguish — a process-local registration generation. That is INV-MEM-006's
territory, below.

**A retirement is announced before it runs.** An explicit reset and the wipe
both place an in-memory *retirement claim* on the key before touching
anything: while any claim is live, both resume-decision sites (the pool's
and the per-turn bypass) steer a racing turn to a fresh session instead of
the dying one, and the registry refuses to re-register the dying session id.
Claims are never persisted — a claim surviving a restart would wrongly force
fresh sessions forever — and they are a *set* per key: a wipe claiming a key
a reset already claimed cannot, by ending its own claim, strip the reset's
protection.

**Three writers run on their own schedule.** The freshness reaper saves
sessions that went cold; a superseded session's transcript is retained by a
detached background task, whose failure spools a durable retry record under
`/data/cold-retain-retry`; and a finishing engagement retains its summary
from a detached task of its own. All of them converge on the shared bank —
which is why the wipe cannot be "delete the bank" alone
([`architecture/memory-wipe.md`](memory-wipe.md)). Every one of them passes
the eligibility rules first: a session save skips the user turn that is Casa's
notice naming a specialist that writes no memory
(`delegated_memory.notice_from_excluded_role`, which reads
`delegated_memory.role_writes_memory`) and keeps the resident's relay after it;
a summary of such a specialist is not written (INV-MEM-024 in
[`architecture/memory-capture.md`](memory-capture.md)).

## Contracts & invariants

**INV-MEM-006**: A session save or removal keyed by channel acts only on the session id its caller snapshotted — a registration carrying a different id in that window is released, not retained or deleted; an explicit reset deliberately removes its snapshotted session even when re-registered.

The registry key names a *conversation slot*, not a session — a new turn can
re-register the slot at any suspension point. Every step therefore carries
the session id its caller judged (the reaper's cold snapshot, a reset's own
snapshot): the save entry point releases a claim that landed on a different
session, `finish_save` and `clear_save_claim` decline when the stored id
moved, and an explicit reset — which takes no *save* claim, retaining its
snapshot directly, in the background, once the pointer is gone — has its
trailing removal decline the same way — as do the reaper's direct removals
of unusable and recall-only entries (a snapshot without a session id guards
on that absence).

Where the id cannot distinguish, the guards also accept a *registration
generation*: a process-local monotonic token stamped per registration, never
persisted (a loaded entry reads "no generation", which no live one
reproduces). Passing the snapshotted generation makes each conditional
mutation — claim placement included — decline once any registration moved
it, even same-id. Stale-resume recovery and the reaper pass it; a *reset*
does not: the id names the conversation being reset, so removing a same-id
re-registration is its contract.

What it does not cover: a caller that passes no expected id gets the
unconditional behavior; and a turn still running on the *same* session when
a reset retains it can have its tail exchanges miss retention — the reset
drops the pointer (its contract) and nothing saves that session again.

**INV-MEM-023**: An explicit reset does not wait for its retain: before it drops the session pointer it writes the ended conversation's retry record whenever that conversation may be banked — a write that fails is reported and does not stop the reset — and the reset's background retain removes the record only after the retain has landed.

`/new` is awaited inside the chat's serial lock, and the reset holds turn
admission and the key's write gate; a retain inside it held the operator's
next message for the whole tier classification and memory write — 18 to 62
seconds measured. The reset therefore ends at the pointer removal and leaves
the retain to a background task that takes only the retain fence. What moved
was the wait, not the durability: the record that a failed retain has always
left behind is now written first, so a process that dies at any point after
the reset returns leaves the freshness sweep a record to retain — provided the
record was written. "May be
banked" is the retain's own guard set — a bank-writable channel, both
provenances on the snapshot, and the fence generation captured at the snapshot
— so a recall-only channel, a legacy entry and a reset a wipe has overtaken
write nothing.

What it does not cover: when the conversation reaches the bank. The fresh
session's first turns may recall before the background retain lands, and a
retain that fails waits for the hourly sweep, whose retries are bounded
(after the last one the record is dropped, loudly). Nor does a record write
that itself fails stop the reset: a restart before the background retain lands
then loses the conversation (see Failure behavior).

**INV-MEM-013**: While a retirement claim is live on a key, no resume-decision site resumes the dying session, no registration re-arms its id, and one owner ending its claim never strips another owner's protection.

Enforced in three places that have to agree: the pool's decision block and
the per-turn bypass both consult the claim in the same no-await block as
their registry read and force the decision to fresh (without a duplicate
retain — the retiring caller owns the retain); the registry's `register`
refuses a session id any live claim names as dying, so an in-flight turn
that resumed it before the claim landed cannot re-arm the pointer (its
exchanges still reach the retained transcript, which is flushed before the
retirement's retain reads it); and claims are a per-key set of opaque tokens,
so overlapping owners — a wipe over a live reset — end only their own.
The reset itself snapshots and claims in one no-await block *before* the
flush-close, so a steered-fresh turn registering mid-close can never become
the session it retains and removes; a reset that finds a session-less slot
re-derives once after the close, because an in-flight pre-reset turn may
publish a session in exactly that window.

What it does not cover: claims are in-memory, so a restart clears them (a
retirement in flight at crash time is finished by the reaper's ordinary
machinery, not by the claim). A claim's *lifetime* is also shorter than the
turn it defends against — it is released when the retirement returns, so a
turn still running at that moment publishes freely. Claims are therefore no
longer the load-bearing protection: turn admission (INV-CONC-005) is, and it
holds until every turn has drained. The claims remain because they still
cover the background retain paths, which are not turns.

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
blocked transcript is kept when its pointer goes. The engagement transcripts and the
`claude_code` workspace are INV-ENG-027's, in
[`architecture/engagement-transcript-reaping.md`](engagement-transcript-reaping.md); the
delegated sessions are INV-ENG-028's, in [`architecture/delegation.md`](delegation.md).
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
jobs and the restricted webhook pin their sources without the user source; the six
utility one-shots (the observer, the tier classifier, `query_engager`'s
synthesis, run admission's reading, the browsing step check's `_ask_check` and the memory writer's model call) pin none, since they have no project of their own and a project
file could carry the key. `cleanupPeriodDays` is never put in the shared
settings flag: there it would enable the cleanup for every launch.

What it does not cover: the `claude_code` executor driver, which is not an
SDK launch. Its run script (`engagement_run_template.sh`) starts the CLI
under the engagement's own `HOME` (`/data/engagements/<id>/.home`), not
Casa's, and passes neither `--setting-sources` nor `--settings`, so the
CLI's default sources, the user source of that home included, apply there;
this rule says nothing about that CLI's cleanup. Nor does it cover a
`cleanupPeriodDays` written into a project settings file a project-sourced
launch loads (Casa writes none), a
managed-policy settings source, and a CLI version whose gate differs. It
chooses no retention policy: a transcript nothing in Casa names is kept,
not cleaned.

## Failure behavior

**Saving a session fails.** The save is abandoned, its claim is released —
including when the failure is a cancellation at shutdown — and the entry
stays for the next freshness sweep to retry, until the time-to-live sweep's
retention removes it (INV-MEM-031); the memory store's own capture never
depends on it.

**An explicit reset's retain fails, or never gets to run.** The reset drops
the pointer either way — that is its contract, and the fresh conversation the
user asked for starts at once — so the entry cannot be what carries the
retry. The durable retry record is, and the reset writes it *before* it drops
the pointer (INV-MEM-023): the conversation's retain then runs in the
background through the same registry-decoupled path a gap-superseded session
uses, which removes the record when the retain lands and keeps it when the
retain fails. A restart, a crash or a backend outage after `/new` therefore
leaves the conversation to the freshness sweep rather than losing it. Two
things this does not promise. A record that cannot be written is a WARNING,
not a stop — the background retain still runs, and if it fails too and its own
spool write fails, that is said out loud at ERROR with the session id and the
transcript directory, the only handle left. And a wipe completing first
discards both, by the retain fence the bank's writers pass.

Because the reset takes no save claim, a freshness sweep already retaining
the same session can submit the same transcript alongside it — and so can the
sweep's retry pass, which may pick up the reset's record while the background
retain is still running. Retains are
content-addressed, so both land on the same documents; each classifies on its
own, so their tiers can differ, and the one applied last stands — never below
the tier both read (INV-MEM-018). What is duplicated is work.
That is the deliberate price of not making the reset's retry depend on
winning a claim it does not need — the reset has already decided which
conversation it is ending.

**A gap-superseded session's background retain fails.** That retain runs
decoupled from the registry (the new turn is about to overwrite the entry),
so failure spools a durable retry record instead; the freshness sweep drives
retries and gives up loudly after a bounded number of attempts. An
unreadable record is dropped, not retried forever.

**The session registry file is corrupt at boot.** An unparseable file is
renamed aside and the process starts with an empty registry; a parseable
file with structurally corrupt individual entries quarantines just those
entries and keeps the rest. Affected session pointers are lost; the app
comes up.

## Extension points

**A new writer** should build its items through the retain-item builder
(bypassing it skips tier tagging, provenance validation, the tier floor and the
write-trust check at once), handing it the stored-tag read of the bank it is
about to write — and must capture a fence generation at its decision point
and enter the fence's shared section around its retain-and-spool critical
section, or a wipe cannot see it. It also owns the completeness question: if
the content it is about to store can be the product of a turn that did not
finish, it must consult that verdict itself. Nothing downstream will — the
builder does not know, and no reader can filter on it afterwards.

**A new channel** that should have its cold sessions retained needs that
retention entry *in addition to* its clearance and write-trust entries —
none of the three lists is inferred from the others, and forgetting one
produces a channel that silently never persists.

**A new retirement caller** (anything that ends a conversation's pointer)
should claim the key first and release in a `finally`, like the reset and
the wipe do — the claim is what keeps racing turns off the dying session.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory_provenance.py::build_retain_items`
- `casa/rootfs/opt/casa/session_saver.py::save_session`
- `casa/rootfs/opt/casa/session_saver.py::reset_channel`
- `casa/rootfs/opt/casa/session_saver.py::retain_cold_session`
- `casa/rootfs/opt/casa/session_saver.py::retry_spooled_cold_retains`
- `casa/rootfs/opt/casa/session_saver.py::freshness_window`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry.begin_retirement`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry.end_retirement`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry.retirement_pending`
- `casa/rootfs/opt/casa/freshness_reaper.py::FreshnessReaper.sweep_once`
- `casa/rootfs/opt/casa/delegated_memory.py::retain_delegated`
- `casa/rootfs/opt/casa/session_sweeper.py::SessionSweeper._sweep_once`
- `casa/rootfs/opt/casa/delegated_memory.py::role_writes_memory`
- `casa/rootfs/opt/casa/delegated_memory.py::notice_from_excluded_role`
- `casa/rootfs/opt/casa/session_saver.py::transcript_to_items`
- `casa/rootfs/opt/casa/memory/transcript_runs.py`

**Tests**
- `tests/test_session_saver.py`
- `tests/test_no_retain_specialist.py`
- `tests/test_freshness_reaper.py`
- `tests/test_retirement_claims.py`
- `tests/test_reset_channel_retirement.py`
- `tests/test_reset_background_save.py`
- `tests/test_memory_provenance.py`
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
