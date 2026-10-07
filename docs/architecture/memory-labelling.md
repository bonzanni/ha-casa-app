---
last_reviewed: 2026-10-07
---

# Memory labelling: tier, provenance, and content addressing

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How each item is labelled on the way in to long-term memory: its sensitivity
tier, its speaker provenance, and the content addressing that deduplicates it.
How a conversation becomes long-term memory and how it stops being one — the
freshness windows, the claim-and-guard save protocol, the explicit reset and
the durable retry spool — is
[`architecture/memory-lifecycle.md`](memory-lifecycle.md). What happens to a
stored fact on the way back out is [`architecture/memory.md`](memory.md), and
who may read it back is [`architecture/memory-scoping.md`](memory-scoping.md).

## Mental model


Content addressing only holds because the hash input is the utterance and
nothing else. Every sent user turn carries a per-turn time envelope, and the
transcript echoes it back — hashed as-is, that second-precision timestamp
would mint a fresh document for the same sentence in every session. Retention
therefore splits the envelope off user turns at the transcript-readback
boundary: stored text and document id are both envelope-free, and the turn's
wall-clock time survives out-of-band as the retain item's timestamp. The
composer and splitter are a pinned pair, so the envelope's shape cannot drift
from what is stripped.

**A scheduled session's output is saved dated and marked; nothing stops being
saved.** Each session entry records whether a turn Casa's own schedule fired
registered it ([`architecture/persistent-state.md`](persistent-state.md)).
Once that marker is known, each model line is dated with the envelope time of
the user turn before it (the transcript carries no per-message time; a user
turn without an envelope clears it). When it says scheduled, every item —
the trigger or reminder prompt, a `[no answer to …]` continuation, every
model line, `<silent/>` — also carries the application tag `casa-scheduled`,
except the operator's tapped answer and a delegated result, which stay
ordinary memories: both are recognised by their producer's fixed text after
the envelope is split off, so a scheduled prompt written in either shape is
saved unmarked too. Each item is sent with its complete tag set — tier,
provenance, mark — because the backend REPLACES an identical document's tags
with its latest save's set while keeping its FIRST date: the mark records
where a text was last saved, and a scheduled line re-said verbatim in an
ordinary session loses it (and the reverse gains it). The tier alone is not
last-save-wins: it is floored by the tier already stored (below). An entry or spool
record written before the marker existed is saved exactly as before —
undated model lines, no mark — unless the turn superseding it on the same
key carries the scheduled marker, which proves the old session scheduled.
Event wakes run on the operator's own conversation and are never marked.

**Each item is labelled before it is stored, and the labels are not the
caller's to choose.** A bounded LLM pass classifies the item's sensitivity
tier, and its speaker provenance is recorded from what the turn actually
established. Both live in reserved tag namespaces that ordinary application
tags may not reach, so a caller cannot promote its own fact to a tier the
classifier did not give it or attribute it to someone it did not come from.
The item is a whole conversation turn, often a request or a question, so the
classifier is given it as quoted data between markers (tagged afresh for each
prompt, so the turn cannot spell the closing one), told not to answer or
act on it, and given the answer format after it
(`sensitivity.classification_prompt`). Sent bare, a turn was often answered
instead of classified.

**A save can raise a memory's tier and never lowers the tier it read.** The
classifier sees only the text, and the same text is classified again on
every save — a reset racing a sweep, a spool retry, the same line said in a new conversation — so
each save is a fresh draw. Before the items are built, the builder therefore
reads the current tags of each document whose verdict is not already a real
`private` from the bank it is about to write, and sends the stricter of the
stored tier and this save's verdict. Only the tier changes; provenance and
marks still follow the latest save, and the stored set is never merged in.
A `private` that came only from a classifier failure (blank input, a backend error after its retry, a reply still unparseable
after the re-ask) is not a verdict: over a stored tier it re-sends that tier
unchanged, and on a document that has none it is stored beside the
Casa-reserved marker `casa-tier-unverified`, which tells a later save the
`private` sets no floor — the next real verdict replaces it. Every stored tier
without that marker is a floor, including one saved before this rule existed
and whatever produced it. A cross-ask conflict — a re-ask less sensitive than
the first reply's own `private` evidence — counts as a real verdict. A real
`private` therefore stays `private` against every save that reads it — the
in-flight exception is INV-MEM-018's — and no operator action to lower a
stored tier exists.

## Contracts & invariants

**INV-MEM-004**: A caller cannot inject a sensitivity tier, a provenance tag or a Casa tier marker through ordinary application tags.

Enforced in the retain-item builder, which refuses reserved tag namespaces —
`casa-source-`, `casa-tier-`, and the tier names — before doing any
classification or I/O, for the batch's tags and every turn's own, and
validates the speaker provenance it is given.

What it does not cover, and this is worth stating plainly: it protects the
*write* path from its own callers. It does not authenticate what the backend
returns. On read, a syntactically valid provenance tag is trusted and is not
cross-checked against the duplicate copy stored in the item's metadata. A
recalled fact can therefore carry a speaker identity that the read path has
not independently established — see [`architecture/memory.md`](memory.md).

