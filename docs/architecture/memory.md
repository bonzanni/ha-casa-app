---
last_reviewed: 2026-08-10
---

# Memory and recall

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Long-term memory on the way *out*: how a recalled fact is rendered, what a caller is told
when the store cannot answer, and what a caller may claim from what comes back. It covers
the memory seam, the provenance carried on a recalled fact, and the mental-model overlay
together with the mental models Casa declares for it. *Who* may read a stored
fact back — read clearance per channel and sender, the engagement clearance clamp, and
the executor-archive epoch scoping — is
[`architecture/memory-scoping.md`](memory-scoping.md). How a fact is written and
labelled — tier classification, speaker provenance, content addressing, and the retention
lifecycle around them — is [`architecture/memory-lifecycle.md`](memory-lifecycle.md). It
does not cover the SDK session transcript, which is short-term context on a different
lifecycle, and it does not describe the memory backend's own internals — those live
outside this repository.

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

**A recalled fact says when it was recorded, and whether a scheduled turn last saved it —
two separate facts, and neither decides which memory is right.** Each rendered hit the backend
returned with a timezone-aware date carries a `[recorded <weekday> <day> <month> <year>]` line,
an absolute date in the operator's timezone (a rendered slice can sit in a resumed session's
prompt for days, so a relative age would go stale); a hit whose date is absent, unparseable or
naive renders without that line, and is not an error. The date is the backend's recorded time: the turn's own time for anything saved with
one — user turns, and the model's lines once their session's scheduled marker is known — and
otherwise the time the save ran, which is never earlier than when the text was said. For
identical text the backend keeps the FIRST date, so a fact re-asserted verbatim still shows
when it was first recorded, and a later date does not mean a later truth. A hit whose last
save came from a scheduled turn — a trigger or reminder prompt, the model's lines on such a
turn, its `<silent/>` — carries a second line, `[last saved by a scheduled turn]`. The backend
replaces an identical text's tags with its latest save's, so that line says where the text
was last saved, not where it was first said; it is never fused with the date into a "said by
… on …" claim. Hits stay in the backend's order, and a tight budget can keep an older or a
scheduled line and drop a newer contradicting one; what settles a question about current
state is a live read, which the assistant's doctrine requires before stating such state as
current. The recall request carries Casa's own clock in the operator's timezone as its
`query_timestamp`.

**A search can be limited to a period, and "only" is Casa's filter, not the backend's.** The
recall tool takes an optional period — today, yesterday, this or last week (weeks start on
Monday), this or last month, one day, or an inclusive range of days. Casa resolves it to whole
days in the operator's timezone, because not every session holding the tool sees the current
time. The backend's time window only *ranks* memories dated in it higher and still returns
the rest, so the window is sent as that ranking hint and the tool itself keeps only the hits
whose recorded date — the first date, for identical text — falls inside the period, after the
clearance re-filter; a hit with no usable date cannot be placed in a period and is left out.
So "from last week" means *first recorded* last week: a fact first said earlier and repeated
last week is not in it. The backend's token budget still truncates before Casa filters, so
in-period memories can be cut; the ranking hint is the only mitigation, and nothing promises
completeness. A search with a period that keeps nothing gets its own empty result, worded
like the others: not proof of absence. A period that cannot be read, or ends before it starts,
is refused before anything is searched — sent to the backend, an inverted window would come
back as an error the tool would report as "memory could not be checked". The seam's typed
recall takes the window only as that hint and never filters by it, so its three outcomes are
unchanged; auto-recall and delegated recall never send one.

**Auto-recall is not "every turn".** It happens when a turn's options are built, which is a
fresh non-voice session only — a warm reused client skips that path entirely, and voice never
auto-recalls. Both can still recall explicitly through the tool. "The agent remembers
automatically" is true of a narrower set of turns than it sounds.

**Writing is narrower than reading, and it has its own document.** Only write-trusted
channels retain to the shared bank; *when* a conversation is retained, reset, or wiped is
the retention lifecycle, and so is everything about how a fact is *labelled* on the way in —
tier classification, provenance, and the content addressing that deduplicates it.
[`architecture/memory-lifecycle.md`](memory-lifecycle.md) owns the freshness windows, the
save guard protocol (INV-MEM-006), the write-side tag and provenance gate (INV-MEM-004), the
content-addressing contract (INV-MEM-009), the tier-classifier parse (INV-MEM-012), the
retirement claims (INV-MEM-013), the tier floor (INV-MEM-018); the operator-consented wipe (INV-MEM-014) is
[`architecture/memory-wipe.md`](memory-wipe.md)'s. What this
document owns is the other direction: what comes back, and what a caller may claim from it.

**The seam has one read that is not a recall, and it fails the other way.** Before a save,
the retain-item builder asks the seam for one document's stored tags, to keep a save from
lowering its tier (INV-MEM-018). The answer is the tag set, or *never saved* — and only the
backend's positive "never saved" may mean that: on the Hindsight backend, a 404 whose detail
is exactly `Document not found`, which a missing bank also answers (correctly — a deleted
bank stores nothing, and the next save recreates it). Any other 404 — an unknown route answers
`Not Found` — any other status, a body without a list of string tags, a timeout or a transport
failure raises, and the save is skipped. Reading an unknown answer as "never saved" would let
a save lower a tier, which is why this read has no lenient arm and no default in the seam.
The route is present in the backend since v0.0.8; the behaviour above was verified on 0.10.2.

