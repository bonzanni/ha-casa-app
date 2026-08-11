---
last_reviewed: 2026-08-10
---

# Memory and recall

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Long-term memory: how a fact is written, who may read it back, and what a caller is told
when the store cannot answer. It covers the memory seam, the sensitivity model, and the
provenance carried on a recalled fact. It does not cover the SDK session transcript, which
is short-term context on a different lifecycle, and it does not describe the memory backend's
own internals — those live outside this repository.

## Mental model

There is **one shared bank**. Roles do not partition memory; separation comes from
sensitivity tiers and read clearance, not from storage.

**Recall has three outcomes, and the third is the reason this subsystem is shaped the way it
is.** A call returns hits, returns a genuine empty result, or fails as *unavailable*. The
distinction between the last two is load-bearing: "I searched and found nothing" and "I could
not search" mean opposite things to a model deciding whether to assert that something never
happened. Collapsing them produces confident false denials, which is the failure this design
exists to prevent.

Two consequences follow, and both are easy to get wrong.

**All hits being unreadable is *unavailable*, not empty.** If every returned hit sits above
the caller's clearance, or carries an unusable tier, the seam raises rather than returning an
empty result. The caller genuinely does not know whether relevant memory exists.

**An empty rendered string does not prove zero hits.** Rendering stops once an entry would
exceed the token budget, and if the *first* entry is already too large, nothing is emitted.
So a rendered `""` may mean no hits, or may mean hits that did not fit. Code that treats an
empty render as evidence of absence is making a claim the render cannot support.

**And an empty result does not prove absence, at any clearance.** The recall request carries
the caller's readable tiers as a server-side tag filter, so a hit above the caller's clearance
is dropped *by the backend* and a clearance-blocked search returns the same well-formed empty
as a genuine miss. Even at the highest clearance, server-side token truncation and the types
filter can hide content. The recall tool therefore never hands the model a bare empty: a
zero-hit result carries guidance that absence is unknowable and must not be asserted, and a
result whose readable hits exist but did not fit the render budget says so and asks for a
narrower query — the one existence claim an empty render *can* support, since those hits
already passed the clearance filter.

**Auto-recall is not "every turn".** It happens when a turn's options are built, which is a
fresh non-voice session only — a warm reused client skips that path entirely, and voice never
auto-recalls. Both can still recall explicitly through the tool. "The agent remembers
automatically" is true of a narrower set of turns than it sounds.

**Read clearance is per channel and fails closed** — and on Telegram, per *sender*. Known
channels are mapped explicitly; an unrecognised one gets the *least* sensitive clearance.
The fail direction is the security control here: an unknown surface must read less, never
more, and a test pins the function's docstring to that direction so prose and behaviour
cannot drift apart again. The Telegram channel's mapped clearance (private) is not
route-wide: the ingress stamps a per-sender origin clearance — private only for the sender
whose id matches the configured operator chat id, public for anyone else the accept-all
mode lets in — and origin-aware resolution honors that stamp, failing closed to public
when a telegram-marked turn carries a missing or malformed one. Both ways into a Telegram
turn stamp it: a message and a button tap.

**An engagement reads at the clearance of the turn that created it.** This needs stating
because the mechanism is not the obvious one: an engagement's own tool calls arrive over the
internal socket, which binds the engagement record but no ambient turn origin — so a
clearance resolved from the ambient origin alone would find nothing and fall through to the
channel default, handing an engagement started by a low-clearance sender the operator's
private tier. The engagement's recorded origin carries the markers its creating turn was
stamped with, and that is what its reads resolve against: its own recall, the prior-
engagement archive injected into an executor's prompt at launch, the engager-side context a
specialist can query, and a nested engagement it spawns, which inherits rather than
re-deriving. An engagement whose record predates this (or came from an origin that stamps no
route) keeps the channel-keyed behaviour.

Inheritance at creation is only half the rule, because an engagement is *steerable*: anyone
in the engagement supergroup can direct it by messaging its topic. So the recorded clearance
is also a **monotonic floor** — a steering turn clamps it down to that sender's clearance and
never raises it back, which makes the property "an engagement never reads above the
least-privileged person who has taken part in it". The clamp is deliberately one-way: two
people steering concurrently can only drive it further down, and the operator returning to
their own engagement does not restore what a lower-clearance participant has already seen.

