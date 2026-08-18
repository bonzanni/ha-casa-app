---
last_reviewed: 2026-08-13
---

# Memory lifecycle: retention, reset, and the wipe

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a conversation becomes long-term memory and how it stops being one: the
freshness windows that make a session save-eligible, the claim-and-guard
protocol that retains it exactly once, how each item is labelled on the way in
(its sensitivity tier, its speaker provenance, and the content addressing that
deduplicates it), the explicit reset, the durable retry spool, and the
operator-consented wipe that removes everything. What happens to a stored fact
on the way back out — rendering and what a caller may claim from a result — is
[`architecture/memory.md`](memory.md), and who may read it back is
[`architecture/memory-scoping.md`](memory-scoping.md); the warm-client pool
whose resume decision this lifecycle steers is
[`architecture/turn-loop.md`](turn-loop.md).

## Mental model

**Writing is narrower than reading.** Only write-trusted channels retain to
the shared bank. A channel that can recall is not thereby able to store.

**When a session goes cold is tunable, and retention deduplicates.** The
freshness windows that decide when a session stops being resumable and
becomes save-eligible are environment-tunable (`FRESHNESS_VOICE_MINUTES`,
default 30; `FRESHNESS_TELEGRAM_HOURS`, default 12). Retained facts are
content-addressed, so the same speaker saying the same thing across sessions
collapses to one stored document — and agent-side deduplication ignores
persona version, so a persona upgrade does not mint duplicate memories.

Content addressing only holds because the hash input is the utterance and
nothing else. Every sent user turn carries a per-turn time envelope, and the
transcript echoes it back — hashed as-is, that second-precision timestamp
would mint a fresh document for the same sentence in every session. Retention
therefore splits the envelope off user turns at the transcript-readback
boundary: stored text and document id are both envelope-free, and the turn's
wall-clock time survives out-of-band as the retain item's timestamp. The
composer and splitter are a pinned pair, so the envelope's shape cannot drift
from what is stripped.

**Each item is labelled before it is stored, and the labels are not the
caller's to choose.** A bounded LLM pass classifies the item's sensitivity
tier, and its speaker provenance is recorded from what the turn actually
established. Both live in reserved tag namespaces that ordinary application
tags may not reach, so a caller cannot promote its own fact to a tier the
classifier did not give it or attribute it to someone it did not come from.

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
which is why the wipe cannot be "delete the bank" alone.

**The wipe is one operation, two doors, one consent posture.** The
orchestrator first closes *turn admission* and drains every in-flight turn
(INV-CONC-005) — until that holds, a turn the client pool never owned can
re-arm a session pointer after the wipe has reported completion, and a
first-ever turn on a key is not even visible to enumerate. It then claims
every key, drains in-flight bank writers behind an exclusive fence, drops the
retry spool, drops every session pointer *without retaining it* (retiring
saves first — exactly the residue a wipe must not leave), deletes the bank,
and reports counts. Both drains are bounded and fail closed: turns or writers
that do not finish in time abort the wipe with nothing deleted, because a wipe
that cannot prove it drained everything must not report that it deleted
everything. The terminal door
(`casactl memory-wipe --yes` → `POST /admin/memory/wipe`) is root-gated by
the same peer-credential check as every admin route; the agent door (the
`wipe_memory` tool) posts an Approve/Cancel keyboard to the configured
operator and executes only on the operator's own tap, from the broker's
finish hook — never inside the invoking turn, whose pool lock the wipe's
flush-close must be able to take. With no operator configured, nobody can
consent and the tool door refuses everyone. At most one wipe runs at a time,
and shutdown freezes new admissions before draining a running one.

**Writers that straddle a wipe discard.** Every bank writer captures a
*fence generation* in the same no-await block as the decision that commits
it to its source data — beside the pool's resume decision for a superseded
session's retain, at summary assembly for an engagement's, at entry for the
save and the spool replay — and enters a shared fence section that compares
it. A wipe bumps the generation under the exclusive side after draining the
in-flight sections, so a pre-wipe writer that resumes later retains nothing
and, crucially, spools nothing: the operator consented to deleting exactly
that content.

## Contracts & invariants

**INV-MEM-004**: A caller cannot inject a sensitivity tier or a provenance tag through ordinary application tags.

Enforced in the retain-item builder, which refuses reserved tag namespaces
before doing any classification or I/O, and validates the speaker provenance
it is given.

