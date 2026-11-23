---
last_reviewed: 2026-11-23
---

# Personality: the role checksum and a re-derived binding

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Why a role's checksum covers the model it resolved to, what that asks of every path that materializes a role for a persisted binding, and how an installed specialist's binding whose role checksum moved for model reasons alone is re-derived rather than dropped. Roles, personas, bindings and the prompt served at turn time are [`personality.md`](personality.md); a rollback's re-derivation of its retained prior is [`specialist-bundle-transactions.md`](specialist-bundle-transactions.md) (INV-SPEC-012).

## Mental model

**A role's checksum covers the model it actually resolved to, so everyone must resolve it the
same way.** A role artifact may declare its model as an operator option rather than a fixed
value, and the checksum hashes the resolved result alongside the declaration — deliberately,
so flipping the option produces a new identity and a new session epoch. The consequence is a
trap: any code path that materializes a role for a binding that will be PERSISTED, or
checked against one that was, has to resolve that option exactly as the loader will.
Materializing with no options resolves the
declared *default*, which silently produces a binding the loader can never re-derive, and the
specialist is then dropped as a binding-activation failure on a host whose option differs
from the default. Install, upgrade, rollback, persona override and the reconcile pass all
share one resolution helper for this reason.

**A binding whose role checksum moved for model reasons alone is re-derived, not dropped.**
The other face of that design: an operator flipping the model option, or an image release
moving a model alias, moves every installed specialist's role checksum while nothing else
about the install changed. The specialist loader treats exactly that case as a re-compile
rather than a broken specialist. It proves the installed component's bytes still hash to
the tuple's root, proves the role it is loading is that component's role under the current
option resolution, and then re-derives the binding by carrying every stored field forward —
the persona identity triple included — and recomputing only the role checksum and the
digest derived from it. The compile that follows compares the carried persona identity
against the pack it loaded, so a persona that moved is still refused, never re-bound. On a
committing load of an enabled specialist the re-derived binding is written back in place,
same generation: the retained prior and any staged candidate are untouched. A validation-only
load, and a disabled specialist, re-derive in memory and write nothing. Residents already
reconcile against the role currently loading; this puts installed specialists on the same
footing without touching consent, because the component root the approval bound is exactly
what the re-derivation proves unchanged.

## Contracts & invariants

**INV-PERS-016**: An installed specialist whose active binding's role checksum differs from the role the loader materialized — with the component root, persona identity triple, configuration and dependency closure unchanged — is activated on a binding re-derived for that role; on a committing load of an enabled specialist the re-derived binding replaces the active tuple in place, leaving the retained prior and any staged candidate untouched, while a validation-only load and a disabled specialist write nothing; and a binding whose persona identity or agent id differs from what the loader resolved is refused, never re-derived.

Three checks gate the re-derivation, in order: the install root digest recomputed from the
component store equals the tuple root's suffix; the component's role artifact, materialized
under the current option resolution, is the role being loaded — so a stale roles overlay
refuses rather than binding a freshly upgraded tuple to an older role; and the stored agent
id is the role's. The candidate carries every stored binding field and recomputes only the
role checksum and its derived digest, and it must compile before anything is written. The
write is a same-generation primitive that re-observes the active tuple under the
materialize lock, refuses on any concurrent change, and admits no delta but those two
fields — so it cannot become a way around prior rotation for a real generation change.

What it does not cover: the retained prior, which is preserved as it was and re-derived
the same way by the rollback that restores it (INV-SPEC-012). Nor the operational files:
the marker they carry is rewritten by the next reconcile pass, not by the load. Nor
engagement resume: nothing here compares a suspended engagement's recorded epoch against
the definition it resumes under.

## Failure behavior

A binding whose persona identity or agent id moved is not re-derived: it fails as any binding that cannot be activated does ([`personality.md`](personality.md)).

## Extension points

A new path that materializes a role for a binding that will be persisted, or checked against one, uses the shared resolution helper the paths above share.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/role_slot.py::resolve_role_model`
- `casa/rootfs/opt/casa/personality_binding.py::rederive_binding_for_role`
- `casa/rootfs/opt/casa/specialist_install.py::_rederive_stale_binding`

**Tests**
- `tests/test_specialist_binding_rederive.py`

**Related**
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
<!-- END SOURCEMAP -->
