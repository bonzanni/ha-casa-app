---
last_reviewed: 2026-11-24
---

# Tool outcomes: results, sends and completion

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a call to one of Casa's tools reports and how it ends: the two-layer result every
wrapped tool returns, what the sending tools — `send_message`, `send_media`, `ask_user` and
`set_reminder` — admit and report, and how engagement completion, a job launch and a
delegated specialist run end, including which failures roll back versus which merely
report. How the tool surface is assembled and gated, and the configurator's own tools, are
in [`tools-interface.md`](tools-interface.md); what `ask_user`'s operator-DM arm and
`wipe_memory`'s consent keyboard report about their question is in
[`tool-questions.md`](tool-questions.md); what a plugin mutation tool's envelope may claim is
in [`plugin-mutation-tools.md`](plugin-mutation-tools.md).

## Mental model

**A result has two layers, and the outer one is inferred.** A tool returns a payload
serialized into one text content block; the wrapper marks the outer result as an error only
when the payload says `status: "error"` or `ok: false`. Statuses like *unavailable*,
*pending* and *acknowledged* deliberately ride as successes — they are outcomes, not
failures. And the wrapper is a convention, not a law: at least one tool returns raw
envelopes without it, so "every error becomes `is_error`" is not a property of the surface.

**One table is the whole media surface, and a kind is not a file type.** The
`send_media` capability derives everything type-specific — the schema's `kind` enum, the
argument check, the content gate, the delivered-filename extension allowlist, the size cap
and the send-method dispatch — from a single policy table, so a new delivery kind is one
entry rather than a change spread across three modules. Two consequences are easy to get
backwards. First, several *kinds* can share one *send method*: PDF, zip and text all leave
as Telegram documents, so the kind is what was gated, never the method — and the kind named
`document` therefore means PDF specifically, a name kept only because it is public API.
Widening it into a catch-all would dissolve the magic gate it exists to be. Second, the
gate's two halves do different work, and for some kinds it is the extension list, not the
content predicate, that confines the kind: a zip signature is equally an OOXML document, a
`.jar` or an `.apk`, so `.zip`-only is the actual boundary. Each predicate is total over
content — every admitted byte string yields a verdict rather than an exception — but
totality is a claim about content, not about the machine: an allocation failure is left to
propagate as an internal error rather than being reported as a verdict about the bytes,
because "these bytes are not valid" is a claim the evidence would not support. A kind may
also validate less than its extensions suggest; the text kind checks encoding, never
structure, so a malformed `.json` is delivered as the text file it is. The table is the
*outbound* surface: files the operator sends in are checked against a separate, smaller
extension table that borrows only these content predicates
([`inbound-files.md`](inbound-files.md)).

