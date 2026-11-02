---
last_reviewed: 2026-11-02
---

# Memory capture: what is kept as it happens, and what is kept out

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a conversation's words become durable memory at the moment they happen — Casa's
per-turn capture into the memory store, with its spool fallback — and which text is
eligible at all: write-trust, completeness, and the specialists whose output is kept out.
Capture is the only source of memory. How each captured item is identified and given its
tier floor is [`architecture/memory-labelling.md`](memory-labelling.md); the session
lifecycle — freshness windows, the reset, transcript retention — is
[`architecture/memory-lifecycle.md`](memory-lifecycle.md); the store itself and the registry
of everything that holds memory are [`architecture/memory-store.md`](memory-store.md).

## Mental model

**Capture happens where the words happen.** Each seam appends what it sees, once, and
awaits the append, so every capture a turn made has returned by the time the turn ends:

- **A resident turn's opening**, in the admitted block of `Agent._process`, before the model
  is called. The text is stored raw: the time envelope and Casa's notes are composed into
  the prompt separately, so neither reaches the event (INV-MEM-022). That covers an
  operator's DM and every other turn on a write-trusted channel — scheduled, reminder and
  trigger turns, and Casa's own turns such as a delegation's completion notice. Once an
  operator's message is durable, its occurrence key goes on the turn's origin, where
  `correct_memory` finds the message to mark ([`memory-tools.md`](memory-tools.md)).
- **Each text-bearing message the resident writes**, in the fold that streams it, before it
  is emitted. Every attempt of the turn folds its own messages, so a retry's output and a
  regenerated answer are captured as well; an API fault's prose is never folded.
- **A delegated run's request**, raw (the task, not the assembled prompt), when its prompt is
  assembled and before its client starts; and **its answer**, the whole live fold, when the
  run completes.
- **An engagement turn of a memory-writing specialist**: the operator's message in the
  topic, as it was admitted (never the preamble Casa prepends after a context rebuild), and
  each message the specialist writes, before it is streamed and whatever the stream's
  output bound clips. The operator's message is captured before the delivery fence, whose
  window must not await. Executors and plugin jobs are not memory-writing specialists.
- **A finalized engagement's summary**, and an executor's per-type summary, into the store's
  `engagement_summaries`, awaited by the finalize tail, under the specialist's own memory
  switch (below).

**A receipt means durable, and the receipt is all a turn waits for.**
`memory.capture.append` makes one item durable and returns a receipt: committed in one
short store transaction, or — when the store is unavailable or held past a 50 ms bound —
written to `/data/memory/spool.jsonl` and fsynced. That write is the only part of a capture
a seam awaits. Everything else is detached and never awaited by a turn: the first append
that commits while a spool waits starts a drain (one at a time); an old spool's notice is
sent under its own bound.

**A drain takes the spool whole, then stores it outside the lock.** Spool appends and the
drain share one lock, and the drain holds it for one step only: renaming `spool.jsonl` to
`spool.draining.jsonl` and fsyncing the directory. A line appended before the rename is in
that snapshot; a line appended after it starts a fresh spool, so a capture that has to spool
never waits for a drain. The drain then stores the immutable snapshot in batches, leaving
the store free between them for the captures of running turns, and removes it only once
every line is committed. At most one snapshot exists: a drain finishes a leftover one before
it takes the spool again. There is no model call on this path and no comparison of text:
the same words said twice are two events.

**Writing is narrower than reading.** Only write-trusted channels capture. A channel that
can read memory is not thereby able to store.

**A turn that did not finish is not a fact.** Write-trust decides *whether* a channel may
store; it does not decide whether what is being stored actually happened. A delegated
specialist run that the CLI ends without completing the turn has produced text, but not an
answer, and the store has no way to say "partial". The exclusion is therefore made where
the terminal verdict is known: the incomplete answer is never captured.

The rest of that exchange is admitted turn by turn. The caller's request is captured before
the run whenever it is non-blank — the caller did genuinely make it — regardless of whether
the specialist produces any answer text. The answer is captured only when it is non-empty and
the terminal evidence says the model actually finished it; a withheld answer is logged where
the run's evidence is read, and a blank request beside a withheld answer captures nothing.

