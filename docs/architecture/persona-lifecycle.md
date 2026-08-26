---
last_reviewed: 2026-08-25
---

# Persona lifecycle

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a persona arrives, is approved, and leaves: checksum-bound consent install, where a
local persona ref may resolve, the image-defaults contract, applying an override,
reference-scan removal and its revocation generations, and the operator-run sweep. It
does not cover what a persona becomes at turn time — roles, bindings, prompt
composition and the refusal record are [`personality.md`](personality.md)'s — nor a
specialist component's own bundle transactions, which are
[`specialist-lifecycle.md`](specialist-lifecycle.md)'s.

## Mental model

**A published persona version is immutable, and installing one is consent-bound.** A bare
persona repo installs through checksum-bound durable operator consent; re-publishing the
same `persona_id@version` with different bytes is refused rather than replaced, and
applying an override to a specialist preserves its component root and configuration. Boot
reconciliation holds the same line: an override is reloaded by `persona_id@version` and the
bytes found on disk must match the binding's pinned checksum — changed bytes under a pinned
version are refused, never silently re-materialized into a fresh binding. Inspection
staging under the personas root is reclaimed on rejection and consumed by a successful
commit; abandoned trees fall to the boot age sweep beside the specialist staging roots.

**A local persona ref resolves only under the approved roots, and every consumer agrees
on where those are.** The ref's id and version segments are validated with the same
patterns every persona path join uses (a traversal-bearing ref never reaches a join),
the loaded pack must *declare* the identity the ref names, and one call-time seam
resolves the installed-personas root everywhere — the install flow's staging and
publication, the apply tools, the resident bindings root, the specialist loader's
override activation (upgrade and rollback included), and the boot staging sweep — so a
pack installed under a custom config root is found by everything that later reads it. A
tool that staged where boot does not look would report success for a swap that never
activates.

**A specialist's own default persona is not one of those roots.** The two approved roots
are the installed-personas root and the image defaults tree; a specialist's
component-default persona is read from that component's CAS store instead, which is
neither. So a component's bundled persona is *not* resolvable by ref — installing a
specialist makes its persona active for that specialist and does nothing else. Whether such
a persona could be applied to a resident is a separate question with its own answer: a fixed
resident slot's compatibility admits only its own slug, so it depends entirely on which
persona the component declares, and nothing stops a component declaring a slot's own.

**The image ships persona packs for the fixed resident slots and nothing else.** Every pack
in the defaults tree is the image default for one of those slots, and each slot's default is
in the tree — the two sets are equal by contract, not by coincidence. The image is not a
distribution channel: specialists and their personas arrive by install, which is why a pack
that no slot defaults to has no reachable consumer at all.

**Removing a persona is decided by who still refers to it, not by who is using it.** A
persona's bytes are needed for as long as any binding tuple that a later load — or a later
*recovery* — can read still names them. That set is wider than "what is active": a staged
`desired` tuple, a specialist's retained prior (its rollback input), the pending-rotation
temporary a failed prior rotation leaves behind, and the tuple bytes captured inside an
bundle journal that would still be *replayed*, whose capture both the tool layer's compensation
and the next boot's journal reconciliation write back verbatim. Removal computes that whole set
and refuses if it is non-empty; there is no force. It is deliberately narrower in two places: a
*resident's* prior tuple is not a reference, because nothing reads one — counting it would pin
the outgoing persona from the moment a reset committed, which is the opposite of what a reset is
for — and a journal that boot would quarantine rather than replay is not a reference either,
which is why whether a journal is replayable is answered by the journal module itself rather
than by a second copy of the rule.

**Unreadable is not unreferenced.** The scan distinguishes "this names no persona" from "this
could not be interpreted", and only the first permits removal. A tuple file that exists but fails
to load, a directory that cannot be walked, a journal that cannot be read — each refuses every
removal, because the same bytes may be perfectly readable at the next boot, and a state that
un-pins a persona by being broken is the boot-fatal failure wearing a disguise. The consent
ledger inverts the same question for the same reason: for the approvals, an unreadable file
reads as none and manufactures no consent, but for the revocation generations an unreadable file
would read as *generation zero*, so mutations refuse it outright rather than accept a baseline
they cannot verify.

**The reference scan and the persona it protects are read under one lock.** Both application
paths resolve a persona pack *before* taking the materialize lock, so removal computes its
references and deletes under that same lock, and the application paths re-prove the pack is still
present with the pinned checksum inside it. Publication does the same: an install re-reads the
operator's approval in-lock before it publishes, so an install that was authorized before a
removal cannot republish the bytes that removal just took away.

**A consent tap in flight is beaten by a generation, not by cancelling the keyboard.** Revoking a
persona's approval cancels its pending keyboards, but an already-answered challenge cannot be
cancelled, and the callback that records the approval runs on the event loop while a removal runs
on a worker thread. The ack ledger therefore carries a per-persona and a per-version revocation
generation; a keyboard captures both when it is posted, and the record is written only if neither
has moved. The wildcard generation is what covers a version that is only *pending* — it appears
nowhere in the ledger, so a per-version counter alone would leave exactly that tap able to
re-create the approval.

