---
last_reviewed: 2026-11-13
---

# Agent logs

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The optional, read-only log each background agent keeps in a Telegram forum group: when it is
on, whose work goes to which topic, what one line says, the writer that posts it within the
group's request budget, the registry and the retention that deletes each post before Telegram
stops allowing it, and what happens to anything a person writes in the group. What a job puts
in the operator's direct chat — the 👍 on the request (or the start line in its place),
questions, replies, the end — is
[`background-job-direct-chat.md`](background-job-direct-chat.md); none of it depends on the
log. The option is in [`reference/operator-options.md`](../reference/operator-options.md).

## Mental model

**The direct chat is where an agent needs the operator; the log is where it can be watched.**
Nothing that needs the operator goes to the log, and nothing is decided from it: a job starts,
asks, is steered and ends in the direct chat whether or not a log group is configured
(INV-BGJOB-023). With no group, there are no logs and nothing mentions them.

**One topic per agent, created on first use.** A specialist and the configurator each log
under their role (`LogKey("agent", role)`); a resident that hosts plugin jobs logs them under
`LogKey("plugins", resident)`, in a topic named `<Resident> · plugins`. A resident's own turns
never log. The topic is named by the agent's display name, cut to 128 characters. Every line
is prefixed with the run it belongs to — the first line of a job's title, cut to 40
characters, or `Request HH:MM` for a delegated run, its start in Casa's local time — so one
topic can hold several runs:

```
Q3 accounting · ▶ started
Q3 accounting · 🔧 Searching memory
Q3 accounting · I found 41 unmatched rows; classifying them now.
Q3 accounting · 📊 Classified 25 · 25 done · 16 left
Q3 accounting · ✅ completed — Tagged 41 rows; 2 need your decision.
```

**Who writes a line.** Every producer runs on Casa's event loop and makes one synchronous call:

| Producer | Line |
|---|---|
| A job's launch, after its brief is queued (quiet runs too) | `▶ started` |
| A job's or plugin worker's session reader: each message's words, and each main-loop tool step (a sub-agent's steps are not logged) | the words; `🔧 <step>` |
| `report_job_progress` | `📊 <summary> · n done · m left` |
| A job resumed after a restart | `↻ Resuming after a restart` |
| The finalize funnel | `✅`/`🛑`/`❌ <outcome> — <first summary line, 300 characters>` |
| The launch-death reporter; a cancellation whose terminal write rolled back | `⚠️ stopped before reporting a result`; `⚠️ could not be recorded` |
| A delegated run of a specialist or the configurator (`_DelegatedRunLog`): a delegation, a run Casa carries on after an approval, a plugin continuation | `▶ started`, its words (a leading or closing run of `<silent/>` dropped; a message of markers alone adds no line) and steps, `✓ answered` or `⚠️ aborted` |

A step (`drivers/turn_progress.py::step_log_line`) of a built-in tool or one of Casa's own
reads as its activity, `🔧 Reading files`; any other MCP tool names itself, underscores read as
spaces, and where it comes from: `🔧 daily sync (bank-feed)` for a plugin's tool, the plugin
found among the published registry's plugins, and `🔧 get state (homeassistant)` for another
server's.

The configurator's step line in the direct chat is posted only while the log is off; with the
log on its steps go to its topic instead ([`configurator.md`](configurator.md)).

**A sink, a writer, a registry.** `AgentLogs.log` appends to an in-memory queue (2,000 lines
across all keys; an overflow drops the logging key's oldest lines, and the next post for that
key starts with `… N lines skipped`). One writer task serves the keys with queued lines
round-robin, one Telegram request per turn, so one busy agent cannot starve the others; lines
that arrive while a key waits ride its next request. A key's lines go into a rolling plain-text
message, edited in place with its whole text until the next line would pass 4,096 characters,
the message has been idle five minutes or is an hour old; then a new message is sent. A line
logged several times in a row — a step repeated call after call — is shown once with its
count (`Q3 accounting · 🔧 Reading files ×4`); a repeat of the line the rolling message ends
with is counted there, in place, and a new message starts its own count. Every
request the log makes to the group — the rights check, topic creation, send, edit, delete,
bulk delete — spends one token from one budget of 18 requests per 60 seconds, which keeps the
bot inside Telegram's per-group limit whether or not edits count towards it. A flood wait
pauses the whole budget. The registry, `/data/log-topics.json`, written privately and at most
once per ten seconds, keeps the group it belongs to, each key's topic id and every post's id
and send time.

**When the log is on.** `telegram_engagement_supergroup_id` names a group, and the bot is an
administrator of it with the "Manage topics" right. The channel checks after every rebuild of
its Telegram application, and the 30-minute retention pass checks again while the log is off;
a failed check leaves it off with one WARNING (none when no group is configured). The first
group a process checks is bound for the life of the process; a different id takes effect after
a restart, and the registry of a group no longer configured is forgotten at start.

## Contracts & invariants

**INV-TG-011**: A producer's log call is synchronous — it queues the line and returns, never awaits Telegram and never raises; every request to the log group spends the log writer's own budget, which nothing outside the log waits on, and the channel's rights check never waits for that budget.

