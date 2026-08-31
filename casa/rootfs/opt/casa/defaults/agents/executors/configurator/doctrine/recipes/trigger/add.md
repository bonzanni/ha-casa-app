# Recipe: add a trigger to an agent

Triggers are per-agent scheduled or webhook-driven events. Residents only (specialists and executors don't have triggers).

## Ask the user

1. **Which agent?** Usually assistant or butler.
2. **Trigger type?** interval (every N minutes), cron, or webhook.
3. **Trigger name?** Lowercase (e.g. morning_briefing, garbage_reminder). For a
   webhook this name IS the endpoint: `POST /webhook/<name>`.
4. **Schedule?** (interval/cron only)
   - interval: how many minutes (e.g., 30).
   - cron: five-field cron string (e.g. "0 7 * * 1-5" = weekdays 7am).
5. **Channel?** interval/cron: telegram or voice (must be a channel the agent
   already owns). **A webhook trigger requires the agent to declare the
   `webhook` channel.**
6. **Prompt?** (interval/cron/date only) One imperative sentence. **A webhook
   trigger has no prompt and the schema refuses one** — see "Webhook triggers"
   below for what its turn actually receives. If the operator describes what
   the agent should *do* when a webhook fires, tell them that before writing
   it: the instruction cannot be stored on the trigger.
7. **Webhook auth?** (webhook only) how does the caller authenticate — see below.

## Write the trigger — `config_trigger_upsert`, never a hand edit

**You cannot Edit or Write `agents/<role>/triggers.yaml`. The hook denies it.**
That file has a second writer — the resident's own reminder tools, running
inside Casa — and a hand edit from here silently throws away any reminder the
resident set since you read the file. `config_trigger_upsert` makes the change
inside Casa, leaving every other entry exactly as it was.

    # interval / cron
    config_trigger_upsert(
        role="<role>",
        name="<trigger_name>",
        type="interval",           # or "cron"
        minutes=<N>,               # interval only
        schedule="<cron>",         # cron only
        channel="<telegram|voice>",
        prompt="<one-line imperative>")

    # webhook — served ONLY at POST /webhook/<name> (no `path` field; it was
    # removed in v0.97.0). The agent must declare the `webhook` channel.
    config_trigger_upsert(
        role="<role>",
        name="<trigger_name>",
        type="webhook",
        clearance="public",        # public|friends|family — memory tiers this
                                   # webhook's turns may recall (NEVER private).
        auth={"mode": "static_header",   # hmac_body|static_header|timestamped_hmac
              "header": "X-API-Key",     # static_header / timestamped_hmac
              "tolerance_secs": 300})    # timestamped_hmac only

Read the file first if you need to see what is already there — reads are fine.
The tool validates the entry against the triggers schema and refuses without
writing anything, so a rejection leaves the operator's config untouched. It
refuses a name the resident owns (`managed_by: agent` — those are its
reminders); ask the resident to change one of those instead.

### Add agents/<role>/prompts/<trigger_name>.md (cron/interval only)

    You are <name>. The <trigger-name> trigger just fired. <Task description.>

## Reload — MANDATORY before emit_completion

**Soft** - casa_reload_triggers(role). No restart needed. Canonical order:

1. config_git_commit(message="add <trigger-name> trigger to <role>")
2. casa_reload_triggers(role="<role>")
3. emit_completion(status="ok", text="...committed SHA <sha>, reloaded triggers for <role>.")

Skipping step 2 leaves the trigger committed to YAML but **NOT registered** in the live scheduler — it never fires. See completion.md for the full doctrine.

## Verify the cron syntax

Five fields: minute hour day month day_of_week. "0 7 * * 1-5" = 7:00 on weekdays. APScheduler uses casa_tz, which defaults to Home Assistant's own timezone.

## Webhook triggers

- **Endpoint:** `POST /webhook/<name>` on port 18065 (publicly, the operator's
  configured `public_url`). There is no `path` field — the trigger NAME is the
  endpoint. Names must be unique across all agents' webhooks.
- **The turn is driven by the payload, not by an instruction.** A firing
  delivers one user message — the trigger name and the request body — and
  nothing else. `prompt`/`prompt_file` are **refused by the schema** for a
  webhook: `config_trigger_upsert` fails and writes nothing, rather than
  storing an instruction that would be committed and then discarded at every
  firing. So the agent decides what to do from the payload plus its own
  doctrine. If an operator wants specific behaviour on a specific hook, there
  is no file in which to put it: a resident's instructions are its role
  doctrine, which ships inside the Casa image and is never synced into
  `/config`, and writing `prompts/system.md` is denied precisely because it
  would look like the place. Say that plainly — see
  `recipes/prompt/resident.md` — and shape the request around what the payload
  itself can carry.
  (An older document may still carry one from before this rule; it loads with
  a warning naming the trigger, and is ignored exactly as it always was.
  Clear it by re-running `config_trigger_upsert` for that trigger without the
  field — an upsert replaces the whole entry, it does not merge.)
- **Auth is per-trigger and fail-closed** (spec A1). Pick the mode that fits the
  caller:
  - `hmac_body` (default) — caller sends `X-Webhook-Signature` = HMAC-SHA256 hex
    of the body, using the global webhook secret. That secret always exists
    (Casa generates one at `/data/webhook_secret` when the operator sets no
    override), so this mode is always available.
  - `static_header` — caller sends a shared secret in a header (default
    `X-API-Key`). For services that can only send static headers (many SaaS
    webhooks). The secret is auto-generated at `/data/webhook_secrets/<name>`;
    read it and give it to the caller.
  - `timestamped_hmac` — caller sends `t=<unix>,v0=<hmac>` (default header
    `ElevenLabs-Signature`). For providers using a timestamped signature.
- **Containment:** a webhook turn is UNTRUSTED third-party content. It runs in a
  restricted runtime (no shell/filesystem/network tools, no plugins) and reads
  memory only at the declared `clearance` (never private). It can notify the
  operator and recall public memory — nothing more. Don't promise a webhook
  trigger can do privileged work; that needs operator-signed `/invoke`.

## Plugin-declared triggers (`plg-…`) are NOT yours to edit

Names starting `plg-` are **plugin-declared** triggers (Release B): they come
from a plugin's `casa.triggers` manifest, never from `triggers.yaml` (the v2
schema reserves the `plg-` prefix — you cannot create one there). They route
at `POST /webhook/plg-<plugin>--<name>` only after install + assignment to
the target resident + the resident declaring the `webhook` channel + the
operator's one-time consent DM. Their state shows in **plugin health**
(`trigger_pending_ack`, `trigger_channel_missing`,
`trigger_unassigned_target`), not in this recipe's files. To change one:
change the plugin (`plugin_update`). Operator off-switch:
`trigger_ack_revoke(name=<plugin>)` — unroutes immediately AND retires the
per-trigger secrets; re-approval (re-prompted on the next plugin mutation or
`casa_reload_triggers`) mints fresh ones, so the plugin's setup tool must
re-provision the external service.
