---
last_reviewed: 2026-11-12
---

# The specialist install lifecycle

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist gets installed: the content-addressed component store, its install
identity, consent receipts and operational-file materialization. It does not cover the
instance tuples an install leaves behind — what a tuple snapshot may hold, its digest, a
pending candidate and its re-commit, and a damaged tuple at boot
([`specialist-instance-tuples.md`](specialist-instance-tuples.md)) — the upgrade, rollback
and uninstall transactions, the bundle journal or its boot-time recovery
([`specialist-bundle-transactions.md`](specialist-bundle-transactions.md)), how a loaded
specialist runs (taxonomy and turn loop), nor persona binding mechanics
(`architecture/personality.md`).

What the operator is asked before an install or update — the tool-grant ceiling, the consent
text, the approval card's facts and the card after the tap — is
[`specialist-install-consent.md`](specialist-install-consent.md).

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

## Contracts & invariants

**INV-SPEC-001**: No durable component-store write happens without a recorded consent for the exact install identity.

Enforced in the commit path, which constructs the identity (root digest plus receipt
digest) and refuses before staging when no acknowledgement matches.

What it does not cover: crash residue. A crash between store publication and journal
creation leaves verified-but-unreferenced content in the store, deliberately outside
rollback.

**INV-SPEC-018**: The ref literal `latest`, given to a specialist inspection for install or upgrade, is resolved before anything is staged to the published release tag `plugin_add` would choose for the same repository (INV-PLUG-022) — the tag, never the literal, is the ref the receipt records and the result reports as `resolved_ref`, the fetch is guarded against that release's peeled commit, and a repository with no published release is refused with `no_release_found` leaving no staging tree and no receipt.

Enforced at the top of the inspection, which shares the plugin store's release resolver
and its failure taxonomy; a caller-supplied expected revision must be the release's peeled
commit, or the inspection refuses with `revision_mismatch`. The configurator's specialist
recipes tell it to pass the literal when the operator asks for the latest version or names
none, and to name the resolved tag in its commit message and completion. An exact ref the
repository does not have (`ref_not_found`) that is a version is fetched once more in its
other spelling —
`0.1.33` as `v0.1.33`, or the reverse (`_other_version_spelling`), under the same expected
revision — and the spelling that resolved is the ref the receipt records and the result
reports as `resolved_ref`; with neither found the refusal names both, and any other failure,
or a ref that is no version, is not retried.

What it does not cover: a plugin dependency a component manifest declares — its ref is
resolved as an exact ref against the manifest's own pinned revision, and a manifest that
wrote `latest` there is refused as a reference not found. Which tag counts as the
published release is INV-PLUG-022's rule, defined in `architecture/plugins.md`.

**INV-SPEC-004**: Operational materialization that changes anything on disk writes a fresh content directory and atomically retargets the slug symlink, with deletion containment-gated.

Enforced in the materializer. The one-time migration of a legacy real directory has a
momentary absent-path window; steady-state swaps do not. A call that would change nothing
writes nothing at all (INV-SPEC-019).

**INV-SPEC-019**: Re-materializing a specialist's operational files from inputs whose rendered bytes already sit — as exactly the four operational files and the binding marker, all regular files — in the slug's own contained content directory writes nothing: the link, the content directory and the files stay as they are. Any difference takes INV-SPEC-004's fresh-directory path.

Enforced in the materializer, which renders the five files' bytes before writing any of
them — only the specialists directory, with any missing parent, is created first — and compares them with what the link's target holds, inside the same call as the write and
so under the caller's `MATERIALIZE_LOCK`. The config repository tracks `agents/**`, and boot
and every specialist-tier reload re-materialize every active specialist, so without this
each of them left a tracked change behind that the next configurator commit swept in —
and reloaded for, though nobody had changed anything (INV-TOOL-014).

What it does not cover: an edit that lands after the comparison. The next re-materialization
compares again and rewrites it, as a fresh write always did. A changed input writes a fresh
content directory once only when it changes the bytes of one of the five files; one that does
not writes nothing. A model option is such an input: `runtime.yaml` records the role's model
policy, not the model it resolves to, so changing the option leaves all five files as they are.

**INV-SPEC-005**: A receipt is integrity-checked on load — a malformed or tampered receipt reads as absent, never as attested.

Enforced by digest recomputation in the receipt loader.

