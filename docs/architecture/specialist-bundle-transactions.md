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
window rebuilds the overlay once and retries before surfacing the error. Above it sits a
second, coarser lock that a whole *transaction* holds: each of the four lifecycle entry
points, and the specialist arm of a persona override, takes the lifecycle lock in the
worker thread around its entire body — the samples it takes, its journal, the registry
swap and both commits — so no other transaction can land between a transaction's tuple
commit and its sidecar publication. The library function holds it, never the tool
handler, because a handler's task can be cancelled while the thread it offloaded to runs
on; and it is taken after the plugin-tools lock and before the materialize lock, never
nested, never on the event loop.

**The tuple pair and the sidecar pair rotate as one generation.** The commit that rotates
the active tuple into the retained prior rotates the active owned-plugins sidecar into the
prior sidecar in the same step, on the tuple's own no-op predicate — a byte-identical
sidecar is rotated all the same, and a tuple no-op rotates nothing — so the retained
sidecar is always the owned set of the generation the retained prior tuple holds. The
sidecar's desired-to-active publication that follows a bundle's registry swap moves no
prior at all. A promotion that fails leaves the outgoing pair pending as two temporaries
beside each other; the next commit of any tuple completes the pair, and a rollback
completes it before it reads either retained file, so what a rollback restores is the
immediately preceding generation and not the one before it. A rollback republishes the
owned set of the generation it restores; the arm that cannot swap the registry — a direct
library rollback — refuses a generation whose owned set differs rather than release a
tuple of one generation beside a registry of another. The retained binding is restored
under the model resolution in force at rollback time: when only the resolved model moved,
the binding is re-derived through the same three gates the loader uses before it is
compiled and committed.

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
candidate (INV-PERS-016), and refuses otherwise. See `architecture/personality.md`. The
rollback re-derives the retained prior the same way, behind the same gates (INV-SPEC-012).

**INV-SPEC-011**: A specialist's retained owned-plugins sidecar is the owned set of the generation its retained prior tuple holds: the commit that rotates the active tuple into the prior rotates the active sidecar into the prior sidecar in the same step, whatever the sidecar's bytes; a commit that rotates no tuple rotates no sidecar; a bundle rollback republishes the owned set of the generation it restores; every specialist-generation transaction — install, upgrade, rollback, uninstall, and a specialist persona override — runs whole under one lifecycle lock, so at every release of it the active tuple, the active sidecar and the registry's owned rows are one generation; a prior rotation whose promotion failed stays pending as a pair of temporaries, and a rollback completes that pair before it reads the retained generation, so the generation a rollback restores is the immediately preceding one; and a rollback that cannot swap the registry refuses a generation whose owned set differs.

Enforced in the tuple commit itself, which copies the active sidecar to its own temporary
beside the tuple copy and promotes both only when the tuple promotion completed; in the
completion step every commit and every rollback runs first; in the lifecycle lock each of
the five transactions takes around its whole body in the worker thread — a caller's role
is also checked there against the installed component's role under the live option
resolution, so an override compiled against a stale overlay refuses as a concurrent
mutation with nothing written; and in the direct rollback arm's typed `bundle_required`
refusal when the retained and active sidecars' plugin rows differ. A specialist persona
override made through the tool is journaled like the four bundle operations, so a raise
restores the captured state and boot reconciliation rolls an in-progress one back.

What it does not cover: a prior generation that predates the sidecar, which reads as the
empty set, as before. The other carve-out it used to name — a transaction cancelled
between its commit and its journal completion, whose in-progress journal boot
reconciliation restored over any generation committed after it — is no longer open:
INV-SPEC-013 below closes it for all four bundle handlers.

**INV-SPEC-012**: A rollback whose retained prior's role checksum differs from the prior component's role materialized under the current option resolution — with the component store's bytes still hashing to the prior's root and the persona identity and agent id unchanged — restores the prior with its binding re-derived for that role, committing the re-derived tuple; a prior whose store bytes drifted, or whose persona identity or agent id moved, is refused by name with the active tuple untouched.

