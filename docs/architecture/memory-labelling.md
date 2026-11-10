---
last_reviewed: 2026-11-08
---

# Memory labelling: occurrence, floor and tier

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How each item captured into the memory store is labelled: the occurrence identity that
names it, the tier floor it carries from the moment it is captured, what of a sent query
reaches it (the operator's words, never Casa's notes or the time envelope), and how a
tier-classifier reply is parsed. Which text is captured, and when, is
[`architecture/memory-capture.md`](memory-capture.md); when the writer classifies a captured
event and what a fallback does is [`architecture/memory-writer.md`](memory-writer.md); who may
read it back is [`architecture/memory-scoping.md`](memory-scoping.md).

## Mental model

**A captured item is named by what occurred, never by what it says.** Every captured item's
`source_key` comes from one constructor, `memory/occurrence.py`, in one of three forms. An
operator message that carries a provider id — a Telegram DM, or the operator's reply into a
job — is keyed by a hash of its channel, its chat and that id, so the same message
delivered twice is one event. Everything else — a turn no person typed, each resident or
specialist message, a delegated request and answer — is keyed by a random id minted once,
when capture builds the item, and carried on its spool line. The same words said twice are
two events; a retried attempt's output and a regenerated answer are new
events; a spool drain run twice adds nothing. No key counts anything, and the short log
correlation id is never an input, since two turns can share one. The third form names Casa's
line that a specialist which keeps no memory posted to the operator's chat
([`memory-capture.md`](memory-capture.md)) by the post's own identity — its owner, tool call
and slot, with the channel and chat — so every page of one post, and the same post landing
again, is one event.

**A captured item starts `private` and provisional, with a floor.** Capture does not classify.
Every event is stored `private`, `provisional`, with an `origin_tier` below which it can never
be classified: for the opening of a turn, the clearance of the origin that sent it; for a
resident's or a specialist's own message, the read clearance of the session that wrote it —
the clearance its served memory and its tools were bounded by. A message the operator
sent himself is the exception (the Telegram text or caption stamped as his words, typed,
forwarded or picked from an inline bot; not a button tap, which carries Casa's option text): it
carries no floor and is classified like any text, so what he says in his private chat can reach
a lower ledger while the resident's reply to it stays `private`. A
friend's Telegram turn and the resident's answer to it carry the friend's clearance as their
floor; a delegated run's request and answer carry the delegating turn's. A turn Casa's own schedule fired is recorded `scheduled`.

**What is captured is the turn's own text, never the query Casa composed.** Every sent user
turn carries a per-turn time envelope, and a turn that carries Casa's notes — a Telegram
resident turn's drained front-desk lines
([`architecture/specialist-desk-reply.md`](specialist-desk-reply.md)) and a reply's note
([`architecture/specialist-desk.md`](specialist-desk.md)) — carries them in one `<casa_notes>`
block directly after the envelope (`compose_turn_preamble`; with no notes the preamble is
exactly the envelope). Capture records the operator's text as it arrived, so neither the
envelope nor a note reaches an event. The composer and the splitter
(`split_time_envelope`) remain a pinned pair: the splitter recognises the block only directly
after the envelope, a block the speaker typed anywhere else is their text, and a note's own
close tag is broken with a zero-width space when composed, so a quoted `</casa_notes>` cannot
end the block early.

**The classifier is given the item as data.** The writer classifies each provisional event
with Casa's tier classifier, a bounded LLM pass. The item is a whole conversation turn, often a
request or a question, so the classifier is given it as quoted data between markers (tagged
afresh for each prompt, so the turn cannot spell the closing one), told not to answer or act
on it, and given the answer format after it (`sensitivity.classification_prompt`). Sent bare, a
turn was often answered instead of classified.

## Contracts & invariants

**INV-MEM-026**: Every `source_key` of an item captured into the memory store is built by `memory/occurrence.py` from no positional counter and never from the log correlation id: an operator message with a provider id is keyed by its channel, chat and provider message id, so a redelivery collapses into one event; Casa's line for a specialist's post by its channel, chat and the post's owner, tool call and slot, so its pages and a re-landing are one event; and every other live item by an id minted once when capture builds it, so a spool drain re-run adds it once while a repeat, a retry's output or a regenerated answer is a new event.

The verbatim store keeps every occurrence. The form for a message handed to a job uses the
engagement as its chat (`engagement_key`), since the driver that captures it knows the
engagement; the channel's seam for a specialist that keeps no memory builds the same key, so
one such message is one event whichever seam captures it.

What it does not cover: the same words reaching two seams — an operator's message, and the
resident quoting it back — are two items by design; and no key is meant to match anything in
an SDK transcript.

**INV-MEM-022**: Casa's per-turn notes block, composed directly after the time envelope, never reaches a captured item: a turn captures the operator's own text, and the splitter strips the block with the envelope only where the composer puts it.

Pinned at the capture boundary by a turn whose sent query carries a front-desk line and a
reply note and whose captured operator event is the operator's words alone, and by the
composer–splitter pair: the block's close is recognised only as the composer writes it,
followed by a blank line or by whitespace to the end. What it does not cover: a block the
operator typed after a turn with no notes is their text, by design.

**INV-MEM-012**: A tier-classifier reply yields a tier only when it is a single line holding one (possibly decorated) tier token, or when a multi-line reply's final non-empty line is the literal `Tier: <word>` answer line whose earlier tier-token or Tier-label lines all resolve to the same tier; prose tier words, conflicts, and unresolvable labels yield no tier; the item defaults to private.

Enforced in `parse_tier`: the single-line arm full-matches one decorated token, never
searching leftmost and never spanning lines; the answer-line arm accepts only the literal
final line the prompt mandates, and an earlier answer-like line only when it resolves to the
same tier — never "last one wins". Replaces retired MEM 007, whose statement the answer-line
arm falsifies.

What it does not cover: a classifier confidently declaring the wrong tier is believed — a
parser contract, not accuracy; the eval set owns accuracy.

## Failure behavior

**Tier classification fails.** A backend error retries once; an unparseable reply is re-asked
once with the format mandate restated, then falls back. "Unparseable" is strict: only a
single-line (possibly decorated) tier token or a final `Tier: <word>` line with agreeing
earlier answers parses — anything else (prose, conflicts) is ambiguity. A fallback leaves the
event provisional: still `private`, and pending in no lower ledger
([`architecture/memory-writer.md`](memory-writer.md)).

## Extension points

**A new capture seam** keys its items only through `memory/occurrence.py` and gives each the
floor of the session that produced it ([`architecture/memory-capture.md`](memory-capture.md)).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/timekeeping.py::compose_time_envelope`
- `casa/rootfs/opt/casa/timekeeping.py::split_time_envelope`
- `casa/rootfs/opt/casa/timekeeping.py::compose_turn_preamble`
- `casa/rootfs/opt/casa/memory/occurrence.py`

**Tests**
- `tests/test_time_envelope.py`
- `tests/test_memory_capture.py`

**Related**
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/memory-capture.md`](../architecture/memory-capture.md)
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
