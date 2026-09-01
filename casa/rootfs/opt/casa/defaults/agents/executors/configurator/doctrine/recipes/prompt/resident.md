# Recipe: change how a resident behaves

**There is no `prompts/system.md` edit to make. Do not attempt one — Write and
Edit of it are refused, and it would not have worked: the file is not served.**

`agents/<role>/prompts/system.md` is loaded, is pointed at by `character.yaml`'s
`prompt_file:`, and is required to exist. The loader does read it — into a
composed prompt that no bundle-bound resident is served. A resident's base
prompt is its **compiled bundle** — the persona
plus the role artifact's own doctrine and response block — and that bundle
REPLACES the composed prompt this file feeds (INV-PERS-001). Every resident is
bundle-bound from its first boot, because all three role artifacts declare
`persona.policy: required`.

So editing it changes the file on disk, commits cleanly, and moves nothing: the
turn's `static_prompt_digest` is byte-identical before and after. That is worth
knowing precisely, because the next reply often *does* look different by chance
— and it reads like the edit worked.

## What to do instead — it depends on what was asked for

**How it sounds — register, warmth, length, name.** That is its persona.
`recipes/persona/install.md` to pull the pack that says it, then
`recipes/persona/apply.md` to bind it; for a resident that STAGES the binding
and takes effect on its next `casa_restart_supervised`. Same answer as
`recipes/response-shape/edit.md`, and for the same reason.

**What it can do — tools, plugins, delegates.**
`recipes/resident/grant_ha_tools.md` for the Home Assistant surface,
`recipes/plugin/` for a plugin's tools, `recipes/delegate/wire.md` for who it
can hand work to. A grant is what makes a capability reachable; there is no
file in which to "teach" a resident how to use one.

**A standing behavioural rule — "always do X when Y".** This is the case with no
configuration surface at all, and saying so plainly is the right answer. A
resident's instructions are its role doctrine, which ships inside the Casa image
and is never synced into `/config`; nothing you can reach changes it. Tell the
user that the behaviour they want is a Casa change rather than a setting, and
what the nearest reachable thing is (a persona, a trigger, a delegate, a
reminder). Do not write the file "so the intent is at least recorded" — a
committed edit that reaches nothing is worse than no edit, because the operator
believes the change is in effect.

## Specialists and executors

- **Specialists**: an installed specialist serves its compiled bundle too, and
  its `prompts/system.md` is MATERIALIZED from the role artifact — an output,
  not an input. That whole tree is managed state; `managed_component_guard`
  denies edits there and routes to the specialist pipeline.
- **Executors**: an executor's composed prompt genuinely IS served. Its prose
  is `prompt.md` and its own `doctrine/*.md` — see `recipes/prompt/edit.md`.

## Reads are fine

Opening a resident's `prompts/system.md` is allowed and is often the right
thing to do — to see what it says, and to explain to the user why changing it
is not the answer.
