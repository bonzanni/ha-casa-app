---
last_reviewed: 2026-11-04
---

# The Stop: a specialist's own work stopped at once

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the operator stops work that runs by itself — a browsing request a specialist's plugin
carries on, or a background job he started — with no model turn and no place on its lane:
the `casa.stopTool` declaration, Casa's direct call of it, the three paths that reach it
(the `stop_work` tool, which stops his background jobs too, a proposal button whose stored
call is the stop tool, the Stop on a card that still shows one), the one flight per request,
the card mark and the line, and the continuations a stop drops. Casa's own cards carry no
Stop: he stops work by asking the agent. A job's cancel funnel, and what its end posts, are
[`background-job-direct-chat.md`](background-job-direct-chat.md). The card and the admission
a stop acts on are [`plugin-operator-calls.md`](plugin-operator-calls.md);
the continuation queue and the driver sweep whose direct call the stop shares are
[`plugin-continuations.md`](plugin-continuations.md); the proposal tap every other stored
call takes is [`stored-call-buttons.md`](stored-call-buttons.md); Casa Browser's own request
cancel is [`../reference/casa-browser.md`](../reference/casa-browser.md).

## Mental model

**A stop never waits for the turn that drives the work.** A continuation turn holds its
specialist's lane while it drives a run, so a stop that went through the lane — a model turn
asked to stop — would wait for exactly the work it is meant to cut. A plugin therefore
declares, in `casa.stopTool` (`plugin_store.manifest_stop_tool`, validated as
`casa.driverSweep` is: a `safe` tool of the result contract that consumes nothing and calls
nothing; an invalid declaration declares nothing), one tool Casa calls itself
(`plugin_stop.call_stop`): Casa starts the plugin's stdio server with the sweep's own
environment (`plugin_continuations.sweep_server`) and calls the tool through
`plugin_continuations.call_plugin_tool` — no model, no session, no lane. The plugins asked
are every loaded specialist's resolved plugins that declare the tool, by the sweep's own
resolution (`plugin_stop.stop_plugins` over `plugin_continuations.declaring_plugins`). The
answer is handed over from inside the client session, so the caller never waits for the
server's teardown, which runs on in a tracked task that Casa's stop cancels and awaits
(`plugin_stop.drain`, after the continuations drain).

**What the tool answers.** Called with no arguments, `{"requests": [{"ref", "tasks",
"state"}]}`: what is running. Called with `{"ref": "request:<key>"}`, the outcome of stopping
that request: `stopped`, `ended` (it had ended on its own), `already_stopped`, `not_taken`
(the plugin could not reach its app) or `unknown`; an answer may carry `tasks`, and one after
which the plugin keeps refusing to drive the request (`stopped`, `already_stopped`,
`not_taken` — `KEEPS_SEAL`) carries `refs`, every continuation reference naming it. Anything else — a timeout
(`STOP_CALL_TIMEOUT_S`, sixty seconds), a failure, another shape — reads as `not_taken`; a
listing that does not come back as a JSON object reads as one the plugin could not make
(`failed`). Casa itself adds `unavailable`, when the role's resolution no longer carries the
plugin declaring the tool, and `in_flight` (below).

**One request, one flight, one telling** (`plugin_stop.stop`). The stops in flight in the
process are keyed by plugin segment, bot and ref: a second stop of the same request, by any path,
answers `in_flight` at once and tells nothing. The path that holds the flight marks the card
and tells the operator while it still holds it (its `tell`), so two taps, or a tap and the
resident, tell him once. On an outcome of `KEEPS_SEAL`, the request's queued continuations
are dropped
(`Continuations.drop`): those of every returned ref, and the admission's own
`admission:<cid>` continuation. A dropped continuation that is waiting for the lane runs no
turn once it holds it; one already started is the running session, left to end itself
([`plugin-continuations.md`](plugin-continuations.md)).

