---
last_reviewed: 2026-11-08
---

# A background job in the operator's direct chat

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a background job puts in the operator's direct chat and what they can do there: the start
line and its Cancel button, a specialist job's questions (`ask_operator`) and their buttons,
replies into a live job, and how those keyboards are retired when the job ends. What a job is
and how its batches run is [`background-jobs.md`](background-jobs.md); the notice a message
the job never took up draws is
[`engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md); what a quiet scheduled
run changes, and how interacting with one ends its quietness, is
[`background-job-quiet-runs.md`](background-job-quiet-runs.md); how a reply to a post that is
not a live job's reaches a desk is [`specialist-desk.md`](specialist-desk.md).

## Mental model

**The direct chat is where a job needs the operator; its topic is where it works.** A running
job says nothing in the DM unless it needs a decision or has its result. A job the operator
started (directly, or through an agent acting on their message — the record's starter is
`operator`) gets one silent line, `📊 <Agent> started «<title>».`, with a **Cancel** button; a
scheduled job, or one an agent started on its own, gets none. A specialist's job asks with
`ask_operator(question, options?)`: a plain post under its label, one button per option and a
**Cancel job** button. A resident's plugin worker has no question tool; it parks what needs an
answer and reports it, and its plugin's delivered posts reach the DM as before.

**Ownership lives on the engagement record.** The post map is memory-only and bounded, so it
cannot say which live job owns a DM message. The record does: `dm_posts` lists the job's start
line, questions and plugin posts (newest last, unbounded while the job lives), `ui_posts` the
ones carrying a keyboard, and `questions` the asked questions by a random id. The channel files
each post as it lands (`EngagementRegistry.record_job_post`, called from the post-landing seam
for plugin posts and from the start-line and question posters), and the registry keeps an index
from message id to record, rebuilt at load.

**"Live" means live or pending-terminal.** A strict terminal transition commits in memory before
its write and rolls back if the write fails. While that write is pending the record still
counts as live (`EngagementRegistry.counts_as_live`) for filing and routing; a turn routed then
waits for the write to settle in the shared readiness step, and is delivered after a rollback or
refused (and told) after a commit.

**A reply or a tap goes into the job, or is told — never elsewhere.** A reply quoting a post the
record holds goes into that job as an operator turn
(`TelegramChannel.deliver_operator_reply`), named with the first line of the post it answers.
A tap on a question's option is resolved against the record — never a memory-only broker — so
it works after a restart, and delivers `[operator answered «<question>»: <option>]` through
`deliver_system_turn`. Neither is ever rerouted: whatever the job does not take up draws one
notice, from the holder of its admission ticket, in reply to the operator's message or to the
tapped card (INV-ENG-029). A question may appear twice (asked again, or a send that timed out
but landed); either copy answers while the job lives.

**Whoever sees the record terminal last retires the keyboard.** The registry's terminal
observer — told by every in-process terminal winner: the finalize funnel, the launch-death
report, the reap, the legacy direct mutators — has the channel take the record's keyboard posts
in one synchronous step and edit them away: the start line to `✅`/`🛑`/`❌ <Agent> «<title>»`,
a question to no buttons. A keyboard post that lands after the terminal write settled is
retired by the code that filed it. At boot, terminal records still carrying keyboard posts are
retired once. A keyboard an edit could not remove keeps its buttons; a tap on it reads the
terminal record and gets "already finished".

## Contracts & invariants

**INV-BGJOB-011**: A job's direct-chat posts — its start line, its questions and its plugin's delivered posts — are filed on its engagement record as they land, without a cap while the record counts as live, persisted, and indexed by message id again at load; a record counts as live while active or idle and while its strict terminal write is pending, and the write that settles the terminal transition stores the record's posts and questions empty, after which they leave the index.

**INV-BGJOB-012**: An authenticated operator's reply quoting a post the job record holds, while that record counts as live and is their job in that chat, is delivered into that job as an operator turn — ahead of the desk route — with its admission ticket taken synchronously at the seam's entry; it is never sent to another route, and if the job does not take it up its one notice comes from the ticket's holder (INV-ENG-029).

A reply to a post of a job that has ended, or to a post no record holds, takes the routes it
took before: the desk ([`specialist-desk.md`](specialist-desk.md), INV-DESK-005), or the
resident with its reply note (INV-DESK-004). The reply's words are admitted as they typed them —
what the completion gate counts and what memory keeps (INV-MEM-044) — and the turn the job
receives carries Casa's `[replying to «…»]` line before them. A reply holds the next batch back
like any queued turn (INV-BGJOB-001).

**INV-BGJOB-013**: A specialist job's question is a filed post whose option buttons carry the engagement id, a random question id redrawn until the record holds no question with it, and the option's index, and whose Cancel button carries the engagement id; job buttons are matched before the generic callback parser; a tap is resolved against the record only — the operator whose job it is, a record that counts as live, a question id it holds and an option it has — and takes the inbound reservation before its first await, so a successful completion is refused from that instant; a question id the record does not hold delivers nothing and answers "This question is no longer active — reply in words".

The payloads are `v1|job_q|<eid>|<qid>|<n>` (at most 53 bytes) and `v1|job_cancel|<eid>`
(46 bytes), inside Telegram's 64-byte cap; one with a job prefix and any other shape answers
"expired". The tap handler never posts a notice. It hands the reservation to the ticket at
`deliver_system_turn`'s entry and releases it in a `finally` on every arm that never reaches the
seam; the question post is the turn's notice target. A second tap, or a tap and a typed reply,
is a second answer. A Cancel tap on a live record answers "Cancelling…" and runs the cancel
funnel on a task of its own; a second tap while it runs answers "already finishing", one on an
ended job "already finished". When the funnel's write rolls back, the record stays live, the
button stays, and one line in reply to the tapped message asks to tap again.

**INV-BGJOB-014**: Every in-process terminal winner retires the job's direct-chat keyboards through the registry's terminal observer, which takes them off the record in one synchronous step before the record leaves the pending-terminal set; a keyboard post that lands after the terminal write settled is retired by its filer; and at boot every terminal record still carrying keyboard posts has them retired once.

## Failure behavior

**A start line or question send fails.** The start line is logged and the job runs on; the
question tool returns `delivery_failed` and the job may ask again. A send that timed out but
landed has no known message id: its taps still work while the record holds the question, a typed
reply to it takes the routes it took before, and it keeps its buttons after the end.

**A filing write never lands.** The record in memory is what routing reads; a restart before
the scheduled write loses that post's ownership, and a reply to it then takes the routes it took
before.

**A keyboard edit fails.** The keyboard stays; the record no longer lists it, so nothing retries
it. Its taps read the terminal record.

## Extension points

**Another kind of job post** is filed through `record_job_post` as it lands, with a keyboard
kind if it carries buttons, so it is steerable and retired like the others. **Another button on
a job post** gets its own `v1|job_…` prefix, matched in `_parse_job_callback` before the generic
parser.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::ask_operator`
- `casa/rootfs/opt/casa/tools.py::_post_job_start_line`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_operator_reply`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._maybe_route_job_reply`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.post_job_start_line`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.post_job_question`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_job_question_callback`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_job_cancel_callback`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.retire_job_keyboards`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.retire_ended_job_keyboards`
- `casa/rootfs/opt/casa/channels/telegram.py::_parse_job_callback`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.record_job_post`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.job_post_owner`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.counts_as_live`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.take_ui_posts`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.new_job_question`

**Tests**
- `tests/test_job_dm_attention.py`
- `tests/test_job_dm_posts_registry.py`

**Related**
- [`architecture/background-jobs.md`](../architecture/background-jobs.md)
- [`architecture/engagement-inbound-disclosure.md`](../architecture/engagement-inbound-disclosure.md)
- [`architecture/background-job-quiet-runs.md`](../architecture/background-job-quiet-runs.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