## Contracts & invariants

**INV-MEM-005**: Only write-trusted channels capture into memory.

Enforced by the channel write-policy check (`channel_policy.writes_to_bank`), read by every
capture seam: a turn, a delegation or a summary whose channel is not write-trusted captures
nothing (`memory.eligibility.admit`).

What it does not cover: `memory.capture.record` itself enforces nothing. A future seam that
skips `admit` would bypass it.

**INV-MEM-016**: A delegated specialist answer the terminal evidence does not certify complete — an aborted run, a terminal fault, or an answer the model did not finish — is never captured; the caller's own non-blank task is captured before the run, independently of the answer.

The answer is captured only when the output's own `answer_incomplete` is false.

A stored-call pinned turn ([`stored-call-buttons.md`](stored-call-buttons.md)) reads
nothing — its prompt carries no memory block — and captures nothing at all —
neither the Casa-composed task nor the model's text — since its only operator-visible
outcome is the plugin's receipt; the read and the write are skipped on the same
`stored_call` marker. The runner records the whole terminal shape once, beside the returned
text, and captures the answer only when a single completeness predicate on that
evidence says the model finished. The predicate is deliberately wider than the run's own
reported verdict, in three ways. A result carrying an error flag while still naming itself
a success is a failed API call, and a result naming any reason for stopping other than a
completed one — cancellation among them — is a turn that ended some other way; both now end
the run as a typed caller failure, raised before the answer is captured, so only the
request of those exchanges is kept. An otherwise completed result whose *stop reason* says
the model ran out of output tokens — or says anything else not on the short list of reasons
that mean *finished* — is a caller-visible success whose unfinished answer is withheld from
memory, audibly. Both vocabularies belong to the CLI and can grow, so each is tested
against an allow-list of completed values, never a list of known-bad ones: a reason this
release has never heard of withholds the answer rather than keeping it. A value that is not
even a string is normalized at capture to a sentinel that fails every such list — malformed
evidence never reads as the absent-field legacy case, which counts as completed. The
reported verdict itself is left alone — it has consumers outside memory, and widening it
there would change what they see. "Did not complete" covers every non-success terminal
subtype — including one this release has never seen — and a stream that ends with no
terminal message at all, which certifies nothing. The subtype is a *required* field on a
parsed terminal message, so a null there is malformed evidence and reads as an abort too;
the completed-without-subtype reading survives only for directly constructed outputs, which
is where the legacy and test shapes live.

The caller's request is kept deliberately: it is a true utterance. The two are independent
events rather than one paired record, so withholding the answer leaves nothing dangling.
Admission is per turn, and a whitespace-only request is blank.

What it does not cover: the rule for the delegation's *delivery* copy: a job row keeps its
answer only while that answer is owed to its creator, and drops it on delivery
(INV-JOB-015). One rule, two purposes — memory keeps what happened, the row keeps what has
not yet been said.

**INV-MEM-032**: No row of the memory store's `events` or `engagement_summaries` holds output authored by a specialist that writes no memory — a loaded specialist whose `memory.token_budget` is 0, or a role no longer loaded — whether it would arrive as its delegated answer, Casa's notice carrying it, a hand-off or relay task it wrote, its engagement topic turns or its engagement summary; the resident's own words relaying it are captured.

The switch is the specialist's own declaration: a role that reads no memory writes none,
and a role Casa no longer has loaded — whose switch it cannot read — writes none either
(`delegated_memory.role_writes_memory`). It is read at the capture seams through
`memory.eligibility.admit`, with `delegated_memory.notice_from_excluded_role` for Casa's
notice header: the opening of a resident turn that is Casa's notice naming such
a role is not captured, while the resident's reply in that turn is; a delegated run of such
a role captures neither side; a request is captured only with the chain's resident shape;
an engagement of such a specialist captures no topic turn and leaves no summary, and the
requester is still told. A delegated run's task is captured only as a resident's own words:
a chain of at most the resident and one specialist (`len(_handoff_chain) <= 2`) on a turn
Casa did not compose (`synthetic != "continuation"`).

