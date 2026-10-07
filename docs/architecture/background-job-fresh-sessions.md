---
last_reviewed: 2026-10-05
---

# Background jobs: fresh sessions

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

A plugin-declared background job's `session` mode and what `fresh` does: the conversation
reset before every turn after the launch, the job brief, the session ids a fresh job keeps,
the `Started by:` line of the launch prompt and the briefs, and a refused completion a fresh
job's later briefs name. The declaration, the launch, the batch loop, the progress tool and
resuming a job after a restart are [`background-jobs.md`](background-jobs.md); the engagement
a job runs in is described by [`engagements.md`](engagements.md) and the documents it routes to.

## Mental model

A job also declares how its turns see the conversation, with `session`. `resume`, the
default, runs every turn in one conversation. `fresh` starts every turn after the launch — a
batch, an operator message, any later turn — in a fresh conversation that begins with the
job brief, so no earlier turn's messages are visible to it. The engagement keeps its one
client: under the turn lock, after the turn is admitted, the driver sends the CLI's `/clear`
through that client, drains it to its result, and goes on only once the CLI confirmed the
reset; it then asks the admission again and sends `job_brief(rec)` followed by the turn's
own text. The brief is the title, the job id, the skill to load, the record's `task`, the
launch context — recorded verbatim at launch in `origin["job"]["brief_context"]` — and the
batch rules.
Every session id a fresh job's client reports, the launch's and each reset's, is appended
once to `origin["job"]["sids"]` (`engagement_registry.JOB_SIDS_KEY`) and persisted when first
seen — the list the transcript reaper reads (INV-ENG-022).
That includes the outgoing id a reset confirmation carries, which is the only frame naming a
clearance-rebuilt client's own session, and a clearance downgrade leaves the list in place.
A restart resumes the recorded session exactly as for any job, and the first turn's reset
then drops that history. Counters, judgment, caps, the sweep, the tool set and the launch
turn are the same in both modes.

## Contracts & invariants

**INV-BGJOB-005**: Every turn of a fresh job after its launch is preceded, under the turn lock, by a conversation reset that is confirmed before the turn is accepted, and begins with the job brief built only from the record's task, its clearance-governed launch context, and Casa's record of who started it; a resume-mode job and every non-job engagement are unchanged.

The reset sits after the turn's admission (a terminal engagement is never reset) and before
the ticket is accepted and the prompt sent, inside the lock that already serialises every
turn, so it can never land inside another turn: an operator message arriving mid-batch waits
on that lock and is then reset and briefed on the same client. It runs on every such turn,
not only after a served one, which is what makes freshness hold without a counter: after a
restart the resumed history is dropped by the first turn's reset, and after a clearance
rebuild the reset is a harmless no-op. The reset awaits, so the admission is asked again,
synchronously, once it is confirmed: a `/cancel` that committed during the reset refuses the
turn with nothing accepted and nothing sent. A clearance downgrade drops `brief_context` in
the same step that withholds the task ([`memory-scoping.md`](memory-scoping.md)), and the
brief then says the context is withheld, so nothing the clamp withheld returns through it.
The launch prompt and every brief also carry one `Job id: <engagement id>` line — an
identifier, not launch material, so it stays through a downgrade — by which a plugin claims
work and matches the job-end notice, whose delegation id is that same engagement id.

