---
last_reviewed: 2026-09-14
---

# The setup-dispatch gate

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What stands between a released setup obligation and the bus send that dispatches it: the
recomputation of applied state the gate decides on, and the reads of the applied routing
overlays that fence the send. Who owes the setup run, and the consent verdict that releases
it, are [`plugin-setup.md`](plugin-setup.md); the per-trigger webhook secret and the callback
discovery markers this gate reads are written by the reconciles described in
[`plugin-triggers.md`](plugin-triggers.md) and [`callbacks.md`](callbacks.md).

## Mental model

**Release is not dispatch, and holding here is the answer the obligation already gives
elsewhere.** An obligation released by a positively sealed consent verdict
([`plugin-setup.md`](plugin-setup.md), INV-PLUG-010) can still hold at this gate. The
obligation holds in the same way in three places of its own: while its plugin still has
unresolved environment variables; for a resident execution target, while that agent's
published binding predates the secrets the plugin needs; and for a specialist target, while
the assistant that must delegate to it does not yet declare it
([`plugin-setup.md`](plugin-setup.md), INV-PLUG-029). Those are the environment, binding and
courier holds; every hold this gate adds is of the same kind — the obligation stays `pending`
and released rather than being spent.

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

## Contracts & invariants

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
proved: the revoke sweep kicks only through reconciles that may raise, and a producer that
raises before it publishes wakes nobody. A callback heal cancelled after its swap used to
clear the marker with no wake at all — with both overlays then live, the recovery pass had
nothing to recover — and now holds its lock until its marker writes land and kicks before the
cancellation propagates ([`callbacks.md`](callbacks.md), INV-CB-010), which removes that case
rather than making the wake something to depend on. A refusal whose waker cannot be proved
must not depend on one. Without a runtime registry there is nothing to read, and the recomputation decides
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

## Failure behavior

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

## Extension points

**Adding a check to the gate** means first establishing that a reconcile will actually write
the artifact the check demands, on every pass that reaches the plugin. A gate may only demand
what its writer will write: a check that lands ahead of that guarantee is a hold with no
exit, which is what the callback marker writer's registry-global availability flag made of
an ordinary plugin update until it was narrowed.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/trigger_reconcile.py::verify_minted_secrets`
- `casa/rootfs/opt/casa/callback_reconcile.py::verify_published_markers`
- `casa/rootfs/opt/casa/webhook_auth.py::secret_bound_to_identity`
- `casa/rootfs/opt/casa/casa_core.py::_applied_plugin_routing`

**Tests**
- `tests/test_plugin_reconcile_pass_integrity.py`
- `tests/test_plugin_setup_dispatch_overlays.py`

**Related**
- [`architecture/plugin-setup.md`](../architecture/plugin-setup.md)
- [`architecture/plugin-triggers.md`](../architecture/plugin-triggers.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
<!-- END SOURCEMAP -->
