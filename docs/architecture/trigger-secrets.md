---
last_reviewed: 2026-08-25
---

# Webhook trigger secrets

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The secrets that authenticate webhook triggers: who mints one and when, what proves who
minted it (the receipt), what the reload report states per slot, and what is not
retired. It does not cover how a request is verified — the three modes and `verify` are
[`http-surface.md`](http-surface.md)'s — nor a plugin trigger's declaration, consent or
secret backing, which are [`plugin-triggers.md`](plugin-triggers.md)'s.

## Mental model

The secrets themselves have a lifecycle worth knowing, and the two halves of the surface do
not share one. **Plugin** trigger secrets are minted *per trigger identity* — bound to the
plugin artifact, so a plugin update means a new identity and a fresh secret — and are
retired when the artifact's grant is revoked, so a later artifact cannot inherit a
credential. **Resident** trigger secrets are minted per NAME, when the trigger is
registered: at boot and on every reload that installs triggers, never on the request that
verifies one. Nothing retires a resident secret, so a trigger recreated under an old name
inherits the old credential; the names are globally unique, and a name is the whole
identity.

That inheritance is still open, and the reason it is hard is worth stating: the owner rules
accept a Casa-minted token as a valid *provider* value too, so nothing about a file's shape
says who wrote it — and Casa can neither regenerate nor import an operator's credential. A
retirement that guessed wrong would destroy something unrecoverable. So a resident mint now
writes a **receipt** next to the slot, holding a digest of the minted value and never the
value itself. It is written only when that mint actually created the file: the publish step
keeps whoever reached the name first rather than clobbering them, and the winner can be an
operator placing a credential by hand, whose bytes must never be certified as Casa's.
A receipt is likewise never written over a value a later pass merely found.

Because it binds the value rather than the name, it stops certifying the moment the bytes
change — by a hand replacement, by a rotation promotion, by anything — with no path needing
to notice; and a receipt that is missing, malformed, stale or unreadable reads as *unproven*
rather than as consent. Failing to write one is not an error: the secret already works, and
only the proof is missing. None of this retires anything. It records the durable fact a
later, owner-aware retirement would have to stand on, and the reload report surfaces it per
slot.

Two things this paragraph used to claim, and the code does not do. There is **no
dual-accept window**: the verifier takes a single secret, and the rotation state machine
has no caller outside its own tests. And a resident credential **can** survive a
declaration change — a casa-minted token also satisfies the provider validation rule, so
flipping `secret_owner` carries the live value over rather than replacing it. Changing the
owner of an existing trigger is not supported; delete it and create it under a new name.

Its **secret exists from the moment the trigger is registered**, which is what makes the
setup instruction true when it is given. `static_header` and `timestamped_hmac` are backed
by a per-trigger file; `hmac_body` rides the one global secret and writes nothing. Casa
mints only what it owns: a slot declared `secret_owner: provider` is never written, because
Casa can neither regenerate nor import that value. Minting runs *after* registration — the
cross-role name-collision check lives there, and minting first would write into another
role's slot for a registration about to be refused — and it only ever creates a file that is
absent. It never deletes, replaces or overwrites one, and it never raises: a filesystem
fault leaves the trigger registered and refusing requests rather than silently changing
which routes exist.

A mint also records a **receipt** beside the slot — a digest of the value, never the value —
and only for a slot the mint itself created, never onto one it merely found. A Casa token
also satisfies the provider rule, so a receipt written over a value Casa did not mint would
certify the operator's own credential as Casa's. See [`http-surface.md`](http-surface.md)
for the mechanism.

## Contracts & invariants

**INV-TRIG-014**: A mint receipt certifies that Casa generated the bytes in a slot only when Casa itself created that slot and the receipt's digest matches those exact bytes; every other state is unproven and is never authority to alter the slot.

Nothing retires a resident secret, so a recreated name still inherits. This records the
fact a later, owner-aware retirement would have to stand on.

## Failure behavior

Every reload envelope that touches registration therefore carries `trigger_secrets`, one
row per trigger, plus counts. The rows are derived from the **registry** — what a request is
actually verified with — and from a real read of the file, never from the declaration alone:
those are different questions, and a route can run at a clearance or an auth mode the file
no longer says. A row states what a request would do, so `readable` means bytes are present
that satisfy the owner's rule, not that the integration works — which is why a `readable`
row also carries `provenance`, read against its owner: under `casa`, `unproven` says Casa
cannot prove it minted the bytes that authenticate — not that it did not, since a receipt
that failed to write reads the same; under `provider`, `casa_minted` does prove the route
authenticates with a Casa token rather than the credential the operator meant to supply.
`awaiting_import` says
plainly that no Casa surface can place a provider secret; `invalid` and `unreadable` say
plainly that the file cannot be repaired or removed through any Casa surface. The report
rides the error envelope too — it exists to explain a failed pass, so withholding it on
failure would withhold it exactly when it is wanted.

## Extension points

**A new writer of a secret slot** inherits the rules above: a mint only ever creates a
file that is absent, a receipt is written only for a slot that mint itself created, and
no state a receipt cannot prove is authority to alter a slot (INV-TRIG-014). A writer
that certified bytes it merely found would certify the operator’s own credential as
Casa’s.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/resident_trigger_secrets.py`

**Tests**
- `tests/test_resident_trigger_secrets.py`
- `tests/test_webhook_secrets.py`
- `tests/test_webhook_mint_receipt.py`

**Related**
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/plugin-triggers.md`](../architecture/plugin-triggers.md)
<!-- END SOURCEMAP -->
