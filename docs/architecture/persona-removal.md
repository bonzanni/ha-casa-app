---
last_reviewed: 2026-11-23
---

# Persona removal and the sweep

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an installed persona leaves: the reference scan that decides whether its bytes may go, why an unreadable state refuses, the lock the scan shares with the paths that apply a persona, the revocation generations that beat a consent tap in flight, and the operator-run sweep. How a persona arrives, is approved and is applied is [`persona-lifecycle.md`](persona-lifecycle.md); what it becomes at turn time is [`personality.md`](personality.md).

## Mental model

**Removing a persona is decided by who still refers to it, not by who is using it.** A
persona's bytes are needed for as long as any binding tuple that a later load — or a later
*recovery* — can read still names them. That set is wider than "what is active": a staged
`desired` tuple, a specialist's retained prior (its rollback input — and, after a first
override, the way back to the bundled persona: `specialist_rollback` restores it), the pending-rotation
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

**INV-PERS-006**: an installed persona that any readable-or-restorable binding tuple still names is never removed — single removal and the sweep both refuse it, and a reference set that cannot be computed refuses too.

What it does not cover: it is about *reachable* references, not about use. A persona nothing
names is removable even if an operator still wants it; a reference held only by a replayable
bundle journal blocks removal even though nothing is running it; and a journal boot would
quarantine holds nothing, because quarantine never restores.

## Failure behavior

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
- `casa/rootfs/opt/casa/persona_install.py::remove_installed_persona`
- `casa/rootfs/opt/casa/persona_install.py::prune_installed_personas`

**Tests**
- `tests/test_persona_removal.py`

**Related**
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
<!-- END SOURCEMAP -->
