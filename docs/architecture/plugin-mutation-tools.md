---
last_reviewed: 2026-09-16
---

# Plugin mutation tools

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What the plugin and specialist mutation tools guarantee: how a mutation orders its
registry commit against the runtime convergence that follows, and what a mutation envelope
may and may not claim about a plugin's integration. What a committed removal discloses
about what it leaves behind is in [`plugin-removal.md`](plugin-removal.md). It covers the
tools' contracts, not the machinery they drive
— the registry and content-addressed store are in [`plugins.md`](plugins.md),
per-call authorization in [`plugin-authorization.md`](plugin-authorization.md), the declared setup run in
[`plugin-setup.md`](plugin-setup.md), and the health surfaces the status tool reads in
[`plugin-health.md`](plugin-health.md). The tool surface itself — one registry, the
two-layer result envelope, the question lifecycle and completion — is in
[`tools-interface.md`](tools-interface.md).

## Mental model

**Reading plugin state and changing it are separate grants.** Every tool that mutates the
plugin registry refuses a caller outside the privileged configuration roles, and that has
not changed. What changed is that *reading* is no longer bundled with mutating: a zero-argument,
read-only status tool is granted to the assistant resident, so an operator asking why a
plugin is not working is answered in the turn rather than by spawning a configurator
engagement to ask on their behalf. It reads through the health and setup-episode modules, never
by widening a filesystem scope — `/data` holds secrets and is deliberately never opened
broadly — and it is outside the specialist dispatch ceiling, so a third-party bundle cannot
reach it. It answers two different questions from two stores: what is standing wrong now, and
what happened during a past setup, which only the episode row's recorded error can say.

Those two questions have an ORDER, and the answer to "why is this plugin not working?"
is the first one. The standing entries lead the result — the concatenation that builds the
report puts the resolver's own issues, which carry the verify-stage row, before the merged
setup-episode rows — and the health renderer appends to each the one detail the operator can
act on. The setup history explains how it got there and comes after. Both carriers that
reach the model now say so: the tool's own description leads with what `standing` is for and
names `history` as following it, and the assistant's role doctrine carries the same telling
in its core section, so it reaches all three of that resident's compiled projections rather
than only the composed prompt a persona-bound assistant never reads. The telling is worded
as what the plugin is waiting for and never as the identifier naming it, because the text
projection it is carried into already forbids putting an environment-variable name or a raw
tool-result field in front of a household member.

What that pair of tellings can and cannot be held to is worth stating plainly, because the
defect behind them was not a wrong result. The result was right and led with the actionable
fact; the model paraphrased the last sentence instead and called the cause unknown. Whether
a carrier changes that selection is not a property a unit test can decide, so what is pinned
is that the telling is present, in order, in every carrier that serves it — and nothing more.
The measurement of the selection itself is the playbook's plugin-status play.

Its result envelope carries an optional third thing: whether an answer is COMPLETE. A
store it could not read used to reach the agent as an empty list, which reads as health,
so the tool now adds a conditional statement naming what it could not see —
`standing_unavailable`, `history_unavailable`, `routing_unavailable`. They are conditional
rather than always present, so the healthy answer keeps exactly the keys it always had;
`ok` stays true, because a partial answer honestly labelled is still a successful read.
See [`plugin-health.md`](plugin-health.md) for which conditions each one covers.

**Plugin mutation is persist-then-converge.** Identity, source and requirement guards run
before the registry is touched; after the registry commits, reload and verification try to
make the runtime match. A failure *before* the commit leaves the registry unchanged. A
failure *after* it does not roll anything back — the honest outcome is
committed-but-not-ready, and the envelope says so.

## Contracts & invariants

**INV-TOOL-003**: Plugin mutations serialize under one lock, and a failure before registry activation leaves the registry unchanged, reported in a pinned envelope shape.

Enforced by the shared mutation lock across all five ordinary plugin tools and by the
guard-resolve-publish-then-save ordering in the synchronous cores. The pinned fields —
kind, activation-committed, runtime-ready, verify — make the failure phase machine-readable.

What it does not cover: published store artifacts and installed system requirements are not
unwound by a later refusal; only the registry is untouched. It also says nothing about
ordering, and ordering is what makes the lock safe to hold across a reload: a mutation
takes this lock and then dispatches an agent-scope reload, which takes the reload
read/write lock, so every other holder must acquire in that same direction. The reload
entry points do — they take this lock before dispatching any scope that will reach it
(INV-CFG-011) — and the reload handlers that regenerate plugin health from underneath the
reload lock re-enter it as the same task rather than acquiring it there.

