---
last_reviewed: 2026-09-01
---

# Webhook trigger secrets

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The secrets that authenticate webhook triggers: who mints one and when, what proves who
minted it and for whom (the receipt), what a route may authenticate with, when a secret
is retired and when it never is, and what the reload report states per slot. It does not cover how a request is verified — the three modes and `verify` are
[`http-surface.md`](http-surface.md)'s — nor a plugin trigger's declaration, consent or
secret backing, which are [`plugin-triggers.md`](plugin-triggers.md)'s.

## Mental model

The secrets themselves have a lifecycle worth knowing, and the two halves of the surface do
not share one. **Plugin** trigger secrets are minted *per trigger identity* — bound to the
plugin artifact, so a plugin update means a new identity and a fresh secret — and are
retired when the artifact's grant is revoked, so a later artifact cannot inherit a
credential. **Resident** trigger secrets are minted per NAME, when the trigger is
registered: at boot and on every reload that installs triggers, never on the request that
verifies one. The names are globally unique, and a name is the whole identity — which is
why a resident secret's lifetime is defined by its **route**, not by its declaration.

**A Casa-minted resident secret lives exactly as long as the route it was minted for.**
A resident mint writes a **receipt** next to the slot before it links the value: a digest
of the minted bytes — never the value — and the **role** the mint registered the name
for. It certifies only while the live bytes still hash to it, so a hand replacement, a
rotation promotion or a restore from backup silently stops it certifying, and a receipt
that is missing, malformed, from an earlier format or unreadable reads as *unproven*
rather than as consent. That receipt is the only authority anything here ever acts on.
Two things stand on it. The wildcard handler, for a Casa-owned resident route, reads the
slot through a *certified read*: the bytes are returned only when the receipt certifies
them for the role that routes the name, so bytes Casa cannot prove it minted — or minted
for another role — are an empty secret and a `401`, never destroyed. And a registration
retires: when a role's routes are re-registered from a declaration that no longer backs a
name with a per-trigger secret (deleted, renamed, `type` changed, `auth.mode` set to
`hmac_body`), the slot its receipt certifies for that role goes; when another role is
about to route a name, a slot certified for the previous role goes first; and at boot,
the certified slots of a role whose agent directory is gone go. Nothing else retires:
not a teardown (a disabled specialist keeps its slots and re-registers the same
credentials when re-enabled), not an eviction (its slots wait, unroutable, for the next
boot or registration), and never a sweep of what happens not to be declared.

Registration, retirement and publication are one operation. A role's re-registration
validates every entry, retires what the receipts no longer entitle, installs its jobs and
only then publishes its webhook routes; a retirement the filesystem refuses aborts the
registration with nothing published — the role is left with no routes and the reload names
the slot — and a slot whose live bytes survived is retried at the role's next
registration. A crash or a later fault cannot leave a route serving a slot that must
not authenticate it; what it can leave is stated below. Two consequences follow. A
trigger deleted and recreated with a reload between gets a fresh credential, because its
route went away; deleted and recreated with none between, it keeps its credential, because
the route never left service. And an owner flip on an existing trigger is still not
supported: a casa-minted token also satisfies the provider validation rule, so flipping
`secret_owner` carries the live value over rather than replacing it — delete it and create
it under a new name.

A resident webhook trigger's **secret exists from the moment the trigger is
registered**, which is what makes the setup instruction true when it is given.
`static_header` and `timestamped_hmac` are backed by a per-trigger file; `hmac_body` rides
the one global secret and writes nothing. Casa mints only what it owns: a slot declared
`secret_owner: provider` is never written, because Casa can neither regenerate nor import
that value. The mint only ever creates a file that is absent, and a receipt is written only
for a slot the mint itself created — a value a later pass merely finds is never certified.
A receipt that cannot be written is a mint that does not happen, reported as one, and the
next pass retries it. There is **no dual-accept window**: the verifier takes a single
secret, and the rotation state machine has no caller outside its own tests.

## Contracts & invariants

**INV-TRIG-014**: A mint receipt certifies that Casa generated the bytes in a slot only when Casa itself created that slot and the receipt's digest matches those exact bytes; every other state is unproven and is never authority to alter the slot.

**INV-TRIG-016**: A Casa-owned resident webhook route authenticates only with bytes whose mint receipt certifies them for the role that routes the name; a Casa-minted resident secret is retired when its role's routes are next successfully re-registered from a declaration that no longer backs the name with a per-trigger secret, when another role stages the name for registration, or at a boot that finds the role's agent directory absent; a retirement the filesystem refuses aborts that registration — the role is left with no routes, the reload names the slot, and a slot whose live bytes survived is retried at the role's next registration — nothing is published over a slot certified for another role, and an aborted registration never retires a slot certified for the registering role under a name its declaration still backs with a per-trigger secret; a slot Casa cannot prove it minted is never destroyed, and a disabled specialist's slots are never touched.