**Mental-model overlays cannot be tier-filtered at all**, because they are bank-wide
summaries rather than individually tagged facts. That is why they are exposed only at the
highest clearance — there is no way to redact part of one.

**The overlay is the backend's mental models, each dated, framed as leads.** A *mental
model* is a standing question the memory backend re-answers from the bank on its own
schedule. On a fresh session at `private` origin clearance, the turn reads the bank's models
(with their content — the backend's list returns metadata only unless asked) and pushes the
ones that have content into the prompt. Each renders under its name with `refreshed <weekday>
<day> <month> <year>` — the date of its last refresh in the operator's timezone, the same form
as a recalled fact's recorded date — and, when its automatic refreshes are paused because the
last one failed (the backend's own rule: a failure later than the last success, or a failure
with no success at all), says that it may be out of date. A model with no content yet (its
first refresh still running) or no readable refresh date is left out: the overlay never shows a
summary whose age it cannot state. A non-empty overlay opens with one line framing it as
memory-derived leads: not live state, and not a complete list, so nothing missing from it is
evidence of absence. A missing bank (after a wipe, before the first save) is simply no overlay;
any other failure to read it is logged and the turn runs without it.

**Casa declares two of those models and keeps them reconciled.** `casa-open-commitments` asks
for the operator's open commitments and follow-ups with their stated dates and deadlines,
leaving out the assistant's own reminders and briefings; it refreshes on the backend's daily
schedule at 05:00 UTC. `casa-operator-profile` asks for a profile of the operator and their
household — who they are, the people around them, how they like to be addressed, standing
preferences and routines with their cadence, each item with the date it was last stated —
leaving out health, money and other people's private matters; it refreshes after the backend
consolidates new memories, at most once a day. Both are untagged, so each reads the whole
bank, including the other model and any model the operator created. The exclusions are
written into the question, not enforced: they shape what the backend writes and filter
nothing. A reconcile pass makes the backend match the declarations: it creates a missing
model, rewrites a definition whose declared fields drifted (fields Casa does not declare are
left as they are), and deletes any model under the **reserved `casa-` prefix** that Casa no
longer declares — so a model the operator creates under that prefix is deleted at the next
pass, while every other id is never touched. The pass reads one page of up to 1,000 models,
and when the backend reports more than it returned, it deletes nothing. A model whose
definition was rewritten, or whose automatic refreshes are paused, gets one explicit refresh in
that pass, because rewriting does not refresh and only an explicit refresh resumes a paused
model. A pass runs in the background
at boot and after every completed wipe, one at a time in arrival order; boot and the wipe never
wait for it, and shutdown cancels it before the memory client closes. It is best-effort: when
the backend is unreachable it logs and changes nothing, and the next boot or wipe tries again.

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

**INV-MEM-004**, **INV-MEM-005**, **INV-MEM-006**, **INV-MEM-009** and **INV-MEM-012** — the
write-side tag and provenance gate, write trust, the save/reset guard protocol, the
content-addressing contract and the tier-classifier parse — are declared in
[`architecture/memory-lifecycle.md`](memory-lifecycle.md), together with the retirement
claims (INV-MEM-013) and the tier floor (INV-MEM-018); the wipe contract (INV-MEM-014) is declared in
[`architecture/memory-wipe.md`](memory-wipe.md).

One consequence of INV-MEM-004 belongs on the read side and is easy to miss: it protects the
*write* path from its own callers, and does not authenticate what the backend returns. On
read, a syntactically valid provenance tag is trusted and is not cross-checked against the
duplicate copy stored in the item's metadata, so a recalled fact can carry a speaker identity
the read path has not independently established.

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

**INV-MEM-019**: A `recall_memory` call given a period returns only readable hits whose recorded date lies inside the period Casa resolved in the operator's timezone — hits without a recorded date are left out — the period filter runs after the clearance gate, an empty in-period result is framed as bounded and never as absence, an invalid or inverted period is refused before any request, and a call without a period sends no window.

Enforced in the recall tool, which resolves the period (`timekeeping.resolve_period`), passes the
window to the seam only when one was given, filters on each hit's recorded date after the
clearance re-filter, and keys every result arm on what that filter keeps; the backend
implementation adds the window to its request only when one is passed.

What it does not cover: completeness. The backend ranks by the window and truncates to its
token budget before Casa filters, so a memory first recorded in the period can still be
missing from the result. A memory's date is the backend's first recorded date for identical
text, not when the event it describes happened.

**INV-MEM-020**: The mental-model overlay is pushed only on a fresh session at `private` origin clearance, and every model it renders carries its name and the date of its last refresh in the operator's timezone.

Enforced by the turn's load plan, which pushes the overlay on a fresh session only, behind the
`private`-only overlay gate; by the backend's overlay read, which asks the list for content;
and by the overlay render, which emits a model only together with its name and its local
refresh date and leaves out a model whose refresh date it cannot read.

What it does not cover: what the summaries *say*. They are written by the backend's own model
from the whole bank; their dates are the last refresh, not when any fact in them was stated,
and the declared exclusions are part of the question rather than a filter. Nor does it cover
the declared models' reconcile, which is best-effort by design and pinned by ordinary tests:
a pass that cannot reach the backend leaves the models as they were until the next boot or
wipe.

## Failure behavior

**The backend is slow, unreachable, or returns an error.** The seam raises `RecallUnavailable`
carrying a reason slug that names the class of failure. There is no HTTP-level retry beyond a
single connection retry — except one: a recall the backend answers 503 ("busy") is retried
once, after its `Retry-After` capped at one second, and a second 503 raises as any other
failure does. No other status is retried, and a write never is.

**The backend returns something malformed** — a bad envelope, unusable hit
text/tags/metadata, or nothing readable at the caller's clearance. The seam raises `RecallProtocolError`.
This is deliberately not an empty result.

**No backend is configured.** Recall raises with a reason naming it, the overlay
comes back empty, no mental model is reconciled, and **writes silently succeed without
persisting anything**. The write side fails quietly here while the read side does not.

**A reconcile pass fails.** If the backend's model list cannot be read — unreachable,
timing out, a server error, an unrecognisable answer — the pass writes nothing and logs it.
A missing bank is not a failure: its list answers 404 and the pass creates both models, which
recreates the bank. One model's create, rewrite, refresh or delete failing is logged and the
pass moves on; a rewrite that failed is not followed by a refresh. A refresh whose connection
drops is not resent — the pass cannot tell whether the backend accepted it. Nothing retries
inside the process: if the backend comes up after Casa, the declared models wait for the next
boot or wipe.

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
clearance — absence on voice and friends surfaces — until a later save's real verdict replaces
it, or for good where an earlier save had already stored a tier, which that save keeps. A
*real* `private`, by contrast, now stays: a save that reads it never stores a lower tier
(INV-MEM-018 states the in-flight exception), so a fact stored with a real `private` stays
invisible below the highest clearance however it is later re-said.

## Extension points

**A new backend** implements the seam's methods and must preserve the three outcomes —
in particular it must raise, not return empty, when it cannot answer or when nothing readable
survives filtering. Its stored-tag read must likewise answer "never saved" only when the backend
positively says so, and raise on everything else; the seam declares it abstract so a backend
cannot inherit a lenient one. If it holds resources, implement the close hook. Its mental-model
reconcile has a do-nothing default: a backend without mental models has nothing to reconcile.

**A new declared mental model** is one more entry in the declarations, under the reserved
`casa-` prefix. Its first reconcile creates it; removing the entry later deletes it from the
backend at the next pass.

**A new recall caller** should decide whether it wants its own telemetry and breaker, which
means choosing a distinct recall path rather than inheriting another's. It must also decide,
explicitly, what it does with unavailability — and if its prompt says anything like "no
prior results found", it must not collapse unavailable into silence.

**A new render surface** means extending the surface type and the provenance view together,
since what may be disclosed is decided per surface.

**A new writer** is the retention lifecycle's:
[`architecture/memory-lifecycle.md`](memory-lifecycle.md).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/semantic_memory.py::SemanticMemory`
- `casa/rootfs/opt/casa/semantic_memory.py::RecallUnavailable`
- `casa/rootfs/opt/casa/semantic_memory.py::RecallProtocolError`
- `casa/rootfs/opt/casa/semantic_memory.py::NoOpSemanticMemory`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.recall_items`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.document_tags`
- `casa/rootfs/opt/casa/recall_renderer.py::render_recall`
- `casa/rootfs/opt/casa/recall_health.py::observed_recall`
- `casa/rootfs/opt/casa/delegated_memory.py::delegated_recall`
- `casa/rootfs/opt/casa/timekeeping.py::resolve_period`
- `casa/rootfs/opt/casa/semantic_memory.py::render_mental_models`
- `casa/rootfs/opt/casa/semantic_memory.py::mental_model_refresh_paused`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.profile`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.reconcile_mental_models`
- `casa/rootfs/opt/casa/mental_models.py::reconcile`
- `casa/rootfs/opt/casa/mental_models.py::schedule_reconcile`
- `casa/rootfs/opt/casa/mental_models.py::drain_reconcile_tasks`

**Tests**
- `tests/test_recall_absence_invariant.py`
- `tests/test_recall_health.py::test_breaker_opens_after_threshold_failures`
- `tests/test_agent_auto_recall_unavailable.py`
- `tests/test_recall_empty_verdict.py`
- `tests/test_recall_readable_slice_framing.py`
- `tests/test_1123_tier_floor_regressions.py`
- `tests/test_pin_1120_recall_period.py`
- `tests/test_mental_models_1126.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/memory-scoping.md`](../architecture/memory-scoping.md)
- [`architecture/memory-wipe.md`](../architecture/memory-wipe.md)
<!-- END SOURCEMAP -->
