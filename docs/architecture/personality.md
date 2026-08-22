---
last_reviewed: 2026-08-01
---

# Personality: roles, personas and bindings

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The separation between what an agent *is* and how it *presents*: role artifacts, personas,
the binding that ties one to the other, and what prompt is actually served at turn time. It
does not cover agent declaration and loading, which belong to the taxonomy document.

## Mental model

**A role is identity; a persona is presentation.** The role decides what an agent *is* —
identity, model, doctrine, and the role-based checks. What a resident or specialist *may do*
— its tool lists, permission mode and MCP servers — comes from its own runtime
configuration, not from the role artifact; only the executor path applies a role-artifact
capability ceiling. The persona decides how the agent sounds. Roles and personas are
versioned and validated separately, and a binding is what associates them.

**The compiled bundle replaces the composed prompt — it does not layer onto it.** This is the
easiest thing to get wrong here. The composed prompt is built from the agent's own
configuration — character prompt, voice, response shape, delegates, disclosure policy; it
reads no role artifact. Role artifacts feed the *compiled bundle* path instead, and separately a
bundle is compiled when a binding activates. At turn time, if a bundle exists, its projection
*is* the base prompt; the composed one is used only when there is no bundle. So text present
in the composed prompt is not automatically carried into an activated compiled prompt — it is
there only if the compilation put it there.

That is a rule about *declarations* as much as prose. A role artifact's response block —
register, and the sentence ceilings for confirmations and status — is compiled into its own
section of each projection, one rendering per surface, so the limits a role declares are the
limits the model is given. The renderer is deliberately exhaustive rather than tolerant: a
key the response schema admits but no renderer handles is a declaration that would be read
by nobody and would fail silently, so the schema's own key set is what the renderer is
checked against. It does not reject an unrecognized key at compile time, because the schema
is closed and every role artifact is validated against it before it becomes a role — a
rejection there could only fire where the loader already refused, while being able to block
an install that is entirely valid.

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

**Projection selection is scoped, not nested.** The shipped doctrines put each surface's
instructions under the shared core heading, and a Markdown section's body would otherwise run
through its subsections — so selecting the core would drag every other surface's directives
into every projection, and repeat the chosen one. Section selection therefore excludes the
sibling projection subtrees explicitly; the surface's own section is always selected in its
own right, never inherited.

**A persona can misdescribe its role, but cannot expand it.** Persona validation checks
structure, required markers, sections and size against the manifest. It does not check that
what the persona *says* about its capabilities is true. A persona cannot grant a tool; it can
claim one it does not have. Capability comes from the role and the tool layer, never from
prose.

**The binding digest does not cover everything about a binding.** It covers the role and
persona checksums, the compiler version, dependency digests and the effective config
digest; it excludes the binding mode, the image-default and component roots, and the
override source — those can change without moving it. Before relying on the digest to
notice a change, check which side of that boundary the change is on.

**Projections differ per surface, and admission has ceilings.** Text, voice and
restricted-webhook prompts have materially different contents — voice carries only the
persona core plus two quirks, and the restricted path no persona at all — and each surface
enforces hard persona/total token ceilings (2k/12k text, 400/6k voice, 0/4k restricted).
A persona that validates structurally can still fail *activation* on a ceiling, and voice
behavior written only outside the core never reaches the voice surface.

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

**The observer and secondary passes run on their own model.** `SECONDARY_AGENT_MODEL`
(default *haiku*) selects the model for engagement observation and engager-query synthesis
— a cost/latency/judgment tunable documented nowhere else.

**The admin surface is internal-only and redacts by default.** The personality admin
routes exist only on the internal Unix socket, and the explain route withholds sensitive
prompt and memory fields unless the request both asks for them and confirms.

## Contracts & invariants

**INV-PERS-001**: When a resident has an activated compiled bundle, that bundle's projection is the base prompt; the composed prompt is a fallback for when there is none.

Enforced where turn options are built, and mirrored on the specialist path.

What it does not cover: it does not merge them. Anything expected in the served prompt must
be present in the compiled bundle.

**INV-PERS-002**: Persona validation is structural; it does not verify that a persona's claims about capability are true.

