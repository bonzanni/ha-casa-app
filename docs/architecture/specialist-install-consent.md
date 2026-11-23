---
last_reviewed: 2026-11-23
---

# Specialist install consent and approval

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What the operator is asked before a specialist is installed or updated: the tool-grant ceiling a bundle meets before any consent exists, the consent text and the facts the approval card is written from, the environment names the consent lists, and what the card says after the tap. The install identity a consent binds, the component store, receipts and materialization are [`specialist-lifecycle.md`](specialist-lifecycle.md); the approval record and the configurator's continuation are [`configurator-approvals.md`](configurator-approvals.md).

## Mental model

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

**A role may say it suits the voice concierge; the configurator asks, Casa wires nothing.**
The optional role field `voice_handoff` (a boolean; absent means false) is read off the
staged role into the inspection (`InspectionResult.voice_handoff`) and reported as
`voice_handoff` by `specialist_install_inspect` and by the success results of
`specialist_install_commit` and `specialist_upgrade`. Nothing at runtime acts on it. The
specialist install and upgrade recipes tell the configurator to end an active install's or
upgrade's answer by asking whether the voice concierge should be able to ask the specialist
too — only when the flag is true, `agents/concierge/` exists and its `delegates.yaml` does
not already list the specialist — and to wire a yes as an ordinary request through
`recipes/delegate/wire.md` in a later run.

## Contracts & invariants

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
the configurator to wire the first and never to ask for the second. The required names sit
under each plugin, not in the inspect payload's `required_config_names` or
`required_secret_names`, so when any plugin has some not yet holding a value in
`plugin-env.conf` (the same read the card's `secrets_required_already_set` uses, so an upgrade
is not asked again for what the install wired), the payload's `next` hint names each such
plugin with those names (`tools.py::specialist_install_inspect`), and the install recipe has them
agreed before the approval card — 1Password searched first, the operator asked only about what
nothing found settles, never for a secret value — and wired after the commit
— for an upgrade, after `specialist_upgrade`, from the owned plugins its result's `verify`
shows unresolved (#1605). The hint
is text for the configurator; nothing refuses a request made without them. The collision
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

## Failure behavior

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

## Extension points

**A new consent-relevant surface** must be attested in the receipt rows and populated at
inspection time, or consent will not cover it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/specialist_install_consent.py::render_install_consent_message`
- `casa/rootfs/opt/casa/specialist_install_consent.py::install_consent_text`
- `casa/rootfs/opt/casa/specialist_install_consent.py::install_card_request`

**Tests**
- `tests/test_specialist_install_consent.py`
- `tests/test_specialist_bundled_inspect.py`

**Related**
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/approval-cards.md`](../architecture/approval-cards.md)
- [`architecture/configurator-approvals.md`](../architecture/configurator-approvals.md)
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
<!-- END SOURCEMAP -->
