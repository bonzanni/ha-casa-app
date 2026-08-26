---
last_reviewed: 2026-08-01
---

# The specialist install lifecycle

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist gets installed, upgraded, rolled back and uninstalled: the instance
tuples, the content-addressed component store, consent receipts, operational-file
materialization, and the crash journal that makes a bundle transaction recoverable. It does
not cover how a loaded specialist runs (taxonomy and turn loop) nor persona binding
mechanics (`architecture/personality.md`).

## Mental model

**The active tuple is the installed specialist; operational files are a projection.** An
install commits `active.yaml` first, and the loader-facing files under the agents tree are
*derived* from it — materialization can fail after a successful commit without rolling
anything back, and the per-slug reconcile pass rebuilds the projection later. Two separate
trees exist: the tuple-and-store tree, scanned by the installed index, and the operational
tree the registry loads.

**Identity is the closure, not the component.** The install root digest hashes the
component checksum, the raw manifest checksum, and every resolved dependency digest — so
"same component, different dependency" is a different install identity, and consent is
taken against exactly that identity plus the receipt digest.

**Consent precedes persistence; verification precedes publication.** The acknowledgement
is checked before any durable component-store write, and publication into the store is
verify-then-publish from a fresh staging copy, tolerant of a same-digest concurrent winner.
The store is append-only in practice: roots are pinned, unreferenced blobs are not
collected.

**A bundle's tool grants are ceilinged before they are disclosed, and disclosed before
they are approved.** A specialist role may declare casa-framework tools only from a
code-owned consumer-safe allowlist — the loader refuses anything else (the bare
server-level grant included) before a consent prompt exists to approve it — and the
consent DM's `Casa tools:` line shows the grants that remain, so the approving operator
sees what powers the specialist arrives with. Two further layers back this at load and at
dispatch (INV-MCP-009), for installs that predate the ceiling.

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

**INV-SPEC-001**: No durable component-store write happens without a recorded consent for the exact install identity.

Enforced in the commit path, which constructs the identity (root digest plus receipt
digest) and refuses before staging when no acknowledgement matches.

What it does not cover: crash residue. A crash between store publication and journal
creation leaves verified-but-unreferenced content in the store, deliberately outside
rollback.

**INV-SPEC-008**: A specialist component whose role declares a casa-framework tool outside the consumer-safe allowlist never reaches consent.

Enforced in the component loader itself, which every lifecycle entry point — install
inspect, upgrade, rollback restage — goes through: the violation is a load error naming
the offending grants, raised before an inspection result (and therefore a consent prompt)
can exist. The allowlist is a frozen code constant; no loadable artifact can widen it.

What it does not cover: an install materialized before the ceiling existed — its
runtime.yaml is clamped at load with a warning (the specialist keeps running minus the
forbidden grants), and a live engagement record that already pinned such a grant is
refused at dispatch (INV-MCP-009). Non-casa entries — CC built-ins, plugin servers — are
governed by the role schema and the bundled-plugin consent surfaces, not this list.

**INV-SPEC-002**: An install whose required configuration is missing becomes a pending instance — a desired tuple only, never an active one.

Enforced in the commit and upgrade cores.

What it does not cover: full invisibility. The component-role overlay considers active *or*
desired, so a pending instance's role can appear there while its operational files are
deliberately not materialized.

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
loader will, or it persists an identity the loader can never re-derive and the specialist
is dropped at activation. See `architecture/personality.md`.

**INV-SPEC-004**: Operational materialization writes a fresh content directory and atomically retargets the slug symlink, with deletion containment-gated.

Enforced in the materializer. The one-time migration of a legacy real directory has a
momentary absent-path window; steady-state swaps do not.

**INV-SPEC-005**: A receipt is integrity-checked on load — a malformed or tampered receipt reads as absent, never as attested.

Enforced by digest recomputation in the receipt loader.

What it does not cover: freshness of the fetched bytes. A valid receipt attests what was
inspected; the commit separately re-checks that what it fetched still matches.

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
be established.

What it does not cover: the config git repository's *history* — commits that predate the
guard may retain pre-guard digests (and, before the boot scrub existed, plaintext);
remediation for an affected install is secret rotation. A slug whose tuple was
tombstoned surfaces as an error-state instance; recovery is uninstall + reinstall with
fresh consent.

