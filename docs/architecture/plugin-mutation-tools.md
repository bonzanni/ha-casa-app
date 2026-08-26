---
last_reviewed: 2026-08-26
---

# Plugin mutation tools

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What the plugin and specialist mutation tools guarantee: how a mutation orders its
registry commit against the runtime convergence that follows, what a mutation envelope may
and may not claim about a plugin's integration, and what a committed removal discloses
about what it leaves behind. It covers the tools' contracts, not the machinery they drive
— the registry, content-addressed store and per-call authorization are in
[`plugins.md`](plugins.md), the declared setup run in
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

Its result envelope carries an optional third thing: whether an answer is COMPLETE. A
store it could not read used to reach the agent as an empty list, which reads as health,
so the tool now adds a conditional statement naming what it could not see —
`standing_unavailable`, `history_unavailable`, `routing_unavailable`. They are conditional
rather than always present, so the healthy answer keeps exactly the keys it always had;
`ok` stays true, because a partial answer honestly labelled is still a successful read.
See [`plugin-health.md`](plugin-health.md) for which conditions each one covers, and for
the one it does not.

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

**INV-TOOL-007**: A committed plugin removal reported by `plugin_remove`, by the owned-set swap of a SUCCESSFUL specialist bundle — install, upgrade, rollback or uninstall alike — or by a bundle compensation that measured the entry still removed — or that could not read the registry back and says so in the same envelope — discloses that the plugin's CLI-managed persistent data may remain and that no provider revocation was performed; no removal-path string claims a deletion or a revocation Casa did not perform.

A plugin's persistent data directory belongs to the Claude CLI, not to Casa: it lives on the
config volume, the CLI injects its path, and it outlives every lifecycle operation Casa
performs. Nothing on the removal path touches it. So whatever a plugin stored there — an
OAuth token among the possibilities — is still there after `plugin_remove`, and installing
the same plugin again re-attaches to those bytes. Whether they still authorize anything is a
question about the provider, which Casa cannot answer in either direction. What was wrong
before #676 is that no surface said any of this: the removal result named only the retained
artifact.

The fix is disclosure, not deletion, and the choice was the operator's. Deleting would bind
Casa to an external path convention it does not own, where a derivation error is data loss
rather than a failed operation; and it would still revoke nothing at the provider, which is
where a grant lives. Disclosure is the part that is true under either option.

Enforced in the synchronous cores, after the registry commit: `_plugin_remove_sync` spreads
the disclosure into its ok payload — so a committed-but-not-ready outcome (INV-TOOL-004)
carries it too, since the survival fact is settled the moment the registry saves — and every
SUCCESSFUL specialist bundle adds it, naming the owned plugins its registry swap dropped. That
swap is where the four bundle operations become one door: an uninstall publishes an empty owned
set so every entry is dropped, an upgrade or a rollback publishes a new generation and drops
whatever it no longer carries, and an install drops any stale owned entry it replaces. So the
transaction records the dropped NAMES at the moment its swap is authoritative rather than
leaving the payload to re-derive them from the pre-swap set, which for an upgrade or a rollback
mostly names plugins that very operation re-published. A successful swap needs no read-back to
confirm it: it is atomic, it saved, its sequencer then succeeded and its journal completed, all
inside the plugin-tools mutation lock. The one `ok:false` envelope whose registry mutation can persist, the
`compensation_failed` arm of a failed bundle sequencer, discloses on measurement rather
than on the flag: a failed compensation does not establish that the removal survived, because
the rollback restores the registry in its first step and then does fallible work, so the arm
reads the registry back and names only the entries actually still absent. It measures the SAME
set the success payloads disclose — what the swap dropped, not what the transaction captured —
because a read-back can only narrow a candidate set, and the arm that cannot read gets no
narrowing at all: fed the captured set, it would name plugins the operation had just
re-published. That measurement decides
WHICH names are disclosed, and the operation is not part of that decision — any of the four can
reach this arm with an entry still gone. What the measurement cannot decide is whether there was
anything to measure: a pending-configuration or error upgrade never swaps the owned set and
hands the UNCHANGED set through in the same field, so the transaction records whether it
actually swapped, and one that did not says nothing — including on the indeterminate arm,
where there is nothing to read back that could correct it. A read that fails
establishes nothing either way and the envelope says so, while still stating the two facts
that hold regardless — the plugin's CLI-managed persistent data was not deleted, and no
provider revocation was performed. The shipped surfaces say the same thing: the tool's own description, the plugin
removal recipe, and all four specialist recipes — install, upgrade, rollback and uninstall —
each instructing the engager to relay the note rather than restate it as a deletion, and the
three that can now receive it from a successful swap naming that arm too, since there the
removal is confirmed rather than measured or unknown. A payload no recipe relays is a
disclosure the operator never sees, so the payload gate and the recipe gate cover the same
set of doors.

