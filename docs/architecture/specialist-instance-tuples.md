---
last_reviewed: 2026-09-13
---

# Specialist instance tuples

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a specialist install leaves behind for its slug, and what guards it: the active, desired
and prior instance tuples, the rule that no secret value persists into a tuple snapshot and
that a tuple's digest is derived from the snapshot persisted with it, the pending candidate an
install or upgrade parks when configuration is missing and the inputs its re-commit takes, and
what boot does with a damaged or pre-guard tuple. How a specialist gets installed — install
identity, the component store, consent and materialization — is in
[`specialist-lifecycle.md`](specialist-lifecycle.md); the upgrade, rollback and uninstall
transactions and the bundle journal are in
[`specialist-bundle-transactions.md`](specialist-bundle-transactions.md), and what they do
when they fail is in [`specialist-bundle-recovery.md`](specialist-bundle-recovery.md).

## Mental model

**A tuple is the slug's saved state, and its snapshot is the operator's settings.**
`active.yaml` is the generation that runs, `desired.yaml` a candidate not yet activated, and
`active.prior.yaml` what a rollback returns to; each names the component root it was built from
and carries a `config_snapshot` of the plain settings supplied for it.

**Secrets stay out of a snapshot, and the digest is how that is checked.** A declared-secret
name is refused as plaintext before anything is staged, and a tuple's `config_digest` is
derived from the snapshot actually persisted, so a digest computed over any other mapping is
detectable wherever the tuple is read.

## Contracts & invariants

**INV-SPEC-002**: An install whose required configuration is missing becomes a pending instance — a desired tuple only, never an active one.

Enforced in the commit and upgrade cores.

What it does not cover: full invisibility. The component-role overlay considers active *or*
desired, so a pending instance's role can appear there while its operational files are
deliberately not materialized.

**INV-SPEC-006**: A secret value never persists into an instance tuple — a schema-declared secret name is refused in the plain config channel, the secret channel accepts only schema-declared names, and an upgrade strips legacy plaintext secret keys from the carried snapshot.

Enforced as a typed refusal in the commit and upgrade cores before any staging, by the
upgrade's snapshot merge excluding secret-named keys (the prior component's declaration
included, so a key reclassified to plain never carries its old plaintext forward), by a
post-commit sanitization of the retained prior tuple that strips the plaintext and
tombstones the prior's digests in the same atomic write, by the rollback core refusing a
prior that still carries a secret-classified key, and by a boot-time scrub of every
persisted tuple snapshot that runs before the boot config-git snapshot.

What it does not cover: bundle-journal captures are sanitized at write and at restore
(INV-SPEC-009), but a journal quarantined during a boot keeps its file for one boot as
diagnostic state before the next boot's sweep deletes it; persona overrides copy the
active snapshot unchanged (loadable, therefore secret-free, post-guard state).

**INV-SPEC-009**: A persisted instance tuple's `config_digest` is the digest of its persisted (secret-free) snapshot — never of any other mapping.