**INV-SPEC-010**: An install approval that was recorded at tap-commit, but whose requesting engagement is terminal or gone when the operator taps it, does not leave the DM claiming an install: the recorded approval is not revoked by the failed continuation, and the single approval edit is selected from the reconciliation outcome rather than written before it.

The invariant is conditioned on a recorded approval because an approval can legitimately
fail to record — the persona store refuses a tap whose consent was revoked underneath it —
and that case takes its own earlier branch, which reconciliation never reaches. When the
acknowledgement IS written (synchronously, at tap-commit) a failed continuation never
revokes it, so the recovery the corrective DM names — start a new configurator engagement
and re-run the install — short-circuits as `pre_authorized` whenever an ack for that exact
artifact identity is still on file.

The finish hook awaits the reconciliation callback *before* it edits, and the edit it then
makes is one of three. A literal `True` selects the success wording. A `False`, a bare
`None` and an absent callback select the corrective wording, which states that the install
was not started automatically — true on each of those, because none of them created a
delivery task. A contained raise selects a third, weaker wording: a raise is a contract
violation with no production producer, and it is the one branch where the outcome is
unknown rather than known-negative, so it says the automatic start could not be confirmed
and that re-running is safe either way. The tap-callback never-raise contract is unchanged
— `except Exception`, so `CancelledError` stays control flow.

What it does not cover, deliberately: `True` is **not a delivery receipt**, and it is not
a deliverability claim either. It says exactly one thing: the requesting engagement's
record still had `active`/`idle` status when the tap reached the callback. That sample is
taken *before* the delivery seam runs, not after — a terminal transition can win the
registry lock during the seam's own bookkeeping await, after which the turn is handed off
anyway, and a later sample would report that real hand-off as a failure.
`deliver_system_turn` still returns `None`, so a first resume failure, a failed context
rebuild or the shutdown gate can abandon delivery after a positive report; those paths
surface only best-effort in the engagement topic (their own send failures are swallowed), and the success wording therefore claims only that a continuation was
*requested*. Reporting an actual hand-off would widen the channel delivery contract,
which is separate work.

**INV-SPEC-007**: A failed system-requirement replacement preserves the previously working installation — the replacement is built as a new generation in the plugin's own namespace and published by a single atomic retarget of the launcher link; the serving generation is never moved, and the superseded one is retained until the next install.

Enforced in the tarball strategy: a requirement that can never succeed (no safe
`verify_bin`) is refused at manifest level before any installer runs; the install-command
shape is validated before anything is disturbed; the verified tree is fsynced, landed at
a never-pre-existing generation path under the plugin's own directory (immune to
plugin-name prefix collisions, whatever the version), and its rename made durable before
the launcher is retargeted via a temporary link and one rename — no unlink gap, no
restore step whose own failure could lose the old tree. Superseded generations are
reclaimed only at the start of the *next* install, so an in-flight consumer of the
previous tree gets a full install-to-install grace window. All three strategies share the
atomic link publication.

What it does not cover: the venv and npm strategies still rebuild their per-plugin
package trees in place — their failure window can leave that one plugin's own requirement
broken, but never another plugin's — and generation retention means up to two
generations of a tarball requirement occupy disk between installs.

## Failure behavior

**Resolution, fetch, manifest or dependency problems.** Typed refusals before anything
durable — reference not found, fetch failure, invalid manifest, slug collision, dependency
unavailable, a secret value in the plain config channel, an undeclared secret name in the
secret channel (INV-SPEC-006). Sourced plugin dependencies are additionally refused categorically when they
declare system requirements or triggers of their own, or when a required environment name
collides with another installed plugin's — otherwise-valid bundles fail with dedicated
error kinds the dependency model alone would not predict. A sourced dependency *may*,
however, declare `casa.callbacks` — a callback grants no turn or memory access — and it
may likewise declare `casa.emits`/`casa.subscribes`: an emit is inert without an
operator-consented subscriber, and a subscribe, though it *does* wake the plugin's agent,
fires only on a real occurrence elsewhere and only behind per-subscription operator
consent (see `architecture/plugin-events.md`). What stays categorically refused is
`casa.triggers` — a plugin granting itself future wake-ups. Each permitted block carries
the same inspect-time gate: the owned entry routes under the scoped registry name, so an
effective name that would overflow the length cap under its *scoped* spelling is refused
(`callback_name_too_long` / `event_name_too_long`) before it can reach the registry (see
`architecture/callbacks.md`). A rejected inspection deletes
its fetched staging tree; a successful one retains it for the commit to consume.