**The sweep is never automatic.** Persona versions are immutable, so upgrades accumulate
superseded bytes; the sweep that reclaims them is a tool the operator asks for, not a boot-time
pass. Deleting content the operator approved, with nobody in the loop, is the silent class this
codebase keeps paying for.

## Contracts & invariants

**INV-PERS-005**: The persona packs the image ships are exactly the fixed resident slots' image defaults — no more, no fewer.

What it does not cover: it says nothing about which personas are *available* on a host.
Installed bare personas and specialists' bundled personas are both reachable content that
this invariant does not count, because neither is shipped in the image.

**INV-PERS-006**: an installed persona that any readable-or-restorable binding tuple still names is never removed — single removal and the sweep both refuse it, and a reference set that cannot be computed refuses too.

What it does not cover: it is about *reachable* references, not about use. A persona nothing
names is removable even if an operator still wants it; a reference held only by a replayable
bundle journal blocks removal even though nothing is running it; and a journal boot would
quarantine holds nothing, because quarantine never restores.

**INV-PERS-007**: Applying a persona to a resident stages the binding rather than activating it, and a candidate that fails the compile proof leaves the binding store untouched.

The two halves answer different questions. *Staged* is what makes the tool's own
`restart_required` true: the resident keeps serving its current binding, and boot
reconciliation performs the promotion — through the same candidate validation it applies to
anything else it promotes. *Untouched on failure* is stronger than "not activated": no
staged tuple and no error record are written either, because a staged tuple is something a
later reconciliation would promote.

The proof itself is one function, shared by the loader and the apply path. Two copies of
"does this candidate compile" drift, and the copy that drifts is the one that admits a
binding the loader then rejects.

What it does not cover: a staged binding is not a promise the next boot will run it. If it
stops compiling before then — its bytes changed under the pinned version, say —
reconciliation discards it and retains the last-known-good, exactly as INV-PERS-003
describes. It also says nothing about specialists, which activate on reload rather than
restart.

**INV-PERS-011**: A persona install approval that was recorded at tap-commit, but whose requesting engagement is terminal or gone when the operator taps it, does not leave the DM claiming an install: the recorded approval is not revoked by the failed continuation, and the single approval edit is selected from the reconciliation outcome rather than written before it.

The persona and specialist consent finish hooks are separately written copies of one
shape, so this is the sibling of INV-SPEC-010 and holds for the same reasons — and it
carries the same condition, which bites harder here: the acknowledgement is written at
tap-commit only if the revocation-generation check passes, so a tap that lands after a
`persona_ack_revoke` records nothing and takes the earlier "this approval was not
recorded" branch, which reconciliation never reaches. When it IS recorded, the hook awaits
the reconciliation
callback before it edits, and only a literal `True` selects the success wording. The
corrective wording names a recovery valid from a terminal engagement — start a new
configurator engagement and re-run the install — and says the recorded approval is reused
*if it still applies*, because an explicit revocation legitimately makes a fresh prompt
correct.

What it does not cover: `True` is not a delivery receipt. See INV-SPEC-010 in
`architecture/specialist-lifecycle.md` for exactly what a positive report establishes and
what remains outside it.

## Failure behavior

**An operator applies a persona a resident cannot compile.** Refused at the point of asking,
with the compile failure reported and nothing written — no staged tuple, no error record, no
change to what is active. The distinction that matters is between *refusing* and *recording a
refusal*: a staged tuple would be promoted by the next reconciliation, so an attempt that
leaves one behind has not been refused. Nothing needs undoing afterwards, which is worth
saying out loud to an operator who has just been told their swap failed.

**A removal is refused.** Nothing is mutated — the bytes stay, and so does the install approval,
which is only revoked on a removal that goes through. The refusal names its referrers; freeing
them is the operator's next step (reset or re-apply the agents that name the persona, and restart,
for a resident — a staged reset does not release the old persona until the restart commits it).
An unreadable bundle journal refuses every removal rather than some, and clears itself at the next
boot's journal reconciliation.

**A removal fails after its approval was revoked.** The bytes remain installed and can no longer
be installed on the old approval — one re-approval. This ordering is deliberate: the inverse
leaves a live approval for bytes that are gone.

## Extension points

**A new referrer of persona bytes** — any state a later load or recovery can read that
names a persona — must join the reference scan, or removal will free bytes it still
needs (INV-PERS-006 is exactly that guarantee, and the scan already counts staged
tuples, retained priors, pending rotations and replayable bundle journals).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/persona_install.py::persona_references`
- `casa/rootfs/opt/casa/persona_install.py::require_persona_present`
- `casa/rootfs/opt/casa/persona_install.py::remove_installed_persona`
- `casa/rootfs/opt/casa/persona_install.py::prune_installed_personas`
- `casa/rootfs/opt/casa/persona_install.py::list_installed_personas`
- `casa/rootfs/opt/casa/persona_install.py::apply_persona_override`

**Tests**
- `tests/test_persona_install.py`
- `tests/test_persona_removal.py`
- `tests/test_persona_apply_resident_staging.py`
- `tests/test_persona_pack.py`
- `tests/test_persona_install_consent.py`
- `tests/test_tools_persona_install.py`

**Related**
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
<!-- END SOURCEMAP -->
