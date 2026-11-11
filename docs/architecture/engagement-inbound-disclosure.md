---
last_reviewed: 2026-11-10
---

# Messages a job never read

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What the operator is told about a message to a job that the job never read — their reply into
it, a tapped answer, an approval's continuation — and who tells it. What a *successful* completion is refused over, and what the driver counts, is in
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md). How each message
is journaled, matched against the session transcript and redelivered after a restart is
[`architecture/background-job-message-journal.md`](background-job-message-journal.md)
(INV-BGJOB-021). How replies and taps reach a job is
[`architecture/background-job-direct-chat.md`](background-job-direct-chat.md).

## Mental model

**Each message is told once, by the one place that knows it was lost.** A seam that hands a
message to a job (`deliver_operator_reply`, `deliver_system_turn`) journals
it, admits it and queues it for the job's delivery worker in one synchronous step, and returns.
If the job does not accept it — the record went terminal, the journal could not be written,
Casa is stopping, no driver is wired, a quiet run's quietness could not be ended — the seam
makes one bounded notice attempt (`TelegramChannel._tell_not_delivered`) and returns `False`.
Once accepted, the message is the journal's: a job that ends without reading it — its session
transcript lacks the message's uuid — has it told as lost once by the journal's terminal branch,
which every terminal path runs after the job's client is closed
(`TelegramChannel.tell_journal_lost`). A session that ended and was resumed redelivers the
message instead of telling it lost (INV-BGJOB-018). The callers of the seams never post a
notice: they get a bare acceptance boolean for their own bookkeeping, a toast or a card edit.

**The notice goes where the message came from.** Every message to a job comes from the
operator's direct chat — a reply into a job, or the card a tap answered — and is told there,
as a reply to it: "The job had
just finished, so your message didn't get through. Could you send it again?" (or "Your message
didn't get through. Could you send it again?" while the job is live) for their words, and "The
job had already finished, so your answer wasn't used." (or "Your answer didn't get through.
Could you answer again?") for a tap. The ended form is chosen from the record's settled status. While a memory wipe's restart
is pending, a reply is answered with the wipe's courtesy line instead, which is that message's
one notice. Nothing is forwarded anywhere else, and nothing written in the agent-log group is
a message to a job (INV-TG-016).

**No end-of-job list.** A terminal outcome — the end notice to the resident, the end's line in
the agent's log, the launch-death line — lists no inbound messages. The messages that died with the job are each
told by the journal's terminal branch, so a list would tell them twice. The driver's teardown
leaves the journal in place, so the terminal branch still finds each entry when it runs.

**Casa's own writes are not messages.** The brief, a nudge, the continue line and the restart
line are Casa's; none is journaled, and none draws a notice when it is never written.

## Contracts & invariants

**INV-ENG-029**: A message to a job — a reply into it, a tapped answer, an approval's continuation — that the job never reads draws exactly one bounded notice attempt, made by the seam when the job did not accept it and otherwise by the job's message journal once the job ended without reading it, never by a caller of the seam; a message from the operator's direct chat is told there in reply to their message or the tapped card, worded from the record's settled status, and no terminal outcome lists such messages.

What it does not cover: the notice is one attempt — a Telegram failure or a crash at that point
loses it, and the message is discharged regardless, because retrying would hold an unclearable
completion veto. A message the job did read is in its transcript and draws no notice: a turn
that ends without answering it is the job's turn end, nudged or parked like any other
([`background-job-session.md`](background-job-session.md), INV-BGJOB-017).

## Failure behavior

**The settled-status read does not return.** It is bounded; the notice is then worded as if the
job were live, which is still true of a message that was not delivered.

**The notice send fails.** It is logged at debug and the message is discharged; nothing retries
it.

## Extension points

**A new seam that hands operator text to a job** goes through `_deliver_to_job`, which journals,
admits and queues the message synchronously and tells the one not-delivered notice when the job
does not accept it; it passes the message's notice target and returns a bare acceptance boolean
that no caller posts on.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._tell_not_delivered`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._post_lost_inbound_notice`

**Tests**
- `tests/test_job_dm_attention.py`
- `tests/test_job_message_seams.py`
- `tests/test_emit_completion_tool.py`
- `tests/test_launch_death_reporter.py`
- `tests/test_in_casa_inbound_admission.py`

**Related**
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/background-job-message-journal.md`](../architecture/background-job-message-journal.md)
- [`architecture/background-job-session.md`](../architecture/background-job-session.md)
- [`architecture/background-job-direct-chat.md`](../architecture/background-job-direct-chat.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
