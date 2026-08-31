# Recipe: edit disclosure policy

**Read this before promising an effect.** `policies/disclosure.yaml` and the
per-resident `agents/<role>/disclosure.yaml` are loaded and schema-validated,
and then rendered into the COMPOSED prompt — which no bundle-bound agent is
served. Every resident is bundle-bound from its first boot, and specialists and
executors are forbidden a `disclosure.yaml` at all, so the rendered disclosure
section currently reaches no shipped agent's live prompt.

So a disclosure edit is a real, committed configuration change with **no effect
on what any agent is told today**. Say that when you report it; do not describe
it as tightening or relaxing what an agent will disclose.

## Ask the user

1. **Policy level or resident-specific?**
2. **What to change?**
3. **Do they understand it will not change any agent's behaviour right now?**
   If what they actually want is a behavioural rule, that is
   `recipes/prompt/resident.md` — and the honest answer there is that it has no
   configuration surface either.

## Files

- /config/policies/disclosure.yaml - global fallback.
- /config/agents/<resident-role>/disclosure.yaml - per-resident override.

## Format

See schema/policy-disclosure.v1.json and schema/disclosure.v1.json.

## Reload

`casa_reload(scope='policies')` for the global file, `casa_reload(scope='agent',
role=<role>)` for a per-resident one. Both reload the configuration correctly;
neither changes a served prompt, for the reason above.
