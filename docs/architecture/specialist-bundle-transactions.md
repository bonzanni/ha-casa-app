---
last_reviewed: 2026-09-02
---

# Specialist bundle transactions

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens to an installed specialist after its install: the upgrade, rollback and
uninstall transactions, the owned-plugin generation each one publishes, the crash journal
that makes a bundle transaction recoverable, and the boot pass that finishes or quarantines
what a crash left. It does not cover install identity, consent and materialization
([`specialist-lifecycle.md`](specialist-lifecycle.md)), nor persona binding mechanics
(`architecture/personality.md`).

## Mental model

**A sourced-plugin install is a journalled bundle transaction.** The pre-state of the
owned registry entries, the journalled tuple/sidecar files (the pending prior-rotation temporary included) and the slug's acknowledgements is
journalled before the visible swap; a sync-phase failure rolls that recorded state back,
and boot reconciliation replays or quarantines whatever a crash left. The journal's reach
is exactly what it records — component-store and plugin-store artifacts published earlier
stay put as inert residue.

**One lock serializes every instance mutation.** Install, upgrade, rollback, uninstall and
the reconcile pass all run under the materialize lock, and mutations re-read the active
tuple inside it — a pre-lock read that went stale refuses as a concurrent mutation rather
than overwriting. The roles overlay a reload builds under that lock is consumed by the
agent loader *after* release (holding the non-reentrant lock across the load would
deadlock the resident reconcile), so a load that fails against a tuple swapped in that
window rebuilds the overlay once and retries before surfacing the error.

## Contracts & invariants

**INV-SPEC-003**: An upgrade failure retains the complete prior active tuple; a rollback restores it.

Enforced by the upgrade core recording an error result without touching the running tuple,
and by the rollback core's restoration from the retained prior.

One explicit carve-out (#372): a retained prior that predates the secret-digest guard —
its digests tombstoned by sanitization, or its snapshot still carrying a
secret-classified key — is refused with a typed `legacy_prior` error instead of being
restored. The current active tuple is untouched; the rollback *target* requires a
reinstall. The same applies to a prior sentineled by an upgrade whose incoming schema
reclassified a persisted plain key as secret.

One identity detail is easy to get wrong at exactly these commit points: a role's checksum
covers the model it *resolved to*, not just the model policy it declares. Every path here
that materializes a role while writing or checking a binding — commit, upgrade, rollback,
the reconcile pass — therefore has to resolve an operator-option model the way the agent
loader will, or it persists an identity the loader can only recover by re-deriving the
binding at the next load — which it does when the resolved model is the only thing that
moved, rewriting `active.yaml` in place without touching the retained prior or a staged
candidate (INV-PERS-016), and refuses otherwise. See `architecture/personality.md`.

## Failure behavior

**A bundle sync phase fails.** The journal rolls the recorded pre-state back; if rollback
itself fails, the journal stays in progress for boot to finish.

**The post-commit sequencer fails.** The transaction compensates: the recorded pre-state
is restored, and for a fresh install (no prior active tuple) that restoration includes
removing the op-symlink materialized during the commit — its content directory is
garbage-collected under the same containment gate materialization uses — so a rolled-back
install leaves nothing for agent discovery to keep tripping over. The failure result
states the outcome explicitly: `rolled_back` when the disk state was restored (with
`runtime_compensation_incomplete` when the compensating runtime sweep did not converge —
the next reload or restart converges it), `compensation_failed` when the disk rollback
itself failed and boot reconciliation is the backstop. A sequencer verdict that blocks
only on integrity and binding reasons: config-pending readiness — an unresolved secret,
a missing system-requirement binary, or a `casa.setupProvides` variable still
unprovisioned on a fresh install — is a verified-legal terminal state and never triggers
compensation.

**Boot finds journals.** Complete ones are pruned, valid in-progress ones rolled back,
corrupt or unrollbackable ones quarantined — a filename that cannot be parsed quarantines
every owned entry rather than guessing. The same boot pass age-sweeps orphan consent
receipts and abandoned staging trees (inspection, bundle and store staging, the persona
staging root included) on a shared seven-day cutoff, so a denied or crashed flow's
fetched repo copies never accumulate unbounded. A live pending-configuration install is
exempt whatever its age — precisely the receipt its commit recorded in a durable
per-slug marker (a same-slug receipt for a different root cannot resume it; newest per
slug is only the fallback when no marker is readable, and keeping every pre-commit
inspection would pin unbounded staging) — and the staged paths surviving receipts
reference keep their trees. A pending candidate is durable operator-visible state, and
sweeping its last usable receipt would make the supported configure re-commit
permanently impossible.

**Two mutations race.** The loser refuses as a concurrent mutation; nothing is overwritten
or resurrected. The in-lock re-check covers both generations: an active tuple that appeared
while waiting refuses outright, and a pending (desired-only) candidate with a *different*
component root refuses too — only the same component's own configure re-commit may replace
its pending tuple. The commit and upgrade tools additionally re-load the consent receipt
inside the mutation lock, so a receipt consumed by a concurrent bundle fails closed as
receipt-required instead of rotating sidecar generations for a no-op; the sidecar commit
itself treats a byte-identical restage as a no-op rather than rotating the prior away.

## Extension points

**A new durable mutation in the bundle transaction** must record its before-state in the
journal and be restorable by rollback, or a crash leaves it outside recovery.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/specialist_install.py::upgrade_specialist`
- `casa/rootfs/opt/casa/specialist_install.py::rollback_specialist`
- `casa/rootfs/opt/casa/specialist_install.py::_rollback_core`
- `casa/rootfs/opt/casa/specialist_install.py::uninstall_specialist`
- `casa/rootfs/opt/casa/specialist_bundle_journal.py::reconcile_boot`

**Tests**
- `tests/test_specialist_bundle_journal.py`

**Related**
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
<!-- END SOURCEMAP -->