Enforced in the rollback core: the prior's binding is compiled as stored while its role
checksum equals the retained component's role under the live options; otherwise it goes
through the loader's own re-derivation — the install root digest recomputed from the
component store must equal the prior root's suffix, the role must be that component's
under the current resolution, and the stored agent id must be the role's — and the
compile that follows compares the carried persona identity against the loaded pack. The
tuple committed is the re-derived one, so the operational-file marker carries the digest
that was persisted and the next load rewrites nothing. A gate refusal is the same typed
`compile_failed` a verbatim compile produced, its detail naming the root that drifted or
the field that moved.

What it does not cover: a prior that predates the secret-digest guard, refused as
`legacy_prior` as before; and the operational-file marker on a materialization that failed
after the commit, which the next reconcile pass rewrites.

**INV-SPEC-013**: A specialist bundle handler whose library call has been dispatched runs the library commit, the post-commit sequencer and the journal's completion or compensation as one unit that cancellation does not interrupt — the unit runs on to a terminal journal disposition while holding the plugin-tools mutation lock, whether or not the handler was cancelled, so boot never replays a cancelled handler's journal over a generation committed after it; the handler reports cancelled exactly once, waits for that disposition only for a bounded time and then stops waiting without cancelling the transaction or releasing its lock; a cancel that arrives before the mutation lock was acquired stops the transaction outright with nothing begun; and a journal left behind by a process that died is still replayed at boot.

The journal's in-progress state means *undo me at boot*. It is written before the visible
swap and the library returns with it still standing, because completion is deferred past a
sequencer that may have to compensate a generation that is already committed. That left
the whole post-commit window owned by an ordinary coroutine, and `CancelledError` is not
an `Exception`: a cancelled handler completed nothing and compensated nothing, whether the
cancel landed during the sequencer or while the library call was still in its worker
thread — a thread cannot be cancelled, so it committed anyway.

Enforced by running that whole in-lock body in a child task which takes the mutation lock
itself and is awaited through a shield. The task that took the lock is the task that
releases it, so a handler that stops waiting cannot let a second mutation in behind it;
the shield begins before the library call, because the executor already has the work by
the time the awaiting coroutine can be cancelled; a cancel arriving before the lock was
acquired cancels the child outright, since nothing has been begun and shielding there
would perform a mutation the caller aborted; and the absorption is bounded from the first
cancellation absorbed, because an unbounded one would make cancellation permanently
ineffective for a wedged reload or notify. Past the bound only the WAIT is abandoned: the
transaction is not cancelled, keeps the lock, and still completes or compensates.

What it does not cover: a journal left in progress on purpose — a compensating disk
rollback that itself failed, which stays for boot as the failure behavior below states —
and a transaction stopped by process death rather than cancellation, which is the
recovery boot reconciliation exists for and is unchanged.

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
receipt-required instead of rotating sidecar generations for a no-op; and the sidecar prior
moves only with the tuple prior, so a no-op tuple recommit rotates neither (INV-SPEC-011).

**A prior promotion fails after the new active is written.** The commit succeeds — the new
active is durable — and the outgoing generation stays pending as a pair of temporaries
until the next commit of any tuple, or the next rollback, completes it; a rollback that
cannot complete the pair refuses (`pending_rotation_failed`) rather than restore the older
visible prior.

**A rollback after a model change.** The retained prior's binding no longer compiles as
stored, because its role checksum covers the model that was resolved when it was active.
The rollback re-derives it for the model now in force and restores the prior (INV-SPEC-012);
it refuses, naming the cause, when the retained component's store bytes no longer hash to
the prior's root or when the persona identity or agent id moved, and the active tuple is
untouched either way.

**A direct rollback would change the owned set.** A library caller that rolls back without
the bundle arm cannot swap the registry, so a retained generation whose plugin rows differ
from the active sidecar's is refused as `bundle_required` before anything is written; the
tool's rollback exchanges tuple and owned set together.

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
- `casa/rootfs/opt/casa/tools.py::_run_bundle_transaction`
- `casa/rootfs/opt/casa/specialist_install.py::_rederive_stale_binding`
- `casa/rootfs/opt/casa/personality_binding.py::InstanceDir.complete_pending_rotation`

**Tests**
- `tests/test_specialist_bundle_journal.py`
- `tests/test_specialist_rollback_owned_generation.py`
- `tests/test_specialist_rollback_model_flip.py`
- `tests/test_specialist_lifecycle_lock.py`
- `tests/test_specialist_rollback_persona_override.py`
- `tests/test_specialist_bundle_cancellation.py`

**Related**
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
<!-- END SOURCEMAP -->
