---
last_reviewed: 2026-09-02
---

# Specialist bundle recovery

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a specialist bundle transaction does when it does not succeed: which refusals happen
before a journal exists, what the sync-phase and post-commit-sequencer failures compensate,
what the boot pass replays or quarantines and the order it does that in, the age sweeps that
follow it, and the refusals a race or a failed prior rotation produces. The transactions
themselves, their contracts and the invariants they declare are in
[`specialist-bundle-transactions.md`](specialist-bundle-transactions.md); install identity,
consent and materialization are in [`specialist-lifecycle.md`](specialist-lifecycle.md).

## Mental model

**Every durable step of a bundle transaction is recorded before it is taken, and failure is
undoing the record rather than guessing at the tree.** A handler journals the pre-state,
mutates, and then either completes the journal or rolls it back; a process that dies between
those leaves the journal standing, and the next boot is what finishes the story. So there are
exactly three places a failure can land — before the journal exists, inside the transaction,
and at the next boot — and which one a given refusal lands in decides what the operator is
left holding.

**A refusal that happens before the journal exists costs nothing**, which is why the preflight
is as long as it is. Once a journal is open the compensation runs, and the compensation
restores what it recorded, not what is on disk now.

## Contracts & invariants

The invariants this document's behaviour satisfies are declared in
[`specialist-bundle-transactions.md`](specialist-bundle-transactions.md), which owns the
transactions themselves: INV-SPEC-003 (an upgrade failure retains the complete prior active
tuple), INV-SPEC-011 (one generation at every lock release), INV-SPEC-012 (rollback after a
model change), INV-SPEC-013 (a dispatched handler runs to a terminal journal disposition),
and INV-SPEC-014 (a writer refuses while standing recovery debt would be replayed over it).
Nothing is declared here; this document says what the code does when it fails, and the
document next door says what that is required to guarantee.

**INV-SPEC-003 has a known arm it does not hold on, and this is the document a reader holding
recovery work lands on, so it says so here.** Every restore described below replays the
before-state the journal recorded, and recording it re-runs the capture sanitizer: a captured
snapshot's keys are dropped when some component declares them secret, so a restore can write
back a snapshot emptier than the tuple it overwrites — a document the failed call never opened.
That is a separate defect class, tracked as **#975**, and it is open. The preflight refusals
listed first below are outside it, because they raise before a journal exists and compensate
nothing. `specialist-bundle-transactions.md` states the bound in full, next to the sanitizer it
belongs to; do not read the compensations below as lossless.

## Failure behavior

**A bundle upgrade's preflight refuses.** No approval on record, a receipt that does not
match the approved inspection, an unreadable active tuple, no active tuple, an incoming
component that fails its dependency closure or its root-digest equation, a prior component
whose declaration cannot be read back, or a pending candidate whose configuration cannot
be read: all of these raise before the journal is created. Nothing is recorded and nothing
is compensated, so the persisted tuple files are exactly as the call found them. The
pending read's result is carried into the transaction rather than taken again afterwards,
so the arm has no second chance to fail at it once a journal exists.

**A bundle sync phase fails.** The journal rolls the recorded pre-state back; if rollback
itself fails, the journal stays in progress for boot to finish, and that slug refuses
further mutations until a restart (INV-SPEC-014).

**The post-commit sequencer fails.** The transaction compensates: the recorded pre-state
is restored, and for a fresh install (no prior active tuple) that restoration includes
removing the op-symlink materialized during the commit — its content directory is
garbage-collected under the same containment gate materialization uses — so a rolled-back
install leaves nothing for agent discovery to keep tripping over. The failure result
states the outcome explicitly: `rolled_back` when the disk state was restored (with
`runtime_compensation_incomplete` when the compensating runtime sweep did not converge —
the next reload or restart converges it), `compensation_failed` when the disk rollback
itself failed and boot reconciliation is the backstop — that arm now also states what
holds until then: the undo record is still standing, further changes to that specialist
are refused, and a restart is what lets boot finish or quarantine it. A sequencer verdict that blocks
only on integrity and binding reasons: config-pending readiness — an unresolved secret,
a missing system-requirement binary, or a `casa.setupProvides` variable still
unprovisioned on a fresh install — is a verified-legal terminal state and never triggers
compensation.

