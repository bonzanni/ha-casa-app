---
last_reviewed: 2026-08-14
---

# The tool interface

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The mutation interface Casa exposes to its agents: how the tool surface is assembled, what
a tool result promises, how engagement mutations sequence their side effects, and which
failures roll back versus which merely report. It covers families and contracts, not a
per-tool catalog — the registry tuple is the authority on what exists. Dispatch and
authorization live in `architecture/mcp-and-tools.md`; this file is about what the tools
themselves guarantee. What the plugin and specialist mutation tools guarantee — how a
mutation orders its registry commit against the convergence that follows, what its envelope
may claim about a plugin's integration, and what a committed removal discloses — is in
[`plugin-mutation-tools.md`](plugin-mutation-tools.md).

## Mental model

**One registry is the whole surface.** A single module-level tuple of handlers drives both
the SDK server an agent sees and the bridge's dispatch map. Adding a tool there exposes it
on both transports; grant filtering is a separate, later cut against fully-qualified names —
with one coarse escape: a *server-level* `mcp__casa-framework` grant exposes every tool at
once, and only its absence makes filtering per-tool. The SDK server also loads eagerly
(`alwaysLoad`), so every added tool's schema lands in every SDK session's up-front context
— adding a tool is a prompt-cost decision, not just a dispatch one.

**A result has two layers, and the outer one is inferred.** A tool returns a payload
serialized into one text content block; the wrapper marks the outer result as an error only
when the payload says `status: "error"` or `ok: false`. Statuses like *unavailable*,
*pending* and *acknowledged* deliberately ride as successes — they are outcomes, not
failures. And the wrapper is a convention, not a law: at least one tool returns raw
envelopes without it, so "every error becomes `is_error`" is not a property of the surface.

**Argument-schema validation depends on the transport.** The SDK path registers the
decorated tools with their schemas, and MCP validation there rejects a missing required
argument or a bad enum before the handler runs — code and tests rely on it. The internal
bridge route checks only that the name is known and passes the arguments through, so a
tool reachable both ways must carry its own validation for the bridge side.

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
structure, so a malformed `.json` is delivered as the text file it is.

**A lifecycle surface is only finished when it can also undo.** Personas install, apply and
publish through consent-bound tools; removing one, sweeping the versions nothing refers to,
enumerating what is installed and revoking a stored approval are tools too — not filesystem
work, because the tree they live in is denied to the configurator precisely so that the
sequencing stays owned by code. The removal tools refuse rather than force, and a refusal
names its referrers; the sweep reports what it kept and why, so "some were skipped" is never
the whole story an operator gets. What each refusal means, and why removal is decided by
references rather than by use, belongs to [`architecture/personality.md`](personality.md).

**Engagement mutation is a funnel, not parallel paths.** Completion and cancellation
converge on one finalize path whose strict registry transition picks a single winner
(INV-ENG-001); everything observable — permits, brokers, topics, notifications, retention —
happens after, best-effort, and is not transactional.

## Contracts & invariants

**INV-TOOL-001**: The result wrapper marks an outer error only for a payload with status "error" or ok false; every other status is a successful outcome.

Enforced in the wrapper itself, which serializes the payload into exactly one content
block.

What it does not cover: tools that do not call the wrapper. A handler returning raw
envelopes carries its own error semantics.

**INV-TOOL-002**: Internal tool calls bind engagement authority only for an active record; completion alone may bind a terminal record, so a duplicate completion gets its truthful already-terminal answer.

Enforced by the internal handler's binding check against an explicit terminal-binding
allowlist containing exactly the completion tool.

What it does not cover: it does not authorize any other tool after termination, and it is
a binding rule, not an argument or schema check.

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

What it does not cover: interactive specialist engagements, which run under the in-casa
driver and its own finalize funnel, and the voice arms, whose abort handling predates this
rule and is unchanged. Nor does it cover a creator-cancelled row, per the paragraph above.
It also says nothing about *retries of the run itself* — nothing retries a delegated run;
the kind is diagnostic, not routing.

**INV-TOOL-008**: `ask_user`'s operator-DM arm and `wipe_memory` report `awaiting_user` only for a request still present in the broker's live map at the moment the tool returns; a request already absent is reported with a non-error settled status that names no outcome the read cannot support.

Enforced by one synchronous sample of that live map, taken after the arm's last await and
before it returns, in the same no-await block as the delivery check — on the plain arm the
same boolean also drives the scheduled-question displacement, so the status and the guard
cannot disagree. The delivery marker is not the oracle and never was: the broker writes it
after the post await onto the request object the tool still holds, and retirement copies
the metadata rather than clearing it.

What it does not cover, deliberately: it does not claim that a request reported
`awaiting_user` will be answered. Retirement in the window between that final sample and
the caller reading the payload cannot be closed by any read, and a status that claimed
otherwise would be a worse lie than the one this rule removes. It also does not cover the
scheduled arm, which has no synchronous oracle that separates a retired question from one
shutdown deliberately preserved for the boot reconcile; that arm's terminal outcomes reach
its session through its finish hook instead.

## Failure behavior

**A malformed request.** Invalid JSON, a missing or unknown name, a non-object request,
and any non-object `params` or `arguments` — truthy or falsy alike — all come back as
typed JSON-RPC-style error objects from the route, before any tool runs (an absent or
null value defaults to an empty object instead); a tool that raises becomes an error
object rather than a transport failure.

**Ill-typed arguments, though, reach the tool.** The route validates the request
*envelope*, not the argument types a tool declares: the internal forwarding path does not
enforce a tool's declared schema, so a handler receives whatever the caller sent. A
declared `bool` can therefore arrive as a string, and Python's truthiness makes the string
`"false"` indistinguishable from `True` to a `bool(...)` coercion. Any argument that acts
as an authorization — a `force` that permits a destructive path, a flag that waives a
refusal — must be type-checked in the handler and rejected as a bad request, not coerced.
Treating the declaration as if something upstream enforced it is how a refusal becomes
opt-out by typo.

