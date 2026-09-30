---
last_reviewed: 2026-09-16
---

# Plugin removal

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a committed plugin removal discloses about what it leaves behind — whether it is
reported by `plugin_remove`, by the owned-set swap of a specialist bundle, or by a bundle
compensation. It covers the disclosure, not the removal: how a mutation orders its registry
commit against the convergence that follows, and what its envelope may claim about a
plugin's integration, are in [`plugin-mutation-tools.md`](plugin-mutation-tools.md); the
question a plugin that declares an eraser is asked first is in
[`plugin-erasure.md`](plugin-erasure.md); and the bundle transactions whose swap and
compensation the disclosure reports on are in
[`specialist-bundle-transactions.md`](specialist-bundle-transactions.md).

## Mental model

**A committed removal says what it left behind rather than claiming it was cleaned up.** It
discloses that the plugin's CLI-managed persistent data may remain and that no provider
revocation was performed, save for a plugin its own eraser reported completely erased, whose
report it relays instead (INV-TOOL-007). Why the data outlives the removal, and why the
answer is disclosure rather than deletion, are stated with that invariant below. The
envelope that carries the disclosure is the mutation envelope of
[`plugin-mutation-tools.md`](plugin-mutation-tools.md), and INV-TOOL-005 there bounds its
wording.

## Contracts & invariants

**INV-TOOL-007**: A committed plugin removal reported by `plugin_remove`, by the owned-set swap of a SUCCESSFUL specialist bundle — install, upgrade, rollback or uninstall alike — or by a bundle compensation that measured the entry still removed — or that could not read the registry back and says so in the same envelope — discloses that the plugin's CLI-managed persistent data may remain and that no provider revocation was performed, save for a plugin its own eraser reported completely erased, whose report it relays instead (INV-PLUG-037); no removal-path string claims a deletion or a revocation Casa did not perform.

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
inside the plugin-tools mutation lock. A reclassifying upgrade kept after its new version is
already active (`kept_new_version`, INV-SPEC-021) rolls nothing back, so it discloses what its
swap dropped exactly as a success does. The one other `ok:false` envelope whose registry mutation can persist, the
`compensation_failed` arm of a failed bundle sequencer, discloses on measurement rather
than on the flag: a failed compensation does not establish that the removal survived, because
the rollback restores the registry in its first step and then does fallible work, so the arm
reads the registry back and names only the entries actually still absent. That arm also states what now holds — the undo record is
still standing, further changes to that specialist are refused, and a restart is what lets boot
reconciliation finish or quarantine it (INV-SPEC-014) — because it was the one arm of the three
with no such sentence, telling least at the moment an operator can still act. It measures the SAME
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

**A plugin that declares an eraser is asked about first**: the question, the erase episode and
the finishing `erase_data=true` call are [`plugin-erasure.md`](plugin-erasure.md)'s.

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

**A removal path raises after the commit.** There is then no result envelope to carry the
disclosure, and the operator gets a hard error where a persisting removal went unstated.
The gap is bounded and loud rather than silent, and every recipe on a removal-capable path
tells the engager to say the removal may have taken effect, that the plugin's CLI-managed
persistent data was not deleted and no provider revocation was performed either way, and to
check with `plugin_list()`.

**A removal's caller is cancelled after the commit.** The registry write runs in a thread
and a thread cannot be cancelled, so a cancellation cannot stop a removal from committing —
it can only stop the tool from continuing. Everything a committed removal owes DURABLY —
retiring its setup obligation, invalidating its lifecycle grants and trigger consents,
revoking its persisted callback consents and purging its callback spool — therefore runs as
one unit that is drained to completion through repeated cancellation before the tool
propagates it. Only the runtime reload is abandoned, because it is the one step that can be
held indefinitely and no durable consequence may wait on it. That ordering is why the
callback teardown runs before the reload rather than after it: the plugin is already absent
from the registry when the commit returns, and the callback reconcile computes from
registry resolution, so for an absent plugin an earlier revocation can produce no route and
no pending consent. The settlement itself unroutes nothing — the runtime overlay stands
until the reload — and a cancelled removal, like a raising one, returns no envelope.

## Extension points

**Closing the gap a raising removal leaves** is the one extension INV-TOOL-007 names: where a
removal path raises after the registry commit there is no envelope to carry the disclosure,
and closing that "means converting exception handling on these paths into structured
outcomes — a different change, and one that touches every bundle tool rather than the
removal ones."

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_plugin_data_disclosure`
- `casa/rootfs/opt/casa/tools.py::_swap_dropped_names`
- `casa/rootfs/opt/casa/tools.py::_swap_removal_disclosure`
- `casa/rootfs/opt/casa/tools.py::_absent_owned_names`
- `casa/rootfs/opt/casa/specialist_install.py::_removed_owned_names`

**Tests**
- `tests/test_plugin_tools.py`
- `tests/test_tools_specialist_install.py`

**Related**
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
- [`architecture/plugin-erasure.md`](../architecture/plugin-erasure.md)
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
<!-- END SOURCEMAP -->