**Boot finds journals.** Complete ones are pruned, valid in-progress ones rolled back,
corrupt or unrollbackable ones quarantined — a filename that cannot be parsed quarantines
every owned entry rather than guessing. **That journal work runs first, and the age
sweeps follow it in the same boot pass**, because the sweeps have to reason about the
tree a replay has already restored into: a pending candidate can exist only inside a
journal capture when boot starts, and deciding what is still owned before the replay
lands reclaims exactly the inputs the replay is about to need. The sweeps run whether or
not there were any journals to reconcile — an install that has never journalled still
reclaims — so the boot report carries the per-journal entries first and the two sweep
entries last. **They run only when the journal work FINISHED, and finishing is read
off the directory rather than inferred from what failed.** Every disposition that
completes removes the journal file — rolled back, pruned complete, or durably
quarantined — so a journal still standing means a replay or a quarantine is still owed
against that tree: the pass raised, or it caught a failure and carried on to the next
journal, or it kept the journal because its quarantine could not be persisted.
Reclaiming then destroys the inputs that unfinished replay needs, exactly as reclaiming
before the replay did. A journal stands here on the same reading that makes it stand in
a writer's way under INV-SPEC-014 below — one classification, asked twice — so residue
that resolves without restoring or removing anything holds nothing back, and a journal
directory that cannot be read at all does. Both sweeps are deferred to the next boot
instead, and the report carries that skip rather than reading like a boot that found
nothing to do: aged staging surviving a few more days is recoverable, an operator's
saved configuration is not.

The sweep half age-sweeps orphan consent
receipts and abandoned staging trees (inspection, bundle and store staging, the persona
staging root included) on a shared seven-day cutoff, so a denied or crashed flow's
fetched repo copies never accumulate unbounded. A live pending-configuration install is
exempt whatever its age — precisely the receipt its commit recorded in a durable
per-slug marker (a same-slug receipt for a different root cannot resume it; newest per
slug is only the fallback when no marker is readable, and keeping every pre-commit
inspection would pin unbounded staging) — and the staged paths surviving receipts
reference keep their trees. A pending candidate is durable operator-visible state, and
sweeping its last usable receipt would make the supported configure re-commit
permanently impossible. Liveness is read from the tuple that is on disk when the sweeps
run, never from what a capture holds: a restore re-runs the capture sanitizer, so a
candidate whose captured tuple is stripped or undigestable lands as a tombstone, is not
a live pending candidate, and its receipt and staged tree are still reclaimed. A replay
that fails quarantines and restores nothing, and one that partly fails can quarantine
while still leaving a live tuple behind — the tree after the journal pass is the only
answer that covers all three.

Outstanding debt is no longer only a writer's concern. The specialist status route reads
the same directory, through the same predicate, before it certifies that a pending
candidate's disclosed resume inputs are usable (INV-SPEC-015,
`architecture/specialist-lifecycle.md`): a standing journal means the tree is
mid-transaction, so the inputs are reported as diagnostics that could not be certified
rather than as a set to act on. It refuses nothing and resolves nothing — boot still
replays or quarantines exactly what it would have.

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

**A new failure arm inside a bundle transaction** must leave the journal in a terminal
disposition — completed after a successful compensation, or standing so boot can finish it —
and must not complete a journal whose compensation raised. A refusal that can be made before
the journal opens belongs in the preflight instead, where there is nothing to undo.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Tests**
- `tests/test_specialist_bundle_journal.py`

**Related**
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/personality.md`](../architecture/personality.md)
<!-- END SOURCEMAP -->
