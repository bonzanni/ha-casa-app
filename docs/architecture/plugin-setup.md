---
last_reviewed: 2026-08-08
---

# The plugin setup obligation

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Who runs a plugin's declared setup tool, and what has to be true before it runs. The
environment that tool provisions, and the two side channels a plugin reaches Casa
through, are [`plugin-runtime.md`](plugin-runtime.md); the consents this obligation waits
on are [`triggers.md`](triggers.md) and [`callbacks.md`](callbacks.md); installation and
artifact identity are [`plugins.md`](plugins.md).

## Mental model

**Setup has exactly one runner, and "not yet" is an answer it can give.** Casa runs a
declared `casa.setupTool` itself; nothing hands that work to an agent. The runner is a
durable per-artifact *obligation* released by a **positively sealed** consent verdict — and
an obligation with no verdict holds rather than guessing (INV-PLUG-010). That third state
is the whole design: the alternative, deciding at mutation time which of two runners owns
the job, has no correct answer for a plugin whose consent the operator has not yet decided.

**A consent decision and the artifacts it authorizes land at different moments, so the
dispatch gate reads the artifacts.** Approval persists an acknowledgement and settles the
round in one step; the per-trigger webhook secret and the callback discovery markers are
written by the *reconcile* that follows. A gate derived from consent alone therefore
described a route as fully live during the window in between — and a setup tool's whole
job is to hand those artifacts to an external provider. So the gate recomputes the applied
state at the moment it decides (INV-PLUG-011), and holds until what the setup tool will
read actually exists. The one applied thing that recomputation does not read — the two
routing overlays requests actually route through — is read separately, before it and again
with no yield before the send (INV-PLUG-016).

**A reconcile pass describes one registry snapshot.** Each pass pins a single registry
resolution and serves every read from it — the plugins and their manifests, each target's
assignment authority, the registry entries the callback and event reconcilers use, and the
setup-candidate sweep. A pass that resolved each of those separately could compose one
generation's manifests with another's assignment authority and publish a route the newer
generation had removed.

## Contracts & invariants

**INV-PLUG-010**: A plugin's declared setup tool is dispatched by Casa alone — no tool result, completion or prompt routes it to an agent — and an artifact's setup obligation dispatches only after a consent verdict has been positively sealed for that exact artifact and settled with no denial; the absence of a sealed verdict never permits a dispatch, and a verdict asserting that an artifact needs no consent is sealed only when the pending-consent computes for both trigger and callback consents succeeded.

**INV-PLUG-012**: A resident-execution setup obligation rests consumed (`dispatched`) only when its dispatched turn positively evidenced the setup tool — the tool produced a non-error result, or the session's init listed it and no attempted call erred without one; a turn with no such evidence (including one that raised or was cancelled) returns the obligation to `pending` with its released verdict intact, boundedly, and past the bound it fails with an operator note rather than being silently spent.

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
(specialists resolve fresh per delegation and need no such hold).

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

**INV-PLUG-011**: The setup-dispatch route gate recomputes the applied state at the moment it decides — a per-trigger webhook secret must already be minted under the consent identity the recomputation derives, and a routed plugin's callback marker pair must already equal the desired one — so an artifact the reconcile has not yet written keeps the obligation holding; and it opens only for a plugin each recomputation reports having actually seen, never merely for one no issue happens to name.

**INV-PLUG-016**: A released setup obligation is dispatched only from a read, with no yield between it and the bus send, in which neither applied plugin routing overlay carries the unavailable marker and no overlay publication of either kind has landed since the route recomputation began — every publication, including one that re-publishes the marker, advances a single registry generation the read compares; the same marker read precedes the recomputation, and a standing marker defers before it runs. Every refusal these reads produce leaves the obligation pending and released and establishes a worker-owned timed retry; a publication's kick may run the pass sooner, but correctness does not depend on it. With no runtime registry bound the recomputation alone decides.