**The mark and the line** (`plugin_stop.MARKS`, `LINES`, `settle_stop`). The request's start
or status card — the admission's `card` address, if one landed, so through the bot that
posted it — is edited to its recorded text plus a mark and loses any keyboard
(`settle_run_card`): `☑ Stopped` (also for `already_stopped`), `☑ Ended`, `☑ Stop asked`
(`not_taken`). A status card an earlier version sent with a reply keyboard can never be
edited: its mark is refused, which `edit_dm_message` takes as an expected outcome (logged at
INFO, not retried). After `stopped`, `ended` and `already_stopped` (`ENDS`) the request no
longer runs: the admission forgets its card (`Confirmations.clear_card`), even when the edit
fails. Any other outcome leaves the card as it was. A Stop tapped on a card or a proposal gets
one line: Casa's fixed text under the specialist's label, posted as a reply to the card or
proposal the Stop came from (`tell_line` through `post_line`,
`TelegramChannel.send_reply_line`), with no keyboard; it is filed under the task the card belongs to, read from
the durable task index — live or ended, and after a restart too — so a reply to it reaches
whatever a reply to the card reaches; it adds no memory line, and it is recorded as one note
for the agent of that chat under the same label (its plain name in the agent's own chat), which
hears it at its next turn: `Stopped.`, `It had already
ended.`, the `not_taken` line saying the browsing stops as soon as Casa Browser answers, and
for `unavailable` that the specialist's browser is no longer set up. `already_stopped` adds
no line: he has been told.

**A request that ends on its own loses its Stop too** (`plugin_stop.settle_ended`). The
plugin's driver sweep answer may name, in `ended`, the requests that ended without a Stop —
`[{"ref": "request:<key>", "binding_ref"}]` ([`plugin-continuations.md`](plugin-continuations.md)).
For each one whose binding is the plugin's own and belongs to the operator as configured now,
and whose admission is `admitted` and still has a card, the card gets the mark a Stop that
finds it ended would give it, `☑ Ended`, with no keyboard, and nothing is posted: his answer
tells him the rest. Nothing is marked while a Stop of the request is in flight (that path
marks it). The plugin names a request on every pass for a while, so the admission forgets its
card (`Confirmations.clear_card`) after one edit attempt, landed or not; no later pass edits
it again. An inline Stop the edit could not remove answers `It had already ended.`
When the binding, the channel or its bot is not there yet (a Telegram rebuild), nothing is
tried and the card is kept for the next pass.

**The Stop on a card** (`TelegramChannel._on_run_stop_callback`, callback data
`v1|run_stop|<cid>|0`, its own DM-scoped namespace): Casa posts its cards without one, and the
callback acts only for a card an earlier version posted that still shows one. Fail closed, answered exactly once: the
admission (`run_admission.CONFIRMATIONS.peek`, durable for thirty days, so a Stop after a
restart still works) must be `admitted` and its binding known, else `expired`; the tap must
come from the binding's chat and from the admission's operator, who is the binding's
operator and the operator as configured now, else `not for you` (`_run_stop_allowed`). Then
the toast `stopping…` at once, and one tracked task (`_spawn_run_stop`) stops `request:<key>`
of the admission's plugin for the binding's last role, marks the card and posts the line.

**A proposal button that stores the stop tool** (`TelegramChannel._stop_tap`, called by the
finish hook before it edits the card, and first by `_dispatch_proposal_tap`). A plugin's own card — browsing's long-run "still going" card —
may carry a Stop button whose stored call is its declared stop tool. The tap passes the
proposal's admission chain and commit as every tap does
([`stored-call-buttons.md`](stored-call-buttons.md)); then, when the stored arguments are
exactly `{"ref": <string>}` and the stored tool is the stop tool the role's current
resolution declares for the proposal's own plugin segment and artifact, it reserves no lane
place and runs no pinned turn: one tracked task stops the request, the proposal's last line becomes
the outcome's mark (`✖ expired` for `unknown`, `✖ unavailable`, `☑ Stop` when another path
holds the flight, `✖ failed` when the task raises), the request's start or status card is
marked too, and the line is posted as a reply to the proposal. Any other stored call goes to
the agent's lane as before.

**`stop_work`** (`tools.stop_work`): the operator stops work by asking the agent he is
talking to. The resident has it from its role files; every specialist's own-chat, delegated
or pinned build is granted it in code (`tools._stop_work_grants`, the condition of
`_guest_grants`), and no engagement or job build is. Refused unless the turn is the
operator's own live message — the spawn cap's `_is_agent_context` is false (the
server-stamped operator marker, no engagement, no delegation) and the sender is the
configured operator in his own chat — so voice, narration, scheduled, delegated and group
turns cannot stop anything. It reaches only his own work: a request whose admission
(`plugin_stop.visible_confirmation`) is not this operator's, and a job whose origin does not
name him, are neither listed nor stopped. An admission asked on the agent's own bot is found there; one
asked on another bot is found when this agent asked for that work — the root of its
binding's chain — as a delegation to an agent with its own chat runs in that agent's chat
([`own-voice.md`](own-voice.md)).

