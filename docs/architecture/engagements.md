---
last_reviewed: 2026-08-10
---

# Engagements

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Durable engagements: their records, how they end, and what survives a restart. How agents
address and launch one another — the delegation ACL, the depth cap, the agent-spawn cap —
lives in [`architecture/delegation.md`](delegation.md). It does not cover the turn loop
itself, nor what a driver's underlying runtime does once started.

## Mental model

**A delegated turn and an engagement are different things.** Delegation in its ordinary form
is a task handed to a specialist that runs and returns — ephemeral. An engagement is a
durable record with its own topic, which outlives the call that created it.

Three launch paths exist and they are not symmetrical. Ordinary specialist delegation runs
ephemerally. *Interactive* specialist delegation creates an engagement. Engaging an executor
always creates one. Both engagement-creating paths pass the agent-spawn gate first
(INV-ENG-008).

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

**Much less survives a restart than the word "durable" suggests.** The record persists;
concurrency permits, live drivers, output sequencers, inbound reservations and various
in-flight maps do not. A record found `active` at startup is rewritten to `idle`, because no
live driver survived to make `active` true.

**A turn being handed to the CLI is what makes a `claude_code` record live again.** An
operator turn that was queued but never consumed is redelivered to the respawned CLI after a
restart, and that delivery — not the restart, and not the respawn — is what returns the
record to `active`. The distinction is load-bearing rather than bookkeeping: the tool
authority an engagement holds over the internal dispatch path is bound to a record that is
`active` (INV-MCP-001), so a turn delivered against an idled record would run stripped of
every non-terminal tool the engagement owns, and the refusal would blame its grant. The same
decision refuses the delivery outright once the record is terminal. It covers the first byte
only: a terminal transition landing after a turn has begun cannot revoke it, because a pipe
has no rollback — stopping an in-flight turn is the finalize path's driver teardown.

**Durable is not indefinite, and engagements can speak up unprompted.** A daily sweep
suspends a live session after a day idle and posts recurring idle reminders (three days for
a specialist, seven for an executor, refiring weekly); terminal tombstones age out after
thirty days, bounding duplicate-task protection. Separately, an observer watches engagement
events and may post a bounded LLM interjection into the resident chat — capped at three per
engagement and suppressible with `/silent`. The cap holds under concurrent dispatch: a
budget slot is reserved before evaluation and returned if nothing is posted.

**A `claude_code` engagement gets its own OS identity, not just its own record.** A
never-reused uid, backed by a durable dual-copy counter, is allocated per engagement, its
workspace chowned to it and reachable by root only through a no-follow accessor, and the run
script's final `exec` drops privilege via `setpriv` — see the invariants below for the rest.

## Contracts & invariants

**INV-ENG-001**: A terminal transition has exactly one winner, and only the winner performs the finalization side effects.

Enforced by the registry's terminal transition, which refuses a missing or already-terminal
record and returns failure; the finalize path performs topic closure, driver teardown and
notification only on success.

The direct status mutators honour the same boundary: each re-checks for a prior terminal
state under the registry lock and declines to overwrite one — the idle sweep cannot flip a
concurrently-cancelled engagement back to resumable, and a failed resume that loses the
race to a cancel neither overwrites the status nor runs its duplicate topic cleanup (the
error mutator reports whether it won, and only a winner cleans up).

What it does not cover: exclusivity covers the *post-transition* side effects only: the pre-close
inbound spool drain runs before the win/lose transition, so a caller that goes on to lose the
race may already have flushed pending receipts and eviction notices externally; the drain is
idempotent, which is why running it ahead of the gate is tolerated.

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
also exists only where the driver implements the inbound accessors — today that is the
claude-code driver alone, so an interactive in-casa specialist completion has no unread-input
gate. Accessor failures fail open with a warning rather than wedging termination. The
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

**INV-ENG-009**: A `claude_code` turn is admitted before its first byte reaches the engagement — a record found idle is `active` by then, and a terminal record is not written to at all.

The admission sits between opening the engagement's stdin FIFO for writing — which succeeds
only once its CLI is reading — and writing the first byte, which is the first thing that CLI
can observe. That is the only instant at which the delivery is certain and the engagement
has seen nothing of it. It is deliberately synchronous: with no suspension point between the
open, the decision and the first write, no inbound tool call and no terminal transition can
interleave, which is what makes the ordering a guarantee rather than a race. Durability
follows behind it — the authority check reads the in-memory record, and a persist that never
lands costs only that a later restart re-idles the record, after which the same redelivery
admits it again.

