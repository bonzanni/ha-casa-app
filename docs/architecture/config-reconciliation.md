---
last_reviewed: 2026-08-18
---

# Config-tree reconciliation

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the config tree on disk is reconciled against image defaults at boot: the ownership
rules that decide what survives an upgrade, the per-entry merge for the explicit set of
list-of-entries files, the `${VAR}` placeholder semantics the loader shares, and the
text declaration carried across an in-process rewrite. The reload scopes that rerun
reconciliation, the version-control whitelist, operator options and secret resolution are
[`architecture/configuration.md`](configuration.md).

## Mental model

**Reconciliation has ownership rules that decide what survives an upgrade.** A default the
operator never touched follows the image — updated in place, deleted if upstream removed
it. An operator-edited file that conflicts with a changed default, or that fails its
schema, is *overwritten by the image* with the prior content preserved as a recovery
artifact. A live file the image does not know is adopted, not deleted. A tracked file the
operator *deleted* is re-seeded from the image — except for paths whose absence is
boot-valid (today exactly the per-agent `delegates.yaml`), where the deletion is honored
while the image copy is unchanged and an image change reintroduces the file; for every
other path the unconditional reseed is what repairs a deleted required file before boot
fatals on it. Predicting which of your edits survive means knowing which of these cases
each file is in.

**Three files resolve per entry rather than per file, and that changes the answer for
them.** A small explicit set — the trigger, delegate and executor lists under each agent —
are lists of named entries for which the shipped copy is a *seed*, not the whole truth.
For these, ownership is decided one entry at a time: a name the image ships tracks the
image, and a name added locally is kept rather than dying alongside it. The same
image-wins rule still settles a name both sides changed. Two consequences are worth
knowing before relying on it. The merge runs only when the file parses into a clean list
of uniquely-named mappings; anything else — a duplicate name, a malformed entry — falls
back to whole-file resolution rather than guessing what was meant. And a merge that has
something to apply rewrites the file, so comments and formatting in it are lost; a merge
with nothing to apply leaves the bytes alone.

**The reconciler reads a file's structure exactly as the components that consume it do.**
It is tempting to make the reconciler stricter than the loader, on the theory that stricter
is safer. It is not: a construct the reconciler refuses but the loader accepts — a YAML
anchor, say — loses the file its entry-level protection while still reaching the loader
untouched, so the strictness buys nothing and costs the operator an entry. It stops at
structure, and deliberately: the reconciler does not resolve `${VAR}`, because it *rewrites*
the file, and resolving first would bake today's environment into it permanently. That has
a consequence worth stating, because it is the one place where reading a file differently
from its consumer is unavoidable: a rewrite re-emits through a plain YAML dump, which does
not preserve quote style or tags, and those are what make a placeholder text. So a file
holding a scalar that is nothing but a placeholder *and* declared text — quoted, or
carrying YAML's string tag — is **not reconciled per entry at all**: refusing sends it to
whole-file resolution, which is loud, rather than silently retyping a locally-added entry
and then dropping it as invalid. Only that shape, because refusing costs a file every
locally-added entry: a placeholder in a comment, one embedded in a larger scalar, and a
plain lone one — none of which a rewrite can change — are all still reconciled per
entry. Aliases are also a
place where "read it as the loader does" is not sufficient on its own: a document that
aliases an ancestor, or that expands astronomically, is refused here rather than met by the
comparisons and dumps further in, which would recurse or never finish. The same
never-guess rule governs how "did anything change?" is answered: a text that does not parse
is the same as nothing, never the same as another text that does not parse, because
treating two failures as agreement makes the reconciler write nothing while advancing the
baseline past the change it was supposed to deliver.

**Refusing is not available to every writer, so the declaration itself is carried across a
rewrite.** One writer of these files cannot refuse: removing a delivered reminder must
always succeed, because the entry *is* the record that delivery is owed and a cleanup that
refuses redelivers forever ([`architecture/reminders.md`](reminders.md)). It therefore
warns and proceeds — and while it did so through a plain dump, the rewrite it performed
erased the very quoting the refusal above tests, so one cancellation silently disarmed that
refusal for every other writer of that file, permanently. The parse a writer uses now keeps
one fact a plain load discards, which scalars the source declared as text, and the dump
re-quotes exactly those. Nothing else about a file's form survives — not comments, not key
order, not which quoting style was used — only whether a scalar consisting solely of a
placeholder was declared text at all, which is the one property that decides what it means.
The two halves are deliberately different mechanisms rather than one: the refusal reads the
file's *tokens*, so it also fires for a declaration the document then discards (a duplicate
key's loser), and a rewrite can only carry what the document keeps. Where they disagree the
file is by definition one whose surviving scalars have nothing at stake, and the writer
says so in the log rather than leaving the change silent.