A clamp that moves the record does not stop at future reads — it evicts what the session
already holds. The transcript and the launch-injected archive were built at the old tier, and
a lower-clearance steerer could simply ask the engagement to restate them, so the same locked
ingress pass that lowers the record also: durably marks the context for rebuild (the flag and
the clamp persist in one write, so a crash between them cannot happen), withholds the
record's own launch materials (task, brief, context, world-state — every later render
re-derives from the record, and evicting the session while the record still carried them
would re-import them), tears the live session down, and drops the resume pointer. Every
resume path — the steering turn itself, a system continuation, boot replay — refuses to
resume while the rebuild is pending and establishes a fresh session at the clamped floor
instead; while it is pending, the old process's tool calls are refused at both dispatch
choke points (the internal socket and the in-process fence), so it cannot read or launch
children carrying its pre-clamp task. A read already in flight when the clamp lands is
re-filtered at the new clearance after it returns, and a launch caught mid-start aborts
rather than deliver a prompt rendered from pre-clamp materials.

What the eviction deliberately does not do: content already posted to the topic stays
posted, and a turn already running at clamp time may still complete into the topic —
engagement topics are readable by every supergroup member regardless, so both are
disclosures the topic already carried. A nested engagement spawned before the clamp keeps
its own record's clearance (steering its topic clamps it the same way). The clamp is
applied in memory first, so a failed persist leaves this process correctly clamped while a
restart would restore the higher tier (logged as security-relevant). And it only moves a
record that carries a stamped clearance: an engagement running before per-sender markers
existed has none, so it keeps channel-keyed clearance — on Telegram, private — until it
finishes.

**Writing is narrower than reading.** Only write-trusted channels retain to the shared bank.
A channel that can recall is not thereby able to store.

**When a session goes cold is tunable, and retention deduplicates.** The freshness windows
that decide when a session stops being resumable and becomes save-eligible are
environment-tunable (`FRESHNESS_VOICE_MINUTES`, default 30; `FRESHNESS_TELEGRAM_HOURS`,
default 12). Retained facts are content-addressed, so the same speaker saying the same
thing across sessions collapses to one stored document — and agent-side deduplication
ignores persona version, so a persona upgrade does not mint duplicate memories.

Content addressing only holds because the hash input is the utterance and nothing else.
Every sent user turn carries a per-turn time envelope, and the transcript echoes it back —
hashed as-is, that second-precision timestamp would mint a fresh document for the same
sentence in every session. Retention therefore splits the envelope off user turns at the
transcript-readback boundary: stored text and document id are both envelope-free, and the
turn's wall-clock time survives out-of-band as the retain item's timestamp. The composer
and splitter are a pinned pair, so the envelope's shape cannot drift from what is stripped.

**Mental-model overlays cannot be tier-filtered at all**, because they are bank-wide
summaries rather than individually tagged facts. That is why they are exposed only at the
highest clearance — there is no way to redact part of one.

## Contracts & invariants

**INV-MEM-001**: Recall reports unavailability by raising; it never represents a failure as a successful empty result.

Enforced in the seam's implementations — the backend implementation raises `RecallUnavailable`
(or its `RecallProtocolError` subclass) for timeout, HTTP failure, transport failure and
malformed envelopes, and the no-op implementation raises rather than returning empty when no
backend is configured.

What it does not cover: **individual call sites may still collapse the distinction after the
fact.** The two model-facing consumers no longer do — `recall_memory` and `query_engager`
scope their empty results explicitly (INV-MEM-010) — but the invariant holds at the seam,
for typed recall, not at every consumer. A prompt-assembly caller that renders an empty
digest as silence (the executor archive slot) is making no claim, which is fine; a new
consumer that words emptiness as absence would reintroduce the defect. Check the call site
you care about rather than assuming it propagates.

**INV-MEM-002**: A typed hit is readable only when its tags carry exactly one recognised tier at or below the caller's clearance; if every hit is dropped, the result is unavailable rather than empty.

