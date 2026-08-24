---
last_reviewed: 2026-08-19
---

# Engagement finalization

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a durable engagement ends: the single-winner terminal transition, the strictness that
keeps the persisted and in-memory records agreeing — at creation as well as at the
terminal flip, including the compensation a cancelled creator owes — the finalization side
effects behind the flip, and the ordering of what an engagement posts to its topic. What a
*successful* completion is refused over, and what each driver counts to answer that, are in
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md). The record
itself, how one is launched, how a turn is admitted to it, the driver protocol and what a
restart rewrites are in [`architecture/engagements.md`](engagements.md). The OS boundary a
`claude_code` engagement runs inside, and what its teardown does and does not establish
about the OS process, are in
[`architecture/engagement-containment.md`](engagement-containment.md).

## Mental model

**Ending an engagement is a race with exactly one winner, and that is the load-bearing
design.** The terminal transition is attempted against the registry; only the caller that
wins it performs the external effects — closing the topic, tearing down the driver, notifying
the resident. Everything else is a loser that does nothing. This is what stops a completion
racing a cancellation from producing two closures and two notifications.

**The transition is strict about persistence.** If writing the terminal state fails, the
in-memory record is restored and the call raises, so there is no state where the process
believes an engagement finished and the durable record disagrees. The caller is told to
retry.

**Completion is gated on unread input, and that gate is its own contract.** A *successful*
completion is refused while inbound messages are unread, in flight or reserved; failure and
cancellation deliberately bypass it. What each driver counts, why "unread" and "in flight" are
different questions, and how a message that dies with the engagement is disclosed are in
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md) (INV-ENG-003).

## Contracts & invariants

**INV-ENG-001**: A terminal transition has exactly one winner, and only the winner performs the finalization side effects.

Enforced by the registry's terminal transition, which refuses a missing or already-terminal
record and returns failure; the finalize path performs topic closure, driver teardown and
notification only on success. The winning transition also schedules the engagement's uid
quiesce, and the funnel waits for it — bounded — before any of those effects, so an
engagement's own processes stop before the operator is told it ended (INV-CONT-006, in
[`architecture/engagement-containment.md`](engagement-containment.md)).

The direct status mutators honour the same boundary: each re-checks for a prior terminal
state under the registry lock and declines to overwrite one — the idle sweep cannot flip a
concurrently-cancelled engagement back to resumable, and a failed resume that loses the
race to a cancel neither overwrites the status nor runs its duplicate topic cleanup (the
error mutator reports whether it won, and only a winner cleans up).

What it does not cover: exclusivity covers the *post-transition* side effects only: the pre-close
inbound spool drain runs before the win/lose transition, so a caller that goes on to lose the
race may already have flushed pending receipts and eviction notices externally; the drain is
idempotent, which is why running it ahead of the gate is tolerated.

**INV-ENG-010**: Once the terminal transition is won, the funnel's post-topic tail — driver teardown, the completion notification, retains and the deferred restart — survives cancellation of the task that ran the funnel, and its payloads carry the record values frozen at the flip.

An `in_casa` engagement's own `emit_completion` runs inside a child task of the very SDK
client the teardown closes, so the close cancels the funnel's host. For that caller the tail
is detached into a Casa-owned anchored task and awaited through a shield: the handler unwinds
cancelled — its tool result is not part of the guarantee and may be lost — while teardown,
notification, retains, the deferred restart (still strictly after the retains have landed)
and the finalized log complete. Every other caller runs the same tail inline, unchanged.
Payload inputs (origin, task, provenance) are snapshotted before the funnel's first
post-flip await, so a post-terminal record rewrite (clearance lowering) cannot alter what
the completion records say.

**Read the scope narrowly: "post-topic" excludes the topic.** The completion post, the
terminal mark and the close all run before the detach, on the caller's own task, and every
await after the commit is cancellable. A cancellation there commits the terminal record with
the topic left open and empty — recoverable, since nothing was closed and nothing was
marked, and the engager is still told from the surviving tail, but the topic carries no
account of what happened. This is a known open exposure, not an oversight: closing it means
moving operator-visible effects across a cancellation boundary in the one funnel that four
other terminal writers race, with no `in_casa` turn-admission fence to order them
(INV-ENG-009 is `claude_code` only).

**INV-ENG-013**: A terminal engagement's topic is never marked with an outcome the topic was never told. The completion post counts as delivered only when the wire acknowledged it; an acknowledgement that did not come withholds the outcome mark and produces exactly one bounded plain disclosure in its place. A post that failed part-way is never replayed. The topic is closed exactly once either way, and no topic operation can strand the post-topic tail behind it.

Until this release the completion post's failure was caught and logged, and the funnel then
marked the topic with its outcome emoji regardless. A record could therefore be durably
`completed` over a topic that received nothing, wearing a ✅. The mark is the defect, not the
missing summary: the missing summary is a delivery failure, while the mark is Casa asserting
that the delivery happened.