**Who started the job (#1277).** In the job's launch prompt and in every fresh-turn brief,
the line IMMEDIATELY AFTER the FIRST line `Job id: <job id>`, before `Request:`, is exactly
one of `Started by: operator`, `Started by: scheduled` or `Started by: agent`: the whole line,
LF-terminated, with no leading or trailing spaces and no variable part. It appears exactly
once. A resume-mode job has it in the launch prompt only. A clearance downgrade keeps it, as it
keeps `Job id:`. The token is Casa's fact, recorded once at launch and unchanged across
restarts: `_launch_interactive_engagement` evaluates it from the launching turn's origin
(`background_jobs.job_started_by`) into `origin["job"]["started_by"]`, and both renderers read
only that field, never the origin's markers.
- `scheduled`: a Casa job trigger started the job on its schedule. No agent turn ran.
- `operator`: the job was started in a turn Casa attributes to an authenticated act of the
  operator: the operator's own message; the operator's tap on a button the assistant
  offered, on a question asked in the DM or from a scheduled turn; the operator's Approve tap
  on a protected call; an operator turn at a specialist desk; a specialist's own start at a
  desk the operator used, or in a delegation made in such a turn, as Casa's release notes for
  that version state. It attributes the TURN. It does not say which option was tapped, or what
  drove the start inside that turn. A start made in such a turn reads `operator` even when the
  operator tapped "No", or when a scheduled turn's own instructions drove it.
- `agent`: an agent's turn that the operator did not author or answer started the job. This
  includes the assistant's own scheduled turns, webhooks, an unanswered or cancelled question,
  and setup or consent turns.

The evidence is the turn's reserved markers: `_scheduled_job` first, then `_operator_turn` or
the scheduled ask's `_answered_by_operator` ([`scheduled-asks.md`](scheduled-asks.md)), else
`agent`. A delegated start, waited for or not, reads the token of the turn that delegated.
No line (a record written before the release) means "Casa did not say"; nothing is inferred
for it. The line is Casa-written at a position no model-written text occupies; a look-alike
inside `Request:` or `Context:` is the model's text, and a reader takes the line immediately
after the first `Job id:` line, never the first `Started by:` it finds.
What it does not cover: the launch turn, which runs its own launch prompt with no reset and
no brief; and the transcript files of earlier conversations, which stay on disk while the
job runs — once it is terminal, the transcript reaper deletes every session the list names
([`engagement-transcript-reaping.md`](engagement-transcript-reaping.md)).

**INV-BGJOB-007**: A completion a fresh job's worker requested and the completion gate refused for unread input is recorded on the job and named in every later brief until the record is terminal — without its text once the clearance was lowered; while it is recorded, a held ingress reservation holds the next batch back, and a job that reaches its batch cap or the no-progress guard is spared one batch, once per job.

Otherwise the fresh turn that reads the message would forget the `unread_inbound`
refusal's "read it, then complete".
`emit_completion` records `origin["job"]["completion_pending"]` (the status and the bounded
text) at both of its refusal sites, and `job_brief` adds one line naming the status and the
text's first line and asking for the completion again. Nothing clears it: an accepted
completion is a terminal transition, and no terminal record is briefed. Its text was authored
at the job's tier, so a clearance downgrade keeps only the status, and a refusal recorded
after the downgrade (a turn begun before it) records only the status. The spare covers input
that never becomes a turn before the limit check — an ingress reservation released without
a ticket, or a restart, which drops unread tickets — and is marked `pending_spared` with the
batch's counters. What it does not cover: Casa never completes a job itself, and a
resume-mode job records nothing — its conversation remembers the refusal.

## Failure behavior

**A fresh job's conversation reset fails.** A `/clear` whose drain raises, ends without the
CLI's reset confirmation, or ends without a clean result fails the turn with
`ConversationResetError` before its ticket is accepted and before its prompt is sent. It
takes the delivery task's ordinary failure path, exactly as a failed client query does: the
topic's `Turn failed` line and the job's `a batch failed: ConversationResetError` finalize
(INV-BGJOB-002). A CLI that cannot reset its conversation is a broken client, and nothing
retries it.

## Extension points

A new job field belongs where [`background-jobs.md`](background-jobs.md) says.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver._reset_conversation`

**Tests**
- `tests/test_job_fresh_conversation.py`
- `tests/test_job_pending_completion.py`
- `tests/test_job_starter_line.py`
- `tests/test_job_starter_regressions.py`

**Related**
- [`architecture/background-jobs.md`](../architecture/background-jobs.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/memory-scoping.md`](../architecture/memory-scoping.md)
<!-- END SOURCEMAP -->