Enforced in the backend implementation's typed recall path, which decodes each hit's tier and
drops what it cannot read, then raises when nothing readable survives.

What it does not cover: the legacy string recall path, mental-model overlays, and the SDK
transcript are not tier-filtered by this check. Filtering is also applied locally to what the
backend returned — the request's own tag filter is not treated as the access control.

**INV-MEM-003**: An unrecognised channel reads at the least-sensitive tier.

Enforced by the channel-clearance lookup's default. The direction matters: an unknown surface
sees less, not more.

What it does not cover: origin-aware resolution is narrower than it sounds. Resident
auto-recall and the recall tool resolve clearance from the stamped origin
(`clearance_for_origin()`), so a webhook turn's clearance there depends on its declared
origin. Delegated recall honours a route its caller passes, but falls back to channel-keyed
clearance for every caller that passes none — check the call site before assuming a
delegated read is origin-filtered.

**INV-MEM-004**: A caller cannot inject a sensitivity tier or a provenance tag through ordinary application tags.

Enforced in the retain-item builder, which refuses reserved tag namespaces before doing any
classification or I/O, and validates the speaker provenance it is given.

What it does not cover, and this is worth stating plainly: it protects the *write* path from
its own callers. It does not authenticate what the backend returns. On read, a syntactically
valid provenance tag is trusted and is not cross-checked against the duplicate copy stored in
the item's metadata. A recalled fact can therefore carry a speaker identity that the read path
has not independently established.

**INV-MEM-005**: Only write-trusted channels retain to the shared bank.

Enforced by the channel write-policy check, consulted by every production retain path.

What it does not cover: the seam's retain method itself enforces nothing. A future caller that
skips the builder and the policy check would bypass both.

**INV-MEM-006**: A session save or removal keyed by channel acts only on the session id its caller snapshotted — a registration carrying a different id in that window is released, not retained or deleted; an explicit reset deliberately removes its snapshotted session even when re-registered.

The registry key names a *conversation slot*, not a session — a new turn can re-register the
slot at any suspension point. Every step of the save protocol therefore carries the session
id its caller judged (the reaper's cold snapshot, a reset's own snapshot): the save entry
point releases a claim that landed on a different session, `finish_save` and
`clear_save_claim` decline when the stored id moved, and an explicit reset's trailing
removal declines the same way — as do the reaper's direct removals of unusable and
recall-only entries (a snapshot without a session id guards on that absence).

The guard is deliberately session-scoped, not registration-scoped, and each path has its own
reason that suffices. A *reset* that removes a same-id re-registration is executing its
contract: the id names exactly the conversation the user asked to reset, whoever refreshed
the pointer meanwhile. The *reaper* cannot meet a same-id re-registration from a racing
turn at all: resuming stamps `last_active` before the turn runs and a past-freshness
session is never resumed, so any turn racing a cold sweep registers a *fresh* id — which
this guard catches.

What it does not cover: a caller that passes no expected id gets the unconditional
behavior; and a turn still running on the *same* session when a reset saves it can have its
tail exchanges miss retention — the reset drops the pointer (its contract) and nothing
saves that session again.

**INV-MEM-008**: An engagement's reads resolve clearance from the origin markers its own record carries, and a steering turn from a lower-clearance sender clamps those markers down permanently.

Enforced in two places that have to agree: where the markers are resolved for an
access-control decision — the ambient origin wins when it has a route of its own (an
in-process delegated turn), otherwise the bound engagement record supplies them — and at the
engagement-topic ingress, which lowers the record's stamped clearance to each steering
sender's before their turn is delivered. Every read an engagement can perform resolves
through the first: its own recall, the executor archive injected at launch, the engager-side
context, and a nested engagement it spawns.

What it does not cover: a record carrying no markers falls back to channel-keyed clearance,
which on Telegram is private — so this tightens what a *newly* stamped origin can reach, and
does not retroactively downgrade engagements created before the markers existed. The clamp
also cannot un-share what was already disclosed before a lower-clearance sender arrived.