**Confirmation is the message id the sender returned, not the fact that the call returned.**
Both direct topic senders are typed to return an id and to raise on failure, so a non-`None`
return is a wire acknowledgement — which is all it claims, and never that a person read it.
The distinction matters because of the sequencer route: the sequencer's completion seam
reports a *definite* send failure by returning nothing, without raising, and the driver hook
in front of it used to discard that answer and report success. On that route the funnel
skipped its own direct send and painted and closed with no exception raised anywhere for
anyone to catch. A predicate built on "did the call raise?" is blind to it entirely.

A sequencer attempt that *raises* is treated differently from one that reports nothing sent.
A raise can come part-way through a paginated send, so some pages may already be in the
topic; replaying the whole summary through the direct sender would duplicate content and
then paint and close over the duplicate. A definite "nothing sent" is safe to follow with a
direct attempt; a raise is not, and gets only the disclosure.

The rule is uniform across outcomes. A cancellation emoji over a topic that never heard why
is the same false assertion as a completion emoji, so `completed`, `cancelled` and `error`
are all gated the same way.

The disclosure says the summary could not be *confirmed*, never that it was not posted: a
transport timeout can lose the acknowledgement of a message the wire accepted. It asserts
only what the funnel can see at that point — the outcome the transition committed, that the
post was not confirmed, and that the mark is being withheld — and nothing about the close
below it, the retention ledger, the engager notification or what the record holds, all of
which are best-effort or downstream. Three successive reviews found the same shape in this
prose, each time a sentence claiming state its own site could not see, so the class of claim
was removed rather than reworded a fourth time. **Whether a notice obeys that rule is a
review question, not a test one.** The suite pins each notice by whole-sentence equality, so
none of them can change silently; what no test can settle is whether a NEW sentence, shipped
with its expectation updated alongside it, is true. A blacklist of forbidden phrases was
tried and cut — it was bypassed three times by respelling the same false claims — so the
rule lives here and at the production sites, where the person making that edit reads it. It counts as a telling in its own right — if it lands, the topic has been told why the engagement ended,
which is what a follow-up turn's owner reads (INV-ENG-012) — and its confirmation is the
returned id, on the same rule as the summary's.

**The close is unconditional, and bounded.** Withholding the close as a second failure
signal was considered and rejected on review. It is not authoritative: the mark and the
close are independent best-effort operations, so a *confirmed* post whose mark and close both
fail leaves exactly the state a withheld close was meant to distinguish. And it is not free:
retention admission happens earlier in this funnel and is not gated on delivery, so the sweep
deletes the topic and every message in it on its deadline — while a terminal topic left open
still accepts messages, each answered with a refusal, so anything typed there is deleted with
it. The withheld MARK carries the whole signal, and the disclosure names the retention
deadline. The close is bounded like every other topic operation here, because it is the one
thing standing between an already-terminal record and its tail: a close that never returned
would strand the driver teardown, the notification and the retains, and with them any
follow-up turn waiting on the teardown to end its stream.

Three things this invariant does *not* do. It does not re-open a completed record because a
channel failed — the work did complete, and reverting the transition would restore tool
authority to an engagement already declared finished. It does not change the funnel's return
value: the transition won, and only the telling failed. And it does not shield, detach or
reorder anything, so INV-ENG-010's guarantee is untouched — which is precisely what makes
this arm separable from the still-open cancellation exposure described under INV-ENG-010.

**INV-ENG-002**: A strict terminal transition never leaves the persisted and in-memory records disagreeing; on a write failure it restores the prior state and raises.

Record *creation* holds the same strictness: a create whose tombstone write fails rolls the
in-memory insert back and raises, rather than handing the caller a running engagement whose
crash-recovery record never reached disk.

Creation also compensates for a *cancelled* creator: a caller cancelled after the persist
committed never receives the record, so the insert is rolled back and its removal persisted
before the cancellation propagates — no durable active record whose driver never started.

That compensation covers the record; the launch path compensates the rest of the window
around it, since a cancellation lands at whichever await is pending and ordinary
`except Exception` handlers never see it. Before the record exists, a cancellation closes the
already-opened topic; after it exists but before the driver is confirmed live, the
compensation also marks the record errored and runs the driver's own terminal teardown —
necessary because a driver can be *partly* live (the claude-code driver starts its supervised
service before its final awaits). The compensation is scheduled, not awaited (a cancelled
task cannot await network round-trips); its steps run in order in that background task.

What it does not cover: the other non-strict registry mutations (status touches, channel
state, counters) warn and continue if their write fails, so the no-disagreement guarantee
belongs to creation and the finalize path specifically. And the cancellation compensation is
itself best-effort on the disk side — if the compensating write fails, the on-disk ghost row
remains until the boot reconcile and reap TTL retire it.