A turn that fits the pipe is written by a single non-blocking write, so in practice the
admission covers the whole of it rather than only its first byte. That is not luck: a payload
larger than the pipe's capacity would be written in pieces, and the suspension between them
is a real scheduling point at which an ungated terminal transition can commit, leaving the
remainder to be written to a record that is already terminal. Since a delivery cannot be
revoked, the fix is to remove the suspension rather than guard it — the pipe is grown to fit
the payload before the first write. The growth is strictly best-effort: a kernel that refuses
it, or a payload beyond the maximum pipe size, simply falls back to writing in pieces.

What it does not cover: the bytes after the first, when the payload does not fit. A terminal
transition landing mid-turn cannot revoke a delivery — closing the writer is itself an
end-of-input the CLI acts on — so a turn already begun runs until the finalize path's driver
teardown stops it. A *completion* cannot land in that window, because a message is still
counted as unread throughout its own write and the gate above refuses; a cancellation can,
and the truncated turn it produces is stopped by teardown rather than by the write path. The
admission also expresses no opinion on a record the registry does not know, which is
unreachable for a live engagement and where the dispatch gate already fails closed.

**INV-ENG-005**: Once the output sequencer is terminalized, ordinary narration and unresolved sends cannot post below the completion.

Enforced by the sequencer's terminalization and its writer checks, with a dedicated path
reserved for the completion notice itself.

What it does not cover: ordering depends on a bounded drain. If the drain times out, the
completion is posted anyway with a warning, and if no live sequencer exists the finalize path
falls back to a direct send that bypasses sequencing entirely.

**INV-CONT-001**: A `claude_code` engagement's uid comes from two independently-written durable high-water copies and is never handed out twice, even if either file is lost.

Reconstruction takes the max of every still-valid durable copy, raised (never lowered) by
records, dir owners, `casa-eng-*` passwd, and `/proc` ids. With both copies lost, a
never-removed `initialized` marker present — OR absent but a real uid (`>= UID_BASE`) evidenced —
poisons (a uid was allocated); marker absent with no real uid inits at base. An unreadable copy
also poisons.

What it does not cover: specialist and legacy `EngagementRecord`s carry `UNALLOCATED_UID` and
are never chowned or uid-dropped — this applies only to `claude_code` executors. SCOPE: a
legacy root survivor keeping `CAP_DAC_OVERRIDE`, or staying root, is root-equivalent
regardless of uid — out of scope for this stage (Stage 3 mount/AppArmor/pid-namespace work),
mitigated only by a best-effort kill during the down-first sweep.

**INV-CONT-002**: Root's own read and write accessors for engagement workspace files refuse to follow a symlink at the final path component or any intermediate one, and can require the resolved file's owner to match an expected uid.

Enforced by `safe_fs.py`'s `open_beneath`/`read_text_beneath` and `atomic_write_beneath`,
preferring `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`, falling back to an
FD-relative `O_NOFOLLOW` walk on a kernel without it.

What it does not cover: root's own reachability into a workspace it already has filesystem
access to — not a boundary between two non-root uids, which ordinary file permissions after
`chown_workspace` enforce instead.

**INV-CONT-003**: Root-touched engagement run-state is never joined into the uid-owned workspace root a `claude_code` engagement's own CLI process can reach.

That root is reachable by the subprocess via `--add-dir`, so a control-only file placed there
would be a symlink-planting target for the process the uid drop exists to contain. Every root
module touching run-state is held to a fixed allowlist of control-only basenames that must
never be path-joined to a workspace-root symbol.

What it does not cover: a static, symbol-name-based check over a fixed set of root modules,
not a full dataflow analysis — it catches established naming for "the workspace root," not an
arbitrarily renamed variable.

**INV-CONT-004**: The uid-drop preflight refuses to plant or resume a `claude_code` service — never starting it as root, never leaving it to crash-loop under its supervisor — whenever `setpriv` is unavailable, the record's uid is unallocated, the workspace is not owned by that uid, no passwd entry exists for it, or a plugin directory it needs is not world-readable.

Run immediately before planting or resuming a service, so a chain that would otherwise fail
at `setpriv`-exec time — or worse, silently exec without dropping — is caught earlier, named.

What it does not cover: the preflight checks the *conditions* a drop requires; it does not
verify the exec'd `claude` process ended up with the expected `Uid`/`CapBnd`, a live property
confirmed operationally.