**INV-MEM-009**: The per-turn time envelope never reaches the content-addressed document id or the stored memory text — an identical utterance retained from any session collapses to one document — and the turn's wall-clock time is carried out-of-band on the retain item.

Enforced at the transcript-readback boundary, which splits a single leading envelope off each
user turn before the retain-item builder hashes or stores it. The envelope's composer and
splitter are a pinned pair; a round-trip test fails the moment the composed shape drifts from
what the splitter recognises.

What it does not cover: documents retained before the split existed keep their enveloped text
and stale ids — the bank converges only as facts are re-said. Writers that bypass the
transcript readback (delegated retains) never carried the envelope in the first place.

**INV-MEM-010**: An empty recall result never asserts non-existence: the model-facing recall tools scope their empty and unknown results at every clearance tier, and readable hits that did not fit the render budget are reported as existing rather than absent.

Enforced in the recall tool's empty-digest arms and the engager-query tool's unknown arm, which
attach explicit do-not-assert-absence guidance in place of a bare empty result.

What it does not cover: it is a statement about what the tool result *says*, not what a model
does with it — prompt-level honesty remains the model's job. Prompt-assembly consumers that
render emptiness as silence are outside it, deliberately: absence of a block is not a claim
of absence.

**INV-MEM-011**: A clearance downgrade durably invalidates the engagement's session context in the same write that lowers the record — until a fresh session is established at the clamped floor, no path resumes the old session and the record's tool calls are refused at both dispatch choke points.

Enforced by the clamp setting the rebuild flag and withholding the record's launch materials
under one strict persist; by the resume core and boot replay routing a flagged record to a
fresh-session rebuild instead of a resume; by the internal-socket handler and the in-process
tool fence refusing a flagged record; and by the post-await re-filter and the drivers'
last-instant launch gates, which close the calls already in flight when the clamp lands.

What it does not cover: output of a turn already running at clamp time, content already
posted to the (group-readable) topic, and a pre-clamp nested child's own record — the
deliberate residuals listed above. The completion tool stays reachable while the rebuild is
pending, its output being in-flight-turn residual.

**INV-MEM-012**: A tier-classifier reply yields a tier only when the whole reply is exactly one tier token, or when its final non-empty line is exactly a `Tier:`-labeled token; a tier word in prose or ending an unlabeled multi-line reply yields no tier and the item falls to the private default.

Enforced in the reply parser (`parse_tier`): the whole-reply arm full-matches a single
decorated token rather than searching for the leftmost tier word (the search let a chatty
reply mis-tag a family fact as public); the final-line arm accepts only the labeled
answer line the prompt mandates. The id replaces retired MEM 007, whose narrower
statement the final-line arm falsifies.

What it does not cover: a classifier that *confidently declares the wrong tier* is
believed — a parser contract, not an accuracy guarantee; the eval set
owns accuracy.

## Failure behavior

**The backend is slow, unreachable, or returns an error.** The seam raises `RecallUnavailable`
carrying a reason slug that names the class of failure. There is no HTTP-level retry beyond a
single connection retry.

**The backend returns something malformed** — a bad envelope, a hit with unusable text or
tags, or nothing readable at the caller's clearance. The seam raises `RecallProtocolError`.
This is deliberately not an empty result.

**No backend is configured.** Recall raises with a reason naming that condition, the overlay
comes back empty, and **writes silently succeed without persisting anything**. The write side
fails quietly here while the read side does not.

**Auto-recall fails.** The turn absorbs it: no memory block is injected and the turn proceeds
without memory. Repeated failures open a per-agent breaker that skips the attempt entirely.
The model is not told that recall was skipped, so an agent cannot distinguish "no memory
matched" from "memory was not consulted" — which is precisely why an agent should not assert
absence from silence.

**A recall path fails repeatedly.** A circuit breaker fast-fails subsequent calls with a
dedicated reason rather than calling the backend. Genuine zero-hit results count as successes
and reset it; only unavailability counts as failure.