What it does not cover: nothing prevents a persona describing a tool the agent lacks. Treat
persona text as presentation, never as a source of truth about what an agent can do.

**INV-PERS-003**: A resident's binding reconciliation runs as part of loading and is not isolated from it.

The consequence is the important part: a failure there propagates into resident loading,
which is boot-fatal. Persona problems on a resident are not a degraded mode.

**INV-PERS-004**: The restricted-origin prompt omits the persona section.

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

**INV-PERS-008**: A persona-bound agent's per-agent `response_shape.yaml` is not read, and an agent's file-tool write to a resident's copy is refused.

This is INV-PERS-001 seen from the config repo. The file renders only into the composed
prompt, so for anything carrying a compiled bundle it reaches nothing — and every resident
carries one from its first boot, because all three role artifacts require a persona. The
refusal exists because the failure was silent in the worst way: the edit was written,
committed, and reported live, while the served prompt was byte-identical. What the model
receives comes from the persona pack and the role artifact's own response block.

What it does not cover: reads. An agent may still open the file, and should, to explain why
changing it is not the answer. The shell half of the refusal is a backstop rather than a
boundary — it recognises the accidental spelling, not every possible one — and the
specialist subtree is denied by the managed-state guard instead, in its own words.

**INV-PERS-009**: Every section body a persona's Markdown declares reaches the text projection exactly once.

A section's body physically runs through its subsections, so a renderer that also walks the
flat heading list emits every nested section once per ancestor — twice at depth two, three
times at depth three. The projection is partitioned by *containment* instead: the sections
no other section contains, split into the validated Core and everything else. Both halves
read the same partition, so nothing can land in both and nothing can be dropped.

What it does not cover: voice, which carries only the Core, and the restricted-origin
prompt, which carries no persona at all. Nor prose authored above the first heading, which
is in no section and reaches no projection. Nor a top-level section's own heading LINE: a
section body begins after its heading, so `# Core` and any sibling `# Heading` reach no
projection — the headings that do appear are the nested ones, carried inside the body that
contains them. "Core" here means the loader's own identity for
it — the single level-1 `# Core` whose body passed the 300–500 character gate. A Core-named
section written outside that one is ordinary prose: it reaches text, and it does not reach
voice. A Core-named section written *inside* it is part of the core, so it reaches voice as
the core does — once, rather than a second time in its own right.

## Failure behavior

**A persona fails validation on a resident.** It depends on what exists already. On a fresh
install — no active binding tuple — loading fails, and because resident loading is
boot-fatal, so does boot. With an existing active binding, a failing *candidate* is
discarded with a diagnostic and reconciliation returns the retained binding — whose own
servability the load then decides, because retaining a binding is not surviving it;
reconciliation raises only when there is nothing good to retain. Candidate validation is
complete before promotion: requirements, the pinned-checksum match, and the full
compile/admission pass all run on the candidate — a persona that would fail an admission
ceiling is discarded as a candidate, never committed active to fail every later boot. The
pre-commit config gate replays this reconciliation in a validation-only mode that writes no
binding state, so validating a commit can never activate a staged persona swap.

**An operator applies a persona a resident cannot compile.** Refused at the point of asking,
with the compile failure reported and nothing written — no staged tuple, no error record, no
change to what is active. The distinction that matters is between *refusing* and *recording a
refusal*: a staged tuple would be promoted by the next reconciliation, so an attempt that
leaves one behind has not been refused. Nothing needs undoing afterwards, which is worth
saying out loud to an operator who has just been told their swap failed.

**A persona fails validation on a specialist.** Absorbed by that tier's isolated loading; the
specialist is unavailable and the system continues.

**A binding cannot be activated.** Folded into the loading error for that agent, so it is
reported as a load failure rather than a separate class. An *active* override whose pinned
persona bytes have gone missing, gone unreadable or changed is not re-materialized — the
altered bytes are never served — but the load of those same bytes then fails, and that
failure is boot-fatal per INV-PERS-003. The refusal is logged as fields: the resident, the
persona ref, the checksum the binding pins, the checksum found when a pack resolved at all,
whether an active and a staged tuple were present, and the reason caught. It states nothing
else — not what is live, not what was written, not whether startup is prevented — because
the same reconciliation serves a boot that is fatal, a reload that leaves the live resident
serving, and a validation replay that writes and logs nothing.

