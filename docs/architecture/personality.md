---
last_reviewed: 2026-08-25
---

# Personality: roles, personas and bindings

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The separation between what an agent *is* and how it *presents*: role artifacts, personas,
the binding that ties one to the other, and what prompt is actually served at turn time. It
does not cover agent declaration and loading, which belong to the taxonomy document. How a
persona arrives, is approved, and leaves — install, consent, where a ref may resolve,
removal, the sweep — is [`persona-lifecycle.md`](persona-lifecycle.md)'s.

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

The same rule has a quieter consequence that has already cost one issue a wrong
turn: the disclosure policy — the global library and the per-resident override —
is rendered only into the composed prompt, so it reaches no shipped agent's live
prompt at all. Residents are bundle-bound, and the tier file rules forbid a
per-agent disclosure file to anyone else, so there is nothing left for the
rendered section to reach. Tightening a disclosure category is therefore a real,
committed configuration change that alters no agent's instructions today. The
role artifact's own `disclosure` block is in the same position: the schema
requires it and no renderer consumes it.

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

**The observer and secondary passes run on their own model.** `SECONDARY_AGENT_MODEL`
(default *haiku*) selects the model for engagement observation and engager-query synthesis
— a cost/latency/judgment tunable documented nowhere else.

**The admin surface is internal-only and redacts by default.** The personality admin
routes exist only on the internal Unix socket, and the explain route withholds sensitive
prompt and memory fields unless the request both asks for them and confirms. Both gate
fields must be JSON booleans; a request that sends anything else is refused with
`invalid_args` and a `detail` naming the offending field and the JSON type received —
never the value. Those routes answer for an installed specialist whose binding has been
activated exactly as they do for a resident, and for one whose binding has not been
activated they answer for nothing at all; where a specialist's persona reference collides
with a resident's, the resident's pack is the one inspect describes.

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

**INV-PERS-015**: A rule stated in the shipped safety kernel is present in every resident projection on every surface.

The kernel is the only image-owned compilation input carried into every resident
projection and every bound specialist's, and it is not an input to the role
checksum — so a rule stated there moves every projection digest without
invalidating a single persisted binding. That is what makes it the surface for a
rule that has to bind the agent that fetches something and the agent that
relays it.

What it does not cover: enforcement. It asserts presence in the served prompt
and nothing about the model obeying it. There is no taint path, no outbound
scrubber and no per-value provenance anywhere in Casa; a rule about handling a
credential-bearing artifact is a judgement instruction, and treating it as a
boundary would be the same mistake as believing a `response_shape.yaml` edit.

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

**A persona fails validation on a specialist.** Absorbed by that tier's isolated loading; the
specialist is unavailable and the system continues.

**A binding cannot be activated.** Folded into the loading error for that agent, so it is
reported as a load failure rather than a separate class. For an installed specialist the one
exception is a role checksum that moved while everything else stayed: that is re-derived on
load (INV-PERS-016), and the load failure is reserved for a binding whose identity actually
moved. An *active* override whose pinned
persona bytes have gone missing, gone unreadable or changed is not re-materialized — the
altered bytes are never served — but the load of those same bytes then fails, and that
failure is boot-fatal per INV-PERS-003. The refusal is logged as fields (INV-PERS-010):
the resident, the source selection's persona ref, pinned checksum, found checksum and
reason — and, for EACH of the active and staged selections, whether it was absent (no
directory entry), unreadable (its file and failure class), or read (its file, its mode
verbatim, its persona ref, and — for a selection whose reload dispatches through the
non-image-default arm, or a staged override — its own pinned checksum and the
config-relative path its bytes must be restored under), together with the offline
recovery facts: the procedure requires the app stopped, and its steps are in this
document. The record asserts nothing about what is live, what was written, or whether
startup is prevented — the same reconciliation serves a boot that is fatal, a reload
that leaves the live resident serving, and a validation replay that writes and logs
nothing. A tuple file that cannot be read — empty, malformed, pathological — draws
the same single record before the original failure propagates exactly as it always
did. The special-file classes are DELIBERATE outcome changes, not parity: a symlink in
a tuple's place — live, dangling, or looped — is refused rather than followed; a
directory is refused with the typed message rather than its native error; a FIFO is
refused in bounded time rather than blocking boot on a read that can never return.
Exact original-exception parity is a regular-file property.

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

**INV-PERS-010**: A committing persona reconciliation that refuses logs exactly one structured record stating, for each of the active and staged selections, whether it was absent (no directory entry), unreadable — naming its file and failure class — or read — naming its file, mode and persona ref, carrying pin and config-relative restore facts for an active selection the reload dispatches through its non-image-default arm and for a staged selection only where reconciliation selects it as an override, and a found checksum only where a pack actually resolved; when the pass staged a candidate before failing, the staged observation reflects that candidate; and the structured record names no recovery requiring the live runtime.

The statement is the fuller form of the narrowed #670 contract, which it subsumes: the
flat source-selection fields remain, and the per-selection objects are what make the
recovery in this document followable from the refusal alone — the record enumerates
every selection that would load, so the operator no longer has to be warned that the
log may have named only one. The recovery steps themselves deliberately stay HERE:
five review rounds established that an emitted string cannot carry the conditions
under which each step applies, so the record carries the condition-free inputs — the
files observed, the pins, the restore paths — and this document carries the judgment.
One caution the record's file list does not remove: it is an observation at refusal
time, and the reconciliation itself discards a staged candidate it rejected, so
re-check what is present before deleting anything.

What it does not cover: the validation replay, which writes and logs nothing by
design (its own test pins that silence); the record's level; and any claim about the
outcome — retaining a tuple is not surviving it (INV-PERS-003).

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
- `casa/rootfs/opt/casa/agent_loader.py::make_candidate_compile_validator`
- `casa/rootfs/opt/casa/agent_loader.py::_activate_resident_binding`
- `casa/rootfs/opt/casa/prompt_compiler.py::compile_prompt_bundle`
- `casa/rootfs/opt/casa/specialist_install.py::activate_binding_for_config`

**Tests**
- `tests/test_personality_binding.py`
- `tests/test_specialist_binding_rederive.py`
- `tests/test_resident_refusal_diagnosis.py`
- `tests/test_resident_refusal_record_boundaries.py`
- `tests/test_refusal_observation.py`
- `tests/test_personality_admin_handlers.py`
- `tests/test_assistant_prompts.py`
- `tests/test_prompt_compiler.py`
- `tests/test_persona_pack.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
- [`architecture/prompt-file-guards.md`](../architecture/prompt-file-guards.md)
<!-- END SOURCEMAP -->