**An environment placeholder is resolved as its scalar is built, not before the file is
read.** A config file may write `${VAR}` inside a scalar, and it is resolved once the
parser has already decided the document's shape. That order is the whole point: the
variable's contents are never lexed as part of the document, so no value can move a field,
truncate its neighbour, or — in any ordinary field — stop the file loading.

**The scalar's own style decides whether its value is text, and that is YAML's rule, not a
new one.** A quoted placeholder (`prompt: "${DETAIL}"`), or one tagged `!!str`, is a
string — quoting is how YAML says "this is text", and it is what an author reaches for when
a value might contain punctuation. **That is the form the guarantee attaches to: its value
arrives exactly as it is, whatever it holds.** A placeholder with text around it is a
string too; it always was. An unquoted lone placeholder (`minutes: ${M}`) means what its
resolved text means as YAML read on its own, so a number is a number and a list is a
list — substantially what it meant before any of this changed, and what keeps a
list-valued variable from reaching a consumer as a string it then iterates character by
character. Nothing filters that reading by what it turns out to be: three attempts to do so
each guessed at what an author meant and each opened a different hole. Two departures from
plain YAML, both narrow: a value that is not a valid YAML document on its own is read as
text rather than raising, and it is read *on its own*, so it cannot refer to an anchor
defined elsewhere in the file. A placeholder under any tag other than `!!str` never reaches
this at all — PyYAML builds those itself and fails on the unresolved text.
An unset variable leaves its placeholder in place, which is what lets a document be
validated without the environment that will eventually fill it in.

**Deciding which strategy a file gets is a deliberate act, not a property of its shape.**
The set is a written table, because a file being a list of objects is not what makes
entry-level resolution safe — entries being independent of each other is, and that is a
fact about these three files rather than about their schemas.

## Contracts & invariants

**INV-CFG-005**: Reconciliation of the config tree is never boot-fatal.

Enforced by catching everything and returning success, and again by the caller tolerating
failure. Problems are recorded rather than raised.

What it does not cover: a recorded residual problem can still cause a later failure when
something tries to load what was left broken.

**INV-CFG-006**: In a file reconciled per entry, an entry whose name the image has never shipped is preserved, except when the image ships that name for the first time.

Enforced in the reconciler's entry-level branch, which resolves each name against the
previous image's copy rather than against the file as a whole.

What it does not cover, and each case is real: the exception is the collision — a local
entry *is* displaced when a future image claims its name, so the guarantee is not
ownership-neutral. An entry that fails the current schema is dropped by the backstop
below. And a file that does not parse into a clean list of uniquely-named mappings is not
reconciled per entry at all, so nothing in it is protected. Every one of those is backed
by a recovery copy, a report record and an operator notification, but in each the running
configuration has lost the entry. Nor does it reach a file the reconciler *adopts*: the
image has never shipped it, so there is nothing to reconcile it against and it is passed
through untouched — reported if it fails its schema, since nothing here can repair it.

A preserved entry can also be dropped without any schema failing: the
post-reconciliation boot-parity pass. Two files can each satisfy their own schema and
together stop an agent loading — a kept delegate entry against a runtime that no longer
allows the delegate tool, a kept trigger against a runtime that no longer declares its
channel. Per-file validation cannot see that. The boot-parity pass can, and it reverts
**every** entry-level merge — not the guilty one, which it does not try to identify —
restoring what whole-file resolution would have produced rather than leaving a system
that will not start. Merges that were not the cause are reverted with it; that is the
deliberate price of not searching for the guilty one and leaving a half-reverted tree
behind a failed restore. Each file is preserved before it is touched — and a file that
cannot be read or backed up is left alone rather than reverted, so the sweep is
best-effort per file rather than atomic across them. Every revert is reported and
notified.

**INV-CFG-007**: Reconciliation never writes a config file that fails its schema.

Enforced by validating the composed document before it replaces anything, and again on
the serialized bytes. A merge that cannot be shown valid is abandoned in favour of
whole-file resolution rather than written.

What it does not cover: files reconciliation does not write. A file adopted because the
image does not know it is never rewritten, and a file kept live because the image did not
change it is only checked by the backstop.

