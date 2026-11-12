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

**A bundle's tool grants are ceilinged before an approval is asked for, and told to the
card's model when it is.** A specialist role may declare casa-framework tools only from a
code-owned consumer-safe allowlist — the loader refuses anything else (the bare
server-level grant included) before a consent prompt exists to approve it — and the grants
that remain are among the facts the approval card is written from. The request tool reads
them from the staged role (`specialist_install.role_tool_grants`) and hands them to the card's
model as `casa_tools_granted`, with each bundled plugin's protected tools and the summaries
its staged manifest declares; the model is told to keep every granted power on the card
([`approval-cards.md`](approval-cards.md)). That is an instruction, not a check; the card's
fallback carries them too, as the consent text below lists `Casa tools:` from the inspection
the request tool rebuilds from the checked staged component (`tools._inspection_from_checked`),
just as the inspect tool's own inspection does. Two further layers back the ceiling
at load and at dispatch (INV-MCP-009), for installs that predate it.

**The consent text reads in the order the operator decides by**
(`specialist_install_consent.render_install_consent_message`). Cut to fit the challenge size
limit when it is over it (`specialist_install_consent.install_consent_text`: its leading whole
lines, then how many were left out), it is what registration measures, so a bundle of any size
is asked about rather than refused, and the approval card's fallback, kept literally with its
first line bold; the card the operator normally reads is written by a model from the same
inputs, digests left out (`specialist_install_consent.install_card_request`). Its first line
asks the question — install which component at which version as which specialist, or update
which specialist to which version — and the mission follows; then what the specialist comes
with (its `Casa tools:` when the role grants any, its persona, its dependencies and,
per bundled plugin, its manifest name, version, source, MCP servers, protected tools and the
secrets it requires or only references); then the action line; and the digests last. The
root digest the approval binds is shown whole; the component, dependency and plugin-content
digests it covers are shown by their first sixteen hex characters (`SHORT_DIGEST_HEX`).

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

**INV-SPEC-017**: The environment names a specialist install consent lists as required, and the `required_env_vars` its commit reports, are exactly the names Casa would withhold the bundled plugin on — a reference the withhold gate exempts is disclosed as referenced and never presented as an ask.

Enforced at inspection. The sourced-plugin validator derives the required set with the
predicate the withhold gate applies — the bare `${VAR}` references in the plugin's
`.mcp.json`, minus the names its manifest declares in `casa.setupProvides` — and records
the remainder of the referenced set (a `${VAR:-default}` the CLI satisfies from its own
default, a name the plugin's own setup tool creates) as a separate, equally attested
receipt-row field. The consent text renders the two on differently labelled lines, the
approval card's facts carry them under different keys (`secrets_required`,
`env_referenced_not_required`), plus `secrets_required_already_set` for the required names
that already hold a value in `plugin-env.conf` — an update usually keeps what an earlier install
wired, and the card must not read those as asks (#1566) — the inspect and commit payloads do too, and the install recipe tells
the configurator to wire the first and never to ask for the second. The collision
preflights — against installed plugins and between siblings of one bundle — keep reading
the union: a name claimed in the defaulted form still collides (#431).

Before this held, the consent and the commit result reused the collision preflight's
extraction as the requirement set, and the configurator interrogated the operator for
every optional and every setup-provisioned name while the plugin's setup episode waited on
two names it had never been asked for.

What it does not cover: whether a required value the operator supplies is usable — that is
the verify surface's question after reload. Nor the wording of the posted card: a model writes
it from facts that keep the two sets apart, and nothing checks that its lines do.
And the parity is by shared predicate, not by
shared call: the gate reads a resolved registry entry after install, the validator reads
the staged tree before it. The pinning test runs the gate over the same staged tree; a
declaration that changes afterwards arrives through an upgrade, which re-inspects and
re-consents.

**INV-SPEC-010**: An install or upgrade approval recorded at the tap, whose configurator run Casa could not start, does not leave the card claiming the configurator is carrying on: the approval stays recorded and approved, the card says the run could not be started and that the approval is kept, and the one edit is chosen from the launch's report rather than written before it.

The invariant is conditioned on a recorded approval because an approval can legitimately
fail to record — the persona store refuses a tap whose consent was revoked underneath it —
and that case takes its own earlier branch: the card says the approval was not recorded and
the install has to be asked for again. When the acknowledgement IS written (synchronously,
in the tap's commit step), the finish hook moves the approval record to `approved` and starts
the configurator's run (`configurator_continue.start`,
[`configurator-approvals.md`](configurator-approvals.md)), which tells the card once how the
launch went. `started` selects "✅ Approved — the Configurator is carrying on with …"; `busy`
or `failed`, and a launcher that raised, select "Approved and saved — but the configurator
could not be started. Ask Ellen again; the approval is kept."; an approval a newer request
replaced in the meantime selects "Replaced by a newer request; nothing was installed." No
governed call was made, so the approval is not used up: asking again starts the run without a
new card (`already_approved`).

What it does not cover, deliberately: `started` is **not a delivery receipt**, nor a
successful install. It says the run was registered and on its way; the run can still be
refused by the configurator lease (INV-CFG-014) or fail, and its own result tells the
resident how it ended.

**An upgrade is worded as an update; a Deny and an expiry close the request.** An upgrade,
and a settings change at the installed version, go through the same card and bind an identity
built the same way as a fresh install's, under an approval of their own kind: an install
approval never authorises an upgrade. The card's model is told it is an update, with the
new version and, when it can be read, the installed one, and the fallback text and every edit but the success one
name the operation an update rather than an install. A Deny closes the approval record and edits the
card; a card nobody answers within its ten minutes is edited to say it expired and nothing
changed. Neither continues the configurator, whose run ended when it asked for the card, and
nothing is reserved for either: the operator asks again, and a new request posts a fresh
card. A newer request for the same component replaces the older one, whose card is edited to
say so ([`configurator-approvals.md`](configurator-approvals.md)), or, while it is still
being written, never posted (INV-PLUG-060). A card that already expired is not re-issued by any
tool — `consent_reprompt` says so ([`plugin-mutation-tools.md`](plugin-mutation-tools.md)) —
it comes back only by running the install or update again.

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
durable — reference not found, no published release for `latest` (INV-SPEC-018), fetch
failure, invalid manifest, slug collision, dependency
unavailable, a secret value in the plain config channel, an undeclared secret name in the
secret channel (INV-SPEC-006), and a slug whose earlier bundle transaction still owes boot
recovery (INV-SPEC-014, `architecture/specialist-bundle-transactions.md`) — a commit there
would be undone by the next boot, so the install is refused before the component store is
written to at all. The bundle commit's own door follows the component's verification and
precedes its journal (INV-SPEC-020, `architecture/specialist-bundle-recovery.md`): an
already-active slug refuses `active_present` — an installed specialist changes version through
the upgrade — and a capture it could not record without losing a saved setting refuses too.
Sourced plugin dependencies are additionally refused categorically when they
declare system requirements or triggers of their own, or when an environment name they
reference — in either expansion form — collides with another installed plugin's —
otherwise-valid bundles fail with dedicated error kinds the dependency model alone would
not predict. A sourced dependency *may*,
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

**A new consent-relevant surface** must be attested in the receipt rows and populated at
inspection time, or consent will not cover it.

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
- `tests/test_specialist_install_consent.py`
- `tests/test_tools_specialist_install.py`
- `tests/test_specialist_bundled_inspect.py`
- `tests/test_specialist_install_latest.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
- [`architecture/specialist-instance-tuples.md`](../architecture/specialist-instance-tuples.md)
<!-- END SOURCEMAP -->