Enforced at three layers (#372): construction goes through one factory that derives the
digest from the snapshot and refuses a binding whose `effective_config_digest` disagrees;
the atomic write primitive independently re-checks the same equation; and the loader
rejects any persisted tuple that violates it. Pre-guard files — snapshots sanitized while
their digest, computed over the original secret-bearing mapping, was retained — fail the
equation and are tombstoned at boot: both digest fields are replaced by a sentinel the
loader turns into a typed "uninstall and reinstall" error (an unparseable tuple file
fails closed to the same tombstone; `desired.error.yaml` and crash residue are deleted
outright, and tombstoning a pending `desired.yaml` releases its receipt marker). Bundle
journals apply the same sanitizer to every captured tuple payload when a journal is
written and again when any journal restores, failing closed to all-keys stripping when
the schema union (the capture's own root plus an install/upgrade's target root) cannot
be established. An upgrade no longer reaches that fallback: it resolves the declarations
its capture needs before it opens the journal and records them in the journal, so the
capture and every restore of it — boot replay included — establish the same union from
the journal itself, and a declaration that cannot be obtained refuses the upgrade instead
of being journalled as an emptiness the compensation would then write back
([`specialist-bundle-transactions.md`](specialist-bundle-transactions.md)).

What it does not cover: the config git repository's *history* — commits that predate the
guard may retain pre-guard digests (and, before the boot scrub existed, plaintext);
remediation for an affected install is secret rotation. A slug whose tuple was
tombstoned surfaces as an error-state instance; recovery is uninstall + reinstall with
fresh consent.

**INV-SPEC-015**: A pending-configuration outcome names the inputs its own re-commit takes AND the tool that takes them — the commit and upgrade tool results, and the status of any slug whose tree holds a desired candidate, carry the retained receipt id and staged directory together with the component id, version and root digest, plus the name of the handler that admits them; status reads that candidate's presence and its values from one locked snapshot of the tree rather than from the loaded index, certifies the assembled set against the very acceptance predicate that handler applies before reporting it usable, reports no usable set and no staleness whenever that cannot be established, and marks its own loaded view stale when that view does not describe the tree's candidate.

The re-commit was already the decided resume route (below), and the values were already
retained; what no surface carried was the values themselves, so the carriers pointed at a
re-inspect that refuses. The status disclosure follows the TREE'S DESIRED CANDIDATE rather than the
state string, and rather than the loaded index: a pending upgrade keeps its active tuple, so
an index that has reloaded calls that slug active — and, more sharply, nothing republishes the
index for a pending candidate at all, because such a candidate is deliberately not loadable
and only a reload republishes. An index asked WHETHER there is a candidate therefore answers
no for the ordinary first install that lands pending, which is exactly the case a later
engagement comes here to recover, so presence is read from the tree with the values. Each member is reported as null rather than raised on: a pending slug
predating the marker has none, an abandoned receipt is swept, and a staged tree can be
reclaimed under a still-standing candidate — the operator diagnosing exactly that must still
get an answer. The staged directory is named only while it still exists, since a reclaimed
path would send the next engagement to a route that refuses.

Which tool takes them is part of the answer, not a detail for the reader to infer: a pending
upgrade keeps its active tuple, and a fresh commit refuses any slug that has one, so a pending
upgrade resumes through the upgrade tool and a pending first install through the commit tool.
A disclosure that named five values and left the route to be guessed would send half its
readers to a handler that refuses.

Whether the named set may be USED is a separate answer, and it is established where the claim
is made rather than where the files were written. The reason is that no argument about writers
is sound enough: the candidate and its retained receipt are read as ONE snapshot of the tree
under the lock every lifecycle writer holds, which makes them contemporaneous — but a write
that FAILS leaves them contemporaneous and still describing different candidates, and the
lock cannot be widened across the failure's own rollback, which re-takes it and requires that
no caller hold it. So the assembled set is checked against the acceptance predicate of the
handler that will consume it — the same function that handler runs: the receipt loads and
belongs to this slug, the staged bytes load, their dependency closure resolves against ONE
registry generation, and their recomputed root digest and identity are the tree candidate's
own. A set that passes is reported verified; anything else is reported not-verified with a
reason, and there is deliberately no third state. Separating a settled refusal from "could not
tell" is what an earlier version did, so that a reader could act on the first — and the only
action it licensed was destroying the candidate, while every read on the way to a verdict can
fail transiently. So the payload never asserts that a candidate is permanently unresumable.
The reason says what to look at; it is never authority to remove anything, and a reader that
cannot make the call reports and looks again.

Because the two sources answer different questions they can disagree, and the payload says
so rather than leaving the contradiction to its reader. `state`, `active` and `desired` remain
the running process's LOADED view — which is what an operator asking whether the slug is
serving is asking, and no tree read answers it — while the disclosed inputs are the tree's.
`state_is_stale` accompanies them exactly when the loaded view's candidate, by presence and by
root, is not the tree's: believe the disclosed inputs about what can be re-committed, `state`
about what is loaded, and expect the loaded view to catch up at the next reload or restart.

What it does not cover: that the re-commit will SUCCEED. The inputs are named; the bytes
they point at are re-validated by the commit tool as they always were, and a closure that
drifted still refuses. Nor does status refresh anything it reports — it triggers no reload,
so a stale loaded view stays stale until something else reloads.

The same reasoning binds what a refusal on this route SAYS. When either merge read of a
pending candidate's configuration fails, the operation refuses rather than restage over the
candidate — and the advice that travels with that refusal is part of the guarantee, because
the tool result carries the refusal's `detail` verbatim to whoever paraphrases it to the
operator. It therefore names what to keep and asks for a retry, and it does not offer an
uninstall: uninstall removes the instance directory, which is where a pending candidate's
supplied configuration lives, so following that advice after one transient read error would
destroy the only copy of what the refusal declined to replace. Both of these reads happen
before their call has recorded anything: the install's before it opens its journal, and the
upgrade's above `specialist_bundle_journal.begin` with the rest of the bundle preflight
(`architecture/specialist-bundle-transactions.md`). So on both paths a refused merge read
leaves no journal and runs no compensation, and the files are as the call found them.

What the advice still does NOT say is that they are intact whatever else happens. A failure
raised later in a bundle transaction restores from the recorded before-state, and that
restore re-runs the capture sanitizer, which can write an emptied snapshot back over a tuple
the failed call never opened — a separate defect class, tracked as #975. Advice states what
its own call did, not an outcome the calling path does not control. This is the operating
doctrine's general preservation rule, declared narrowly over these two paths as INV-OPS-001
(`doctrine/operating-casa.md`).

## Failure behavior

**An install lands pending-configuration.** The staged inspection tree and the source
receipt are both retained — the follow-up configure re-commit requires that receipt, and
a fresh re-inspect would refuse the now-occupied slug — and both the commit result and
`casactl specialist status` name the retained values that re-commit takes (INV-SPEC-015);
a retry that supplies only
the still-missing settings merges over the pending candidate's persisted snapshot
(schema-known, non-secret keys only; the caller wins per key). Upgrades carry the active
snapshot and a same-target pending candidate the same way. The receipt and staging tree
are consumed only after the activating bundle's reload-and-verify sequencer succeeds —
a sequencer failure compensates back to the pending state with both intact, so the retry
still has its attested bytes; whatever is abandoned falls to the boot age sweep.

**A damaged instance tuple is found at boot.** The installed index loads active and
desired independently (#372): a damaged or tombstoned *active* isolates the slug as an
error-state instance — its slug stays reserved against reinstall-overwrite and the error
is surfaced for inspection — instead of aborting the whole scan and with it the app
start, while a damaged *desired* beside a healthy active leaves the running generation in
the fleet and surfaces the desired failure as diagnostic state.

## Extension points

**A new field on a tuple** is a schema change: the tuple schema refuses unknown properties and
every read validates against it, so the field needs the schema amended and a migration for the
tuples already on disk.

**A new writer of a tuple** constructs it through the one factory that derives the digest from
the snapshot (INV-SPEC-009), never by editing a persisted payload.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/personality_binding.py::make_instance_tuple`
- `casa/rootfs/opt/casa/personality_binding.py::verify_instance_tuple`
- `casa/rootfs/opt/casa/specialist_install.py::sanitize_specialist_snapshots`

**Tests**
- `tests/test_specialist_install.py`
- `tests/test_personality_binding.py`
- `tests/test_specialist_lifecycle_matrix.py`
- `tests/test_tools_specialist_install.py`
- `tests/test_personality_admin_handlers.py`

**Related**
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
- [`architecture/specialist-bundle-recovery.md`](../architecture/specialist-bundle-recovery.md)
- [`architecture/personality.md`](../architecture/personality.md)
<!-- END SOURCEMAP -->