**The completion gate is INV-ENG-003, and it lives in its own document.** A successful
completion is refused over unread, in-flight or reserved inbound input when the driver
exposes its inbound state — see
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md).

**INV-ENG-005**: Once the output sequencer is terminalized, ordinary narration and unresolved sends cannot post below the completion.

Enforced by the sequencer's terminalization and its writer checks, with a dedicated path
reserved for the completion notice itself.

What it does not cover: ordering depends on a bounded drain. If the drain times out, the
completion is posted anyway with a warning, and if no live sequencer exists the finalize path
falls back to a direct send that bypasses sequencing entirely.

**One terminal path deliberately stays outside this funnel.** A launch turn that ends
without leaving any artifact (INV-ENG-011, [`architecture/engagements.md`](engagements.md))
is reported by its launcher, not finalized here, and the exclusion is what makes it safe.
This funnel retains a tier-classified engagement summary onto the shared memory bank on
*every* outcome; a launch death recorded as a completion with empty text would put a
fabricated success into a store with no other copy. It also hard-codes a single completion
error kind, which would erase the specific kind the report exists to name, and it notifies
the engager over the bus, which the launcher's own error envelope already does. So that path
uses the strict terminal primitive directly, writes only `error`, and touches neither the bus
notification nor any retention. Nothing outside a completion or the operator's complete
command ever writes `completed`.

Two properties of the primitive make that direct use correct rather than a shortcut, and
both are the reason it is the primitive and not the best-effort marker. Strictness means the
in-memory flip and its persistence stand or fall together, so a caller that acts on the
returned win is acting on a record that reached disk — the best-effort marker returns a win
over a swallowed write failure, and an irreversible topic close taken on that answer would
sit over a record disk still calls live. And the single-winner contract means the caller
learns, in the same operation, whether the engagement had already reported itself: a lost
transition is the instruction to do nothing, because the winner owns every side effect.

## Failure behavior

**Completion is called with a bad status or arguments.** Rejected before any transition; the
engagement stays live and the caller sees a tool error.

**Completion is refused for unread input.** The transition is vetoed and the record stays
live; the outcome and its disclosure are INV-ENG-003's subject
([`architecture/engagement-completion-gate.md`](engagement-completion-gate.md)).

**The terminal write fails.** The record is rolled back to live and no side effects run.
Both the completion tool and the cancellation tool surface this as the same distinct
retryable outcome — the caller is told the record is still live and to call again, rather
than being handed a success for a transition that did not happen. Distinguishing the
retryable outcome from the precondition failure matters where it is surfaced: one says
"read your messages", the other says "try again".

**Two callers race.** The loser is absorbed as already-terminal. No duplicate topic closure
and no duplicate notification.

**Topic sends, driver teardown, notification and retention fail after the transition.** All
are caught and logged. **The terminal state stays committed** — so an engagement can be
genuinely finished while no completion message ever reached its topic and no notification
reached the resident. These are best-effort effects *after* the authoritative state change,
by design. What the topic then SAYS about it is INV-ENG-013's subject: the outcome mark is
withheld and a plain disclosure is attempted, so a closed topic with no outcome mark is the
signal that a terminal engagement's summary did not land. Read it as a prompt to check, not
as proof: the mark and the close are independent best-effort operations, so a confirmed
summary whose mark failed looks the same, and the returned message id — not the topic's
appearance — is the delivery fact.

**The driver teardown overruns.** It is bounded as a whole — the compile lock, both s6 stops
and the recompile — and a timeout is logged and stepped over, so the notification and the
retains behind it still run. The engagement's processes are already dead by then: the kill
happened at the transition, not here.

## Extension points

**A new terminal path** should go through the shared finalize funnel to inherit the
single-winner transition, teardown, notification and retention. Setting a terminal status
directly gets none of that.

**A new topic output** should go through the per-engagement sequencer if its ordering
relative to narration matters. Direct sends exist as a fallback and bypass ordering.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.create`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.try_transition_terminal`
- `casa/rootfs/opt/casa/tools.py::_finalize_engagement`
- `casa/rootfs/opt/casa/tools.py::_finalize_engagement_tail`
- `casa/rootfs/opt/casa/tools.py::FinalizeResult`
- `casa/rootfs/opt/casa/tools.py::cancel_engagement`
- `casa/rootfs/opt/casa/channels/output_sequencer.py::OutputSequencer`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver`

**Tests**
- `tests/test_emit_completion_tool.py`
- `tests/test_cancel_engagement_tool.py`
- `tests/test_engagement_registry.py`
- `tests/test_finalize_engagement.py`
- `tests/test_output_sequencer.py`
- `tests/test_anchor_narration_buffer.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/engagement-containment.md`](../architecture/engagement-containment.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
