---
last_reviewed: 2026-08-01
---

# Configuration, reload and secrets

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Where configuration comes from, what is version-controlled, what a running system can pick up
without restarting, and how secrets are resolved. It does not cover what any individual
option means — the app manifest and its translations are the authority on that.

## Mental model

**There are two configuration worlds and they behave completely differently.**

The first is the **app manifest**: options an operator sets in Home Assistant. These are read
by the supervisor at service start, exported into the environment, and consumed once during
startup. **Changing one requires a restart.** Nothing reloads them.

The second is the **config tree on disk** — agents, policies, bindings, specialists — which
is reconciled against image defaults at boot and can be reloaded in-process afterwards.

**Reload is scope-specific, and it is not a restart.** The registered scopes — triggers,
agent, agents, policies, plugin_env, executors, config_sync, and full (the set INV-CFG-001
pins) — each rebuild a defined slice: executors rebuilds the executor registry with a
resident cascade, and config_sync reruns reconciliation then cascades agents and policies.
There is no
scope that rereads manifest options, reconstructs channels, or re-reads arbitrary files. If
your change is an operator option, reload will not help you, and this is the single most
common wrong expectation in this area.

**A full reload is exclusive but not atomic.** It takes a writer lock that excludes every
other reload, then runs its steps in order — and there is no rollback across them. A failure
partway leaves earlier steps applied. The lock prevents interleaving, not partial
application. It also omits the on-disk reconciliation entirely, and omits plugin environment
unless explicitly asked.

**The config tree is a git repository, but only a whitelist is tracked.** Agents, policies,
bindings, schema, and specific registry files are versioned; plugin stores, staging areas,
the environment file and general working state are not. The whitelist is the authority, and
it is duplicated in the boot script — both must agree.

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

**Some identity changes cannot be hot-swapped at all.** If a resident's identity changes, the
reload path returns a restart-required outcome *before* mutating live state rather than
attempting a swap.

**A specialist reload consumes the roles overlay after the lock that built it.** The
overlay rebuild runs under the personality materialize lock, but the agent load that
consumes it cannot hold that lock (resident loads re-acquire it internally), so a
concurrent install or upgrade can swap the active tuple in the gap. A load that fails in
that gap rebuilds the overlay once and retries before surfacing an error. The overlay
itself stays a shared, destructively rebuilt path — not a per-reload snapshot — so a
mutation committing after a successful load can still advance what the following registry
refresh reads; that state is internally consistent and converges, because reloads
serialize per scope and the mutating flow's own sequencer re-runs the refresh last.

**Secret indirection covers every password-typed option.** Exactly four options resolve an
external `op://` secret reference at startup — the Claude OAuth token, the Telegram bot
token, the webhook secret and the context7 API key. Resolved values are cached for the
process lifetime by the reference string; the plugin-environment reload scope invalidates
that cache first, so rotating a referenced field takes effect on reload for plugin
variables but requires a restart for the four startup options. Every successful reload
dispatch — not just this scope — kicks the plugin setup-episode worker, because both the
secrets landing (this scope) and any agent-reconstructing scope can make a setup episode
held under INV-PLUG-008 dispatchable. The webhook secret is also
resolved by the Supervisor discovery publisher, so the companion integration signs with
the same value the add-on verifies.

## Contracts & invariants

**INV-CFG-001**: Exactly eight reload scopes exist, and none of them rereads the app manifest options.

Maintained by the module-level registration calls in the reload module. The set is not
mechanically closed — the registry is a plain dict and accepts any scope string — so
"exactly eight" is the current, deliberate count of registrations, held by review and the
pinning test rather than by an enum.

What it does not cover, and it is the point of stating it: no scope reloads operator options,
global channel setup, process environment generally, or arbitrary files in the config tree.

**INV-CFG-002**: A full reload excludes every other dispatched reload for its duration.

Enforced by a reader/writer lock, with non-full scopes serialized per scope key.

What it does not cover: the sequence is not transactional. There is no rollback across its
steps, so a mid-sequence failure leaves earlier steps in effect. A handler called directly,
outside the dispatcher, takes no lock for *itself*; the cascading handlers (policies,
executors, config_sync) do take the lock of each scope or role they fan out into, in a
fixed one-directional order, so a cascade cannot interleave with a directly-dispatched
reload of the same role or scope.

**INV-CFG-003**: A resident identity change is refused as restart-required rather than hot-swapped.

Enforced in the agent and trigger reload paths, checked before any live runtime mutation.

What it does not cover: the policies cascade skips such a resident quietly rather than
surfacing the same refusal, so the outcome depends on which scope you asked for.

**INV-CFG-004**: Only an explicit whitelist of the config tree is version-controlled.

Enforced by the ignore file the repository is initialised with, reconciled on every boot, and
mirrored by the boot script.

What it does not cover: the version-controlled set and the set the reconciler owns are
*different*. A path can be tracked without being reconciled, and vice versa.

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

## Failure behavior

**The required credential option is missing.** Boot stops at validation — the earliest fatal
gate, and every service is gated behind it. It is not the only fatal configuration failure:
later in startup, malformed policies, a malformed agent configuration, and the absence of the
primary assistant role each raise and stop the process too. "Reconciliation is never
boot-fatal" (INV-CFG-005) is about the config *tree*, not about validation.

**Reconciliation fails.** Absorbed and logged. A recovery artifact is preserved — a
pre-reconciliation commit where the repository is usable, and a backup copy otherwise — so an
overwritten local edit is recoverable.

**Repository initialisation fails.** Degraded, not fatal. Versioning is unavailable;
everything else proceeds.

**A secret reference fails to resolve.** Absorbed, and the behaviour differs by path in a way
worth knowing: on the startup path the raw reference is retained — except the webhook
secret, which is blanked rather than used as an HMAC key (a vault path is a predictable
string, and the discovery publisher also removes any previously published record so the
companion integration cannot keep signing with it) — while the plugin environment leaves
the variable unset at boot but installs the literal reference on reload. Same failure,
several outcomes.

**A reload handler raises.** The dispatcher returns an error envelope rather than propagating
— a failed reload is a reported outcome, not an exception at the caller.

## Extension points

**A new option** means the manifest options block, its schema entry, the translations entry,
and an explicit export or read wherever it is consumed. Nothing picks up an option
automatically.

**Removing an option** leaves its stored value behind: the host warns about the unknown
key at boot until the stored options are cleaned by hand, and Casa itself ignores it.
Pre-1.0 that is accepted — there is no boot-time pruning of removed keys.

**Making an option hot-reloadable** is not a small change: it means a new scope and rebuilding
every consumer, because no generic mechanism exists.

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

**A new reload scope** needs its handler, a lock key, a decision about whether the full scope
composes it, whether it participates in trigger reconciliation, and what its failure means.
None of those are inferred.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/reload.py::dispatch`
- `casa/rootfs/opt/casa/reload.py::reload_full`
- `casa/rootfs/opt/casa/reload.py::_resident_identity_changed`
- `casa/rootfs/opt/casa/config_git.py::init_repo`
- `casa/rootfs/opt/casa/config_sync.py::reconcile`
- `casa/rootfs/opt/casa/secrets_resolver.py::resolve`
- `casa/config.yaml::schema`
- `casa/rootfs/etc/s6-overlay/scripts/setup-configs.sh`

**Tests**
- `tests/test_casa_reload_tool.py`
- `tests/test_config_git.py`
- `tests/test_config_sync_backstop.py`
- `tests/test_admin_reload_route.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
<!-- END SOURCEMAP -->
