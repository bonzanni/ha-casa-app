---
last_reviewed: 2026-08-16
---

# Plugin-declared triggers

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a plugin has to satisfy before one of its declared webhooks will route: the routing
overlay, the preconditions reconciliation checks, and the durable operator approval that
gates all of it. It does not cover resident triggers — scheduled or webhook — nor firing and
dispatch, which are [`architecture/triggers.md`](triggers.md); nor webhook authentication
mechanics, which belong to [`architecture/http-surface.md`](http-surface.md). Plugin-declared
*authorization callbacks* share this shape but produce no turn and grant no access:
[`architecture/callbacks.md`](callbacks.md).

## Mental model

**A plugin's declared webhook does not route because the plugin is installed.** Declaration
is only intrinsic validity. Routing additionally requires the target to exist and accept
webhooks, the plugin to be assigned to that target, a secret to back the chosen
authentication mode, and a durable operator approval bound to the exact trigger identity.
Until all of those hold, the name is not in the overlay and the route returns not-found.

**Plugin trigger routing is an overlay, replaced atomically.** It is a data structure, not a
set of registered routes, and reconciliation swaps the whole thing at once rather than
mutating entries. Resident registrations are untouched by that swap.

**Approval is all-or-nothing per plugin.** If one declared trigger fails any check, none of
that plugin's triggers route. Partial routing is deliberately not offered.

**Approval and the secret it authorizes land at different moments.** The acknowledgement
persists in the operator's tap; the per-trigger secret is minted by the reconcile that
follows. Routing is unaffected — the overlay swaps in the same reconcile — but anything
deciding whether a plugin's ingress is *usable by an external service*, the setup-tool
dispatch gate above all, has to read the minted secret rather than infer it from the
approval (INV-PLUG-011 in [`plugin-setup.md`](plugin-setup.md)).

**The reconciliation mint is identity-bound.** A surviving secret under a different approval
identity is retired and re-minted rather than inherited, and a stale one that cannot be
removed yields no secret at all rather than the old value — non-inheritance is enforced when
the credential is activated, not by tidying up after a revocation.

**One reconcile pass sees one registry snapshot.** The plugins, their manifests and each
target's assignment authority are served from a single pinned resolution, so a registry
reload landing mid-pass cannot make the overlay compose two generations.

## Contracts & invariants

**INV-TRIG-003**: A plugin's triggers route only as a complete set, and only when target, assignment, secret backing and a persisted operator approval all hold.

Enforced during reconciliation, which computes the desired set and refuses with a specific
reason for each missing precondition.

What it does not cover: intrinsic validation happens earlier and separately, and passing it
means only that the declaration is well-formed.

**INV-TRIG-004**: A trigger approval is persisted and bound to an exact identity, and an unreadable or mismatched approval store yields no approvals.

Enforced by an atomic write, and by a load path that treats anything malformed or
identity-mismatched as zero approvals rather than trusting it. This approval outlives a
restart — as do the specialist and persona install acknowledgements, which keep their own
stores — so it fails closed on read.

"Exact identity" is a specific tuple: plugin, artifact id, effective name, target, and the
normalized auth policy. **Clearance is not in it** — a clearance change on a trigger installs
under the old approval without renewed consent. Everything in the tuple, including any auth
mode, header or tolerance change, does invalidate the approval.

**INV-TRIG-005**: Reconciliation replaces the entire plugin overlay in a single rebind.

There is no window in which the overlay is half-updated. Names absent from the new set stop
routing immediately.

## Failure behavior

**Reconciliation raises.** The overlay is replaced with an empty one before the exception
propagates, so a failure removes plugin routing rather than leaving a stale set.

**A secret cannot be minted.** That plugin's whole set fails closed — every one of its
routes drops out of the overlay, rather than some routing without a usable credential. One
plugin's storage failure never aborts the pass for the others.

**The approval store is missing or corrupt.** Treated as no approvals. Pending routes stay
absent rather than opening.

## Extension points

**A new plugin trigger** needs the manifest declaration, an assigned target that accepts
webhooks, secret backing, and operator consent — and reconciliation must then run. The
declaration itself has hard rails a plugin author cannot discover from the routing model: at
most eight triggers per plugin, effective names capped at 64 characters, and provider-owned
secrets rejected outright. Secret backing is mode-specific: static-header and timestamped
modes get a per-trigger secret minted eagerly after consent into the webhook-secrets state
directory, while body-HMAC rides the one global webhook secret — provisioning the wrong kind
leaves the plugin unroutable.

**Re-issuing an expired consent DM** is the `consent_reprompt` tool's job — the prompt-only
re-issue shared by all three consent kinds (documented with the tool interface), which skips
triggers the operator explicitly denied.

**Changing when reconciliation runs** changes what a stale overlay can survive. It is hooked
at boot, at plugin lifecycle changes, at consent and revocation, and at exactly four reload
scopes: triggers, agent, agents, and full. The policies and config-sync reloads refresh agent
configuration without reconciling the overlay, so a routing-relevant change arriving through
those leaves the old overlay live until a covered scope runs.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/trigger_reconcile.py`
- `casa/rootfs/opt/casa/trigger_acks.py::TriggerAckStore`
- `casa/rootfs/opt/casa/plugin_triggers.py::parse_and_validate`
- `casa/rootfs/opt/casa/trigger_registry.py::TriggerRegistry.replace_plugin_overlay`

**Tests**
- `tests/test_plugin_triggers_reconcile.py`
- `tests/test_plugin_triggers_consent.py`
- `tests/test_plugin_triggers_overlay.py`
- `tests/test_plugin_triggers_manifest.py`
- `tests/test_trigger_consent.py`

**Related**
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
- [`architecture/plugin-setup.md`](../architecture/plugin-setup.md)
<!-- END SOURCEMAP -->
