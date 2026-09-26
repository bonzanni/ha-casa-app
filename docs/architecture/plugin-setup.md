---
last_reviewed: 2026-09-16
---

# The plugin setup obligation

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Who runs a plugin's declared setup tool, and what has to be true before it runs. The
environment that tool provisions, and the two side channels a plugin reaches Casa
through, are [`plugin-runtime.md`](plugin-runtime.md); the consents this obligation waits
on are [`triggers.md`](triggers.md) and [`callbacks.md`](callbacks.md); installation and
artifact identity are [`plugins.md`](plugins.md).
The gate a released obligation passes through before it is dispatched is [`plugin-setup-dispatch-gate.md`](plugin-setup-dispatch-gate.md);
what the dispatched turn itself evidences, how a courier turn's delegation is correlated,
and whose identity the turn carries are [`plugin-setup-turn.md`](plugin-setup-turn.md).

## Mental model

**Setup has exactly one runner, and "not yet" is an answer it can give.** Casa runs a
declared `casa.setupTool` itself; nothing hands that work to an agent. The runner is a
durable per-artifact *obligation* released by a **positively sealed** consent verdict — and
an obligation with no verdict holds rather than guessing (INV-PLUG-010). That third state
is the whole design: the alternative, deciding at mutation time which of two runners owns
the job, has no correct answer for a plugin whose consent the operator has not yet decided.

**Release is not dispatch, and dispatch is not consumption.** A released obligation still
passes through the setup-dispatch gate, which recomputes the applied state the reconcile
has actually written and reads the applied routing overlays before the send (INV-PLUG-011,
INV-PLUG-016); that gate is [`plugin-setup-dispatch-gate.md`](plugin-setup-dispatch-gate.md),
and it is the route gate this document refers to below. Bus acceptance marks the row
`dispatched`, but the turn itself reports back what it evidenced, and only positive
evidence keeps the row consumed (INV-PLUG-012, INV-PLUG-023, INV-PLUG-024) —
[`plugin-setup-turn.md`](plugin-setup-turn.md). A row that ended `failed` or `stale` asks
the operator to run the setup by hand; a successful run of the setup tool by the plugin's
executing role, resident or delegated specialist, clears it from plugin health (INV-PLUG-030).

**A reconcile pass describes one registry snapshot.** Each pass pins a single registry
resolution and serves every read from it — the plugins and their manifests, each target's
assignment authority, the registry entries the callback and event reconcilers use, and the
setup-candidate sweep. A pass that resolved each of those separately could compose one
generation's manifests with another's assignment authority and publish a route the newer
generation had removed.

## Contracts & invariants

**INV-PLUG-010**: A plugin's declared setup tool is dispatched by Casa alone — no tool result, completion or prompt routes it to an agent — and an artifact's setup obligation dispatches only after a consent verdict has been positively sealed for that exact artifact and settled with no denial; the absence of a sealed verdict never permits a dispatch, and a verdict asserting that an artifact needs no consent is sealed only when the pending-consent computes for both trigger and callback consents succeeded.

**A plugin's declared setup tool is run by Casa and by nothing else — released only by a
positively sealed consent verdict for that exact artifact, and then only once its trigger
**and callback** routes are live — the gate rejects any outstanding issue of either kind,
per plugin and all-or-nothing — its required environment resolves, and the executing agent
can load it**. The obligation is durable, retrying and crash-recovered; a single denial withholds
it, so consent is not merely route authorization; an obligation whose plugin still has
unresolved environment variables stays pending rather than running the setup tool against
a placeholder-credentialed server — a consent round can settle while the installing
engagement is still wiring secrets, and every successful reload re-kicks the dispatch
worker; and for a resident execution target it stays pending while that agent's published
binding predates those secrets, until an agent reload makes the plugin loadable there
(specialists resolve fresh per delegation and need no such hold). A specialist target has a
hold of its own instead: its setup is sent as a courier turn asking the assistant to
delegate, and it stays pending until the assistant's live delegate list names that
specialist (INV-PLUG-029).

**INV-PLUG-029**: A specialist-target setup obligation is not dispatched while the courier resident's live delegate declarations — the map the delegation ACL itself reads — do not name the specialist; it holds `pending` with its released verdict intact and no execution retry spent, every successful reload (which refreshes those declarations first) re-kicks the check, and a check that raises holds rather than dispatches.

An install makes the delegation valid only at its last step, the per-role reload of the
resident. Before this hold, a setup obligation released earlier in the install was sent
into that window, the ACL refused the courier's delegation, and each refusal spent one of
the bounded execution retries INV-PLUG-024 grants — so the whole budget could be gone
before the reload that would have let it succeed. Holding means nothing is spent until the
courier can succeed. The row stays visible in plugin health while it waits. If the
assistant is never given the delegate, it waits indefinitely, which is accurate: no courier
turn could run the setup in that state.