**INV-CONT-005**: Boot replay migrates a legacy root run-script to the uid-dropped form, or resumes an existing one, only after confirming the corresponding s6 service is fully down; an unconfirmed-down service refuses the resume.

The confirmation scans the supervised service tree itself rather than trusting the durable
record, so a service still up — including one with no matching "undergoing" record — is
never migrated or restarted out from under itself. Only once every service is confirmed down
does replay re-fold live `/proc` uids into the high-water, before any uid backfill or
`setpriv` render.

What it does not cover: a service that never registers as fully down refuses the resume
outright, rather than forcing the old process down itself.

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

**A delegation is refused at one of its gates.** The ACL, alias, spawn-cap and
plugin-withholding refusals, and what each payload discloses, are described in
[`architecture/delegation.md`](delegation.md).

**Two callers race.** The loser is absorbed as already-terminal. No duplicate topic closure
and no duplicate notification.

**A driver fails to start after the record exists.** The engagement is marked errored, topic
cleanup is attempted, and the caller is told the start failed.

**Topic sends, driver teardown, notification and retention fail after the transition.** All
are caught and logged. **The terminal state stays committed** — so an engagement can be
genuinely finished while no completion message ever reached its topic and no notification
reached the resident. These are best-effort effects *after* the authoritative state change,
by design.

**A restart interrupts an engagement.** Persisted records load with `active` rewritten to
`idle`. Replay is attempted only for the driver kind that supports it. A record whose
workspace or recorded plugin artifact is missing is *refused* with a warning — validated
before the intact-service fast path, so an ordinary restart cannot start a service whose run
script would exit-and-respawn forever — and a missing definition is skipped with a warning.
A failed stdin-FIFO recreation and a failed service start are refusals of the same kind:
the record is marked errored and no background spool/relay machinery attaches, rather than
accepting operator messages into an engagement with no consumer (or starting one that would
crash-loop under its supervisor). A record still owing a clearance-downgrade context
rebuild (INV-MEM-011) is never *resumed*: replay drops its session pointer and archive
cache and re-renders the workspace at the clamped floor first, refusing the same way if
that fails.

## Extension points

**A new driver** implements the driver protocol: start, send, cancel, resume, liveness —
plus the downgrade-recovery seams (invalidate the live session with confirmed teardown;
rebuild fresh at the record's current clearance). `start()` may raise `StaleLaunchError`
at its last suspension point; the launcher then aborts rather than deliver a prompt
rendered from pre-downgrade materials.

**A new terminal path** should go through the shared finalize funnel to inherit the
single-winner transition, teardown, notification and retention. Setting a terminal status
directly gets none of that.

**A new durable field** must be added to the record, its load path and its write path
together — otherwise it exists at runtime and silently vanishes across a restart.

**A new origin value that may hold a live object** must be registered as non-persistable, or
serialization will either fail or persist something meaningless.

**A new topic output** should go through the per-engagement sequencer if its ordering
relative to narration matters. Direct sends exist as a fallback and bypass ordering.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRecord`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.try_transition_terminal`
- `casa/rootfs/opt/casa/engagement_registry.py::TerminalPreconditionFailed`
- `casa/rootfs/opt/casa/tools.py::_finalize_engagement`
- `casa/rootfs/opt/casa/tools.py::FinalizeResult`
- `casa/rootfs/opt/casa/tools.py::cancel_engagement`
- `casa/rootfs/opt/casa/drivers/driver_protocol.py::DriverProtocol`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver`
- `casa/rootfs/opt/casa/channels/output_sequencer.py::OutputSequencer`
- `casa/rootfs/opt/casa/casa_core.py::replay_undergoing_engagements`
- `casa/rootfs/opt/casa/engagement_uids.py::UidAllocator`
- `casa/rootfs/opt/casa/safe_fs.py::read_text_beneath`

**Tests**
- `tests/test_delegate_to_agent.py`
- `tests/test_delegate_to_agent_interactive.py`
- `tests/test_claude_code_driver.py`
- `tests/test_cancel_engagement_tool.py`
- `tests/test_engagement_registry.py`
- `tests/test_observer.py`
- `tests/test_engagement_uids.py`
- `tests/test_safe_fs.py`
- `tests/test_root_workspace_accessor_inventory.py`
- `tests/test_boot_replay.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
<!-- END SOURCEMAP -->
