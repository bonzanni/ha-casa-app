---
last_reviewed: 2026-08-19
---

# Engagement finalization

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a durable engagement ends: the single-winner terminal transition, the strictness that
keeps the persisted and in-memory records agreeing — at creation as well as at the
terminal flip, including the compensation a cancelled creator owes — the completion gate
that refuses a success over input nobody read, the finalization side effects behind the
flip, and the ordering of what an engagement posts to its topic. The record itself, how
one is launched, how a turn is admitted to it, the driver protocol and what a restart
rewrites are in [`architecture/engagements.md`](engagements.md). The OS boundary a
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

**Completion is gated on unread input.** A *successful* completion is refused while inbound
messages are unread or reserved — an agent cannot declare victory over a question it has not
read. Failure and cancellation deliberately bypass that gate, because something going wrong
must always be able to end.

**"Unread" and "in flight" are different questions, and the gate needs both.** A message that
has left the queue but not yet reached a turn — written into the engagement's stdin FIFO,
with no turn-start evidence back — is invisible to the unread accounting, deliberately: that
accounting also answers the ask gate's question, *is the operator still waiting to be
answered?*, and a delivered message may well have been read. The terminal question is a
different one, *is there operator input in flight that will only be taken up after I commit?*,
and the same exclusion is wrong for it. So the driver answers both separately rather than
changing what "unread" means. The completion gate refuses on either, and the annotation
described below discloses both. There is no third state to track for a message being written
right now: it is still queued throughout its own write, so the unread accounting already
holds it.

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

**INV-ENG-003**: A successful completion is refused while unread inbound messages, inbound messages in flight to the engagement's CLI, or inbound reservations exist, when the driver exposes its inbound state.

Enforced both as a pre-check and again as a hook inside the transition itself, so the
condition is re-evaluated at the moment the state changes rather than only before it. The
hook is where the invariant actually lives: delivery can happen during the finalize path's
own awaits, long after the pre-check found the spool clean.

The in-flight half of the refusal is bounded in time, and that asymmetry is deliberate. A
delivered message normally reaches a turn within a second; one that has not after far longer
is evidence that no turn is coming, and a veto on a state nothing will clear would make a
successful completion impossible for the rest of the engagement's life — the opposite of this
hook's fail-open contract. Past the bound the completion proceeds and the message is still
disclosed. Each expiry is logged, so the bound is answerable to production rather than
permanently a guess.

What it does not cover: failed and cancelled outcomes intentionally skip the gate, and so
does the operator's own complete command — only the completion *tool* arms the gate, so an
operator marking an engagement complete finalizes past unread input deliberately. The gate
exists where the driver implements the inbound accessors — today that is both drivers. The
claude-code driver counts its durable spool, its in-flight envelopes and its ingress
reservations. The in-casa driver counts admission tickets: a turn is unread from its
synchronous admission at the Telegram entry seam until the embedded client takes the prompt,
then disclosure-only until the first model-evidence frame; the ticket ledger is in-memory
and dies with the process, and it has no in-flight veto, no reservations and no
forced-boundary valve — the refusal ending the turn releases the per-turn lock, which is
what delivers the queued turn, and a ticket whose delivery fails is discharged only after
one bounded failure notice, never retried into a permanent veto. Accessor failures fail
open with a warning rather than wedging termination. The claude-code-only
escalation that forces a turn boundary after repeated refusals stays scoped to the queued
population: a queued message cannot move until a respawn re-arms delivery, which is what the
escalation forces, while an in-flight one is already past that boundary and killing its epoch
could destroy a message a turn had just consumed.

**A message that dies with the engagement is disclosed, not swallowed.** Every terminal
outcome posts the messages no turn ever took up into the topic — both populations, at any
age, excerpted and counted. The claim it makes is what the system can evidence: that no turn
start was *recorded* for them before the engagement ended. It does not claim they were never
read, because that is not provable for a message already handed to the CLI — the CLI can read
the line and emit its init frame before the relay processes it, and a cancellation landing in
that interval would otherwise assert something false about a message the agent did see.

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

**Completion is refused for unread input.** The transition is vetoed, the record stays live,
and the caller gets a retryable outcome naming the condition. This is a precondition failure,
not an error state.

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
by design.

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
- `casa/rootfs/opt/casa/engagement_registry.py::TerminalPreconditionFailed`
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
- `tests/test_output_sequencer.py`
- `tests/test_anchor_narration_buffer.py`
- `tests/test_answer_reservation.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-containment.md`](../architecture/engagement-containment.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
