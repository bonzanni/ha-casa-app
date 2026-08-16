---
last_reviewed: 2026-08-10
---

# Memory and recall

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Long-term memory on the way *out*: who may read a stored fact back, how it is rendered, what
a caller is told when the store cannot answer, and what a caller may claim from what comes
back. It covers the memory seam, the sensitivity model as a read control, and the provenance
carried on a recalled fact. How a fact is written and labelled — tier classification, speaker
provenance, content addressing, and the retention lifecycle around them — is
[`architecture/memory-lifecycle.md`](memory-lifecycle.md). It does not cover the SDK session
transcript, which is short-term context on a different lifecycle, and it does not describe
the memory backend's own internals — those live outside this repository.

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

**And no result proves absence, at any clearance — empty or not.** The recall request carries
the caller's readable tiers as a server-side tag filter, so a hit above the caller's clearance
is dropped *by the backend* and a clearance-blocked search returns the same well-formed empty
as a genuine miss. Even at the highest clearance, server-side token truncation and the types
filter can hide content. The recall tool therefore never hands the model a bare empty: a
zero-hit result carries guidance that absence is unknowable and must not be asserted, and a
result whose readable hits exist but did not fit the render budget says so and asks for a
narrower query — the one existence claim an empty render *can* support, since those hits
already passed the clearance filter.

Emptiness, though, was never the property that needed saying. *Boundedness* is, and a large
useful result is exactly as bounded as an empty one: a model handed thirty readable memories
with nothing on the asked topic concludes "there is no record" just as confidently as one
handed none, and the filtering that removed the on-topic entry happened server-side, so
nothing in the result betrays it. Every non-empty slice injected into an agent's context
therefore carries one shared note saying so — use these entries normally, but they are the
view readable here, not an inventory of Casa's memory. The note is constant at every tier and
on every surface: a caveat that appeared only below `private`, or read differently there,
would itself be an oracle for "something was filtered out of *this* answer".

One slice stays deliberately unframed, and it is worth knowing why it is safe: the
engager-query synthesizer. It is a model, but it never speaks to a person — it may only
answer from the context it was handed or return `UNKNOWN`, and that `UNKNOWN` becomes the
already-framed unknown result. The framing is attached where a slice reaches an agent that
can *say something to someone*, which is where the false denial happens.

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

**Writing is narrower than reading, and it has its own document.** Only write-trusted
channels retain to the shared bank; *when* a conversation is retained, reset, or wiped is
the retention lifecycle, and so is everything about how a fact is *labelled* on the way in —
tier classification, provenance, and the content addressing that deduplicates it.
[`architecture/memory-lifecycle.md`](memory-lifecycle.md) owns the freshness windows, the
save guard protocol (INV-MEM-006), the write-side tag and provenance gate (INV-MEM-004), the
content-addressing contract (INV-MEM-009), the tier-classifier parse (INV-MEM-012), the
retirement claims (INV-MEM-013), and the operator-consented wipe (INV-MEM-014). What this
document owns is the other direction: what comes back, and what a caller may claim from it.

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
fact.** The model-facing consumers no longer do — `recall_memory` and `query_engager` scope
their empty results explicitly, and every consumer of a rendered slice frames its non-empty
one (INV-MEM-010) — but the invariant holds at the seam, for typed recall, not at every
consumer. A prompt-assembly caller that renders an empty digest as silence (the executor
archive slot) is making no claim, which is fine; a new consumer that words emptiness as
absence, or hands over a bounded slice unframed, would reintroduce the defect. That is a
standing risk of a per-consumer contract, so the consumer inventory is itself pinned: a new
`render_recall` or `delegated_recall` call site — or one more inside an existing caller —
fails the suite until it declares how it frames a non-empty result. Check the call site you
care about rather than assuming it propagates.

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

**INV-MEM-004**, **INV-MEM-005**, **INV-MEM-006**, **INV-MEM-009** and **INV-MEM-012** — the
write-side tag and provenance gate, write trust, the save/reset guard protocol, the
content-addressing contract and the tier-classifier parse — are declared in
[`architecture/memory-lifecycle.md`](memory-lifecycle.md), together with the retirement
claims (INV-MEM-013) and the wipe contract (INV-MEM-014).

One consequence of INV-MEM-004 belongs on the read side and is easy to miss: it protects the
*write* path from its own callers, and does not authenticate what the backend returns. On
read, a syntactically valid provenance tag is trusted and is not cross-checked against the
duplicate copy stored in the item's metadata, so a recalled fact can carry a speaker identity
the read path has not independently established.

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

**INV-MEM-010**: No recall result licenses a claim that Casa lacks something: every non-empty readable slice injected into the context of an agent that speaks to a person is framed as bounded, the sole unframed slice reaches only a synthesizer that can answer from it or return `UNKNOWN`, empty and unknown results carry explicit do-not-assert-absence guidance at every clearance tier, and readable hits that did not fit the render budget are reported as existing rather than absent.

Enforced in the recall tool's empty-digest arms and the engager-query tool's unknown arm,
which attach explicit guidance in place of a bare empty result; and, on the non-empty side, by
one shared note (`recall_renderer.READABLE_SLICE_NOTE`) attached by all four consumers of a
rendered slice — the recall tool's ok-arm as a result `message`, and auto-recall, the
specialist delegation and the executor lessons block as an instruction line inside the
injected block. The engager-query synthesizer is the exception described above.

What it does not cover: it is a statement about what the result *says*, not what a model does
with it — prompt-level honesty remains the model's job. Prompt-assembly consumers that render
emptiness as *silence* stay outside it, deliberately: absence of a block is not a claim of
absence, and a framing line with no memories under it would be a header for a search that
found nothing. The consumer inventory that guards against a fifth, unframed consumer resolves
direct calls only; an alias or other indirection escapes it.

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

