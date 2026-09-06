# Ingress reality — how a plugin receives events from the outside world

## Plugins cannot listen

A Casa plugin has **no way to open a port, register an HTTP route, or run a
resident process**. MCP servers are stdio child processes spawned per turn —
one that binds a socket is dead weight. Never promise "the plugin will
receive the webhook"; the plugin *declares* ingress and **Casa** receives it.

## The sanctioned mechanism: `casa.triggers`

Declare webhook triggers in `.claude-plugin/plugin.json`:

    {
      "name": "elevenlabs",
      ...
      "casa": {
        "triggers": [
          {
            "name": "voicemail",
            "type": "webhook",
            "target": "resident:assistant",
            "clearance": "public",
            "auth": { "mode": "static_header", "header": "X-API-Key" }
          }
        ]
      }
    }

- **Endpoint:** `POST /webhook/plg-<plugin>--<name>` (e.g.
  `/webhook/plg-elevenlabs--voicemail`) on Casa's authenticated wildcard
  handler. The `plg-` prefix is reserved — user triggers can never collide.
- **Webhook-only, one `resident:<role>` target per entry.** Need two
  residents? Duplicate the entry under distinct names.
- **Limits:** declared `name` matches `[a-zA-Z0-9_-]+`, no `--`, no `plg-`
  prefix; ≤ 8 triggers per plugin; effective name ≤ 64 chars.
- **Auth modes** (Casa-owned secrets only — `secret_owner: "provider"` is
  rejected at publish in this release): `static_header` (default header
  `X-API-Key`), `timestamped_hmac` (`t=<unix>,v0=<hmac>`, default header
  `X-Webhook-Signature`, `tolerance_secs` 60–3600), `hmac_body` (global
  webhook secret). No default names a vendor — declare `header` explicitly
  when your caller mandates a particular name, and note that the header is
  part of the consent identity, so changing it re-prompts the operator.
  Invalid declarations refuse the PUBLISH — fix the manifest, don't ask the
  operator to override.

## Nothing routes until the operator consents

A declared trigger is **derived state, fail-closed**. It routes only when
ALL hold: the artifact resolves; the plugin is *assigned* to the target
resident; that resident declares the `webhook` channel; and the operator
tapped **Approve** on the one-time consent DM ("Plugin X wants to open
POST /webhook/… → assistant"). Until then the endpoint 404s and plugin
health shows `trigger_pending_ack` / `trigger_channel_missing` /
`trigger_unassigned_target`. Consent is bound to the exact
(artifact, name, target, auth-policy) tuple — **every plugin update
re-prompts and rotates the secret**. The operator off-switch is
`trigger_ack_revoke` (immediate 404).

## The setup flow your plugin must ship

After consent, Casa eagerly mints the per-trigger secret at
`/data/webhook_secrets/plg-<plugin>--<name>`. Ship an MCP **setup tool**
that reads that file and provisions the external service — e.g. create the
provider's workspace secret and point its webhook/tool URL at
`<public_url>/webhook/plg-<plugin>--<name>`. Rollback = point the provider
back. Because an update rotates the secret, the setup tool must be
re-runnable (idempotent provisioning, not create-once). The tool must be
**argument-free**, its name must start with `setup_` (grammar
`^setup_[a-z0-9_]{1,64}$` — lowercase ASCII only), and you must declare it
in the manifest as **`casa.setupTool`** (v0.112.0) AND name it in your
completion handoff. With the manifest declaration, Casa runs the tool
AUTOMATICALLY once the operator's trigger consent settles with an approval
(durable episode, crash-safe, at-least-once) — the operator never asks for
setup. A malformed declaration refuses the artifact at verify; a
declaration on a plugin whose targets are executor-only is a blocking
verify issue (`setup_tool_unsupported_target` — no invocation path).

## What the webhook turn can — and cannot — do

A webhook-triggered turn is UNTRUSTED third-party content and runs in
Casa's **restricted runtime**: no shell, no filesystem, **no plugins — not
even yours**, no skills; exactly memory recall at the declared `clearance`
(never private) plus an operator-bound notification. Design accordingly:

- The payload itself must carry everything the notification needs — the
  resident summarizes it to the operator, nothing more.
- Your plugin's skills and MCP tools serve NORMAL operator-driven turns
  (follow-ups like "call them back", provider API queries) — never the
  webhook turn itself.
- Anything privileged in response to an event needs the operator-signed
  `/invoke` path, not a plugin trigger.