With no `ref` it lists every stop plugin's running requests, each with its ref
(`request:<key>`), its specialist's name, its tasks and its state, and says of a specialist
whose listing `failed` that what it is doing could not be listed right now; then each live
background job of his (`_his_live_jobs`: an active or idle job record — its origin holds a
`job` — whose origin names him, whoever started it — he, a schedule or an agent; another
engagement of his is no job and is neither listed nor stopped), as `- job:<engagement id>: <Agent> — <title>
(running)`; `Nothing is running.` when there is neither. With a `request:` ref from that
list it stops the request through the same flight and marks its card, posts nothing, adds
no note for the agent, and returns a short instruction: the agent says in one plain line
what stopped, and nothing about how it found it, or adds nothing when another path has
already told him (`already_stopped`, `in_flight`). A ref no plugin knows is an error naming
the listing. With a `job:` ref (`JOB_REF_PREFIX`) it runs the job's cancel funnel
(`_finalize_engagement`, outcome `cancelled`, as a Cancel button does, with
`STOP_WORK_CANCEL_TEXT`: the outcome, then a note telling the end's narration he was already
told, so where he asked it need add nothing) and answers
`Stopped «<title>».`; a job that is not among his live ones, or whose terminal write another
writer won, answers `It had already ended.`, and a cancel whose write rolled back answers
`The job could not be stopped just now — try again.` as an error, the job still live. A
funnel that raises is logged, as the Cancel button's is, and answered by what the record then
shows. The
resident's prompt tells it to stop such work itself, never by delegation, to ask which one
when several run and the person's words do not say, and otherwise to write nothing to the
person until it is stopped (the tool's description says the same: unless it must ask,
nothing between the two calls), then one plain line of what stopped, never how it was found
or what else runs.

## Contracts & invariants

**INV-PLUG-056**: A Stop of a request admitted for the configured operator runs no model turn and takes no place on its agent's lane; after its plugin answers that it keeps the request stopped, no queued continuation of that request starts a turn.

Enforced by `plugin_stop.call_stop` calling the declared tool through Casa's own stdio
client, by `_on_run_stop_callback`, `_stop_tap` and `stop_work` reaching it outside any lane
(the first in a task of its own, `_stop_tap` before `_dispatch_proposal_tap` reserves a lane
place), by `_on_run_stop_callback` checking the admitted admission, the chat, the sender and
the configured operator (`_run_stop_allowed`), `_stop_tap` running only after the proposal's own admission
chain (INV-PROP-001), `stop_work` checking the live operator turn and the configured operator
and starting no turn beyond the calling agent's own, and by `Continuations.drop` marking a
pending continuation dropped, which `run_continuation` checks once it holds the lane and
before the turn starts.

What it does not cover: a continuation whose turn has already started, which runs on as the
session driving the work and ends as the plugin lets it; a continuation the plugin queues
after its answer; a ref the plugin's `stopped` answer does not return; and whether the
plugin's app has actually stopped the work — Casa records only the plugin's answer, and a
`not_taken` request is left to the plugin.

## Failure behavior

**The plugin does not answer within sixty seconds, fails, or answers another shape.**
`not_taken`: the card reads `☑ Stop asked` and the line says, in the agent's voice, that it
will stop as soon as the browser answers; a call still running at the timeout is cancelled. **The plugin does
not know the request** (`unknown`). The request keeps its card and no line is posted; a
proposal reads `✖ expired`. **The plugin is no longer resolved for the role.**
`unavailable`: no card mark, the line says so. **The card edit or the line fails.** It is logged; the stop already ran. **The stop
task raises.** Logged; the request keeps its card, on a proposal the last line is
`✖ failed`. **Casa restarts.** The flights are gone with
the process; admissions and job records survive, so a card's Stop and `stop_work` still
work. **A card edit fails.** It is tried once; the card is forgotten all the
same after `stopped`, `ended` or `already_stopped` (`settle_stop`, `settle_ended`), and an
inline Stop left on it says the request had already ended. **No card landed**
(the status card's post failed). Nothing is marked, and `stop_work` still stops the request.
**A Stop key from an earlier version is still at his keyboard.** Its press arrives as his
own text, `⏹ Stop «…»`, and is an ordinary message to the chat's agent, which can call
`stop_work` for it. Casa sends nothing to take such a keyboard down.

## Extension points

**A plugin with work that outlives a turn** declares `casa.stopTool` beside
`casa.driverSweep`, answers the listing and the outcome shapes above, and returns in `refs`
every continuation reference that names a request it keeps from being driven.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_stop.py`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._spawn_run_stop`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_stop_tool`
- `casa/rootfs/opt/casa/tools.py::stop_work`
- `casa/rootfs/opt/casa/tools.py::_stop_work_grants`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_run_stop_callback`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._stop_tap`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.send_reply_line`
- `casa/rootfs/opt/casa/plugin_continuations.py::Continuations.drop`

**Tests**
- `tests/test_plugin_stop.py`

**Related**
- [`architecture/plugin-operator-calls.md`](../architecture/plugin-operator-calls.md)
- [`architecture/plugin-continuations.md`](../architecture/plugin-continuations.md)
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`architecture/background-job-direct-chat.md`](../architecture/background-job-direct-chat.md)
<!-- END SOURCEMAP -->
