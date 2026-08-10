---
last_reviewed: 2026-08-10
---

# Engagements and delegation

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Work one agent hands to another: ephemeral delegation, durable engagements, how they end,
and what survives a restart. It does not cover the turn loop itself, nor what a driver's
underlying runtime does once started.

## Mental model

**A delegated turn and an engagement are different things.** Delegation in its ordinary form
is a task handed to a specialist that runs and returns — ephemeral. An engagement is a
durable record with its own topic, which outlives the call that created it.

Three launch paths exist and they are not symmetrical. Ordinary specialist delegation runs
ephemerally. *Interactive* specialist delegation creates an engagement. Engaging an executor
always creates one.

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

**Much less survives a restart than the word "durable" suggests.** The record persists;
concurrency permits, live drivers, output sequencers, inbound reservations and various
in-flight maps do not. A record found `active` at startup is rewritten to `idle`, because no
live driver survived to make `active` true.

**Durable is not indefinite, and engagements can speak up unprompted.** A daily sweep
suspends a live session after a day idle and posts recurring idle reminders (three days for
a specialist, seven for an executor, refiring weekly); terminal tombstones age out after
thirty days, bounding duplicate-task protection. Separately, an observer watches engagement
events and may post a bounded LLM interjection into the resident chat — capped at three per
engagement and suppressible with `/silent`. The cap holds under concurrent dispatch: a
budget slot is reserved before evaluation and returned if nothing is posted.

**The depth cap is narrower than it sounds.** It stops an ephemerally delegated agent —
resident or specialist alike — from delegating onwards. It is read in one place and stamped
in one place, and the executor launch path touches neither — so it is not a general limit on
agents creating long-running work.

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

**INV-ENG-003**: A successful completion is refused while unread inbound messages or inbound reservations exist, when the driver exposes its inbound state.

Enforced both as a pre-check and again as a hook inside the transition itself, so the
condition is re-evaluated at the moment the state changes rather than only before it.

What it does not cover: failed and cancelled outcomes intentionally skip the gate, and so
does the operator's own complete command — only the completion *tool* arms the gate, so an
operator marking an engagement complete finalizes past unread input deliberately. The gate
also exists only where the driver implements the inbound accessors — today that is the
claude-code driver alone, so an interactive in-casa specialist completion has no unread-input
gate. Accessor failures fail open with a warning rather than wedging termination.

**INV-ENG-004**: Ephemeral delegation stops at depth one.

Enforced in the pre-launch check for the delegation tool, against a depth stamped when an
ephemeral delegated child's origin is built — stamped for every delegated target, resident
and specialist alike, and checked without regard to the caller's tier.

What it does not cover: the executor launch path neither reads nor stamps the depth, and the
interactive branch that creates a specialist engagement copies the caller's origin without
stamping — an interactively-engaged specialist runs at the caller's depth and can delegate
onwards. The guarantee is "an agent reached through ephemeral delegation cannot delegate
again", not "agent-created work cannot chain".

**INV-ENG-006**: Accepting a delegate's display name never widens the delegation ACL.

The delegation tool accepts either a delegate's role id or its persona display name, because
Casa advertises both to the model. The block renders each entry as `role (Display Name)`,
collapsing to the bare role when no distinct persona name exists — role first, because that
is what the tool is keyed on; rendering the persona first is what taught the model to
address delegates by a name the ACL then refused. The display name stays as the
parenthetical so the model can still map "ask Tina to…" onto a role.

Resolution is scoped: the candidate set is the *caller's own* declared delegates, so every
value it can produce is already inside the ACL.
An exact role id is matched first, so a delegate whose display name happens to be another
delegate's role id cannot shadow it. A name matching two declared delegates is refused with
its own kind rather than resolved to either.

The name is canonicalized **before the voice-handoff decision**, not only inside the ACL.
That decision runs first of all — before any await, so a live voice turn reserves
foreground ownership before work can race ahead — and applies its own exact-role-id test.
A display name reaching it unresolved would read as "not declared", skip the handoff, and
then be accepted by the ACL, running the delegation on the ordinary sync path under the
voice budget: the Concierge policy silently bypassed. The pre-handoff pass is deliberately
silent and total — it rewrites only a name it can resolve to exactly one declared role, and
every denial stays the ACL's to emit, in the established gate order.