A failure inside the sink is logged as one WARNING per minute at most and never reaches the
producer. The session reader that carries a job's turns hands each line to the sink and drops
whatever the call returns, so no Telegram wait, flood wait or failed post can delay a job's
take-up, its turn end or anything in the direct chat. What it does not cover: lines queued when
Casa stops are lost, and a restart starts new rolling messages rather than continuing the old.

**INV-TG-012**: The log never posts without its key's topic thread id: a key with no topic gets one created first, and a send without a thread id raises instead of posting into the group's General topic.

**INV-TG-013**: A line is filed only under the log key of a specialist or the configurator (its role) or of a resident's plugin worker (that resident's plugin log); a record or a delegated run of any other kind has no key and logs nothing, and each key keeps one topic in the registry until Telegram reports it gone.

A topic Telegram reports missing, closed or deleted is forgotten and created again once, and
the lines are posted there; losing it a second time drops those lines with one WARNING.

**INV-TG-014**: The log never shows words written after a protected call that is still waiting on the operator's approval: they are held, logged as soon as a consume of the same grant clears the cut, and never logged when the turn or run ends with the cut in place.

The cut is the approval cut of #1207 ([`output-scope-properties.md`](output-scope-properties.md)),
read in two places: the job session reader for a job's turns, and `_DelegatedRunLog` for a
delegated run. A step names the tool, never its outcome, so it is not held. What it does not
cover: the session transcript and memory keep the words on every turn.

**INV-TG-015**: The retention pass asks Telegram to delete only posts more than 44 hours past their first send, in bulk requests of at most 100 ids, retrying a refused batch one id at a time; a post more than 47.5 hours old when the pass starts is forgotten without a request, and a flood wait ends the pass rather than being waited out.

A bot may delete a message only while it is younger than 48 hours, and one undeletable id fails
a whole bulk request. An edit does not extend a post's age; the hour cap on a rolling message
bounds how much later text one post can hold. Deleting the bot's own posts needs no admin
right, so retention runs while the log is off for want of the right. What it does not cover: a
post whose window passed while Casa was down, or while the budget was saturated, stays in the
group (one WARNING per pass with the count).

**INV-TG-016**: Anything a person writes in the log group — text, a command or a file, in any topic or in General, the operator or another member — draws at most one pointer to the direct chat per person per boot, posted in the thread they wrote in, and reaches nothing else: no job, no memory, no model; a bot's message draws nothing.

The pointer is `This group is a read-only log. Talk to me in our direct chat.`, sent directly
by the channel (`_redirect_from_log_group`), not through the writer; once sent, it is recorded
with the log's posts (`AgentLogs.note_post`), so retention deletes it like a log post. Its seen-set is shared by
the text and file paths and lives in memory, so a restart may point the same person once more.
Logs are visible to every member of the group; the operator decides who is in it.

## Failure behavior

| Failure | Behaviour |
|---|---|
| The bot is not an administrator, lacks "Manage topics", or the group cannot be reached | Logging is off with one WARNING; `log()` does nothing until a later check succeeds. |
| Telegram asks the writer to wait (`RetryAfter`) | Every request to the group pauses for the stated time; the lines stay queued. |
| An edit is refused as not modified | Counted as done. |
| The message to edit is gone or can no longer be edited | A new message is sent. |
| The topic is gone, closed or deleted | Created again once (INV-TG-013). |
| "not enough rights", "chat not found", the bot was kicked | Logging is off until the next check; queued lines are dropped. |
| Any other refusal | That request's lines are dropped with a WARNING; the writer continues. |
| The registry file is corrupt | Archived as `log-topics.json.casabak`; the log starts with no topics. |

## Extension points

**A new producer** calls `background_jobs.log_job_line` for a job record, or `AgentLogs.log`
with a `LogKey`, synchronously; it never awaits and needs no `try`. A producer that shows an
agent's words reads the approval cut first (INV-TG-014).

**A new key kind** adds a `LogKind`, its topic title in `topic_name`, and the rule that decides
which runs map to it — beside `job_log_key` and `_DelegatedRunLog`, where the two existing
rules live.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent_logs.py`
- `casa/rootfs/opt/casa/background_jobs.py::job_log_key`
- `casa/rootfs/opt/casa/background_jobs.py::log_job_line`
- `casa/rootfs/opt/casa/tools.py::_DelegatedRunLog`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.check_agent_logs`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._redirect_from_log_group`
- `casa/rootfs/opt/casa/casa_core.py::_agent_log_retention`

**Tests**
- `tests/test_agent_logs.py`
- `tests/test_agent_log_producers.py`
- `tests/test_telegram_engagement_routing.py`

**Related**
- [`architecture/background-job-direct-chat.md`](../architecture/background-job-direct-chat.md)
- [`architecture/background-jobs.md`](../architecture/background-jobs.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/output-scope-properties.md`](../architecture/output-scope-properties.md)
- [`architecture/configurator.md`](../architecture/configurator.md)
- [`reference/operator-options.md`](../reference/operator-options.md)
<!-- END SOURCEMAP -->
