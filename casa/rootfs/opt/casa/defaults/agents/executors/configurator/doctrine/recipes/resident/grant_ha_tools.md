# Recipe: grant a resident Home Assistant tools

When a resident agent needs to control HA devices (lights, climate, locks, media, sensors), grant the **whole HA Assist tool surface in one line**: server-level `mcp__homeassistant`. Per-tool enumeration is unnecessary.

The default Casa setup wires this for `butler` (Tina). Use this recipe to add it to any other resident.

Keep the same logical server name, `homeassistant`, for every role with direct
HA grants. Casa may transparently substitute a role-specific eager facade under
that name. Never grant raw `homeassistant` alongside a second facade server
name: that exposes duplicate tools to the model and makes tool selection
ambiguous.

## Prerequisite — HA-side configuration

The user must, in Home Assistant:

1. Enable **Settings → Devices & Services → Add Integration → "Model Context Protocol Server"**.
2. Expose every entity the resident should control to the **default Assist pipeline** (Settings → Voice Assistants → Expose).

Without these, `http://supervisor/core/api/mcp` returns 404 and tool calls fail at the transport layer.

## Step 1 — Server-level grant in `runtime.yaml`

Edit `/config/agents/<role>/runtime.yaml`:

```yaml
tools:
  allowed:
    - Read
    - Skill
    - mcp__homeassistant   # ← grants every HA tool, present and future
```

Bare `mcp__<server>` (no `__<tool>` suffix) is a server-level wildcard. As the user adds new exposed entities to Assist, the resident gets access automatically; no Casa restart needed beyond the next session pool turn.

## Step 2 — Confirm `homeassistant` in `mcp_server_names`

Same file:

```yaml
mcp_server_names:
  - homeassistant
  - casa-framework
```

Without this, the allow-list grant points at nothing. casa_core only registers the homeassistant server when SUPERVISOR_TOKEN is set (always true on a real HA install).

## Step 3 — what the HA surface actually does (facts to relay, not a file to write)

Worth knowing, and worth telling the user when they ask why a resident behaves
the way it does — but there is nowhere to write it, and that is the point of the
section below.

`GetLiveContext` accepts an optional domain filter, and that filter is **local
to Casa**: Casa sends `{}` upstream and filters the returned snapshot
afterwards. It is a state-query tool, not a prerequisite for action — action
tools (`HassTurnOn`, `HassTurnOff`, `HassLightSet`,
`HassClimateSetTemperature`, …) are called directly. Casa's shipped resident
doctrine bounds `GetLiveContext` to **at most once per turn**, then requires the
resident to act or answer, because framing it as a prerequisite once produced a
delegated turn that looped on it without ever acting.

## Step 4 — there is no step 4

The grant is what makes the tools reachable. There is no file in which to
"teach the resident how": `prompts/system.md` is not an input to anything a
persona-bound resident is served, Write/Edit of it are refused, and an appended
`## Home Assistant tools` section would be committed and reported live while
changing nothing the model sees. See `recipes/prompt/resident.md`.

How a resident uses the tools it holds comes from its role doctrine, which
ships inside the Casa image. If the shipped doctrine turns out to handle the
HA surface badly, that is a Casa change and belongs in an issue — say so rather
than writing a file that reaches nobody.

## Verify

```
/ha-prod-console:restart 91d4d4c8_casa
```

Then ask the resident a control question via its primary channel ("turn off the kitchen lights"). Check the addon logs for an `mcp__homeassistant__HassTurnOff` call.

## Common pitfalls

- **HA integration not enabled** — addon log shows `404 Not Found` against `/core/api/mcp`. Fix: enable the integration in HA.
- **Entity not exposed to Assist** — model gets back `entity not found`. Fix: expose the entity in Voice Assistants settings.
- **Agent has the tool but doesn't call it** — check the grant first: `casactl persona render` shows what the resident is actually served, and `runtime.yaml`'s `tools.allowed` plus `mcp_server_names` decide what it actually holds. Do NOT reach for `prompts/system.md`; it is not served, so nothing written there can be the cause or the cure.
- **Wrong Casa version** — the bare `mcp__homeassistant` grant requires v0.15.1+. Earlier versions used per-tool entries.