The single-runner rule is load-bearing rather than tidy. Until v0.161.0 an agent could
also run setup, acting on a `run_plugin_setup_tool` hand-back in the configurator's
completion, and *which* runner acted was classified when the registry mutated. Two
attempts to make that classification total failed adversarial review, for one reason:
at mutation time there is no third answer. A runner must be named then and there, and
every hole the attempts found was a case whose correct answer was **"not yet"** — a
future operator decision, or a question about what an updated setup tool needs that
nothing in the manifest answers. So the second runner is gone, and the remaining one
expresses "not yet" as *hold*: the obligation stays pending, stays visible in plugin
health (where `pending` never decays), and is re-checked on every reconcile.

What releases it is a **positive** statement, never an absence. The reconciler — the only
component that computes the consent requirement, and one that runs at every lifecycle
site — seals one round per `(plugin, artifact_id)` whose membership is the union of the
plugin's pending *trigger* and *callback* consents, so neither kind alone describes it.
That membership may be **empty**, which asserts that this artifact needs no consent and
releases the obligation; that is deliberately distinct from no round at all, which means
no verdict yet. Reading absence as permission is the concrete defect the first attempt
shipped: it would dispatch before the reconcile had opened the round.

An empty membership is sealed only where the consent position is genuinely *knowable*. A
declared trigger or callback carrying a **non-consent** gap — an unassigned target, a role
without the `webhook` channel, a missing global secret, an invalid public base URL — is
omitted from the pending rows altogether, so reading that omission as "needs none" would
assert precisely what the plugin contradicts. Such a plugin's obligation is recorded and
holds, unsealed, until the gap clears. The route gate would also stop the dispatch, but a
verdict is the one thing this design requires to be true rather than merely harmless.

For the same reason
a zero-member verdict is sealed only when the pending computes for *both* consent kinds
succeeded — a compute that degrades a failure to "nothing pending" cannot be
distinguished from one that means it — and sealing happens before the
operator-reachability gate, so an unreachable DM yields a members-bearing verdict that
correctly holds instead of no verdict at all.

The obligation is created level-triggered by that same sweep, for every resolved plugin
declaring `casa.setupTool`, keyed by the current `artifact_id`. That covers all three
artifact-publishing paths — `plugin_add`, `plugin_update`, and a specialist's bundled
plugins — without a hook at any of them. The setup tool itself is resolved at dispatch
time from the current manifest, so an update that changes `casa.setupTool` while leaving
`casa.callbacks` byte-identical still runs the new tool without binding the setup
contract into a consent identity. A denial marks the obligation refused rather than
dispatching; a later re-prompt for the same artifact re-arms it, which is also how a
re-consent that re-mints a secret gets setup re-run on an unchanged artifact. A plugin
that names a setup tool only in a producer handoff or a README, with no `casa.setupTool`,
has no supported automatic path before v1.0 — nobody runs it, and the configurator says
so rather than guessing a tool name.

**A plugin's setup history outlives the plugin, and standing health must not.** The
episode store is durable and deliberately so: it is the only record of why a setup
failed, and the status tool answers from it long after the fact. The health report is
the other thing entirely — what is wrong *now* — so the projection from one to the other
is filtered to plugins the registry still lists. Without that filter a plugin the
operator removed kept a live standing issue, and a notification with it, until the
episode aged out days later. The filter runs only when the registry read was valid, and
consults the very name map that read produced: a torn or unreadable registry yields no
names, and treating that emptiness as authority would erase every setup row in the
report — the same defect, relocated and worse. So on a bad read nothing is filtered and
every row stands. Removal clears the row's notification mark along with the row, which
is what lets a reinstall that fails again be announced once more rather than silently.

**A removal is recorded on the row it keeps, and a reinstall from the same download is
owed setup again.** The failed row's exhausted budget belonged to the installation the
operator removed, not to the one they installed next — but the reconcile sweep keys
obligations by artifact, and the same download is the same artifact, so the sweep used to
read the retained row as settled and the reinstalled plugin never retried: the old
failure re-entered health as though it were this installation's, and the only routes
out were an update to a different commit or a manual run of a tool that may not load.
Removal now stamps the failed row rather than rewriting it — the error, its counters and
its timestamp stay, since they are the only record of why setup failed, and `stale`
would both overwrite that and let it decay — and the sweep re-arms a stamped row the
first time it resolves the plugin at that artifact again. The decision runs at the sweep
and nowhere earlier, for the same reason an approval racing a removal is declined: a
pending row for a plugin the registry cannot resolve can never be sealed or released. And
because the removal stamps the row as part of the settlement that completes before its
reload, while the resolver's cached snapshot is refreshed only by that reload, the sweep
consumes a stamp only when a fresh read of the registry file agrees with the snapshot
that the artifact is installed; any
disagreement leaves the row as it is for the next pass to ask again. The re-armed row is
a fresh attempt — its own bounded execution budget and its own exhaustion note, holding
for the sweep's positive seal like any other — and carries the earlier failure with it,
so `plugin_status` still says what the previous installation failed on. A reinstall at a
*different* artifact is unchanged: a new obligation, the old row dropped. A `refused` row
is not stamped; the consent the removal revoked is its way back, and the sweep already
re-arms it while that consent is pending again.

