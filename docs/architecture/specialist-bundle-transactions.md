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
stay put as inert residue, and the component-store publication is deliberately earlier
than the journal on both the install and the upgrade, so the journal can always resolve
what the incoming generation declares.

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

On the receipt-bearing bundle arm the retention depends on something further back than
the core, because a *refusal* there runs the compensation, and the compensation rewrites
tuple files from the journal's recorded before-state. So the upgrade resolves everything
that before-state depends on **before it opens the journal**, the way the install does:
the receipt check, the operator's approval, the active-tuple read and the publication and
full verification of the incoming component all happen first. Every refusal they can
raise therefore leaves no journal at all — nothing recorded, nothing to compensate. An
already-present component-store directory is verified rather than trusted, so a corrupt
one refuses here too.

What the capture is sanitized against is then carried rather than looked up. A captured
snapshot's keys are removed only because some component declares them secret, and the
declarations for every root a capture needs — the tuples' own, and the incoming one, read
off the component whose root digest was just checked — are resolved once at that point and
recorded in the journal itself. Every later consumer reads them from there: the runtime
compensation, and boot replay, which rebuilds its transaction from the payload alone. A
declaration is a list of key *names*, which is schema the component already ships; the
journal still holds no value and no secret-derived digest.

That leaves the case where a declaration genuinely cannot be obtained, and it is a
refusal rather than a record. "I cannot tell which of these keys are secret" is the right
answer to *may this go in a journal* and the wrong answer to *what was on disk before* —
and the compensation asks the second question. So the upgrade raises
`prior_schema_unreadable` with nothing staged, captured or compensated, and the operator's
tuple untouched; recovery is the reinstall `active_unreadable` already documents.

The retention is bounded to what the upgrade call leaves on disk when it returns. The
boot-time snapshot scrub reads the same declarations independently, with no journal and no
carried provenance, and is not covered by this invariant.

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

What it does not cover: a transaction stopped by process death rather than cancellation,
which is the recovery boot reconciliation exists for and is unchanged. The other case it
does not cover — a journal left in progress on purpose because its compensating disk
rollback failed — is covered by INV-SPEC-014 below: the debt still stands for boot, but
nothing may commit a further generation of that slug meanwhile.

**INV-SPEC-014**: Before its first durable write, each of the five specialist lifecycle writers — install, upgrade, rollback, uninstall and the specialist persona override, in their journaled and unjournaled arms alike — inspects the bundle-journal directory it would journal into, and refuses rather than commit a new generation of the slug while a journal stands there that the next boot's reconciliation would replay over that generation, or that would make it quarantine owned registry rows — the slug's own, or, for a journal whose filename carries no trustworthy slug, every owned row there is. What boot resolves without restoring or removing anything does not stand in the way: a journal a finished transaction left behind, a quarantined file, a write temporary, and any entry boot's scan skips because it is not a regular file. Uncertainty does stand: a journal that cannot be read, a listed entry that cannot be examined at all, and a journal directory that cannot be enumerated — an absent one is a real answer and does not stand. The refusal resolves nothing; boot still replays or quarantines exactly what it would have.

The journal's in-progress state means *undo me at boot*, and two doors leave one standing
on purpose: a compensating disk rollback that itself failed, and a sync-phase failure
whose own rollback raised. Both are correct — completing a journal whose rollback did not
succeed would strand a half-rolled-back mutation with no recovery. What was missing is the
other half. Nothing consulted that directory before starting the next transaction, so a
later generation of the same slug committed and completed normally beside the older
record, and the next boot either replayed the older capture over it or, when that rollback
failed too, quarantined the slug and dropped the newer generation's owned rows. Either way
the operator was told the change had succeeded.

Enforced by one classifier-backed read at each of the five writers, above the branch
between their journaled and unjournaled arms — the unjournaled arms commit generations
too, so a check placed at the journal write would leave exactly those outside it — and
before each function's own first durable write, which for a rollback is the completion of
a pending tuple rotation and for an uninstall is the retirement of the slug's consent
acknowledgements, both of which happen before either journals. What counts as debt is not
a second opinion about journals: the classification is the same single authority boot and
the persona reference scan already share, and the only thing decided here is which of its
verdicts describe a boot that would damage what came after. The check resolves nothing and
writes nothing, and it returns the directory it inspected, which the writer then journals
into — so the directory read and the directory written are the same one by construction.

One state is deliberately outside it: a write temporary left by a process that died
mid-write is skipped, because boot's pre-scan sweep deletes it. If that deletion ever
fails, boot falls through to classification and the unparseable name quarantines every
owned row, including an admitted generation's. Refusing every specialist mutation, for
every slug, whenever a process died mid-write is the worse trade.

What clears the debt is a restart, and the refusal says so, naming the journal file. It is
not an unconditional unlock: boot retains a journal whenever the quarantine it needed could
not be persisted — over an unreadable registry, say — and the fence then keeps refusing
until a boot resolves it. The operator's own repair paths are refused with everything else,
and that is the point: an uninstall permitted here would be undone by the very next boot.

## Failure behavior

Moved out of this document when it crossed the corpus size ceiling (#974). What a bundle
transaction does when it does not succeed — the pre-journal refusals, the sync-phase and
sequencer compensations, the boot replay/quarantine pass and the age sweeps that follow
it, and the race and failed-rotation refusals — is in
[`specialist-bundle-recovery.md`](specialist-bundle-recovery.md). The invariants those
behaviours satisfy are declared above, in this document.

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
- `tests/test_specialist_recovery_debt.py`
- `tests/test_specialist_recovery_debt_scanner.py`

**Related**
- [`architecture/specialist-bundle-recovery.md`](../architecture/specialist-bundle-recovery.md)
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
<!-- END SOURCEMAP -->