The recomputation verifies durable artifacts and reads no overlay, so on its own it answered
"live" while a reconcile's unavailable marker had ingress shut — and that state is ordinary,
not exotic: every paired producer wakes this worker from its trigger half before its callback
half has swapped, the consent-tap reconciles heal one half only, and the scheduled recovery
pass is itself a paired producer. Nor could a read taken only before the recomputation close
it, because the recomputation blocks off the loop and a publication can land inside it. So the
worker reads the applied overlays twice. The first read defers on a standing marker without
paying the recomputation. The second is the last thing before the send, with nothing that
yields between them, and it compares a registry generation captured at the first: the
registry advances it on every publication of either overlay, so a marker published and
cleared inside the recomputation, or an ordinary map that dropped this plugin's routes — the
revoke sweep's shape, invisible to the marker predicates — is seen before the decision. A
publication is one synchronous rebind followed by the increment, so a reader on the loop never
sees a new generation with an old overlay. The bus enqueues onto an unbounded queue without
suspending, so what the second read sees is what the bus accepts against; that fact is pinned
by a test rather than claimed by the rule.

What it does not cover: the provider's own registration, which happens in the agent's later
turn and cannot be fenced from here; the scheduled recovery bounds how long ingress stays
closed after that. And every refusal these reads produce is a *deferral* on the worker's own
timer rather than a hold waiting for a publication to wake it, because the wake cannot be
proved: the revoke sweep kicks only through reconciles that may raise, a heal cancelled between
its swap and its kick clears the marker with no wake, and with both overlays then live the
recovery pass has nothing to recover. A refusal whose waker cannot be proved must not depend
on one. Without a runtime registry there is nothing to read, and the recomputation decides
alone — the state before the runtime is bound, in which it already answers not-ok.

The gate the obligation passes through is a *recomputation*, not a cached verdict: it
re-derives every plugin's trigger and callback gaps from the live approval stores and
registry, and refuses on any outstanding issue of either kind, per plugin and
all-or-nothing. Recomputation is what makes it honest across restarts and unrelated health
refreshes — but derivation alone knows only about consent, assignment and declarations,
and the two artifacts a setup tool actually hands to a provider are written by the apply
half of a reconcile.

The window that opens is small and the damage is not. On a first approval the secret has
never been minted; on a *re-approval after a revoke* the file on disk is still bound to the
previous approval generation, which the next mint rekeys; and the webhook handler mints
lazily and unbound for an unrouted name, which a reconcile also replaces. In each case a
setup run dispatched from the derived state alone would provision the external service
against a credential — or a redirect URI — Casa is about to change, which is the exact
failure automatic setup exists to prevent. Both checks therefore read the durable artifact:
the secret's identity sidecar must name the consent identity this pass computed, and the
marker pair is compared byte-strictly against the pair the reconcile would publish.

Holding is not a dead end, and making that true took a second look: the gate may only
demand an artifact the reconcile will actually write. The trigger side mints on every pass,
so it self-heals. The callback side did not — the marker writer declined to rewrite an
existing-but-different pair whenever the pass was *untrustworthy*, and that flag is
registry-global, so one unresolvable artifact anywhere froze every other plugin's markers
for as long as it stayed broken. Against an advisory marker that was survivable; against a
gate it is a hold with no exit, reached by an ordinary plugin update. The
availability gate now covers only what it was for — refusing to *delete* a marker on a pass
that may simply have failed to see its plugin — while a plugin in the routed set, which
resolved cleanly in that very pass and holds a persisted ack, has its own pair refreshed.
Every reconcile then kicks the dispatch worker, and the obligation stays `pending` and
visible in plugin health throughout, exactly like the environment and binding holds above.

What it does not cover: the **global** webhook secret behind `hmac_body`. That mode has no
per-trigger file, so there is no applied artifact to compare — the check is that a secret is
configured, not that the one the request handler captured at boot matches it. A plugin using
only `hmac_body` can therefore pass this gate and be provisioned against a route that
refuses every request, which is the same derived-versus-applied gap one level up. The two
checks here are also a *recomputation*, not a transaction: a pass landing inside a
reconcile's marker rewrite sees the pair briefly absent, which costs one spurious health row
or one spurious hold, both cleared by the next pass. A publication landing inside the
recomputation is a different matter and is the applied-overlay reads' to see (INV-PLUG-016).