**A tool that emits or stores model text admits it first, and says what it added.**
`send_message`, `ask_user`, `send_media`'s caption and `set_reminder` resolve the scope this
call commits under — a bound engagement's, else the turn scope on the handler's entry
snapshot — and refuse when none is bound (`send_message` with an error result, the others
with `unsupported_origin`), never defaulting to plain text. `send_message`'s result then
names the exact line — `Message sent via telegram. Casa prefixed: “…”`; `ask_user` admits
the body before its plain and scheduled arms branch, so the keyboard, the stored record and
every settle edit carry the same text, and its `awaiting_user` payload gains `casa_prefixed`
when a line was added; `send_media`'s length cap applies over the admitted whole, and a
caption the channel refuses reports `kind_error: refused`, never a down channel;
`set_reminder` stores the resolved note as the entry's `output_note` with the prompt
unchanged and reports it as `note`; `list_inbound_files` arms the obligation from the files
it lists; `delegate_to_agent` resolves the brief's note at every launch and reports it as
`casa_note` on either pending result ([`delegation.md`](delegation.md)); and
`wipe_memory`'s consent keyboard is Casa's own text. The rules are INV-OUT-002 to
INV-OUT-005 in [`output-boundary.md`](output-boundary.md). Each call of `send_message`,
`send_media` or `ask_user` is also recorded as a send attempt on that scope, refusals
included, by a wrapper outside the registered handler that changes no result (#1075).

**Engagement mutation is a funnel, not parallel paths.** Completion and cancellation
converge on one finalize path whose strict registry transition picks a single winner
(INV-ENG-001); everything observable — permits, brokers, notifications, retention —
happens after, best-effort, and is not transactional.

## Contracts & invariants

**INV-TOOL-001**: The result wrapper marks an outer error only for a payload with status "error" or ok false; every other status is a successful outcome.

Enforced in the wrapper itself, which serializes the payload into exactly one content
block.

What it does not cover: tools that do not call the wrapper. A handler returning raw
envelopes carries its own error semantics.

**INV-TOOL-006**: A delegated run the CLI aborted reaches every non-voice caller-facing consumer as a failure — the durable row ends failed with the mapped abort kind, the tool result and completion notification carry an error status with no partial text, a success write is unreachable, and a failed abort write retries only a failure transition carrying the original typed failure. Completion is never keyed on empty answer text.

Enforced in both non-voice terminal implementations: the synchronous arm tests the recorded
abort verdict before output truncation and before the success write, and the shared
completion callback (async mode and the degraded synchronous wait use the same one) reads
the whole terminal verdict rather than the accumulated text. Both persist the explicit
typed failure through the same compatibility route the voice arms already used, and the
failure-reconciliation retry accepts that failure so a write that dies cannot resurface as
the generic persistence fallback — or, worse, as a success.

The completion callback now settles the durable terminal BEFORE it enqueues the
notification, rather than spawning the two independently, and it asks the settled row one
question first: a row the creator had already cancelled ends CANCELLED — as it always did
— and is no longer announced as a completion, because a creator who withdrew the question
is not told it was answered. The notice it does enqueue carries a delivery
acknowledgement, so the obligation to announce it outlives a restart (INV-JOB-010).

What it does not cover: specialist engagements (a specialist's jobs), which run under the in-casa
driver and its own finalize funnel, and the voice arms, whose abort handling predates this
rule and is unchanged. Nor does it cover a creator-cancelled row, per the paragraph above.
It also says nothing about *retries of the run itself* — nothing retries a delegated run;
the kind is diagnostic, not routing.

## Failure behavior

**A completion is invalid or refused.** Bad arguments and the unread-inbound veto both leave
the engagement active; a duplicate completion is acknowledged as already terminal; a failed
strict persist reports retryable.

**An `in_casa` completion's own acknowledgement can be lost.** A winning in_casa
`emit_completion` tears down the client hosting the tool call itself, and the control
transport can close before the tool result is written; the durable finalization side
effects still complete, detached (INV-ENG-010 in
[`engagement-finalization.md`](engagement-finalization.md)).
The pre-terminal refusals above all precede the teardown and always return their results.

**A launch ends before it answers.** The job-launch tools answer `pending` once the job's
session is open and its brief is queued; the job's first turn runs after that, in the
session's reader (INV-ENG-021, [`engagement-launch-detach.md`](engagement-launch-detach.md)).
A launch that fails before that answer with a named fault returns that fault's kind to the
calling turn; one cancelled after its record exists records `launch_cancelled` and tells the engager over the bus, since no envelope carries it (INV-ENG-030, same document). From the answer on, nothing is reported to the
launching call: a first turn that ends with nothing visible is a turn end the job's
turn-end handler nudges or parks, and a session that ends is resumed once
([`background-job-session.md`](background-job-session.md)).

**A delivery is uncertain.** The send classifiers separate definitive refusal from
uncertainty, and an uncertain Telegram send is deliberately not retried — a duplicate
message is worse than a missing one that the operator can see is missing.

**A specialist context exhausts its media-send budget.** `send_media` in specialist
context — a specialist engagement, or a delegated turn keyed by its server-stamped
delegation id — carries a code-owned lifetime budget per context, debited synchronously
before the delivery await so parallel calls cannot overrun it. Attempts count; failed and
uncertain deliveries are not refunded (a refund path would let delivery errors mint
budget). Over budget is its own typed refusal telling the agent to reference remaining
artifacts in its result text; a specialist-classified call with *no* quota key is refused
rather than passed unmetered. Residents are unmetered, and the configurator holds no `send_media`. The
counters are in-memory: a restart refills them — this is a quota, not an authorization.

**A delegated specialist run ends without completing its turn.** The runner records the
whole terminal shape — subtype, error flag, API status, terminal reason, stop reason — at
one capture point, normalizing a malformed value — a non-string anywhere, and null in a
required field such as the subtype or the error flag — to fail-closed evidence rather
than to the absent-field legacy case, and every consumer reads that one verdict. The CLI's own terminal aborts — a turn limit, a spend ceiling, a
result-contract failure, an execution error, an unrecognised future verdict, or a stream
that ends with no terminal message at all — reach every caller the same way: the sync
result and the completion notification say `status="error"` with the mapped specialist
abort kind and carry no partial text, and the durable job row ends failed with that same
kind, checked before the success write so a background completion retry can never repaint
an abort as success. A terminal result that reports an API-level fault while still calling
itself a success — the error flag, or a reason for stopping other than a completed one —
ends the run as a typed failure raised before the memory write, classified from the
API status (429/529 as the transient overload pair, other 5xx as an SDK fault, anything
else failing closed), and the kind persisted on the durable row is the kind the caller
was told, never an exception class name. A specialist abort keeps precedence: an error
flag beside a non-success subtype does not flatten the abort taxonomy. None of these
verdicts is ever keyed on empty answer text — a completed run with an empty answer stays
a success. Memory admission is per turn: the caller's own non-blank request is captured
before the run, regardless of the answer; the answer itself needs the completeness
predicate, including a stop reason on the finished list — an answer the model did not
finish (output tokens exhausted among the reasons) stays a caller-visible success whose
text is withheld from memory, audibly (INV-MEM-016, and see
[`memory-capture.md`](memory-capture.md); the ledger and reconciliation rules are in
[`jobs-and-delivery.md`](jobs-and-delivery.md)).

## Extension points

**A new terminal side effect** for engagements goes after the winner is decided in the
finalize path, and must tolerate running on a record whose other side effects partially
failed.

**A new delivery medium** touches the media policies, filename validation and the send
classifier together — the classifier is where refusal-versus-uncertainty is decided, and
that distinction is the contract.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tool_result.py::_result`
- `casa/rootfs/opt/casa/tools.py::emit_completion`

**Tests**
- `tests/test_internal_handlers.py`
- `tests/test_emit_completion_tool.py`

**Related**
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/tool-questions.md`](../architecture/tool-questions.md)
<!-- END SOURCEMAP -->
