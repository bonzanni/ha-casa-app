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
6. **Prompt?** (interval/cron/date only) One imperative sentence, plus the
   closing-silence clause below when the turn delivers its own message. **A
   webhook trigger has no prompt and the schema refuses one** — see "Webhook
   triggers" below for what its turn actually receives. If the operator
   describes what the agent should *do* when a webhook fires, tell them that
   before writing it: the instruction cannot be stored on the trigger.
7. **Webhook auth?** (webhook only) how does the caller authenticate — see below.

## Every scheduled prompt says how the turn ends

A scheduled turn delivers TWICE when its prompt tells the agent to send a
message and says nothing about the closing text: the send goes out at once, and
the turn's own final text is then delivered to the same chat as a second
message ("Sent."). Casa never suppresses that final text — a scheduled turn
that legitimately has something to say must still be heard, and a correction
after a send must reach the operator — so the prompt is what closes the gap.

For interval/cron/date prompts whose turn delivers its own message, keep
the send instruction first and unconditional, and end the prompt with:
After the send, output the sentinel `<silent/>` and nothing else.

**The question is never "does this turn call a tool". It is: where does the
operator's copy of the message come from?** Exactly two answers exist, and they
take opposite endings:

- from a DELIVERY tool call — `send_message` or `send_media` — which puts the
  message in the chat by itself, leaving the turn with nothing left to say. This
  is the shape that needs the clause, and it is the shape a reminder's generated
  prompt has.
- from the turn's OWN final text, which Casa delivers when the turn ends. This
  shape must NOT be given the clause: the sentinel would be the whole final text,
  the turn would be suppressed, and the operator would get nothing. The heartbeat
  and morning-briefing defaults are this shape — they tell the agent to output
  ONLY the final message text — and so is any turn that calls tools to look
  something up and then REPORTS what it found.

A tool call that is not a delivery decides nothing here. A turn may read the
calendar, query Home Assistant, or acknowledge a background wake and still be
the second shape, because none of those put anything in the operator's chat.

**Decide which before you write the prompt, and end it accordingly.** Both
endings, written out:

    # shape A — the operator's copy of the message arrives from a DELIVERY
    # tool call (send_message / send_media), so the turn has nothing left
    # to say. Any other tool the turn calls is irrelevant to this choice.
    prompt="Send this exact message via telegram: \"Bins out tonight.\" After the send, output the sentinel `<silent/>` and nothing else."

    # shape B — the operator's copy arrives as the turn's OWN final text. No
    # closing clause; the sentinel appears only as the way to say nothing at
    # all. A turn that calls tools and then REPORTS the result is this shape.
    prompt="Output today's forecast as the final message text, with no preamble. If there is nothing worth sending, output the sentinel `<silent/>` and nothing else."

Neither ending is the default. Copying shape A onto a shape-B prompt loses the
delivery; leaving shape A's ending off a shape-A prompt costs a second message
on every firing.

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
        prompt="<one-line imperative, ended as its shape requires — see above>")

    # webhook — served ONLY at POST /webhook/<name> (no `path` field; it was
    # removed in v0.97.0). The agent must declare the `webhook` channel.
    config_trigger_upsert(
        role="<role>",
        name="<trigger_name>",
        type="webhook",
        clearance="public",        # public|friends|family — memory tiers this
                                   # webhook's turns may recall (NEVER private).
        auth={"mode": "static_header",   # hmac_body|static_header|timestamped_hmac
              "header": "X-API-Key",     # OPTIONAL — omit to take the mode's
                                         # default; set it when the caller
                                         # mandates a particular header name.
              "tolerance_secs": 300})    # timestamped_hmac only

Read the file first if you need to see what is already there — reads are fine.
The tool validates the entry against the triggers schema and refuses without
writing anything, so a rejection leaves the operator's config untouched. It
refuses a name the resident owns (`managed_by: agent` — those are its
reminders); ask the resident to change one of those instead.

### Add agents/<role>/prompts/<trigger_name>.md (cron/interval only)

    You are <name>. The <trigger-name> trigger just fired. <Task description.>
    <closing line for this prompt's shape — see "Every scheduled prompt says
    how the turn ends" above.>

A prompt file ends the same way a `prompt=` string does, and the choice is the
same one: shape A's closing clause when the task description tells the agent to
deliver with `send_message` or `send_media`, shape B's no-clause ending when the
turn's own reply is what the operator reads.

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
    `X-Webhook-Signature`). For providers using a timestamped signature. The
    default names no vendor: if the caller mandates its own header name —
    `ElevenLabs-Signature`, say — write that name into `header` explicitly.
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