What it does not cover: what the resident chooses to say about the result, which is the
resident's; a specialist that writes memory quoting the excluded specialist's answer in its
own reply, which is that specialist's own output; and a Casa whose tools were never
initialised (an empty role map), where nothing is gated. It is the capture half of the rule; what reads the store later (ledgers, search, export, restore) is held to it by
the mechanisms that read.

## Failure behavior

**The store is unavailable, or busy past the capture's bound.** The item is spooled and
fsynced, with a receipt, and the turn goes on; a locked store costs a turn at most the
50 ms bound per captured item, and never waits for a drain that is running. The next append
that commits starts a detached drain, and the memory writer drains a waiting spool on each of
its passes once the store is available, which covers a store that only came back after boot
([`memory-writer.md`](memory-writer.md)); `correct_memory` asks for the same detached drain
when the message it would mark is still spooled ([`memory-tools.md`](memory-tools.md)). The
drain inserts every line with an `INSERT` that
ignores a `source_key` already stored, so a drain cut off at any point and run again adds
nothing twice.

**The spool cannot be written either.** No receipt; the failure is logged at ERROR with the
item's key, and the turn goes on without that item in memory.

**The spool grows old.** When a spooled append finds the spool's oldest line more than an
hour old, the operator gets one private notice, sent detached so no turn waits on its
delivery; it is sent once per spool, and a send that fails or exceeds its own bound is
logged and retried at the next spooled append.

**A drain fails, or the process dies during one.** Whatever the drain had taken stays in
`spool.draining.jsonl` (a drain waits on the store no longer than a capture does, so one
blocked by another writer gives up at its next batch). The next drain, started by the next
healthy append, stores that snapshot first — its committed batches are ignored — and only
then takes the spool. Both files are registered memory: the export drains them, the wipe
deletes them. A line a
crash tore part-way is skipped by the drain — it was never fsynced, so it never had a
receipt — and the next spool write starts on a fresh line so it is never glued to one.

**A seam's capture raises.** It is logged and swallowed: capture never fails a turn, a
delegation or a finalize. A cancellation is the caller's own and propagates.

## Extension points

**A new capture seam** builds its item with `memory.capture.event` (or `summary`), decides
it with `memory.eligibility.admit` from the predicates Casa already has, keys it only
through `memory.occurrence`, and awaits `memory.capture.record` at the seam — before
anything relies on the words being remembered.

**A new eligibility fact** is a mechanical fact the seam computes (a channel, a marker, a
role's switch, a chain's shape, a terminal verdict); what the text means is never decided
in code.

**A new file under `/data/memory`** is a registry row, and the store pin runs the producer
that writes it ([`memory-store.md`](memory-store.md)).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory/capture.py`
- `casa/rootfs/opt/casa/memory/eligibility.py`
- `casa/rootfs/opt/casa/agent.py::_TurnCapture`
- `casa/rootfs/opt/casa/tools.py::_run_delegated_agent`
- `casa/rootfs/opt/casa/tools.py::_capture_delegated`
- `casa/rootfs/opt/casa/tools.py::_capture_summary`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::_EngagementCapture`
- `casa/rootfs/opt/casa/channel_policy.py::writes_to_bank`
- `casa/rootfs/opt/casa/delegated_memory.py::role_writes_memory`
- `casa/rootfs/opt/casa/delegated_memory.py::notice_from_excluded_role`

**Tests**
- `tests/test_memory_capture.py`
- `tests/test_memory_eligibility.py`
- `tests/test_memory_excluded_specialist.py`
- `tests/test_no_retain_specialist.py`
- `tests/test_channel_policy.py`
- `tests/test_specialist_memory_tiers.py`

**Related**
- [`architecture/memory-store.md`](../architecture/memory-store.md)
- [`architecture/memory-labelling.md`](../architecture/memory-labelling.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/memory-scoping.md`](../architecture/memory-scoping.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
<!-- END SOURCEMAP -->
