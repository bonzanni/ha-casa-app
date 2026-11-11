---
last_reviewed: 2026-11-10
---

# A background job in the operator's direct chat

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a background job puts in the operator's direct chat and what they can do there: the start
line and its Cancel button, a specialist job's questions (`ask_operator`) and their buttons,
replies into a live job, the line a parked job posts, and how those keyboards are retired when
the job ends. What a job is is [`background-jobs.md`](background-jobs.md), and how its session
runs, parks and shows its cost is [`background-job-session.md`](background-job-session.md); the
notice a message
the job never took up draws is
[`engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md); what a quiet scheduled
run changes, and how interacting with one ends its quietness, is
[`background-job-quiet-runs.md`](background-job-quiet-runs.md); how a reply to a post that is
no job's reaches a desk is [`specialist-desk.md`](specialist-desk.md); the optional agent log,
where a job can be watched, is [`agent-logs.md`](agent-logs.md).

## Mental model

**The direct chat is where a job needs the operator; its log is where it can be watched.** A
running job says nothing in the DM unless it needs a decision or has its result. Everything
here works with no log group configured, and nothing here reads one (INV-BGJOB-023). A job the operator
started (directly, or through an agent acting on their message — the record's starter is
`operator`) gets one silent line, `📊 <Agent> started «<title>».`, with a **Cancel** button,
edited while the job runs to show its running time and cost (INV-BGJOB-020); a scheduled job,
or one an agent started on its own, gets none. A job Casa parks posts one line with a
**Cancel** button — in reply to the start line when there is one — and a reply to it continues
the job (INV-BGJOB-017). A specialist's job asks with
`ask_operator(question, options?)`: a plain post under its label, one button per option and a
**Cancel job** button. A resident's plugin worker has no question tool; it parks what needs an
answer and reports it, and its plugin's delivered posts reach the DM as before.

**Every post goes to the job's delivery, and a reply finds it task-first.** A job is a task:
its launcher fixes, at its birth, the chat its posts go to (`rec.delivery` — its host's own
chat when that is open, the resident's for a resident's job, none otherwise) and who receives
its result (`rec.requester`) ([`own-voice.md`](own-voice.md)). Its start line, questions,
parking line and plugin posts all go there, through that delivery's bot; a job with no
delivery posts none of them, and `ask_operator` refuses. In the host's own chat the lines carry
no label (`Started «<title>».`). The reply route described here is one case of the task-first
route every endpoint runs: a delegation's posts in an agent's chat are filed and routed the same
way, and a file replying to a job's post reaches the job (INV-OWN-004, INV-OWN-005).

**Ownership lives on the engagement record.** The post map is memory-only and bounded, so it
cannot say which live job owns a DM message. The record does: `dm_posts` lists the job's start
line, questions and plugin posts (newest last, unbounded while the job lives, and kept with the
tombstone after it ends), `ui_posts` the ones carrying a keyboard (start line, questions,
parking lines), and `questions` the asked questions by a random id. The channel files
each post as it lands (`EngagementRegistry.record_job_post`, reached through `task_posts.record`
from the post-landing seam for plugin posts, and called from the start-line and question
posters), and the registry keeps an index
from each post's address — the bot that posted it, the chat and the message id, since Telegram
numbers messages per bot and per chat — to live record, rebuilt at load. When the job ends, its
posts move to a second index keyed by the same address, which load rebuilds
from terminal job records; a post that lands after the end is filed there directly. A record
pruned from the tombstone takes its posts out of both.

**"Live" means live or pending-terminal.** A strict terminal transition commits in memory before
its write and rolls back if the write fails. While that write is pending the record still
counts as live (`EngagementRegistry.counts_as_live`) for filing and routing; a turn routed then
waits for the write to settle in the shared readiness step, and is delivered after a rollback or
refused (and told) after a commit.

**A reply or a tap goes into the live job, or is told — never elsewhere.** A reply quoting a post the
record holds goes into that job as an operator message
(`TelegramChannel.deliver_operator_reply`), named with the first line of the post it answers,
and is written into the running session as it arrives.
A tap on a question's option is resolved against the record — never a memory-only broker — so
it works after a restart, and delivers `[operator answered «<question>»: <option>]` through
`deliver_system_turn`. Neither is ever rerouted: whatever the job does not take up draws one
notice, from the holder of its admission ticket, in reply to the operator's message or to the
tapped card (INV-ENG-029), and a message journaled for a job that ends unread draws one
notice the same way
([`background-job-message-journal.md`](background-job-message-journal.md)). A question may appear twice (asked again, or a send that timed out
but landed); either copy answers while the job lives.

**A reply to an ended job's post goes to the chat's own agent, told how the job ended.** Once the job
has ended, a reply quoting one of its posts — its start line, a question, a plugin post, a post
that landed after the end — is not the desk's: the desk cannot see the job, and a specialist-hosted
job's plugin post sits in the post map under that specialist. The channel checks the ended
index before the post map (`EngagementRegistry.ended_job_post_owner`), with the live route's
own checks — the authenticated operator who started the job, in its chat — and the message
becomes the ordinary turn of that chat's own agent (the resident, in the resident's chat), whose reply note names the job's agent, its title, how it
ended and when (`TelegramChannel._ended_job_phrase`), as in "a message Casa posted for
Finance's background job «Q3 accounting», which ended (completed) at 2026-11-10 14:02". A job a
resident's plugin worker ran is named as the resident's own, with its plugin. The resident narrated the
job's result in this chat, so she can connect the two. A file sent as such a reply is the
resident's file: with a caption it starts her turn with the same note; without one it is
stored and acknowledged, like every captionless file, and no turn starts.

**Whoever sees the record terminal last retires the keyboard.** The registry's terminal
observer — told by every in-process terminal winner: the finalize funnel, the launch-death
report, the reap, the legacy direct mutators — has the channel take the record's keyboard posts
in one synchronous step and edit them away: the start line to `✅`/`🛑`/`❌ <Agent> «<title>»`
with the job's running time and cost, a question or parking line to no buttons. A job that
completes from inside a running turn is the one exception to the timing: the observer leaves its
keyboards to the completion's finalize tail, which retires them once that turn has ended (or a
bounded wait passed), so the end paint carries the completing turn's cost. A keyboard post that lands after the terminal write settled is
retired by the code that filed it. At boot, terminal records still carrying keyboard posts are
retired once. A keyboard an edit could not remove keeps its buttons; a tap on it reads the
terminal record and gets "already finished".

## Contracts & invariants

**INV-BGJOB-026**: A job's direct-chat posts — its start line, its questions and its plugin's delivered posts — are filed on its engagement record as they land, without a cap while the record counts as live, persisted, and indexed by address (bot, chat and message id) again at load; a record counts as live while active or idle and while its strict terminal write is pending; the write that settles the terminal transition stores the record's questions empty and keeps its posts, which then leave the live index for the ended-post index keyed by the same address — rebuilt at load from terminal job records, joined by any post of the job that lands after the end, and left by a record pruned from the tombstone.

**INV-BGJOB-012**: An authenticated operator's reply quoting a post the job record holds, while that record counts as live and is their job in that chat, is delivered into that job as an operator message — ahead of the desk route — journaled and admitted synchronously at the seam's entry; it is never sent to another route, and if the job does not take it up its one notice comes from the ticket's holder (INV-ENG-029).

A reply to a post of a job that has ended is INV-BGJOB-024's; a reply to a post no record
holds takes the desk ([`specialist-desk.md`](specialist-desk.md), INV-DESK-005), or the
resident with its reply note (INV-DESK-004). The reply's words are admitted as they typed them —
what the completion gate counts and what memory keeps (INV-MEM-044) — and the turn the job
receives carries Casa's `[replying to «…»]` line before them. It is written into the running
session as it arrives, never held for a turn to end (INV-BGJOB-016).

**INV-BGJOB-024**: An authenticated operator's reply in their direct chat — text, or a file with a caption — quoting a post filed in that chat by a job they started that no longer counts as live, including a post that landed after the job ended, never reaches a specialist's desk while the tombstone holds the record: it is the turn of that chat's own agent — the resident in the resident's chat — whose reply note names the job's agent, title, outcome and end time from its record; a reply from anyone else, or in another chat, keeps the routes and the note it would have had.

Checked ahead of the post map in both the text path (`_handle_serialized`, through
`_quoted_task` after the live route `_route_task_reply`, so a live or pending-terminal job still
takes its reply) and the file path (`_file_address`).
What it does not cover: a file without a caption, which is stored in the chat's agent's inbox and
acknowledged and starts no turn, so no note is attached; and a reply to an ended job whose
record the tombstone has pruned, which gets the note any reply would.

**INV-BGJOB-023**: With no log group configured, a job is started, binds its protected calls to the operator in its origin, asks, takes the operator's replies and taps, and ends with its keyboards retired; neither its launch, its grant identity nor its busy refusal requires a log group.

**INV-BGJOB-013**: A specialist job's question is a filed post whose option buttons carry the engagement id, a random question id redrawn until the record holds no question with it, and the option's index, and whose Cancel button carries the engagement id; job buttons are matched before the generic callback parser; a tap is resolved against the record only — the operator whose job it is, a record that counts as live, a question id it holds and an option it has — and takes the inbound reservation before its first await, so a successful completion is refused from that instant; a question id the record does not hold delivers nothing and answers "This question is no longer active — reply in words".

The payloads are `v1|job_q|<eid>|<qid>|<n>` (at most 53 bytes) and `v1|job_cancel|<eid>`
(46 bytes), inside Telegram's 64-byte cap; one with a job prefix and any other shape answers
"expired". The tap handler never posts a notice. It hands the reservation to the ticket at
`deliver_system_turn`'s entry and releases it in a `finally` on every arm that never reaches the
seam; the question post is the turn's notice target. A second tap, or a tap and a typed reply,
is a second answer. A Cancel tap on a live record answers "Cancelling…" and runs the cancel
funnel on a task of its own; a second tap while it runs answers "already finishing", one on an
ended job "already finished". A tap while another terminal write of the job is still pending —
a completion's, say — is a cancellation like any other: its transition waits for that write,
cancels the job if it rolled back, and ends with nothing more if it committed. When the funnel's write rolls back, the record stays live, the
button stays, and one line in reply to the tapped message asks to tap again.

**INV-BGJOB-014**: Every in-process terminal winner retires the job's direct-chat keyboards — through the registry's terminal observer, which takes them off the record in one synchronous step before the record leaves the pending-terminal set, or, for a completion from inside a running turn, through that completion's finalize tail once the turn has ended or its bounded wait passed; a keyboard post that lands after the terminal write settled is retired by its filer; and at boot every terminal record still carrying keyboard posts has them retired once.

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
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._route_task_reply`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.post_job_start_line`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.post_job_question`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_job_question_callback`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_job_cancel_callback`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.retire_job_keyboards`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.retire_ended_job_keyboards`
- `casa/rootfs/opt/casa/channels/telegram.py::_parse_job_callback`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.record_job_post`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.job_post_owner`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.ended_job_post_owner`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._quoted_task`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._ended_job_phrase`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.counts_as_live`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.take_ui_posts`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.new_job_question`

**Tests**
- `tests/test_job_dm_attention.py`
- `tests/test_job_dm_posts_registry.py`
- `tests/test_ended_job_replies.py`

**Related**
- [`architecture/agent-logs.md`](../architecture/agent-logs.md)
- [`architecture/background-jobs.md`](../architecture/background-jobs.md)
- [`architecture/background-job-session.md`](../architecture/background-job-session.md)
- [`architecture/engagement-inbound-disclosure.md`](../architecture/engagement-inbound-disclosure.md)
- [`architecture/background-job-quiet-runs.md`](../architecture/background-job-quiet-runs.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
