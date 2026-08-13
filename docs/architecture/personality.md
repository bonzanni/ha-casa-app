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
checked against one that was, has to resolve that option exactly as the loader will. (The
export verifier is the deliberate exception: it materializes and compiles a throwaway
binding purely to prove a bundle is self-consistent, so it resolves against no options at
all and never persists the result.) Materializing with no options resolves the
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

## Failure behavior

**A persona fails validation on a resident.** It depends on what exists already. On a fresh
install — no active binding tuple — loading fails, and because resident loading is
boot-fatal, so does boot. With an existing active binding, a failing *candidate* is
discarded with a diagnostic and boot proceeds on the retained last-known-good binding;
reconciliation raises only when there is nothing good to retain. Candidate validation is
complete before promotion: requirements, the pinned-checksum match, and the full
compile/admission pass all run on the candidate — a persona that would fail an admission
ceiling is discarded as a candidate, never committed active to fail every later boot. The
pre-commit config gate replays this reconciliation in a validation-only mode that writes no
binding state, so validating a commit can never activate a staged persona swap.

**A persona fails validation on a specialist.** Absorbed by that tier's isolated loading; the
specialist is unavailable and the system continues.

**A binding cannot be activated.** Folded into the loading error for that agent, so it is
reported as a load failure rather than a separate class.

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

**Tests**
- `tests/test_personality_binding.py`
- `tests/test_persona_install.py`
- `tests/test_personality_admin_handlers.py`

**Related**
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
