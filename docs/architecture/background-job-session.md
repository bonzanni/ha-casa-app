---
last_reviewed: 2026-11-10
---

# Background jobs: one long session

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a running background job's session works: the brief and every later write, the one
writer and the one reader per job, what Casa does when a turn ends (continue, nudge once,
park), what happens when the job's CLI process dies or Casa restarts, how context stays
bounded, and the cost shown to the operator. The declaration, `start_job` and the launch are
[`background-jobs.md`](background-jobs.md); the operator-message journal that makes a
restart lose nothing is [`background-job-message-journal.md`](background-job-message-journal.md);
how a write is admitted is [`engagement-turn-admission.md`](engagement-turn-admission.md);
the completion gate is [`engagement-completion-gate.md`](engagement-completion-gate.md); what
the job puts in the operator's direct chat is
[`background-job-direct-chat.md`](background-job-direct-chat.md).

## Mental model

**A job is one long session.** Casa writes one brief into the job's `ClaudeSDKClient` at
launch. The brief's turn does the work, and the model decides when to post progress, when to
ask and when to finish with `emit_completion`. Casa never serialises turns: everything else
— an operator's reply, a tapped answer, an approval's continuation, Casa's
nudge, "Continue the job.", the restart line — is written into the running session as it
arrives. The pinned CLI absorbs input written mid-turn at its next tool boundary, and runs it
as the next turn when no boundary is left.

**One writer, one reader.** `InCasaDriver` keeps, per live job, a delivery queue and one
worker task (`InCasaDriver._work`) that is the only writer into the session: the seams journal,
admit and enqueue synchronously (`submit_operator`, `submit_casa`, `enqueue_brief`) and return
without waiting on the session, so a slow resume never holds the next message back. The
worker resumes the session when none is registered (under the per-engagement lifecycle lock
that every resume owner takes) — except while the job's launch is still enrolled
([`engagement-launch-detach.md`](engagement-launch-detach.md)): the launch owns the job, it
queues the brief at the head of the queue, and anything that arrived meanwhile waits behind it
until the launcher lets go. The worker admits each write immediately before `client.query`,
removes exactly the ticket it wrote (the head can change while a write is awaited), and
stamps it with its ticket's uuid. One reader task per session (`InCasaDriver._read`) consumes
`receive_messages()` for the session's whole life: it hands each message's words and each
main-loop tool step to the agent's log, synchronously and never waiting on Telegram
([`agent-logs.md`](agent-logs.md), INV-TG-011), detects API faults, and at each
`ResultMessage` records the turn's cost and calls the turn-end handler.

**Take-up is the CLI's own word.** Every job client runs with `--replay-user-messages`: the CLI
echoes each written message once, with the uuid it was written with, at the moment it takes
the message into a turn — never at write time. The reader matches that uuid to a written ticket
and discharges it. Nothing else discharges a ticket: no timeout and no frame-order rule.

**Casa never ends a job on its own judgement.** `background_jobs.JobRunner.turn_ended` runs at
every turn end. Input already queued means the CLI runs it next, and a job waiting for the
operator (`origin["job"]["awaiting"]`: it called `ask_operator`, a protected call waits on
approval, or it was parked) is left alone. A turn that hit the step ceiling (`max_turns` 1000
per write) is a continuation boundary: Casa writes "Continue the job." if the job called
`report_job_progress` since the previous ceiling, and parks it otherwise. Any other turn end is
a stop: the first since the operator's last input gets one nudge, the second parks the job.
An API fault is continued once after a minute; a second in a row parks the job. **Parking**
sets `awaiting = "stopped"` and posts one line to the operator's direct chat with the job's
Cancel button (`TelegramChannel.post_job_parked`); the operator's reply to it continues the
job. Operator input resets the guard. Only Cancel, a failed resume and the stale-job reap end
a job.

**Context is bounded by the CLI's auto-compaction.** Every job session is built with the
long-session window env (`claude_runtime.long_session_env`), which lowers the threshold at
which the CLI compacts mid-turn. After a compaction a `SessionStart` callback hook
(`background_jobs.compaction_hook_matcher`) re-states the job header
(`background_jobs.job_header`): title, skill, `Job id:`, `Started by:`, the request, and that
the context was just summarised. `background_jobs.job_session_options` applies all of this —
ceiling, env, echo flag, hook, scratchpad — in the launch, resident-worker and resume builders
alike.