**A component declares system requirements.** Each installs by its declared strategy —
verified tarball, virtualenv or npm, processed in declaration order; OS packages are
refused — and the winning strategy is recorded durably. A present-but-malformed
declaration refuses rather than reading as "no requirements"; a binary name another
plugin already publishes refuses rather than repointing the shared `tools/bin` entry —
by manifest row, and independently by the live launcher's own target, so a corrupt
manifest (which deliberately reads as empty) cannot authorize a takeover; a tarball
reinstall preserves the working install until its
replacement fully succeeds (INV-SPEC-007); and an update or removal that drops a
previously published binary name retires that launcher link (ownership-checked against
the plugin's own install namespace) instead of leaving it resolvable but unverified.
Boot reconciliation then only *reports* a missing binary as
degraded; nothing reinstalls tooling automatically.

**An install lands pending-configuration.** The staged inspection tree and the source
receipt are both retained — the follow-up configure re-commit requires that receipt, and
a fresh re-inspect would refuse the now-occupied slug — and a retry that supplies only
the still-missing settings merges over the pending candidate's persisted snapshot
(schema-known, non-secret keys only; the caller wins per key). Upgrades carry the active
snapshot and a same-target pending candidate the same way. The receipt and staging tree
are consumed only after the activating bundle's reload-and-verify sequencer succeeds —
a sequencer failure compensates back to the pending state with both intact, so the retry
still has its attested bytes; whatever is abandoned falls to the boot age sweep.

**Consent missing or the inspection disagrees with the receipt.** Refused before tuple
activation; a changed closure means a changed identity means new consent.

**Materialization fails after commit.** Logged and reported as the instance's last
activation error; the reconcile pass retries per slug, and one slug's failure does not
block another's.

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

**A damaged instance tuple is found at boot.** The installed index loads active and
desired independently (#372): a damaged or tombstoned *active* isolates the slug as an
error-state instance — its slug stays reserved against reinstall-overwrite and the error
is surfaced for inspection — instead of aborting the whole scan and with it the app
start, while a damaged *desired* beside a healthy active leaves the running generation in
the fleet and surfaces the desired failure as diagnostic state.

## Extension points

**A new checksum-covered component file or manifest field** belongs in the component
loader and its checksum computation — changing either changes every install identity, so
it is a migration, not a tweak.

**A new dependency kind** goes through closure resolution and enters the root digest only
via its resolved digest.

**A new consent-relevant surface** must be attested in the receipt rows and populated at
inspection time, or consent will not cover it.

**A new durable mutation in the bundle transaction** must record its before-state in the
journal and be restorable by rollback, or a crash leaves it outside recovery.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/specialist_install.py::commit_specialist_install`
- `casa/rootfs/opt/casa/specialist_install.py::compute_install_root_digest`
- `casa/rootfs/opt/casa/specialist_install.py::sweep_staging_aged`
- `casa/rootfs/opt/casa/specialist_materialize.py::current_specialist_roles_dir`
- `casa/rootfs/opt/casa/specialist_materialize.py::materialize_specialist_operational_files`
- `casa/rootfs/opt/casa/specialist_receipt.py::compute_receipt_digest`
- `casa/rootfs/opt/casa/specialist_bundle_journal.py::reconcile_boot`
- `casa/rootfs/opt/casa/specialist_registry.py::InstalledSpecialistIndex`
- `casa/rootfs/opt/casa/system_requirements/tarball.py::install_tarball`
- `casa/rootfs/opt/casa/system_requirements/manifest.py::ensure_bin_claim`

**Tests**
- `tests/test_specialist_install.py`
- `tests/test_specialist_lifecycle_matrix.py`
- `tests/test_specialist_materialize.py`
- `tests/test_specialist_bundle_journal.py`
- `tests/test_system_requirements_installer_tarball.py`
- `tests/test_specialist_install_consent.py`
- `tests/test_tools_specialist_install.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