What it does not cover, and this is worth stating plainly: it protects the
*write* path from its own callers. It does not authenticate what the backend
returns. On read, a syntactically valid provenance tag is trusted and is not
cross-checked against the duplicate copy stored in the item's metadata. A
recalled fact can therefore carry a speaker identity that the read path has
not independently established — see [`architecture/memory.md`](memory.md).

**INV-MEM-005**: Only write-trusted channels retain to the shared bank.

Enforced by the channel write-policy check, consulted by every production
retain path.

What it does not cover: the seam's retain method itself enforces nothing. A
future caller that skips the builder and the policy check would bypass both.

**INV-MEM-006**: A session save or removal keyed by channel acts only on the session id its caller snapshotted — a registration carrying a different id in that window is released, not retained or deleted; an explicit reset deliberately removes its snapshotted session even when re-registered.

The registry key names a *conversation slot*, not a session — a new turn can
re-register the slot at any suspension point. Every step of the save
protocol therefore carries the session id its caller judged (the reaper's
cold snapshot, a reset's own snapshot): the save entry point releases a
claim that landed on a different session, `finish_save` and
`clear_save_claim` decline when the stored id moved, and an explicit reset's
trailing removal declines the same way — as do the reaper's direct removals
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
a reset saves it can have its tail exchanges miss retention — the reset
drops the pointer (its contract) and nothing saves that session again.

**INV-MEM-009**: The per-turn time envelope never reaches the content-addressed document id or the stored memory text — an identical utterance retained from any session collapses to one document — and the turn's wall-clock time is carried out-of-band on the retain item.

Enforced at the transcript-readback boundary, which splits a single leading
envelope off each user turn before the retain-item builder hashes or stores
it. The envelope's composer and splitter are a pinned pair; a round-trip test
fails the moment the composed shape drifts from what the splitter recognises.

What it does not cover: documents retained before the split existed keep their
enveloped text and stale ids — the bank converges only as facts are re-said.
Writers that bypass the transcript readback (delegated retains) never carried
the envelope in the first place.

**INV-MEM-012**: A tier-classifier reply yields a tier only when it is a single line holding one (possibly decorated) tier token, or when a multi-line reply's final non-empty line is the literal `Tier: <word>` answer line whose earlier tier-token or Tier-label lines all resolve to the same tier; prose tier words, conflicts, and unresolvable labels yield no tier; the item defaults to private.

Enforced in `parse_tier`: the single-line arm full-matches one decorated
token, never searching leftmost and never spanning lines; the answer-line arm
accepts only the literal final line the prompt mandates, and an earlier
answer-like line only when it resolves to the same tier — never "last one
wins". Replaces retired MEM 007, whose statement the answer-line arm
falsifies.

What it does not cover: a classifier confidently declaring the wrong tier is
believed — a parser contract, not accuracy; the eval set owns accuracy.

**INV-MEM-013**: While a retirement claim is live on a key, no resume-decision site resumes the dying session, no registration re-arms its id, and one owner ending its claim never strips another owner's protection.

Enforced in three places that have to agree: the pool's decision block and
the per-turn bypass both consult the claim in the same no-await block as
their registry read and force the decision to fresh (without a duplicate
retain — the retiring caller owns the retain); the registry's `register`
refuses a session id any live claim names as dying, so an in-flight turn
that resumed it before the claim landed cannot re-arm the pointer (its
exchanges still reach the retained transcript, which is flushed before the
retirement's save reads it); and claims are a per-key set of opaque tokens,
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

**INV-MEM-014**: The wipe executes only on the configured operator's explicit consent at its consent-bearing door, and no durable pre-wipe writer survives it: the spool is dropped, every claimed pointer is dropped without retention, and a bank writer that straddles the wipe discards — retaining nothing and spooling nothing.

Enforced by the tool door binding its keyboard to the configured operator's
identity (the broker refuses any other actor's tap, and a broker cancel can
only finish as *cancelled*, never as an answer); by the admin door sitting
behind the root peer-credential gate plus an explicit confirm field; by the
orchestrator's order — claims first, then the exclusive fence drain and
generation bump, then the spool, then sid-guarded pointer removals, then the
bank — with claims released in a `finally`; and by every writer's
generation check sitting in front of both its retain *and* its
failure-spool arm.

What it does not cover, deliberately disclosed: a turn or engagement already
in flight when the wipe runs may contribute one post-wipe item (its retain
enters the fence with a post-wipe generation); a session pointer registered
by a steered-fresh turn mid-wipe survives (its conversation is
post-wipe-initiation — the sid-guarded remove protects it on purpose); the
backend applies retains it accepted before the delete on its own schedule;
and the bank deletion itself is the backend's — Casa does not verify
emptiness afterwards.

## Failure behavior

**Saving a session fails.** The save is abandoned, its claim is released —
including when the failure is a cancellation at shutdown — and the entry
stays for a later sweep to retry. An explicit reset is the exception — it
drops the pointer whether or not the save succeeded, unless a newer session
registered meanwhile (INV-MEM-006), in which case the newer registration
stands.

**A gap-superseded session's background retain fails.** That retain runs
decoupled from the registry (the new turn is about to overwrite the entry),
so failure spools a durable retry record instead; the freshness sweep drives
retries and gives up loudly after a bounded number of attempts. An
unreadable record is dropped, not retried forever.

**Tier classification fails.** Retention classifies each item with a bounded
LLM pass; a backend error retries once; an unparseable reply is re-asked once
with the format mandate restated, then falls to *private* with a log warning,
and the save logs one aggregate N-defaulted-of-M line. "Unparseable" is
strict: only a single-line (possibly decorated) tier token or a final
`Tier: <word>` line with agreeing earlier answers parses — anything else
(prose, conflicts) is ambiguity. The write is not lost, but the fact goes
invisible below the highest clearance — absence on voice and friends
surfaces.

**The session registry file is corrupt at boot.** An unparseable file is
renamed aside and the process starts with an empty registry; a parseable
file with structurally corrupt individual entries quarantines just those
entries and keeps the rest. Affected session pointers are lost; the app
comes up.

**The wipe's writer drain times out.** The wipe aborts with nothing deleted
and says so — a stuck retain must not be raced, and an abort is retryable.
A wipe interrupted by shutdown is drained to completion (or a truthful
failure report) before the channels and the memory backend close; a consent
approval landing during shutdown is refused, never half-run.

**The bank delete itself fails.** The failure propagates to the door that
asked (an error edit on the consent keyboard, a non-2xx with the reason on
the admin route) — by then the spool and the pointers are already gone,
and the report says exactly that rather than claiming a deletion that did
not happen.

## Extension points

**A new writer** should build its items through the retain-item builder
(bypassing it skips tier tagging, provenance validation and the write-trust
check at once) — and must capture a fence generation at its decision point
and enter the fence's shared section around its retain-and-spool critical
section, or a wipe cannot see it.

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
- `casa/rootfs/opt/casa/channel_policy.py::writes_to_bank`
- `casa/rootfs/opt/casa/timekeeping.py::compose_time_envelope`
- `casa/rootfs/opt/casa/timekeeping.py::split_time_envelope`
- `casa/rootfs/opt/casa/session_saver.py::save_session`
- `casa/rootfs/opt/casa/session_saver.py::reset_channel`
- `casa/rootfs/opt/casa/session_saver.py::retain_cold_session`
- `casa/rootfs/opt/casa/session_saver.py::retry_spooled_cold_retains`
- `casa/rootfs/opt/casa/session_saver.py::freshness_window`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry.begin_retirement`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry.end_retirement`
- `casa/rootfs/opt/casa/session_registry.py::SessionRegistry.retirement_pending`
- `casa/rootfs/opt/casa/memory_wipe.py::RetainFence`
- `casa/rootfs/opt/casa/memory_wipe.py::wipe_long_term_memory`
- `casa/rootfs/opt/casa/memory_wipe.py::start_wipe_task`
- `casa/rootfs/opt/casa/memory_wipe.py::freeze_wipes`
- `casa/rootfs/opt/casa/memory_wipe.py::drain_wipe_task`
- `casa/rootfs/opt/casa/tools.py::wipe_memory`
- `casa/rootfs/opt/casa/internal_handlers.py::build_admin_memory_wipe_handler`
- `casa/rootfs/opt/casa/semantic_memory.py::SemanticMemory.delete_bank`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.delete_bank`
- `casa/rootfs/opt/casa/freshness_reaper.py::FreshnessReaper.sweep_once`

**Tests**
- `tests/test_session_saver.py`
- `tests/test_channel_policy.py`
- `tests/test_freshness_reaper.py`
- `tests/test_retirement_claims.py`
- `tests/test_reset_channel_retirement.py`
- `tests/test_memory_wipe.py`
- `tests/test_retain_fence_writers.py`
- `tests/test_wipe_memory_tool.py`
- `tests/test_admin_memory_wipe_route.py`
- `tests/test_memory_provenance.py`
- `tests/test_time_envelope.py`

**Related**
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