What INV-TRIG-016 does not promise, stated because the alternative is a reader assuming
it. Retirement is per name, not a transaction: a registration aborted or interrupted part
way may already have retired some of the names its own declaration stopped backing, and —
once it reached the slots certified for other roles under the names it was about to
route — any of those; a predecessor whose route was already gone loses a credential Casa
can regenerate, and gets a fresh one at its own next registration. An orphan receipt left
by a refused receipt unlink certifies nothing and is replaced by the next mint under that
name. At boot, an artifact that cannot be read, or an inventory that cannot be enumerated,
is left in place with a warning, and a role directory recreated afterwards with the same
declaration reuses the credential. A provider-owned trigger authenticates with whatever its
slot holds: a name that once belonged to another role whose slot Casa could not certify is
inherited by a provider successor, because Casa cannot attribute an uncertified value — the
cure is an import surface that binds the value to the role, which does not exist. And the
scheduler's own partial-failure contract is unchanged: once validation and retirement have
succeeded, an install-time fault on a later entry leaves jobs installed before it possibly
fired and a job the unwind cannot remove live and named in the error.

## Failure behavior

Every reload envelope that touches registration carries `trigger_secrets`, one
row per trigger, plus counts. The rows are derived from the **registry** — what a request is
actually verified with — and from a real read of the file, never from the declaration alone:
those are different questions, and a route can run at a clearance or an auth mode the file
no longer says. Each row is read from one route record, the same one the handler reads, so
a re-registration between two reads cannot describe a route that never existed. A row
states what a request would do, so `readable` means bytes are present that the route
would authenticate with — for a Casa-owned resident route, bytes its receipt certifies for
the role that routes the name — and carries `provenance`, read against its owner: under
`provider`, `casa_minted` says the route authenticates with a Casa token rather than the
credential the operator meant to supply. Bytes that are present but that Casa cannot prove
it minted for the routing role are `unproven_blocked`: requests are refused, nothing is
destroyed, and the remedy is to delete the file on the host or use a different trigger
name — the row a name shows after an upgrade from a version that minted without receipts,
and the row a `misrouted` name shows in its nested probe when the receipt names the
declaring role rather than the routing one. `awaiting_import` says plainly that no Casa
surface can place a provider secret; `invalid` and `unreadable` say plainly that the file
cannot be repaired or removed through any Casa surface. The report rides the error envelope
too — it exists to explain a failed pass, so withholding it on failure would withhold it
exactly when it is wanted.

A retirement is reported by name on the success envelope (`trigger_secret_retired_<name>`;
the `agents` sweep, which aggregates per role, also names a refused one). A refused
retirement is a `reregister_failed` error whose message names the slot; the role is left
with no triggers until a reload retries it. A mint that could not write its receipt is
`trigger_secret_mint_failed_<name>` and the route `401`s until the next pass. At boot, a
role whose retirement was refused is unwound before the server starts, and a directory-gone
sweep that met an unreadable artifact or inventory leaves it in place with a warning.

## Extension points

**A new resident-secret mint path** inherits the rules above: a mint only ever creates a
file that is absent, a receipt is written before the value is linked and only for a slot
that mint itself created, and no state a receipt cannot prove is authority to alter a slot
(INV-TRIG-014). A writer that certified bytes it merely found would certify the operator's
own credential as Casa's.

**A new removal path** is not a new retirement site. Retirement happens in exactly two
places — the pre-install hook of a role's route registration, and boot's directory-gone
sweep — and both act only on a receipt Casa wrote (INV-TRIG-016). A path that observes a
declaration change (a tool, the reconciler, a hand edit) needs nothing: the credential
follows the route, and the next registration or restart re-derives what to retire from the
receipts. Retiring at the observing writer instead would rotate a live credential on an
edit that is reverted before it is ever applied.

**A new consumer of a route** reads `webhook_route` once and takes every field from that
record; two getters read across a concurrent re-registration describe a route that never
existed.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/resident_trigger_secrets.py`

**Tests**
- `tests/test_resident_trigger_secrets.py`
- `tests/test_webhook_secrets.py`
- `tests/test_webhook_mint_receipt.py`
- `tests/test_resident_trigger_retirement.py`

**Related**
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/plugin-triggers.md`](../architecture/plugin-triggers.md)
<!-- END SOURCEMAP -->