**Tier classification fails.** Retention classifies each item's sensitivity with a bounded
LLM pass; a failed or unparseable classification retries once and then assigns *private*,
with only a log warning to show for it. "Unparseable" is strict: only a bare tier word or
a final line of exactly `Tier: <word>` parses. A tier word inside a longer sentence
("this is not public; it is family") or ending an unlabeled multi-line reply is ambiguity,
not an answer, and falls to the private default. The write is not lost — but the fact
becomes invisible below the highest clearance, which reads as absence on voice and
friends surfaces.

**Saving a session fails.** The save is abandoned, its claim is released — including when the
failure is a cancellation at shutdown — and the entry stays for a later sweep to retry. An
explicit reset is the exception — it drops the pointer whether or not the save succeeded,
unless a newer session registered meanwhile (INV-MEM-006), in which case the newer
registration stands.

**A gap-superseded session's background retain fails.** That retain runs decoupled from the
registry (the new turn is about to overwrite the entry), so failure spools a durable retry
record instead; the freshness sweep drives retries and gives up loudly after a bounded
number of attempts. An unreadable record is dropped, not retried forever.

**The session registry file is corrupt at boot.** An unparseable file is renamed aside and
the process starts with an empty registry; a parseable file with structurally corrupt
individual entries quarantines just those entries and keeps the rest. Affected session
pointers are lost; the app comes up.

## Extension points

**A new backend** implements the seam's methods and must preserve the three outcomes —
in particular it must raise, not return empty, when it cannot answer or when nothing readable
survives filtering. If it holds resources, implement the close hook.

**A new channel** needs an explicit clearance entry, or it reads at the least-sensitive tier
by default. Write access is a separate, deliberate addition. If its sessions should be
retained when they go cold, that is a *third* list — none of the three is inferred from the
others, and forgetting one produces a channel that silently never persists.

**A new recall caller** should decide whether it wants its own telemetry and breaker, which
means choosing a distinct recall path rather than inheriting another's. It must also decide,
explicitly, what it does with unavailability — and if its prompt says anything like "no
prior results found", it must not collapse unavailable into silence.

**A new render surface** means extending the surface type and the provenance view together,
since what may be disclosed is decided per surface.

**A new writer** should build its items through the retain-item builder. Calling the seam's
retain directly bypasses tier tagging, provenance validation and the write-trust check at
once.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/semantic_memory.py::SemanticMemory`
- `casa/rootfs/opt/casa/semantic_memory.py::RecallUnavailable`
- `casa/rootfs/opt/casa/semantic_memory.py::RecallProtocolError`
- `casa/rootfs/opt/casa/semantic_memory.py::NoOpSemanticMemory`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.recall_items`
- `casa/rootfs/opt/casa/sensitivity.py::clearance_for_channel`
- `casa/rootfs/opt/casa/sensitivity.py::clearance_for_origin`
- `casa/rootfs/opt/casa/channel_policy.py::writes_to_bank`
- `casa/rootfs/opt/casa/memory_provenance.py::build_retain_items`
- `casa/rootfs/opt/casa/timekeeping.py::compose_time_envelope`
- `casa/rootfs/opt/casa/timekeeping.py::split_time_envelope`
- `casa/rootfs/opt/casa/recall_renderer.py::render_recall`
- `casa/rootfs/opt/casa/recall_health.py::observed_recall`
- `casa/rootfs/opt/casa/session_saver.py::save_session`
- `casa/rootfs/opt/casa/session_saver.py::reset_channel`
- `casa/rootfs/opt/casa/freshness_reaper.py::FreshnessReaper.sweep_once`

**Tests**
- `tests/test_recall_absence_invariant.py`
- `tests/test_recall_health.py::test_breaker_opens_after_threshold_failures`
- `tests/test_sensitivity.py::test_readable_tiers_is_clearance_and_below`
- `tests/test_sensitivity.py::test_clearance_docstring_states_the_fail_closed_direction`
- `tests/test_channel_policy.py`
- `tests/test_memory_provenance.py`
- `tests/test_agent_auto_recall_unavailable.py`
- `tests/test_session_saver.py`
- `tests/test_freshness_reaper.py`
- `tests/test_time_envelope.py`
- `tests/test_recall_empty_verdict.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