**It names no tool.** Every persona tool resolves a resident's role through
`casa/rootfs/opt/casa/tools.py::_resolve_resident_role`, which answers `runtime_unavailable` without a live
runtime, and `casactl` needs a socket a stopped app does not have — so recovery advice
naming any of them cannot be followed in the state it would be read in. The recovery is
here instead, where its conditions fit. Two options, and which one applies depends on
whether a tuple still names the persona. **Restore the admitted bytes** under
`<config>/personas/<id>/<version>/` until they reproduce the pinned checksum — the personas
tree is not tracked by the config repo, so the source is a backup, not a config-repo revert.
That recovers the pin only while some tuple still names it: a refused *sole staged*
selection has already been discarded, so the next start comes up on the slot's image default
and the swap has to be applied again. **Or discard the pin** by deleting the resident's tuple
files with the app stopped — and it must be every selection that would load, not only the one
the log named, because an active and a staged tuple can name different personas and deleting
one leaves the other to fail.

**A removal is refused.** Nothing is mutated — the bytes stay, and so does the install approval,
which is only revoked on a removal that goes through. The refusal names its referrers; freeing
them is the operator's next step (reset or re-apply the agents that name the persona, and restart,
for a resident — a staged reset does not release the old persona until the restart commits it).
An unreadable bundle journal refuses every removal rather than some, and clears itself at the next
boot's journal reconciliation.

**A removal fails after its approval was revoked.** The bytes remain installed and can no longer
be installed on the old approval — one re-approval. This ordering is deliberate: the inverse
leaves a live approval for bytes that are gone.

**No bundle exists.** The composed prompt is served. This is a working state, not an error —
which means a silently missing bundle presents as an agent that behaves correctly but sounds
wrong.

## Extension points

**A new persona** must satisfy the structural contract — markers, sections, size — and be
bound to a role. Getting it *accurate* is not something validation will help with.

**A new role artifact** changes what the compilation must carry — and only that: the
composed fallback prompt never reads role artifacts (see the mental model), so an artifact
change lands solely on the compiled-bundle path.

**Changing what the binding digest covers** changes what counts as drift, and therefore what
forces a re-activation. Widening it is safe; narrowing it silently stops detecting something.

**Anything that must appear in every prompt** belongs in the compilation, not only in the
composed prompt — otherwise it appears exactly for the agents that have no bundle.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/personality_binding.py`
- `casa/rootfs/opt/casa/personality_types.py`
- `casa/rootfs/opt/casa/agent_loader.py::_compose_prompt`
- `casa/rootfs/opt/casa/role_slot.py`
- `casa/rootfs/opt/casa/prompt_compiler.py::compile_projection_set`
- `casa/rootfs/opt/casa/markdown_sections.py::root_sections`
- `casa/rootfs/opt/casa/persona_install.py::persona_references`
- `casa/rootfs/opt/casa/persona_install.py::require_persona_present`
- `casa/rootfs/opt/casa/persona_install.py::remove_installed_persona`
- `casa/rootfs/opt/casa/persona_install.py::prune_installed_personas`
- `casa/rootfs/opt/casa/persona_install.py::list_installed_personas`
- `casa/rootfs/opt/casa/persona_install.py::apply_persona_override`
- `casa/rootfs/opt/casa/agent_loader.py::make_candidate_compile_validator`
- `casa/rootfs/opt/casa/hooks.py::make_response_shape_write_guard`

**Tests**
- `tests/test_personality_binding.py`
- `tests/test_resident_refusal_diagnosis.py`
- `tests/test_resident_refusal_record_boundaries.py`
- `tests/test_persona_install.py`
- `tests/test_persona_removal.py`
- `tests/test_personality_admin_handlers.py`
- `tests/test_persona_apply_resident_staging.py`
- `tests/test_response_shape_write_guard.py`
- `tests/test_prompt_compiler.py`
- `tests/test_persona_pack.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