**INV-MEM-015**: The lessons block injected into an executor's prompt at launch carries only summaries stamped with that launch's own procedural epoch; a summary is stamped at finalize with the epoch persisted on its engagement's record at create, never with the finalize-time definition.

The epoch is a digest over the procedural bytes *as read at launch* — the role-artifact
checksum, the resolved prompt template's path and just-read bytes, every doctrine file,
and (for workspace-driven executors) the *source* workspace instruction file,
pre-interpolation, whichever of the template or plain-copy forms will be consumed.
Launch-specific substitutions are never inputs: rendered bytes embed the task and the
recalled memory itself, which would make every epoch unique and the filter circular.
Workspace provisioning re-reads those sources, so after provisioning the driver recomputes
the digest and aborts the launch on a mismatch rather than stamp an engagement with an
epoch it did not run under. Filtering is client-side between recall and render (the tag
filter cannot narrow), drops foreign-epoch *and* unstamped hits alike — a lesson that
cannot prove it was learned under the current doctrine is exactly what the filter exists
for — and the injected block subordinates itself to the doctrine files in as many words.

What it does not cover: `query_engager` is deliberately unfiltered (an answer to "what
happened last time" is not injected procedure); a same-epoch lesson that is merely wrong
survives, tempered only by the subordination line; epochs never expire lessons — a
doctrine rollback to byte-identical state resurrects that epoch's lessons, by
construction; and the recheck reads the current sources, not the bytes provisioning
consumed, so an edit that lands *and reverts* inside the provisioning window can still
mislabel — accepted: it needs two opposing edits within milliseconds, and closing it
would thread an immutable byte snapshot through the whole provisioning path.

## Failure behavior

**The backend is slow, unreachable, or returns an error.** The seam raises `RecallUnavailable`
carrying a reason slug that names the class of failure. There is no HTTP-level retry beyond a
single connection retry.

**The backend returns something malformed** — a bad envelope, unusable hit
text/tags/metadata, or nothing readable at the caller's clearance. The seam raises `RecallProtocolError`.
This is deliberately not an empty result.

**No backend is configured.** Recall raises with a reason naming it, the overlay
comes back empty, and **writes silently succeed without persisting anything**. The write side
fails quietly here while the read side does not.

**Auto-recall fails.** The turn absorbs it: no memory block is injected and the turn proceeds
without memory. Repeated failures open a per-agent breaker that skips the attempt.
The model is not told that recall was skipped, so an agent cannot distinguish "no memory
matched" from "memory was not consulted" — so an agent should not assert absence from
silence.

**A recall path fails repeatedly.** A circuit breaker fast-fails subsequent calls with a
dedicated reason rather than calling the backend. Genuine zero-hit results count as successes
and reset it; only unavailability counts as failure.

Failures on the *write* side — a save that fails, a spooled retry, a corrupt session
registry, a wipe whose drain times out, a tier classification that cannot be parsed — are the
retention lifecycle's: [`architecture/memory-lifecycle.md`](memory-lifecycle.md). The last of
those has a read-side consequence worth knowing here: an unparseable classification defaults
the item to *private*, so the write is not lost but the fact goes invisible below the highest
clearance — absence on voice and friends surfaces.

## Extension points

**A new backend** implements the seam's methods and must preserve the three outcomes —
in particular it must raise, not return empty, when it cannot answer or when nothing readable
survives filtering. If it holds resources, implement the close hook.

**A new channel** needs an explicit clearance entry, or it reads at the least-sensitive tier
by default. Write access is a separate, deliberate addition, and the retention list is a
third ([`architecture/memory-lifecycle.md`](memory-lifecycle.md)).

**A new recall caller** should decide whether it wants its own telemetry and breaker, which
means choosing a distinct recall path rather than inheriting another's. It must also decide,
explicitly, what it does with unavailability — and if its prompt says anything like "no
prior results found", it must not collapse unavailable into silence.

**A new render surface** means extending the surface type and the provenance view together,
since what may be disclosed is decided per surface.

**A new writer** is the retention lifecycle's:
[`architecture/memory-lifecycle.md`](memory-lifecycle.md).

**Scoping what a recall surfaces** is a caller-side decision: the backend's only content
filter is the sensitivity tags, so a caller that must narrow further (the executor archive
drops lessons from another doctrine epoch — INV-MEM-015) filters the typed hits between
recall and render, never by adding tags to the request (an added tag *broadens* an
any-match filter).

**One archive recall per launch.** A recall is relevance-ranked and bounded, so two of them
are not two copies of one answer: an executor handed both is handed two versions of what
happened last time. A launch fetches once — into the first turn for an in-process executor,
into the workspace instructions for one with a workspace, never both.

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
- `casa/rootfs/opt/casa/recall_renderer.py::render_recall`
- `casa/rootfs/opt/casa/recall_health.py::observed_recall`
- `casa/rootfs/opt/casa/executor_epoch.py::compute_procedural_epoch`
- `casa/rootfs/opt/casa/executor_epoch.py::epoch_application_tag`
- `casa/rootfs/opt/casa/executor_epoch.py::make_archive_epoch_filter`

**Tests**
- `tests/test_recall_absence_invariant.py`
- `tests/test_recall_health.py::test_breaker_opens_after_threshold_failures`
- `tests/test_sensitivity.py::test_readable_tiers_is_clearance_and_below`
- `tests/test_sensitivity.py::test_clearance_docstring_states_the_fail_closed_direction`
- `tests/test_channel_policy.py`
- `tests/test_agent_auto_recall_unavailable.py`
- `tests/test_recall_empty_verdict.py`
- `tests/test_recall_readable_slice_framing.py`
- `tests/test_executor_epoch.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
<!-- END SOURCEMAP -->
