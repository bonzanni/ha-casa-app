---
last_reviewed: 2026-08-10
---

# Memory and recall

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Long-term memory on the way *out*: how a recalled fact is rendered, what a caller is told
when the store cannot answer, and what a caller may claim from what comes back. It covers
the memory seam and the provenance carried on a recalled fact. *Who* may read a stored
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
- `casa/rootfs/opt/casa/recall_renderer.py::render_recall`
- `casa/rootfs/opt/casa/recall_health.py::observed_recall`
- `casa/rootfs/opt/casa/delegated_memory.py::delegated_recall`

**Tests**
- `tests/test_recall_absence_invariant.py`
- `tests/test_recall_health.py::test_breaker_opens_after_threshold_failures`
- `tests/test_agent_auto_recall_unavailable.py`
- `tests/test_recall_empty_verdict.py`
- `tests/test_recall_readable_slice_framing.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/memory-scoping.md`](../architecture/memory-scoping.md)
<!-- END SOURCEMAP -->