**The job keeps its own notes.** Every job session has a scratchpad under the job's engagement
id, its tool loaded up front, and its prompt says the notes last through compaction and a Casa
restart and go when the job ends. The launch opens it with a `## brief` section (the request, its
context, and the operator's words when the launching turn has them); every change is written to
`/data/job-pads/<id>.pad`, a resume loads that file, and after each compaction the hook adds the
pad's shape and brief to the header. Every path that ends the job deletes the pad, and boot removes
a file whose job has ended or is unknown ([`scratchpad.md`](scratchpad.md), INV-PAD-010,
INV-PAD-011).

**What survives what.** A Casa restart and an unexpected CLI exit both go through
`InCasaDriver.recover`: the replacement session resumes the recorded transcript, the journal is
reconciled against it, and the restart line (or the brief, when the transcript shows the job
never read it) goes first, then every operator message it never read. At most the in-flight
model step and a running tool's unrecorded result are lost, so a plugin must tolerate a tool
call that ran but whose result the model never saw, a fresh claim of its job after a
compaction or a restart, and operator messages between any two steps.

## Contracts & invariants

**INV-BGJOB-016**: A job's session receives Casa's brief once, at launch, and every later input — operator reply, tapped answer, approval continuation, nudge, continue line, restart line — as it arrives, from one writer per job and never held for a turn to end; each write is preceded synchronously by the registry's admission, and the brief and the post-compaction header carry `Started by:` immediately after their first `Job id:` line.

The brief (`background_jobs.launch_prompt`) is the job's title and skill, `Job id: <engagement
id>` — by which a plugin claims work and matches the job-end notice, whose delegation id is the
same id — the starter line, the request, the launch context and the job's rules. Its uuid is
recorded at `start_job` (`origin["job"]["brief_uuid"]`) so a later resume can tell from the
transcript alone whether the job ever read it; `background_jobs.job_brief` re-renders it from
the record for that case. The memory-correction note goes before the brief and before each
operator write.

**Who started the job (#1277).** The line immediately after the first `Job id:` line is exactly
one of `Started by: operator`, `Started by: scheduled` or `Started by: agent`, LF-terminated,
appearing once. The token is Casa's fact, evaluated once at launch from the launching turn's
origin (`background_jobs.job_started_by`) into `origin["job"]["started_by"]`; both renderers
read only that field.
- `scheduled`: a Casa job trigger started the job on its schedule. No agent turn ran.
- `operator`: the job was started in a turn Casa attributes to an authenticated act of the
  operator: their own message; their tap on a button the assistant offered, on a question asked
  in the DM or from a scheduled turn; their Approve tap on a protected call; an operator turn at
  a specialist desk; a specialist's own start at a desk the operator used, or in a delegation
  made in such a turn. It attributes the TURN, not which option was tapped or what drove the
  start inside it.
- `agent`: an agent's turn that the operator did not author or answer started the job: the
  assistant's own scheduled turns, webhooks, an unanswered or cancelled question, setup or
  consent turns.

The evidence is the turn's reserved markers: `_scheduled_job` first, then `_operator_turn` or the
scheduled ask's `_answered_by_operator` ([`scheduled-asks.md`](scheduled-asks.md)), else `agent`.
A delegated start reads the token of the turn that delegated. A record without the field renders
no line; nothing is inferred for it. A look-alike inside `Request:` or `Context:` is model text:
a reader takes the line after the first `Job id:` line, never the first `Started by:` it finds.

**INV-BGJOB-017**: When a job's turn ends with the record live, no input pending, and no question, approval or parking outstanding since the operator's last input, Casa writes one nudge, and a second such end with no operator input between parks the job — it waits for the operator, who gets one line in their direct chat with Cancel; a turn stopped at the step ceiling is continued if the job reported progress since the previous ceiling and parked otherwise; neither path ends the job, and cost never ends or parks one.

The guard runs synchronously in the reader, so a reply admitted before the decision is seen as
pending input and is never mistaken for a stop. A reply admitted just after it may cross the
nudge; both reach the model and the reply resets the guard. Its counters live in memory and
start over on any operator input and on restart; `awaiting` is persisted, because boot reads
it. A parking post replies to the start line when the job has one and is otherwise a new post;
either way it is filed as a job post, so a reply quoting it reaches the job
([`background-job-direct-chat.md`](background-job-direct-chat.md), INV-BGJOB-012). The line is
Casa's own text (the job's title); the job's progress summaries stay in its log. A quiet run
that parks stops being quiet ([`background-job-quiet-runs.md`](background-job-quiet-runs.md)).
The nudge offers `ask_operator` only to a specialist's job; a resident's plugin worker has no
question tool.

**INV-BGJOB-018**: A job session whose message stream ends while the record is live, other than by Casa's own teardown, is removed from the driver and its client disconnected before a replacement opens, then resumed once with the restart line and every journaled message its transcript lacks; only a failed resume or a second unexpected end ends the job `error`, and then each message the transcript lacks is told as lost once.

The dead reader marks the job recovering (the stale reap and the idle suspension skip a
recovering job as they skip a running turn), removes its own session from the map with an
identity check, and disconnects its client explicitly — the SDK's close cancels the session's
outstanding hook and tool callbacks, which an EOF alone does not. Every path that opens a
job's client goes through `InCasaDriver._open_session`, which first waits until no earlier
client of that job is still disconnecting (`InCasaDriver._await_prior_clients`), whatever its
caller waited on before — a lock, a transcript read, a resume. The driver keeps its own list of
every client it registered for a job and has not yet seen retired, whatever took the client out
— a death, the idle suspension, a cancel that ends the job — and no teardown path clears that
list early; it is re-read after every wait. So no callback of the old session
acts after the new one exists, and a message that arrives during a disconnect never runs on a
second client alongside the first. A write
that fails because the CLI died stays at the head of the queue and is written once into the
replacement. The recovery takes the lifecycle lock, re-checks the record, opens the
replacement, reconciles the journal
([`background-job-message-journal.md`](background-job-message-journal.md), INV-BGJOB-021) and
stages its items at the head of the queue; it then clears the recovering mark without waiting
for a write. If the session it is reconciling dies while the transcript is read, it stages
nothing and leaves the dead session's retirement in place: that death's own recovery runs next
under the same lock, waits for the retirement and reconciles against a fresh replacement. The second death, or a resume that raises, ends the job through the finalize
funnel with "its session ended unexpectedly" and its last progress.

At boot, `casa_core._resume_background_jobs` hands each job that was open when Casa stopped —
the set is taken right after the registry loads, before the channels and agent loops start, so
a job launched in the first seconds after start-up belongs to its launch and is never resumed or
ended by boot — to `JobRunner.resume_at_boot`, which re-reads the live record first (a job
cancelled while boot was resuming earlier ones is skipped):
`↻ Resuming after a restart` in the agent's log, then the same recovery with Casa's restart
line ("Casa restarted. Your conversation is intact up to the restart; a tool call that was
running may or may not have finished. Continue the job."). A job waiting for the operator with
nothing journaled is not resumed until their next input reaches it. One attempt: a failure ends
the job `error` with "could not be resumed after a restart".

**INV-BGJOB-019**: Every job session runs with the long-session auto-compaction window env, the step ceiling and the take-up echo flag, and after a compaction its `SessionStart` hook re-states the job header; any other session start adds nothing.

The window is one shared constant (`claude_runtime.LONG_SESSION_COMPACT_WINDOW`) for every
session meant to run long. After the header the hook adds the job's pad (INV-PAD-011). The SDK's hook type does not list `SessionStart`, but the pinned CLI
accepts it as a callback hook, fires it with `source: "compact"` after manual and automatic
compaction, and puts its `additionalContext` into the model's context. At every client open the
driver logs the window and threshold the CLI actually applies (`get_context_usage`, no model
call), which is where to read whether the env took effect on a given model.

**INV-BGJOB-020**: A job's cost is recorded at each turn end as its cost before the client opened plus the CLI's cumulative total, persisted, and shown on its start line, its end paint and its end notice; a completion from inside a running turn is torn down only after that turn ends or a bounded wait passes; and no code path reads the cost to stop or refuse anything.

The CLI reports `total_cost_usd` cumulatively for its own process, which starts again at zero
after a resume, so the job's cost is `cost_base` (the record's cost when this client opened)
plus that total, kept on `origin["job"]["cost_usd"]`. The DM start line of an operator-started
job is edited silently at each turn end and every five minutes:
`📊 Finance started «Accounting check». · 40 min · $2.40` (in the host's own chat, without the
label: `Started «Accounting check». · …`) — elapsed time always, the cost once a
turn has ended. A job's end paints that line `✅ Finance «Accounting check» · 1 h 5 min · $3.90`
(in the host's own chat `✅ «Accounting check» · …`),
and the end notice to the resident carries `Job ran 1 h 5 min · $3.90`, so a job with no start
line (scheduled or agent-started) still shows it.

A job usually finishes from inside a turn: it calls `emit_completion`, and that turn ends only
after the tool returns. The completion's Casa-owned finalization therefore returns the tool
result at once, waits for the completing turn's `ResultMessage` (`InCasaDriver.wait_turn_end`,
at most 30 s), stores the cost it recorded, then closes the session and paints the start line;
the registry's terminal observer leaves such a job's keyboards to that step. Past the bound it
finalizes with the last cost known. Who owns finalization and the completion gate are unchanged
(INV-ENG-003). Between turn ends the cost shown is the last one: no estimate is made.

## Failure behavior

**A turn ends on an API fault.** The first fault since the last successful turn end waits a
minute and writes "Continue the job.", resuming the session first if it died. A second in a row
parks the job with "«title» stopped on an error (<kind>). Reply to continue it, or Cancel."
Operator input during the wait cancels the continue.

**The CLI dies during a write.** The write raises; the ticket stays unwritten at the head of the
queue and the session is treated as lost (INV-BGJOB-018).

**A memory wipe.** A wipe removes every session transcript and marks each open engagement
`context_rebuild_pending`. `InCasaDriver.resume` refuses such a record — there is no transcript to
resume — so a wiped job's next resume fails and the job ends as one that could not be resumed
([`memory-wipe.md`](memory-wipe.md)).

**A completing turn never ends.** The teardown proceeds after the bound with the last cost known;
nothing is reported as an error.

**The agent log is best effort.** A line the log could not post is dropped and not told
([`agent-logs.md`](agent-logs.md)).

## Extension points

A new kind of Casa write goes through `InCasaDriver.submit_casa`, never `client.query`
directly — the worker is what keeps writes ordered and admitted. A new job-session option belongs
in `background_jobs.job_session_options`, so launch, worker and resume builds stay identical.
Anything new the header should carry after a compaction belongs in `job_header`.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.submit_operator`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.submit_casa`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.enqueue_brief`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver._work`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver._read`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver._end_segment`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver._record_cost`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.wait_turn_end`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver._begin_loss`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.recover`
- `casa/rootfs/opt/casa/background_jobs.py::JobRunner`
- `casa/rootfs/opt/casa/background_jobs.py::job_session_options`
- `casa/rootfs/opt/casa/background_jobs.py::job_header`
- `casa/rootfs/opt/casa/background_jobs.py::launch_prompt`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.post_job_parked`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.refresh_job_start_line`
- `casa/rootfs/opt/casa/tools.py::_await_completing_turn`
- `casa/rootfs/opt/casa/casa_core.py::_resume_background_jobs`

**Tests**
- `tests/test_job_session_driver.py`
- `tests/test_job_turn_guard.py`
- `tests/test_job_session_declaration.py`
- `tests/test_job_message_seams.py`
- `tests/test_job_cost.py`
- `tests/test_job_brief.py`
- `tests/test_job_starter_line.py`
- `tests/test_job_starter_regressions.py`

**Related**
- [`architecture/background-jobs.md`](../architecture/background-jobs.md)
- [`architecture/background-job-message-journal.md`](../architecture/background-job-message-journal.md)
- [`architecture/engagement-turn-admission.md`](../architecture/engagement-turn-admission.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/background-job-direct-chat.md`](../architecture/background-job-direct-chat.md)
- [`architecture/memory-wipe.md`](../architecture/memory-wipe.md)
<!-- END SOURCEMAP -->
