---
last_reviewed: 2026-11-23
---

# Specialist bundle transactions: cancellation and an outstanding journal

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Two contracts that keep a bundle journal's in-progress record — which means undo me at boot — from undoing a later generation: a dispatched handler's transaction runs on to a terminal journal disposition however it is cancelled, and every specialist lifecycle writer refuses while a journal boot would replay or quarantine stands for the slug. The transactions themselves are [`specialist-bundle-transactions.md`](specialist-bundle-transactions.md); what a failed one does and the boot pass are [`specialist-bundle-recovery.md`](specialist-bundle-recovery.md).

## Mental model

A journal left in progress is a debt the next boot settles. A handler that was cancelled must not leave one behind over a generation committed after it, and no writer may commit a new generation of a slug while one stands that boot would settle over that generation.

## Contracts & invariants

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

What a bundle transaction does when it does not succeed — the refusals, the compensations and the boot replay or quarantine — is [`specialist-bundle-recovery.md`](specialist-bundle-recovery.md).

## Extension points

A new specialist lifecycle writer runs the same journal-directory check before its first durable write, in its journaled and unjournaled arms alike (INV-SPEC-014).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/specialist_install.py::require_no_recovery_debt`
- `casa/rootfs/opt/casa/specialist_bundle_journal.py::recovery_debt`

**Tests**
- `tests/test_specialist_bundle_cancellation.py`
- `tests/test_specialist_recovery_debt.py`
- `tests/test_specialist_recovery_debt_scanner.py`

**Related**
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
- [`architecture/specialist-bundle-recovery.md`](../architecture/specialist-bundle-recovery.md)
<!-- END SOURCEMAP -->