**INV-MEM-009**: The per-turn time envelope never reaches the content-addressed document id or the stored memory text — an identical utterance retained from any session collapses to one document — and the turn's wall-clock time is carried out-of-band on the retain item.

Enforced at the transcript-readback boundary, which splits a single leading
envelope off each user turn before the retain-item builder hashes or stores
it, and carries that time onto the model lines that follow it once the
session's scheduled marker is known. The envelope's composer and splitter are a pinned pair; a round-trip test
fails the moment the composed shape drifts from what the splitter recognises.

What it does not cover: documents retained before the split existed keep their
enveloped text and stale ids — the bank converges only as facts are re-said.
Writers that bypass the transcript readback (delegated retains) never carried
the envelope in the first place. Model lines saved from an entry that predates the
scheduled marker carry no time, so the backend dates them by the save.

**INV-MEM-012**: A tier-classifier reply yields a tier only when it is a single line holding one (possibly decorated) tier token, or when a multi-line reply's final non-empty line is the literal `Tier: <word>` answer line whose earlier tier-token or Tier-label lines all resolve to the same tier; prose tier words, conflicts, and unresolvable labels yield no tier; the item defaults to private.

Enforced in `parse_tier`: the single-line arm full-matches one decorated
token, never searching leftmost and never spanning lines; the answer-line arm
accepts only the literal final line the prompt mandates, and an earlier
answer-like line only when it resolves to the same tier — never "last one
wins". Replaces retired MEM 007, whose statement the answer-line arm
falsifies.

What it does not cover: a classifier confidently declaring the wrong tier is
believed — a parser contract, not accuracy; the eval set owns accuracy.

**INV-MEM-018**: A save never stores a memory at a less restrictive tier than the tier the memory server reported for that document when the save read it, unless that stored tier is a `private` carrying Casa's reserved provisional marker; the marker is written only beside a `private` that came from a classifier failure on a document storing no tier that counts as a floor, and a save whose read fails writes nothing.

Enforced in the retain-item builder, the one place every writer's items are
built: after classifying, it reads each document whose verdict is not already
a real `private` through the memory seam's stored-tag read, under the same
bound as classification and inside the writer's fence. The read is a required
argument, so a writer cannot leave it out. A stored value that is not a set of
tag strings is a failed read, never an empty one; several stored tiers count
as the strictest, and a document holding no tier at all sets no floor. A
failed classification is carried as a distinct value equal to `private`, so it
can be told from a real `private` without changing what any caller stores.

What it does not cover. Read and write are not one step, and saves are queued
on the server: two FIRST saves of the same text that both read "never saved"
each apply their own tier, and the last applied wins — the behaviour before
this rule; and a raise can be lost behind a concurrent save that read the
pre-raise tier, though neither goes below what both read. A reset racing a
sweep over one transcript, a cold retain beside its own spool retry, a
delegated request matching the same person's direct line, and one resident
line said in two conversations all produce such pairs. A `private` stored
before this rule — whatever produced it — cannot be told from a real one and
stays a floor; no operator action to lower a tier exists. The server's
replace-and-read semantics are pinned only through a fake of them.

## Failure behavior

**Tier classification fails.** Retention classifies each item with a bounded
LLM pass; a backend error retries once; an unparseable reply is re-asked once
with the format mandate restated, then falls to *private* with a log warning,
and the save logs one aggregate N-defaulted-of-M line. "Unparseable" is
strict: only a single-line (possibly decorated) tier token or a final
`Tier: <word>` line with agreeing earlier answers parses — anything else
(prose, conflicts) is ambiguity. The write is not lost, but the fact goes
invisible below the highest clearance — absence on voice and friends
surfaces. That `private` is provisional: over a tier already stored the save
re-sends the stored tier, and on a new document the provisional marker lets
the next real verdict replace it (INV-MEM-018).

**The stored-tier read fails.** Casa cannot tell what tier a memory already
has — the memory store is busy, restarting or unreachable, the read timed
out, or it answered anything but the document's tags or its positive "never
saved" — so the whole save is skipped and nothing is written with an
unchecked tier. A warning names the failure. Each writer's existing arm then
applies: a freshness sweep releases its claim and the next sweep retries, a
reset or gap-superseded session spools a durable retry, a spool retry counts
an attempt toward its limit, and a delegated or engagement memory is dropped —
it is not retried, as when the retain itself fails. The read can fail where the
write alone would have succeeded, so this skips a little more than before. A
memory server whose "never saved" answer differs from the one Casa recognises
fails every first save the same way, which is the safe direction.

## Extension points

**A new writer** is the retention lifecycle's extension point, listed in
[`architecture/memory-lifecycle.md`](memory-lifecycle.md): it builds its items
through the retain-item builder — bypassing it skips tier tagging, provenance
validation and the tier floor — and hands it the stored-tag read of the bank it
is about to write.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory_provenance.py::build_retain_items`
- `casa/rootfs/opt/casa/timekeeping.py::compose_time_envelope`
- `casa/rootfs/opt/casa/timekeeping.py::split_time_envelope`

**Tests**
- `tests/test_memory_provenance.py`
- `tests/test_time_envelope.py`
- `tests/test_pin_1123_tier_floor.py`

**Related**
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
