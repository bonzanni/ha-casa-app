---
last_reviewed: 2026-11-09
---

# Background jobs: the operator-message journal

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an operator message to a background job survives a Casa restart, a Casa crash or the death
of the job's CLI process: the journal file each job keeps, when an entry is written and when it
goes, the check of every entry against the session transcript at boot and at recovery, the one
marked redelivery, and the lost-message notice an ended job's unread messages draw. How the
session itself runs, resumes and is written into is
[`background-job-session.md`](background-job-session.md); how a message is admitted is
[`engagement-turn-admission.md`](engagement-turn-admission.md); the notice's wording and target
are [`engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md).

## Mental model

**An entry stays until the transcript is certain to hold it.** `job_inbox` keeps one JSON file per
job, `/data/job-inbox/<engagement id>.json`, rewritten atomically (temp file, fsync, rename, mode
0600) on every change. An entry is `{"uuid", "text", "notice", "taken_up"}`: the uuid the message
is written into the session with, the text as written, and the direct chat message or tapped card
a lost-message notice would reply to (none for a topic message, whose notice goes to the topic).
Only operator messages are journaled; Casa's own writes (the brief, the nudge, the continue line,
the restart line) are not — a restart re-derives them.

**The transcript is the authority on what a session read.** The pinned CLI records a written
message's uuid in the session transcript only when it takes the message into a turn, about a
tenth of a second after take-up; the line written when the message is queued carries no uuid.
`job_inbox.uuids_in_transcript` reads the three shapes that mean taken up: a `user` line with our
uuid (a message that ran as its own turn), and a `queued_command` attachment's `source_uuid` or a
`queue-operation` `remove` line's `commandUuid` (a message absorbed mid-turn). A missing or
unreadable transcript holds nothing.

**The life of an entry.** The seam calls `InCasaDriver.submit_operator`, which appends the entry
before it admits the message — synchronously, nothing awaited. The CLI's echo at take-up
discharges the ticket in memory (the completion gate reads that) and marks the entry `taken_up`;
the entry is removed at the end of the turn that took it up (that turn's `ResultMessage`), by
which time the transcript line certainly exists. It is also removed after its lost-message
notice. One reconcile routine runs, for a live job, at boot and at recovery from a session
death, before the restart line, under the lifecycle lock; the terminal branch runs when a job
ends:
- uuid in the transcript: the job read it; the entry is dropped and nothing is sent. The
  transcript is authoritative for memory too: a matching ticket that was written but never
  echoed (a CLI death between the transcript line and the echo) is discharged and any queued copy
  removed.
- uuid missing, record live: the message is redelivered after the restart line, with its
  original uuid, prefixed `(Sent before Casa restarted:)` or, after a CLI death,
  `(Sent before the job's session restarted:)`. One ticket per uuid: a ticket already written to
  the dead session is re-bound to the replacement, one still queued stays queued, and after a
  Casa restart (no tickets in memory) the reconcile creates the only one. A job with entries is
  resumed at boot even while it waits for the operator — an entry is operator input.
- uuid missing, job ended: one lost-message notice, then the entry goes, and the file goes
  with it (`InCasaDriver.settle_journal_terminal`, run by every terminal path through
  `tools._settle_job_journal` after the job's client is closed; the channel's
  `TelegramChannel.tell_journal_lost` makes one bounded attempt).

## Contracts & invariants

**INV-BGJOB-021**: An operator message is admitted for a job only after its journal entry is committed, and the entry stays until the turn that took it up has ended or it has been told as lost; at boot and at recovery from a session death each entry of a live job is checked against the session transcript by uuid — found means dropped, missing means redelivered once, marked as sent before the restart — and when a job ends, each entry its transcript lacks is told as lost once, at the next boot if Casa died before the job's journal was settled.

A journal write that fails before admission refuses the message with the seam's "not delivered,
please resend" reply, and nothing was accepted: no ticket, no queue entry. A write after
acceptance (marking take-up, removing entries) that fails keeps the older file, whose extra
entries the next reconcile settles against the transcript. The journal is what the completion
gate's ledger cannot be after a restart: memory forgets, the file and the transcript do not.

What it does not cover: a Casa process death between Telegram handing Casa an update and the
journal commit loses that message with no notice — the commit is where acceptance begins. A
death inside the tenth of a second between the CLI taking a message up and writing its
transcript line makes that message be redelivered once, marked as sent before the restart. A
quiet run's promotion write, and the wait for a pending terminal write, happen in the seam before
the commit, inside that same window. The terminal branch runs on the path that ends the job, after
the terminal write: a Casa death between the two leaves that job's file behind, and the next boot
settles it the same way (`casa_core._settle_ended_job_journals`, after the live jobs are resumed),
removing any file whose record is gone.

## Failure behavior

**The journal cannot be written.** `job_inbox.append` raises `JournalError`; the seam tells the
operator once that the message was not delivered and asks them to resend.

**The transcript cannot be found.** It holds nothing: every entry of a live job is redelivered
(a duplicate the job can recognise by its marking), and every entry of an ended job is told as
lost.

**A memory wipe removes the transcripts.** A wiped job is not resumed
([`background-job-session.md`](background-job-session.md)); it ends, and its journal's entries
are told as lost through the terminal branch.

## Extension points

A new seam that carries operator input to a job calls `InCasaDriver.submit_operator` with the
notice target its lost-message notice should reply to; it never writes into the session or the
journal itself.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/job_inbox.py`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver._reconcile_live`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.settle_journal_terminal`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver._take_up`
- `casa/rootfs/opt/casa/tools.py::_settle_job_journal`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.tell_journal_lost`

**Tests**
- `tests/test_job_session_driver.py`
- `tests/test_job_message_seams.py`

**Related**
- [`architecture/background-job-session.md`](../architecture/background-job-session.md)
- [`architecture/engagement-inbound-disclosure.md`](../architecture/engagement-inbound-disclosure.md)
- [`architecture/engagement-turn-admission.md`](../architecture/engagement-turn-admission.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
