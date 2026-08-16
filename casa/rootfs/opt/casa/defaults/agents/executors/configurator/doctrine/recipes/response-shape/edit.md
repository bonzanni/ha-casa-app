# Recipe: change how an agent writes or speaks

**There is no response_shape.yaml edit to make. Do not attempt one — the hook
denies it, and it would not have worked.**

`agents/<role>/response_shape.yaml` is loaded and schema-validated, and then
nothing reads it for a resident. A resident's base prompt is its **compiled
bundle** — the persona plus the role artifact's own `response:` block — and
that bundle REPLACES the composed prompt this file feeds (INV-PERS-001). Every
resident is bundle-bound from its first boot, because all three role artifacts
declare `persona.policy: required`.

So editing it changes the file on disk, commits cleanly, and moves nothing: the
turn's `static_prompt_digest` is byte-identical before and after. That is worth
knowing precisely, because the next reply often *is* shorter by chance — asking
for short confirmations produces short confirmations — and it reads like the
edit worked.

## What to do instead

**The persona pack is the surface.** How a resident phrases things is part of
who it is, and that lives in its pack, not in a config file.

1. The pack that says it must exist and be installed —
   `recipes/persona/install.md` (repository + ref; there is no way to author or
   modify a pack from here, see #615).
2. Apply it — `recipes/persona/apply.md`. For a resident this **stages** the
   binding; it takes effect on that resident's next `casa_restart_supervised`.

## If the operator asks for something small

"Keep your confirmations to one sentence" is a persona change under this model,
and a whole pack install is a heavy answer to it. Say so plainly rather than
reaching for a file:

> That's part of Ellen's persona rather than a setting I can change — her
> response style is compiled into her prompt from her persona pack. Changing it
> means installing a pack that says it, and restarting her.

Do not offer `response_shape.yaml` as a fallback, and do not edit it "so the
value is at least right" — a committed edit that reaches nothing is worse than
no edit, because the operator believes the change is in effect.

## Specialists and executors

- **Specialists**: same story — an installed specialist serves its compiled
  bundle too. Its `response_shape.yaml` is materialized FROM the role artifact
  (`specialist_materialize`), so it is an output, not an input. That whole tree
  is managed state; `managed_component_guard` denies edits there and routes to
  the specialist pipeline.
- **Executors**: `agent_loader`'s tier map FORBIDS `response_shape.yaml` for an
  executor. An executor's wording comes from its `prompt.md` and doctrine —
  see `recipes/prompt/`.