**INV-PLUG-020**: A setup obligation that reached `failed` and whose plugin was then removed is re-armed by the first reconcile sweep that resolves the same artifact again — to `pending`/`awaiting_verdict`, as a fresh attempt with its own bounded budget, carrying the earlier failure readably — and by nothing earlier: removal itself mints no pending row, and a sweep that cannot resolve the plugin both live and from a fresh read of the registry file leaves the row as it is.

## Failure behavior

**No consent verdict has settled for an artifact.** The obligation holds, indefinitely and
visibly: `pending` never decays out of plugin health. Two distinct situations reach it, and
the difference matters when reading a store by hand. With **no operator DM reachable**, the
verdict *is* sealed — complete, members-bearing — and simply cannot settle, because no
keyboard was posted for the operator to answer. When a pending-consent compute failed, the
pass spanned registry generations, or a non-consent gap hid part of the plugin's consent
position, the round is sealed **non-authoritative** instead: the keyboards still get their
nonces, but settlement draws no conclusion and leaves the obligation exactly as it was.
Neither situation is a licence to dispatch. The generation case survives as a guard rather
than an expected state — a pass built on the pinned registry resolution cannot span two —
and it still fires for any pass whose resolver is supplied from outside.

**The episode store on disk cannot be read.** Every reader gets an empty store and every
writer's next save replaces the damaged file with a valid one: the reconcile, consent and
worker paths are yield-free and never raise, and they re-derive obligations from live
registry state, so a regenerable store is never allowed to strand setup. What that
replacement erases is the setup HISTORY, so the read that resets also records the reset in
the store it hands the writer, and both reporting surfaces say the history is unavailable
for as long as a failed setup would stay in health — INV-PLUG-015, in
[`plugin-health.md`](plugin-health.md). A round the ledger cannot read is dropped and
re-sealed from live state; that is repair, not history loss, and is logged rather than
reported.

**A consent round settles with any denial.** The obligation is refused and nothing is
dispatched; the operator gets one note naming re-consent as the way forward, not a manual
run they have no tool call for. A later re-prompt for the same artifact re-arms it — via
`ensure_obligation` when a lifecycle pass sees the consent pending again, or, for an
approval arriving through the on-demand [`consent_reprompt`](plugin-mutation-tools.md)
path after the round was already consumed (an expired keyboard settles its member as
denied), via the same synchronous commit step that persists the ack: a refused obligation
whose exact artifact still resolves is re-armed to `pending`/`awaiting_verdict`, and the
approve-time reconcile's fresh authoritative seal releases it.

**The registry cannot be resolved at dispatch time.** The obligation stays released and
retries on later kicks, bounded; past that bound it goes stale with an operator note, since
a plugin that never resolves is a plugin that is gone. Settlement itself never resolves the
registry, so a release can never be lost this way.

**The plugin's server binding is ambiguous.** An obligation whose plugin does not resolve
to exactly one server grant fails with that reason rather than guessing a namespace;
verification blocks such plugins upstream.

**The dispatch is accepted but the turn could not run or hand over the tool.** The turn
reports what it evidenced and the row returns to `pending` or is retired visibly, never
silently spent — [`plugin-setup-turn.md`](plugin-setup-turn.md).

**The plugin is removed after its setup failed, and reinstalled from the same download.**
The removal stamps the failed row and the next sweep that resolves the same artifact
re-arms it as a fresh attempt (INV-PLUG-020): the obligation holds for a positive seal,
dispatches, and either settles or exhausts a new budget with a new note. Nothing retries
while the plugin is absent, and the earlier failure stays readable in the status tool
throughout. A failed row whose plugin was never removed is settled and is never retried by
a reconcile — the routes out remain an update to a different commit and the manual run.

## Extension points

**Declaring a setup tool** means adding `casa.setupTool` to the manifest. It must be
argument-free and idempotent, `setup_`-prefixed, and its plugin must target at least one
resident or specialist — an executor-only target has no invocation path and is refused at
verification. Nothing else is needed: the reconciler sweep finds it and Casa owes the run.

**Changing what releases an obligation** means changing what the reconciler seals, not what
the worker infers. The worker deliberately holds on anything it cannot read as a positive
verdict; adding an inference there would reintroduce the defect this design removed.


## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_setup_episodes.py::ensure_obligation`
- `casa/rootfs/opt/casa/plugin_setup_episodes.py::open_round`
- `casa/rootfs/opt/casa/trigger_reconcile.py::seal_setup_state`
- `casa/rootfs/opt/casa/trigger_reconcile.py::setup_candidates`
- `casa/rootfs/opt/casa/plugin_registry.py::pinned_resolver`

**Tests**
- `tests/test_plugin_setup_single_runner.py`
- `tests/test_plugin_setup_episodes.py`

**Related**
- [`architecture/plugin-runtime.md`](../architecture/plugin-runtime.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
- [`architecture/plugin-setup-dispatch-gate.md`](../architecture/plugin-setup-dispatch-gate.md)
- [`architecture/plugin-setup-turn.md`](../architecture/plugin-setup-turn.md)
<!-- END SOURCEMAP -->