What it does not cover: freshness of the fetched bytes. A valid receipt attests what was
inspected; the commit separately re-checks that what it fetched still matches.

**INV-SPEC-007**: A failed system-requirement replacement preserves the previously working installation — the replacement is built as a new generation in the plugin's own namespace and published by a single atomic retarget of the launcher link; the serving generation is never moved, and a superseded tarball generation is retained until the next install.

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

The venv and npm strategies build each install in a fresh directory inside the plugin's
own `venv-<plugin>` or `npm/<plugin>` directory and switch the launcher link only once the
declared binary exists there; a failed install removes only that new directory, so the
version in use keeps its environment and link. Unlike a tarball generation, the
superseded venv or npm environment is removed right after the switch, not at the next
install (a virtualenv carries its absolute path, so it is never renamed into place).

What it does not cover: generation retention means up to two generations of a tarball
requirement occupy disk between installs.

## Failure behavior

**Resolution, fetch, manifest or dependency problems.** Typed refusals before anything
durable — reference not found, no published release for `latest` (INV-SPEC-018), fetch
failure, invalid manifest, slug collision, dependency
unavailable, a secret value in the plain config channel, an undeclared secret name in the
secret channel (INV-SPEC-006), and a slug whose earlier bundle transaction still owes boot
recovery (INV-SPEC-014, `architecture/specialist-bundle-journal-debt.md`) — a commit there
would be undone by the next boot, so the install is refused before the component store is
written to at all. The bundle commit's own door follows the component's verification and
precedes its journal (INV-SPEC-020, `architecture/specialist-bundle-recovery.md`): an
already-active slug refuses `active_present` — an installed specialist changes version through
the upgrade — and a capture it could not record without losing a saved setting refuses too.
Sourced plugin dependencies are also refused categorically when they
declare system requirements or triggers of their own, or when an environment name they
reference — in either expansion form — collides with another installed plugin's —
otherwise-valid bundles fail with dedicated error kinds the dependency model alone would
not predict. The name check runs only at a fresh install's inspect: upgrade inspect,
commit, resume of a pending-configuration install and rollback skip it, and `plugin_add`
checks no names, so a later plugin sharing one never blocks an installed specialist
(#1641). Bundle siblings are checked against each other at every inspect. A sourced dependency *may*,
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
its fetched staging tree; a successful one keeps it for the commit.

**A component declares system requirements.** Each installs by its declared strategy —
verified tarball, virtualenv or npm, processed in declaration order; OS packages are
refused — and the winning strategy is recorded durably. A present-but-malformed
declaration refuses rather than reading as "no requirements"; a binary name another
plugin already publishes refuses rather than repointing the shared `tools/bin` entry —
by manifest row, and independently by the live launcher's own target, so a corrupt
manifest (which deliberately reads as empty) cannot authorize a takeover; a reinstall by
any strategy preserves the working install until its
replacement fully succeeds (INV-SPEC-007); and an update or removal that drops a
previously published binary name retires that launcher link (ownership-checked against
the plugin's own install namespace) instead of leaving it resolvable but unverified.
Boot reconciliation then only *reports* a missing binary as
degraded; nothing reinstalls tooling automatically.

**Consent missing or the inspection disagrees with the receipt.** Refused before tuple
activation; a changed closure means a changed identity means new consent.

**Materialization fails after commit.** Logged and reported as the instance's last
activation error; the reconcile pass retries per slug, and one slug's failure does not
block another's.

## Extension points

**A new checksum-covered component file or manifest field** belongs in the component
loader and its checksum computation — changing either changes every install identity, so
it is a migration, not a tweak.

**A new dependency kind** goes through closure resolution and enters the root digest only
via its resolved digest.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/specialist_install.py::commit_specialist_install`
- `casa/rootfs/opt/casa/specialist_install.py::compute_install_root_digest`
- `casa/rootfs/opt/casa/specialist_install.py::inspect_specialist_repo`
- `casa/rootfs/opt/casa/specialist_install.py::_other_version_spelling`
- `casa/rootfs/opt/casa/specialist_install.py::resolve_release_ref`
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
- `tests/test_tools_specialist_install.py`
- `tests/test_specialist_install_latest.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
- [`architecture/specialist-instance-tuples.md`](../architecture/specialist-instance-tuples.md)
- [`architecture/specialist-install-consent.md`](../architecture/specialist-install-consent.md)
<!-- END SOURCEMAP -->