What it does not cover: this says nothing about *global* name uniqueness. The registry's
own `name_to_role` is a global, first-binding-wins map and is deliberately **not** what the
ACL consults — a collision there would silently pick a winner, which is not a resolution an
authorization boundary may perform. Nothing prevents two agents elsewhere in a deployment
from sharing a display name; it only stops mattering at this gate.

**INV-ENG-007**: Every `<delegates>` block Casa builds names delegates as the ACL then resolves them.

The two used to come from objects with different lifetimes. `AgentRegistry` is immutable
and a live agent keeps the instance it was constructed with — deliberately, so that
rebinding the runtime's registry cannot reach a running agent — while the delegation role
map is rebuilt on every reload path. Reloading a single role therefore refreshed what
resolution accepted and left every *other* agent rendering its boot-time snapshot: rename a
persona and the assistant went on offering a name the ACL had stopped recognising.

Prompt building now reads the live role map at point of use, so the block, the caller
identity a specialist is handed, and the ACL's alias resolution share one source. Membership
follows: a delegate the map dropped is not advertised, and one added since the caller was
built is. The construction-time registry survives only as fallback for a process where the
tools module was never initialized — so the live directory reports that state as *absent*
rather than empty, and "nobody is dispatchable" is never mistaken for "nothing is wired
yet".

Reading it at build time is only half of it. Options are assembled on a **cold** pool
connect; a warm client is reused without rebuilding them, and a per-role reload closes only
the reloaded role's own pool. So the reload paths additionally drop the warm clients of
agents whose block would now render differently — what carries a rename into a conversation
already in progress. The drop is scoped by an actual diff of the directory (a cold reconnect
costs seconds and a fresh prompt-cache prefix), and *scheduled*, never awaited, since a
reload runs inside a caller's own turn and the invalidation waits on that turn's lock. Every reload scope that commits an agent config does this, including the policy
cascade — which swaps every role without any per-role reload requested, reached by a
config-sync run *after* its own agents sweep.

What it does not cover, and the boundary is the word *builds*: a block is a snapshot taken
when assembled, and the model calls the tool later. A reload landing in between degrades the
alias to the ordinary undeclared refusal, enumerating the current delegates; the next turn
is consistent again. Two further gaps predate this rule: concurrent per-role reloads can publish a briefly
partial role map, since the specialist registry is cleared and refilled in place off-loop
while another reload snapshots it; and *tier* lookups still read a boot-time registry global
no reload refreshes.

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

**A delegation names a target the caller does not declare.** Refused before any lookup, so
the refusal cannot distinguish an agent that exists from one that does not. The payload
enumerates the caller's *own* declared delegates as role/name pairs, filtered against the
role map target resolution itself reads — so an advertised role resolves, and a declared
delegate that is disabled or removed is excluded rather than offered as a retry that would
fail as unknown at the next gate. Naming them discloses nothing the caller does not
already hold: these are its own declarations.

Its role/name pairs match that caller's rendered `<delegates>` block, because both are
built from the same live role map (INV-ENG-007) rather than from a per-agent snapshot that
a per-role reload could leave behind. The enumeration is what tells apart "this delegate is
not wired to me" from "wired, but addressed by the wrong key". The refusal is logged with the
caller role and the target collapsed to `<other>` when unregistered; it moves **no
per-role telemetry counter**, because the target is caller-supplied and the check runs
before authorization.

**A delegation names something that matches two declared delegates.** Refused with a
distinct kind that lists the candidate roles, rather than picking one (INV-ENG-006).

**A required plugin is withheld because its environment is unresolved.** The refusal names
the cause, not only the absence: the payload carries per-plugin entries with the unresolved
variable names and the remediation. Causes that record no reason — a plugin not assigned to
the target, an invalid registry — still deny, with the reason list present and empty.

Read the trust boundary carefully. Environment *values* are never read — only the names a
plugin's own `.mcp.json` references, and whether each resolves. But those names are
**manifest-controlled content** shown to the model, not only to the operator log. The
extractor accepts any `[A-Z_][A-Z0-9_]*`, so a hostile artifact can park an
uppercase-alphanumeric literal there — an AWS access key id is exactly that shape —
indistinguishable from a genuine variable name. So the guarantee is "no environment value",
not "no secret": exposure is bounded by a cap on how many names one denial reports and how
long each may be, with over-long tokens dropped rather than truncated — a truncated
credential is still a credential prefix.

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

**Not enforced anywhere**: nothing caps how deeply agents can create *engagements*. If that
matters for a change you are making, it needs new code — do not expect the delegation depth
cap to cover it.

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
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
<!-- END SOURCEMAP -->