**INV-CFG-008**: Entry-level reconciliation only ever writes a `schema_version` that already appears among its input documents; it never synthesizes or migrates one.

Enforced by composing the merged document from the live file's own top-level fields,
falling back to the shipped default's only when the live one would make the result
invalid.

What it does not cover: it constrains the reconciler, not the writers. Different producers
of the same file may emit different versions — the configurator writes a newer trigger
schema version than the shipped default does — and nothing here reconciles that
disagreement. Carrying content *across* a version is migration, which does not exist yet
([#402](https://github.com/bonzanni/ha-casa-app/issues/402)).

**INV-CFG-009**: A `${VAR}` placeholder is resolved once its scalar is being built, so a variable's contents are never lexed as part of the document, and a quoted placeholder's value reaches its field unaltered.

Enforced in the loader's own YAML constructor — the last point at which the scalar's style
is still known — in the one reader every consumer that RESOLVES these files shares. The
reconciler is not one of those: it reads the same files structurally and never resolves
them, because it rewrites them, and it refuses per-entry reconciliation to a file holding a
placeholder written as text for exactly that reason (see the mental model above).

What it does not cover, and the boundary is exact rather than hedged. The guarantee about a
value's *contents* is the quoted form's alone: an unquoted lone placeholder means what its
text means as YAML, so its value can still be truncated at a `#`, folded, or read as a
mapping — unchanged from before, and the reason to quote. Nor does the invariant say a file
always loads: a placeholder resolving into a position YAML cannot accept still fails, and
loudly — a mapping key that resolves to a list is unhashable, a scalar under a tag other
than `!!str` never reaches this at all (PyYAML builds those itself, so `!!int ${VAR}` fails
on the unresolved text while `!!null ${VAR}` quietly becomes null), and a placeholder
written where the file's own YAML needs
punctuation (unquoted inside a flow collection, where `{` is an indicator) is a parse error
the environment cannot rescue, because the shape is fixed before any variable is read. What
*is* ruled out is the case that made this worth fixing: an ordinary value, in an ordinary
field, deciding whether the file parses at all. Two residues of reading a value on its own
rather than in place, both narrow. A variable holding a *self-contained* anchor and alias
(`&a [*a]`) still produces a self-referential value, which loads and then fails in whatever
walks it ([#442](https://github.com/bonzanni/ha-casa-app/issues/442) is the consumer-side
half); one holding a bare alias to an anchor defined elsewhere in the file cannot see it,
and reads as text. And a value that is a document marker rather than a scalar (`---`) reads
as the empty document it is, where parsing it in place would have made it the string.

**INV-CFG-010**: An in-process rewrite of a config file re-emits a scalar the source declared as text and whose whole value is a `${VAR}` placeholder still declared as text, so the rewrite changes neither what that scalar means nor whether the refusal above applies to the file afterwards.

Enforced in the writers' own load/dump pair, which records the declaration on the node and
re-quotes it on the way out; a scalar the source left plain stays plain, since quoting that
one would retype it in the other direction. It binds the rewrite, not the file: what the
document *discards* — a duplicate key's loser, an overridden merge donor — carries no
declaration to preserve, and that is the one case in which a rewrite ends with the refusal
no longer applying. Nothing surviving in such a file can have changed meaning, and the
writer logs the transition rather than performing it silently.

## Failure behavior

**Reconciliation fails.** Absorbed and logged. A recovery artifact is preserved — a
pre-reconciliation commit where the repository is usable, and a backup copy otherwise — so an
overwritten local edit is recoverable.

## Extension points

**A new default tree** must be added to the reconciler's list *and* to the version-control
whitelist separately. Neither implies the other.

**A new list-of-entries file** does not get entry-level reconciliation by being shaped like
one. It must be added to the reconciler's table with its list key and the field that names
an entry, and the question to answer first is whether its entries are genuinely
independent — if one entry's meaning depends on its neighbours or on their order, merging
them separately is wrong and whole-file resolution is the correct strategy.

**Tightening a schema** is not a local change to that schema. There is no migration
mechanism, so live content that fails the new shape is dropped rather than carried
forward; files the reconciler adopts are never repaired at all and will stop the next boot;
and any writer that emits a version literal has to move with it.
[#402](https://github.com/bonzanni/ha-casa-app/issues/402) carries the constraints.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/config_sync.py::reconcile`

**Tests**
- `tests/test_config_sync_backstop.py`

**Related**
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/reminders.md`](../architecture/reminders.md)
<!-- END SOURCEMAP -->
