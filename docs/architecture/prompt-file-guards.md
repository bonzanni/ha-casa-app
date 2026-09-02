---
last_reviewed: 2026-09-02
---

# Prompt-file write guards

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The two per-agent prompt files a persona-bound resident still carries in the config
repository — `response_shape.yaml` and `prompts/system.md` — why an edit to either reaches
no served prompt, and the file-tool guards that refuse the edit. It does not cover the
personas, bindings and compiled bundles that replace those files
([`architecture/personality.md`](personality.md)), nor how a hook call is resolved and
authenticated ([`architecture/hook-resolution.md`](hook-resolution.md)).

## Mental model

**The file is read; what it composes is not served.** Both files feed only the composed
fallback prompt, and every resident is served a compiled bundle from its first boot, so an
edit to either is written, committed and reported live while the served prompt stays
byte-identical. The guards exist because that failure was silent: they refuse the write
through the file tools on every hook path Casa builds, and the instructions that used to
route behaviour into these files now name what actually works.

## Contracts & invariants

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

**INV-PERS-012**: A persona-bound resident's `prompts/system.md` is read only into the composed fallback prompt and is not served when its compiled bundle is active; and an agent's write to a resident's copy through `Write`, `Edit`, `MultiEdit` or `NotebookEdit` is refused on the executor, resident, delegated-resident and claude_code hook paths.

This is INV-PERS-008 one file over, and the file operators actually reach for.
`character.yaml` points at it, the loader requires it to exist and opens it on
every load — the file *is* read — and what it reads composes into a prompt no
bundle-bound resident is served, because the compiled bundle replaces the
composed one from the first boot. Five configurator recipes and the operator
documentation used to route behavioural instructions into it and promise the
edit was live on the next turn. The refusal resolves its target before deciding:
a relative path against the session's own working directory, redundant
separators and `.` and `..` segments away, and a symlink through to what it
points at, rather than comparing the path it was handed.

What it does not cover. **It is not a claim of completeness over path
spellings** — an implementation can condition a resolution stage on a spelling
class no test exercises, so what is asserted is the resolution behaviour that is
exercised, not that no spelling escapes. Nor reads: an agent may still open the
file, and should, to explain why changing it is not the answer. **Nor the
shell.** A `Bash` call is not routed to this guard and its callback classifies
no command text: a first cut decided the shell from the command's text, the way
the trigger-file and response-shape guards still do, and was measured wrong in
both directions — it refused reads the invariant promises to allow and missed
writes — because what a shell command writes to is not decidable from its text.
So a shell-capable agent can still make the edit. It is inert for a bundle-bound
resident; no shipped resident holds `Bash` and the configurator has none, so the
one shipped path is the plugin-developer's shell, and whether that tree gets an
execution boundary is a separate decision from this guard. Nor the claude_code
transport's fail-open when the hook resolver is unreachable, which is a property
of that transport that the managed-state guard shares. Actual specialist
sessions are not claimed: a specialist's copy is materialized output under
managed state, which the managed-state guard denies in its own words.

The half that matters more is not the refusal. A guard alone would leave every
instruction still pointing at the file, which is the half an operator hits;
those instructions now name what actually works instead — the persona pack for
how a resident sounds, a grant for what it can do, and, for a standing
behavioural rule, the fact that there is no configuration surface at all,
because a resident's instructions are its role doctrine and that ships inside
the image.

## Failure behavior

**An agent writes to a resident's copy through a file tool.** Refused on every hook path with
a denial naming the file and the surface that does carry the behaviour. **Through the shell.**
Not classified: a `Bash` call is not routed to these guards, so a shell-capable agent can
still make the edit — inert for a bundle-bound resident, and a separate decision for the
plugin-developer's tree.

## Extension points

**A new per-agent file that the compiled bundle makes dead** needs the same treatment as
these two: a guard on the file-tool paths, resolved against the session's working directory,
and the instructions that pointed at the file rewritten to name the surface that works.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/hooks.py::make_response_shape_write_guard`
- `casa/rootfs/opt/casa/hooks.py::make_resident_prompt_write_guard`

**Tests**
- `tests/test_response_shape_write_guard.py`
- `tests/test_resident_prompt_write_guard.py`
- `tests/test_resident_prompt_guard_denial_wording.py`

**Related**
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/hook-resolution.md`](../architecture/hook-resolution.md)
<!-- END SOURCEMAP -->
