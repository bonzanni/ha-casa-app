---
last_reviewed: 2026-08-13
---

# Delegation and the agent-spawn boundary

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How one agent addresses and launches another: the delegation ACL and its alias resolution,
the `<delegates>` block, the delegation depth cap, and the global cap on agent-spawned
engagements. Engagement lifecycle itself — creation, the driver
protocol, restart — lives in [`architecture/engagements.md`](engagements.md), terminal
transitions and the completion gate are in
[`architecture/engagement-finalization.md`](engagement-finalization.md), and the OS
boundary a `claude_code` engagement runs inside is in
[`architecture/engagement-containment.md`](engagement-containment.md).

## Mental model

**A delegated turn and an engagement are different things.** Delegation in its ordinary form
is a task handed to a specialist that runs and returns — ephemeral. An engagement is a
durable record with its own topic, which outlives the call that created it. Three launch
paths exist and they are not symmetrical: ordinary specialist delegation runs ephemerally;
*interactive* specialist delegation creates an engagement; engaging an executor always
creates one.

**Two different limits bound agent-created work, and they deliberately divide the
territory.** The *depth cap* stops delegation from chaining: an agent reached through
delegation — ephemeral or interactive — cannot call the delegation tool again. The
*agent-spawn cap* bounds fan-out: engagements created from agent context, whatever the
path, draw from one small global pool of live slots. The executor launch path is
deliberately outside the depth cap (launching executors is what that tool is for) and
inside the spawn cap.

**Operator exemption is positive, never inferred.** The spawn cap exempts a turn only when
it carries the reserved `_operator_turn` origin marker, stamped exclusively by the Telegram
channel for an authenticated operator sender (live message or button tap). Synthesized
delegation-completion turns, scheduled and trigger turns, webhook and plugin-event turns
carry no marker and count as agent context — a prompt-injected resident turn cannot
classify itself as the operator, because absence fails closed.

**The caller's own declarations are the whole ACL.** A delegation target resolves only
within the caller's declared delegates, by role id first and display name second, and every
`<delegates>` block is rendered from the same live map the ACL resolves against.

## Contracts & invariants

**INV-ENG-004**: An agent reached through delegation cannot delegate onward.

Enforced in the pre-launch check for the delegation tool against the *effective* delegation
depth: the ambient origin's stamped depth when present, else the depth persisted on the
bound engagement record's origin — the fallback matters because an in-process engagement
turn inherits the parent task's ambient origin rather than the record's. The depth is
stamped when an ephemeral delegated child's origin is built AND when the interactive branch
creates a specialist engagement, so an interactively-engaged specialist runs at depth one
and is refused onward delegation exactly as an ephemeral delegate is.

What it does not cover: the executor launch path neither reads nor stamps the depth — BY
DESIGN, since launching executors is the purpose of that tool; its fan-out is bounded by
INV-ENG-008 instead. The guarantee is "delegated work cannot chain the delegation tool",
not "agent-created work cannot exist".

**INV-ENG-008**: Engagements created from agent context never exceed a fixed global live count, and refusal is side-effect-free.

Agent context is anything that is not a positively-marked live operator turn: a bound
engagement turn, a delegated turn, or any turn without the reserved `_operator_turn`
marker. Both engagement-creating call sites acquire an occupancy reservation from one
global limiter *synchronously, before any await with external effects* — before the
channel-setup retry, duplicate-task lookups and topic creation — so a refused spawn
performs no Telegram traffic at all, and parallel calls cannot pass a shared count check
(the reservation, not a count read, is the permit). The reservation transfers to the
record at successful creation, rides a dedicated field distinct from the specialist
concurrency permit, and is released exactly once by the terminal transitions. Boot
reconciliation restores one reservation per live marked record — unconditionally, so more
live marked records than the cap become *debt* that must drain below the cap before any
new agent spawn is admitted.

What it does not cover: operator-marked turns are never counted or refused — the cap
bounds agents, not the operator. The marker rides the origin, so records created before
the marker existed carry none and are not counted after a restart. The limiter is
in-memory; the persisted marker plus boot restoration is what carries occupancy across a
restart.

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
reload runs inside a caller's own turn and the invalidation waits on that turn's lock.
Every reload scope that commits an agent config does this, including the policy cascade —
which swaps every role without any per-role reload requested, reached by a config-sync run
*after* its own agents sweep.

What it does not cover, and the boundary is the word *builds*: a block is a snapshot taken
when assembled, and the model calls the tool later. A reload landing in between degrades the
alias to the ordinary undeclared refusal, enumerating the current delegates; the next turn
is consistent again. Two further gaps predate this rule: concurrent per-role reloads can
publish a briefly partial role map, since the specialist registry is cleared and refilled in
place off-loop while another reload snapshots it; and *tier* lookups still read a boot-time
registry global no reload refreshes.

## Failure behavior

**A delegation names a target the caller does not declare.** Refused before any lookup, so
the refusal cannot distinguish an agent that exists from one that does not. The payload
enumerates the caller's *own* declared delegates as role/name pairs, filtered against the
role map target resolution itself reads — so an advertised role resolves, and a declared
delegate that is disabled or removed is excluded rather than offered as a retry that would
fail as unknown at the next gate. Naming them discloses nothing the caller does not
already hold: these are its own declarations. Its role/name pairs match that caller's
rendered `<delegates>` block, because both are built from the same live role map
(INV-ENG-007). The refusal is logged with the caller role and the target collapsed to
`<other>` when unregistered; it moves **no per-role telemetry counter**, because the target
is caller-supplied and the check runs before authorization.

**A delegation names something that matches two declared delegates.** Refused with a
distinct kind that lists the candidate roles, rather than picking one (INV-ENG-006).

**An agent-context spawn arrives at the cap.** Refused with its own kind
(`agent_spawn_cap_exceeded`) before any external side effect — no topic is created, no
channel setup retried. The remediation names the way out: finish or cancel a live
engagement, or have the operator start it.

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

## Extension points

**A new engagement-creating call site** must classify the caller and acquire the spawn
reservation before any await with external effects, pass it to `create()`, and rely on the
terminal transitions for release — mirroring the two existing sites. A count check is not a
permit; acquire the reservation.

**A new authenticated live-operator ingress** must stamp `_operator_turn` server-side after
context sanitization, or its turns will (correctly, but perhaps surprisingly) count against
the agent-spawn cap. Never copy the marker into a synthesized or scheduled turn's context.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::delegate_to_agent`
- `casa/rootfs/opt/casa/tools.py::_prelaunch`
- `casa/rootfs/opt/casa/tools.py::_effective_delegation_depth`
- `casa/rootfs/opt/casa/tools.py::_is_agent_context`
- `casa/rootfs/opt/casa/specialist_limits.py::AgentSpawnLimiter`
- `casa/rootfs/opt/casa/specialist_limits.py::SpawnToken`

**Tests**
- `tests/test_delegation_acl.py`
- `tests/test_delegates_block_live_names.py`
- `tests/test_agent_spawn_limiter.py`
- `tests/test_agent_spawn_cap_registry.py`
- `tests/test_agent_spawn_cap_tools.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
<!-- END SOURCEMAP -->