It does now cover a plugin the recomputation never *saw*, and getting there took naming the
shape twice. The gate asked whether any issue named the plugin — and an invalid registry, or
a single artifact that fails to resolve, produces a *successful* computation with no issues
at all, which reads as "no gap" for a plugin it never looked at. In the first of those states
the reconcile that follows swaps in an empty overlay, so every plugin webhook 404s while the
gate reports every plugin live. The setup worker's own three-state resolution of the registry
entry did defer on both, but that was a separate, earlier read — a shield, not a property of
the gate, and the same composed-from-moving-reads shape one level up. So each recomputation
now reports the set of plugins it actually iterated, and the gate requires membership in it
POSITIVELY before reading the absence of an issue as a verdict (INV-PLUG-011). Absence is not
consent, and this was the last place in this design that treated it as such.

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

**The secret or marker a released obligation needs is not on disk yet.** The gate holds and
records what it is waiting for; the obligation stays `pending` and released, and the next
reconcile's mint or marker publish closes it (INV-PLUG-011). The same hold covers a mint
that keeps failing — an unwritable state directory, say — which is visible as a trigger
issue rather than as a setup run against a credential that does not exist.

**An applied routing overlay is unavailable, or a publication lands during the route
check.** The obligation defers on the worker's own timer and records which it was — the
marker standing, a publication since the check began, or a read that raised — and the next
pass re-reads, recomputes and compares afresh; the row stays `pending` and released, its
attempt counter untouched, visible in plugin health throughout. A publication's kick runs
that pass sooner, and nothing depends on it arriving (INV-PLUG-016).

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

**The dispatch is accepted but the session cannot run the tool.** Bus acceptance marks the
obligation `dispatched` before the turn runs, and until v0.184.0 that was terminal even
when the turn then had no setup tool to call — observed live when a just-published
artifact's MCP server failed to come up in a session built moments after an agent
reconstruction, so the one automatic run was silently spent. Now the turn itself reports
back (INV-PLUG-012): the agent correlates the episode marker on the dispatched turn with
what the turn actually evidenced — a non-error result from the tool consumes the
obligation; a session whose init listed the tool consumes it too (an available tool the
agent chose not to call is its reply's business); anything else — the tool absent,
availability unknown, every attempted call an error, the turn raising or cancelled —
returns the row to `pending` with its released verdict kept, and the next reload or
reconcile kick re-dispatches. Deliberately no immediate retry: the broken session is
usually a warm one that would fail identically, and the healer in practice is the next
agent reload. The budget is bounded; exhausting it fails the obligation with a note naming
the manual run. A specialist-target dispatch stays delivery-only — the assistant is just
the delegation courier there, and its own session says nothing about the specialist's.

**The dispatch is accepted and the tool runs, but the integration is broken.** Delivery
and in-session execution are what the obligation tracks; the executing agent reports the
tool's own outcome to the operator. Casa makes no claim of its own about whether the
integration works — it cannot see the external side (INV-TOOL-005).

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
- `casa/rootfs/opt/casa/trigger_reconcile.py::verify_minted_secrets`
- `casa/rootfs/opt/casa/callback_reconcile.py::verify_published_markers`
- `casa/rootfs/opt/casa/plugin_registry.py::pinned_resolver`
- `casa/rootfs/opt/casa/webhook_auth.py::secret_bound_to_identity`
- `casa/rootfs/opt/casa/casa_core.py::_applied_plugin_routing`

**Tests**
- `tests/test_plugin_setup_single_runner.py`
- `tests/test_plugin_setup_episodes.py`
- `tests/test_plugin_reconcile_pass_integrity.py`
- `tests/test_plugin_setup_dispatch_overlays.py`

**Related**
- [`architecture/plugin-runtime.md`](../architecture/plugin-runtime.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
<!-- END SOURCEMAP -->