The wording is bounded by INV-TOOL-005, which is why it reads the way it does. `may remain`,
because Casa cannot see whether the plugin ever stored anything; and
`provider_revocation_performed: false` names an operation Casa did not perform rather than the
grant's state, which Casa has no standing to report in either direction.

What it does not cover: envelopes that describe no persisting removal carry none of it — the
pre-commit refusals, `plugin_unassign`, `plugin_update`, and the rolled-back arms, where the
entries are back, and a bundle that never ran its swap, which the paragraph above separates from
one that did. Saying what the statement covers in the statement rather than only in this paragraph is the point —
an invariant whose exclusions live below it is still false as declared, and it is the
declaration that gets read. What it genuinely does not reach is a removal path that RAISES after the registry commit — any
bundle whose sequencer raises, where compensation runs and the exception propagates, and equally a
direct `plugin_remove` whose reload-and-verify tail raises. In both the mutation has
committed and there is no result envelope to carry the disclosure, so the operator gets a
hard error where a persisting removal went unstated. That is a bounded, non-silent gap: the
error is loud, and every recipe on a removal-capable path — the plugin removal one and all
four specialist ones — tells the engager to say the removal may have taken
effect, that the plugin's CLI-managed persistent data was not deleted and no provider
revocation was performed either way, and to check with `plugin_list()`. That fallback is the
same shape in all five, because the gap is: an invariant about envelopes says nothing where
there is no envelope. Closing it properly means converting exception
handling on these paths into structured outcomes — a different change, and one that touches
every bundle tool rather than the removal ones.

## Failure behavior

**A guard refuses before the registry is touched.** Identity, source and requirement
guards, and the privileged-role check every mutating tool applies, all run ahead of the
commit, so the refusal leaves the registry exactly as it was and says which phase it
stopped in (INV-TOOL-003). What a refusal does not unwind is work that already landed
outside the registry: a published store artifact stays published and an installed system
requirement stays installed, to be reused or swept later rather than rolled back here.

**The registry commits and the runtime does not follow.** A reload or verification failure
after the save is reported as committed-but-not-ready rather than compensated
(INV-TOOL-004); the repair path is the next reload or an explicit verify, and the envelope
never implies the runtime already matches. A committed removal carries its disclosure in
that same envelope, because what the removal left behind is settled the moment the registry
saves (INV-TOOL-007).

**A removal path raises after the commit.** There is then no result envelope to carry the
disclosure, and the operator gets a hard error where a persisting removal went unstated.
The gap is bounded and loud rather than silent, and every recipe on a removal-capable path
tells the engager to say the removal may have taken effect, that the plugin's CLI-managed
persistent data was not deleted and no provider revocation was performed either way, and to
check with `plugin_list()`.

**A consent keyboard cannot be delivered.** `consent_reprompt` reports delivery from each
keyboard's settled post outcome rather than from the pending rows it computed, so a
re-issue that needed keyboards and landed none is a typed `delivery_failed`, never a
success with nothing on screen.

**A store the status tool needs cannot be read.** Where the tool can see the failure it
names the store in the result — `standing_unavailable`, `history_unavailable`,
`routing_unavailable` — rather than letting it reach the agent as an empty list that reads
as health; `ok` stays true, because a partial answer honestly labelled is still a successful
read. Where the failure is absorbed below the tool the marker is simply absent, and its
absence is not a claim that the answer is complete: a setup-episode store that fails to load
is reset to an empty one underneath, so the tool reads zero rows successfully and cannot
tell that from a box where no setup has ever run. Which conditions each marker covers, and
the one it does not, is in [`plugin-health.md`](plugin-health.md).

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
- `tests/test_tools_specialist_install.py`

**Related**
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/plugin-setup.md`](../architecture/plugin-setup.md)
- [`architecture/plugin-health.md`](../architecture/plugin-health.md)
<!-- END SOURCEMAP -->
