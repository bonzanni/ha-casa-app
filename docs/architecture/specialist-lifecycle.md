---
last_reviewed: 2026-08-01
---

# The specialist install lifecycle

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist gets installed: the instance tuples, the content-addressed component
store, its install identity, consent receipts and operational-file materialization. It does
not cover the upgrade, rollback and uninstall transactions, the bundle journal or its
boot-time recovery ([`specialist-bundle-transactions.md`](specialist-bundle-transactions.md)),
how a loaded specialist runs (taxonomy and turn loop), nor persona binding mechanics
(`architecture/personality.md`).

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
was not started automatically. That is the callback's report, not a proof: see the
both-directions caveat below, under which a `False` can accompany a real hand-off. A
contained raise selects a third, weaker wording: a raise is a contract
violation with no production producer, and it is the one branch where the outcome is
unknown rather than known-negative, so it says the automatic start could not be confirmed
and that re-running is safe either way. The tap-callback never-raise contract is unchanged
— `except Exception`, so `CancelledError` stays control flow.

What it does not cover, deliberately: `True` is **not a delivery receipt**. It now says one
thing rather than a weaker one — the seam accepted the continuation and a delivery task exists
for it — and that remains exactly what the DM claims: that a continuation was *requested*.

That is a narrowing of what this paragraph used to say, and it is worth being precise about
which half moved. The report used to be a lock-free status sample taken *before* the seam ran,
and it could be wrong in both directions: a resume failure, a failed context rebuild or the
shutdown gate abandoned delivery after a positive report, while a transiently terminal status —
a strict terminal transition whose persistence then rolled back — produced a negative report
for a turn that WAS handed off. Both of those are gone, because the answer is now the seam's
own decision rather than a guess about it: the gate the seam runs is what decides, and it
decides under the topic lock after the context rebuild.

**One direction remains open, and it is not closable from here.** The seam reports its
hand-off, not the driver's admission: the admission is taken inside the engagement's per-turn
lock, which is held for a whole turn, and waiting for it would park this tap callback behind
an unbounded model turn — measured, not supposed, and the reason the seam reports what it
does. So an ungated terminal writer (a cancellation, the operator's own complete command, the
stale-engagement reap, a forced workspace delete, a failed or cancelled completion outcome, a
direct error mark) can still terminalize between the hand-off and the admission, after this DM
has said a continuation was requested. **Do not read a positive report as a guarantee that a
turn ran.** The engagement's own terminal path discloses the un-taken message, and the recorded
approval makes a re-run safe — but this DM is not where that is learned.

The pre-admission half of the race is separately closed, and not by this report: the tap now
takes a synchronous ingress reservation at its commit step, so a successful completion can no
longer commit in the interval between the operator's tap and the continuation's admission. See
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md).

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
- `casa/rootfs/opt/casa/specialist_registry.py::InstalledSpecialistIndex`
- `casa/rootfs/opt/casa/system_requirements/tarball.py::install_tarball`
- `casa/rootfs/opt/casa/system_requirements/manifest.py::ensure_bin_claim`

**Tests**
- `tests/test_specialist_install.py`
- `tests/test_specialist_lifecycle_matrix.py`
- `tests/test_specialist_materialize.py`
- `tests/test_system_requirements_installer_tarball.py`
- `tests/test_specialist_install_consent.py`
- `tests/test_tools_specialist_install.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
<!-- END SOURCEMAP -->