**INV-TOOL-004**: A reload or verification failure after the registry commit yields committed-but-not-ready; nothing rolls the registry back.

Enforced by the converge step reporting `activation_committed: true, runtime_ready: false`
rather than compensating. The next reload — or an explicit verify — is the repair path.

What it does not cover: it makes no promise about *when* the runtime converges, only that
the registry's word is already given.

**INV-TOOL-005**: No plugin-mutation result, completion hand-back or shipped prompt states whether a plugin's integration is live — neither that it is, nor that it is not.

Casa cannot see the external side of an integration. It knows whether it re-minted a secret
and whether it has queued a setup run — neither of which establishes that the service is or is
not reachable. A plugin's credential need not be artifact-bound at all: one gated by
`casa.callbacks` keeps its consent ack across an update (the ack binds the *declaration*, not
the artifact) and holds its credential outside the replaced artifact, so it is commonly
serving throughout an update Casa has just performed.

Enforced in the shipped prose: `recipes/plugin/add.md` and `update.md` tell the engager to say
only that the setup tool still needs to run and that its own result is what to go on, and the
assistant is forbidden to relay another party's verdict about a connection. That prohibition
sits in the assistant's *role doctrine*, in the core section every projection selects, because
a persona-bound resident is served the compiled bundle and never reads the composed prompt —
so a rule written only there has no force on the normal configuration, which is the same
failure `architecture/personality.md` describes for declared response limits. Before this,
`update.md` instructed the engager to report that "the update succeeded but the integration is
dead" whenever the setup tool was not Casa-run — which, for a callback-gated plugin, was every
update. A Gmail that was serving throughout was announced as down and the operator was asked
to re-authorize it (#443).

The failure is symmetric and that is why the rule is two-sided: announcing a fault that does
not exist costs the operator the same trust as missing one that does. An unfounded "it's
fine" is the same defect as an unfounded "it's dead".

**The setup tool's result is not automatically a liveness verdict either**, and the prompts say
so rather than deferring to it as though it were. Its authoring contract is idempotent
*provisioning* — argument-free, re-runnable, `setup_`-prefixed — and it is not *required* to
test what it provisioned. A given tool may check more; only its own output says. So the rule is
to relay what it returned rather than restate it as a verdict, which is the same discipline the
invariant applies to Casa's own claims.

What it does not cover: **which** runner executes the setup tool — because there is no longer a
choice to make. Casa runs a declared `casa.setupTool` and nothing else does; a mutation result
reports the declared tool but routes nothing, and no completion or prompt hands it to an agent.
See [`plugin-setup.md`](plugin-setup.md) (INV-PLUG-010) for what releases the run. That
matters to this invariant for a reason beyond tidiness: a hand-back the plugin did not need used
to cause an unnecessary run, and idempotence means repeat calls converge on the same state, not
that a call is side-effect-free — an unnecessary run can rewrite the provider's configuration,
spend rate budget, or briefly interrupt delivery, and what it costs depends on the plugin.

This invariant is also pinned as *wording*: the tests
assert each shipped surface carries the prohibition and has not reverted to a
previously-shipped phrasing, which is not the same as proving no new phrasing can express the
claim.

**A mutation result also carries what the default vault holds for the plugin's unresolved
secrets** — `secret_candidates`: ids, roles and types, never an operator-typed string, wired by nothing here. Its contract, the vault
tools it shares a projection with, and the classified `op` failure are
[`architecture/plugin-secret-exploration.md`](plugin-secret-exploration.md)'s.

**A mutation result names the ref it actually pinned** — `resolved_ref`: the tag the literal
`latest` resolved to, or the exact ref given; the contract is INV-PLUG-022 in
[`architecture/plugins.md`](plugins.md). A specialist inspection reports the same field under
the same rule (INV-SPEC-018, [`architecture/specialist-lifecycle.md`](specialist-lifecycle.md)).

## Failure behavior

**A guard refuses before the registry is touched.** Identity, source and requirement
guards, and the privileged-role check every mutating tool applies, all run ahead of the
commit, so the refusal leaves the registry exactly as it was and says which phase it
stopped in (INV-TOOL-003). What a refusal does not unwind is work that already landed
outside the registry: a published store artifact stays published and an installed system
requirement stays installed.

**The registry commits and the runtime does not follow.** A reload or verification failure
after the save is reported as committed-but-not-ready rather than compensated
(INV-TOOL-004); the repair path is the next reload or an explicit verify, and the envelope
never implies the runtime already matches. A committed removal carries its disclosure in
that same envelope, because what the removal left behind is settled the moment the registry
saves (INV-TOOL-007).

**A consent keyboard cannot be delivered.** `consent_reprompt` reports delivery from each
keyboard's settled post outcome rather than from the pending rows it computed, so a
re-issue that needed keyboards and landed none is a typed `delivery_failed`, never a
success with nothing on screen.

**A store the status tool needs cannot be read.** Where the tool can see the failure it
names the store in the result — `standing_unavailable`, `history_unavailable`,
`routing_unavailable` — rather than letting it reach the agent as an empty list that reads
as health; `ok` stays true, because a partial answer honestly labelled is still a successful
read. Which conditions each marker covers is defined in [`plugin-health.md`](plugin-health.md)
— for the history, INV-PLUG-015: a setup-episode store that cannot be read as a valid store
is disclosed, and so is the reset a writer performs on it, while an absent store stays
silent.

## Extension points

**An expired or missed plugin-consent DM is recovered by `consent_reprompt`** — the
on-demand, prompt-only re-issue for all three consent kinds (trigger, callback, event), and
the only way to re-surface a committing consent keyboard outside a plugin mutation or
reload: a consent question relayed any other way (`ask_user`, an engagement ask) accepts the
tap, acks it, and commits nothing. The tool never reconciles — no overlay swap, no
setup-round sealing or re-arming — it recomputes each kind's pending set under that kind's
reconcile lock, re-reads the ack store per row (a concurrent Approve earns no fresh
keyboard), threads the sealed setup-round member's nonce back in read-only, and reports
delivery from each keyboard's actual settled post outcome, never from pending rows: when
keyboards were needed and none could be delivered, the result is a typed
`delivery_failed`, not success. Consents the operator explicitly *denied* on a keyboard are
skipped and reported `denied` rather than re-asked — the in-process `consent_denials`
registry records the latest decision in the same synchronous commit step that persists the
ack (Approve clears, Deny records, expiry writes nothing), so agent-driven re-issue can
never nag past a Deny while mutations and reloads re-ask as they always did.

**A refusal of an OPERATION, as against a malformed input,** belongs in the synchronous
core and ahead of every side effect, not in the tool's input schema. The schema has no way
to state a reason the operator can read, and the same target grammar that an operator may
not use is the grammar Casa's own seeded assignments are written in — so the grammar check
stays where it is and the population rule is a separate refusal beside it. Three of these
now exist for executor targets (see *Plugins* for the rule): add and assign refuse before
the registry is even read, and update refuses after the entry is found but before the ref
is resolved, so a refused call publishes nothing, installs no system requirement and leaves
the registry byte-identical. A successful update additionally relabels the entry's source
as the operator's, in the same write as the new pin and never ahead of it — a publish that
raises must not leave the label moved.

**Reporting the operator's stored state alongside what Casa serves** is additive on
purpose. The listing tool's `targets` keeps meaning the registry's stored value, because
the shipped configurator doctrine already reads that field and a redefinition is a misread
no reader can see; what Casa serves and what it ignores are named beside it. The
verification tool is the other way round, and for its own reason: its rows grade what will
actually be loaded, so its desired set is the served one, with the ignored assignments
disclosed rather than dropped — grading a target the resolver will never serve is exactly
the verification-versus-resolver disagreement that function exists to avoid.

**A new plugin lifecycle operation** follows the established split: synchronous
disk-and-registry ordering in a core, then the async wrapper that takes the lock, reloads,
verifies and pins the envelope.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::plugin_add`
- `casa/rootfs/opt/casa/consent_denials.py`

**Tests**
- `tests/test_plugin_tools.py`
- `tests/test_assistant_prompts.py`

**Related**
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/plugin-setup.md`](../architecture/plugin-setup.md)
- [`architecture/plugin-health.md`](../architecture/plugin-health.md)
- [`architecture/plugin-secret-exploration.md`](../architecture/plugin-secret-exploration.md)
- [`architecture/plugin-removal.md`](../architecture/plugin-removal.md)
<!-- END SOURCEMAP -->