**A completion is invalid or refused.** Bad arguments, the plugin-developer release guard,
and the unread-inbound veto all leave the engagement active; a duplicate completion is
acknowledged as already terminal; a failed strict persist reports retryable.

**An `in_casa` completion's own acknowledgement can be lost.** A winning in_casa
`emit_completion` tears down the client hosting the tool call itself, and the control
transport can close before the tool result is written; the durable finalization side
effects still complete, detached (INV-ENG-010 in
[`engagement-finalization.md`](engagement-finalization.md)).
The pre-terminal refusals above all precede the teardown and always return their results.

**A launch's first turn ends without leaving an artifact.** The engagement-launch tools
answer `pending` when the driver's `start` returns, and that answer used to be given even
when the first turn had been cut off mid-flight — the record then sat active with nothing
posted. The `in_casa` launch branches now ask the driver what the turn left behind and, when
it left nothing, hand the death to one owner that records a distinct `launch_turn_incomplete`
error kind, tells the operator in the topic before closing it, and reports the failure in the
envelope. "Left nothing" includes text that never arrived: a launch turn whose streamed text
was wholly refused by Telegram (the stream handle's finalize established `not delivered` —
INV-TG-006 in [`telegram.md`](telegram.md)) is a topic showing nothing, and takes the same
owner; an ambiguous delivery records nothing, since the text may be on screen. Only that
outcome changes: a launch whose turn ran to its end still answers
`pending`, and so does one that lost the terminal race to its own completion, because the
engagement really did report itself. The kind is deliberately its own rather than the generic
driver-start failure — a reader who cannot tell "the driver never got going" from "the turn
ran and then died" cannot act on either. See INV-ENG-011 in
[`engagements.md`](engagements.md).

**A delivery is uncertain.** The send classifiers separate definitive refusal from
uncertainty, and an uncertain Telegram send is deliberately not retried — a duplicate
message is worse than a missing one that the operator can see is missing.

**A question cannot be delivered.** `ask_user`'s operator-DM arm registers its request
before it posts the keyboard, so a delivery failure is discovered when the request already
exists: the broker unregisters it and the tool answers with a typed delivery failure rather
than a request id nobody can answer. Two consequences a caller can rely on. The arm retires a
live *human* question in that DM at registration, in the same indivisible step as its own —
a replacement can never race a tap. It displaces a waiting *machine-timed* question only
after its own keyboard is confirmed on screen and still live, so a question that never
arrived, or that was retired while its post was in flight, costs the operator nothing; the
lane rule itself is in [`jobs-and-delivery.md`](jobs-and-delivery.md).

**A question is over before the tool returns.** The same window that costs the operator
nothing used to cost the *agent* something: a question the broker had already retired —
by a `/new`, by a typed answer, by a replacement question, by its own timeout, by an
operator tap, by shutdown — still came back as an outstanding request id, while the
keyboard on screen already read expired. It now comes back as settled, with no reply
pending on that id. Settled is a successful outcome, not an error: every one of those
retirements is a benign operator or system action, and recording them as tool failures
would misreport the common case to fix the rare one. The status names no outcome, because
the single read that produced it cannot tell a cancellation from an answer — which is also
why the consent keyboard `wipe_memory` posts reports it the same way and neither claims
nor denies that anything was deleted (an Approve tapped inside that window may already
have started the wipe, whose result is reported in the keyboard message).

**A specialist context exhausts its media-send budget.** `send_media` in specialist
context — a specialist engagement, or a delegated turn keyed by its server-stamped
delegation id — carries a code-owned lifetime budget per context, debited synchronously
before the delivery await so parallel calls cannot overrun it. Attempts count; failed and
uncertain deliveries are not refunded (a refund path would let delivery errors mint
budget). Over budget is its own typed refusal telling the agent to reference remaining
artifacts in its result text; a specialist-classified call with *no* quota key is refused
rather than passed unmetered. Residents and executor engagements are unmetered. The
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
a success. Memory admission is per turn: the caller's own non-blank request turn, which
nothing else records, is submitted regardless of the answer; the answer itself needs the
completeness predicate, including a stop reason on the finished list — an answer the
model did not finish (output tokens exhausted among the reasons) stays a caller-visible
success whose text is withheld from the bank, audibly (INV-MEM-016, and see
[`memory-lifecycle.md`](memory-lifecycle.md); the ledger and reconciliation rules are in
[`jobs-and-delivery.md`](jobs-and-delivery.md)).

**Memory cannot answer.** The recall tools report unavailability as its own status and
refuse blank queries outright; neither is ever a fake empty result (INV-MEM-001's
tool-level face).

## Extension points

**A new tool** is a decorated handler added to the registry tuple — that alone puts it on
both transports, which is why the addition is also a security decision; grant filtering and
the coverage ledger pick it up from there.

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
- `casa/rootfs/opt/casa/tools.py::create_casa_tools`
- `casa/rootfs/opt/casa/tools.py::_result`
- `casa/rootfs/opt/casa/tools.py::select_casa_tools`
- `casa/rootfs/opt/casa/tools.py::emit_completion`
- `casa/rootfs/opt/casa/internal_handlers.py::_make_internal_tools_call_handler`
- `casa/rootfs/opt/casa/mcp_envelope.py::_tool_schema`

**Tests**
- `tests/test_internal_handlers.py`
- `tests/test_emit_completion_tool.py`

**Related**
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
<!-- END SOURCEMAP -->
