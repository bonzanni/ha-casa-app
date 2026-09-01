---
last_reviewed: 2026-07-29
---

# What may be written down here

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

This file states the one rule that decides whether a fact belongs in this repository at
all. It applies to every file under `docs/`, to source comments, to commit messages, to
pull-request titles and bodies, and to branch names. It does not describe how to write a
good document — that is [`../contributing/doc-contract.md`](../contributing/doc-contract.md).

## Mental model

This repository is public. Everything committed here is published, and everything
published stays published: once an object reaches a public remote it is fetchable by its
hash regardless of what happens to the branch that carried it. Deleting a file, force-
pushing a branch, or rewriting history does not unpublish anything.

So the question is never "can this be removed later". It is "may this exist at all".

## Contracts & invariants

**INV-PUB-001**: A fact belongs in this repository only if it is verifiable from the public commit alone — with no operator, no production system, and no private repository.

That is the whole rule. Everything below is it, applied.

**What the rule excludes.** Anything describing one specific installation rather than the
software: an installation's hostnames, addresses and network layout, credentials of every
kind and references that name where a credential lives, chat, group and channel identifiers,
and an operator's deployment arrangements. The software's *own* topology is different — the
ports the manifest publishes, the listeners the shipped nginx config defines, a loopback
bind in the source — all of that is already in the public commit and is exactly what these
documents describe. Also excluded: roadmaps, plans, design specifications, review notes,
incident write-ups and captured agent transcripts. Personal addresses and contact details,
including in commit metadata and branch names.

**INV-PUB-002**: Doctrine states the mechanism, never the incident.

"Never rebind a module's `asyncio.sleep`" is publishable *with its mechanism* — it replaces
the shared module attribute, so an unrelated `while True: await asyncio.sleep(...)` loop in
the same process spins at CPU speed under a mock whose call list grows without bound. What
must not be written down is how anyone came to know that: whether something broke, when, or
to whom. The mechanism is what makes the rule checkable and the mechanism stays true, so the
history adds nothing a reader can act on.

A sentence asserting that a failure once happened publishes exactly the class of fact this
rule argues against — and it is the easiest sentence to write while arguing it. The rule is
easier to state than to follow.

**INV-PUB-003**: When a claim cannot be checked from the commit, stop and ask. Do not guess, and do not paraphrase around it.

This is the invariant that gets broken quietly. A sentence like "the supervisor grants this
at boot" is either verifiable from code in this repository or it is not. If it is not, the
honest options are to check, to cut it, or to ask — never to soften the wording until it
sounds safe.

**INV-PUB-004**: With no override set, for a branch update that is not a deletion, the pre-push hook refuses unless every commit it enumerates is in the attested reviewed set; it enumerates the pushed tip excluding the destination's current sha for that ref, when the ref already exists, and every branch head the destination advertises that resolves locally, and when nothing can be subtracted, or the enumeration itself fails, it enumerates the whole local ancestry.

Coverage is asked of the destination itself, never of this clone's tracking refs: a
`refs/remotes/<name>/*` entry records what this clone last saw, and a repointed or reset
remote turns it into a claim about nothing. Because the subtraction is against every branch
the destination advertises rather than the one tip being replaced, a set reviewed against the
destination's current `main` is accepted whichever tip the branch previously held, while a
commit the destination holds on no branch is refused by name unless it was reviewed. What it
does not cover: an advertised head that reaches commits nobody here has read is already
public at that destination by hash — the hook guards publication, not the review of what is
published — and a head advertised but never fetched is skipped, which loses nothing because a
clone holds every ancestor of its own commits.

## Failure behavior

Three automated layers refuse what they can recognise, and none of them is the rule:

- the pre-commit guard refuses staged paths and added lines matching generic patterns;
- the pre-push gate sweeps the endpoint tree, every unpublished commit, their messages,
  runs a pinned secret scanner over both tree and history, and refuses a change that
  alters a documented surface without updating or explicitly waiving the document that
  claims it — where "unpublished" is judged against the branches the destination
  advertises, not against the tip being replaced or this clone's tracking refs
  (INV-PUB-004);
- CI repeats the generic layers on every pull request.

What they cannot do is recognise a paragraph that is confidential without containing a
secret. Patterns match shapes; prose has none. That gap is why the gate also requires a
human to read every introduced file before anything is pushed, and why an attestation
recording that read is what the push hook actually checks.

Know the layers' limits as layers, too: the hooks run only where they are installed and git
lets a determined pusher bypass them; the pre-push gate honours an explicit override
variable for emergencies; and CI runs from the pull request's own checkout, so a PR can
weaken CI's copy of the guard. They are defence in depth around a human rule, not a
guarantee. And the rule's reach is the *commit*: pull-request titles and bodies are GitHub
metadata the gate never sees, so applying the rule there is a review habit, not a swept
surface.

A guard that refuses your change is not an obstacle to route around. Loosening a pattern to
make a commit succeed inverts the control: the pattern existed because someone decided that
shape must not be published, and the commit is the thing in question.

## Extension points

Adding a rule means adding a generic pattern to `.githooks/deny-patterns.txt` — generic,
because that file is public and an exact private string written into it is published by the
very commit that adds it. Exact strings belong in a private supplement supplied through
`CASA_DENY_SUPPLEMENT`, which CI deliberately does not have.

Before adding a pattern, check it against the whole tree. A rule that fires on legitimate
content will be disabled by whoever it blocks first, which is worse than not having it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `.githooks/pre-commit`
- `.githooks/deny-patterns.txt`
- `scripts/deny-sweep.sh`
- `scripts/gate.sh`
- `scripts/attest.sh`
- `.githooks/pre-push`

**Tests**
- `tests/test_deny_sweep.py`
- `tests/test_leak_guard.py`
- `tests/test_gate_hook.py`

**Related**
- [`contributing/doc-contract.md`](../contributing/doc-contract.md)
<!-- END SOURCEMAP -->
