---
last_reviewed: 2026-09-26
---

# What a finished delegation owes its creator

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The obligation a finished delegation leaves behind on the durable job ledger: the
announcement Casa owes its creator and when that is discharged, the answer retained so the
announcement survives a restart, what the boot replay tells the narrating resident, and how
long the finished row is kept. The ledger itself — snapshots, restart reconciliation, what a
graceful stop does to a live row — is
[`architecture/jobs-and-delivery.md`](jobs-and-delivery.md); the device delivery protocol
for a voice answer is [`architecture/voice-delivery.md`](voice-delivery.md).

## Mental model

**An announcement is owed until it is delivered, not until it is enqueued.** A durable
marker on the row says a creator is still owed a notice, and only the consuming resident's
channel reporting that the notice's turn reached the transport clears it; a process lost
before that announces again at the next boot.

**The answer lives exactly as long as the obligation.** A non-voice answer is written onto
the row in the snapshot that arms the obligation and removed in the snapshot that clears
it, so a replay can quote it — and the replay says that it is a replay.

## Contracts & invariants

**INV-JOB-010**: An announcement Casa owes a creator is durably owed until it has been DELIVERED — the row's pending marker is cleared only once the consuming resident's channel reports that its turn reached the transport, never when the bus accepted the notice for enqueue — so an announcement lost with the process is announced again at the next boot.

Two markers carry it, and a row can only ever hold one. `orphan_notification_pending` is
written where it always was, by the conversion of a *live* row at boot.
`terminal_notification_pending` is new, and is written **in the same snapshot as the
terminal itself** by the arm that actually posts a notification — the async and
degraded-to-pending delegation callback, in all three of its shapes (a CLI abort, an
answer, an exception). A synchronous delegation arms nothing: its answer went back to the
caller in-band and no notice is owed, so arming it would re-announce every sync delegation
of the last result-TTL window at the next boot. A cancelled terminal arms nothing either,
for the reason INV-JOB-009 gives for a row its creator had already
cancelled ([`jobs-and-delivery.md`](jobs-and-delivery.md)).

What "delivered" means here is what the channel already says it means (INV-TG-006): the
first unit of output was accepted by the transport. A *normal return* is deliberately not
enough — every delivery method returns normally when the Telegram application is absent,
having made zero Bot API calls — and neither is a generic turn-failure reply, which tells
the operator that something broke rather than what their delegation did. Every ambiguous
answer keeps the obligation, because the cost of keeping it is one duplicate announcement
and the cost of dropping it is silence.

What it does **not** claim is that the resident's words describe the delegation. The
acknowledgement is discharged by Casa's own output for that notification reaching the
transport, and nothing inspects the narration's content — a resident that answers something
else entirely still discharges it. That limit is deliberate and is the same one the LIVE
completion path has always had: a delegation that finishes while Casa is up is narrated by
a resident in its own words, with no verification either. Holding the recovery path to a
stricter standard would mean the announcement could no longer be a resident's narration at
all, which is a different product, not a stricter guard.

That duplicate is the accepted trade, stated plainly: a process lost between the delivery
and the durable acknowledgement announces the same outcome again at the next boot, as does
a terminal whose snapshot write failed and was landed afterwards by the registry-owned
retry. Boot never waits for any of it — a resident's turn can take minutes — so recovery
enqueues its notices and returns.

A terminal replayed at boot carries the answer the row kept for it, when it kept one. A
delegation whose answer was in hand when it completed retained that answer for exactly this
moment (INV-JOB-015 below), and the replay quotes it just as the live announcement would
have. A row that kept none — one written before the field existed, or by an arm that owed no
announcement — is still announced truthfully as having completed with its answer
unavailable, never as an empty answer and never laundered into a failure. Which of the two a
notice is, is the row's own durable fact and never "is the stored text empty": a specialist
that legitimately answered with nothing is not a row that kept nothing. A delegation that
*failed* replays its own durable typed kind, exactly as the live path would have reported it.

That changes what the accepted duplicate above costs. A process lost between the delivery
and the durable acknowledgement now announces the same outcome again **with its answer** —
there is no dedupe on the resident's side, and none was added, because hiding the duplicate
would mean tracking delivery of content Casa deliberately does not inspect. What the replay
does instead is say what it is. The resident is told that this is a post-restart
re-announcement whose full delivery was not confirmed — the interrupted relay may have shown
the operator nothing, a partial streamed draft, or the whole answer with only its
acknowledgement lost — and that the complete result is still owed; a live completion is never
so marked, and the two prompts differ in exactly that statement. Repeating an answer the
operator may already have read is the price of never losing one, and a resident that read
the replay as a duplicate and narrated a fragment discharged the whole answer by the rule
above, which is why it is now told not to.

**INV-JOB-016**: A retained answer replayed at boot is handed to the consuming resident as a post-restart re-announcement whose full delivery was not confirmed, with the instruction to relay the whole answer — a completion announced live is never so marked, and the two synthesized prompts differ in exactly that statement.

**INV-JOB-015**: A non-voice delegated answer is retained on the durable row exactly while its announcement is owed — it is written in the same snapshot that arms the obligation and only when the obligation is armed, and it is removed in the same snapshot that clears the obligation on DELIVERY — so an answer that was in hand when a delegation completed reaches its creator across a restart, and stops being retained once a delivery has been acknowledged.

One predicate decides both ends, and it is the one that already decides whether the
announcement is owed at all: a terminal that owes no notice cannot store an answer, whatever
its caller passes, so the synchronous arm and any non-Telegram creator keep exactly the
posture they had. One method drops it, `ack_terminal_notification`, which is where the LIVE
announcement's acknowledgement and the boot replay's already both arrive — so the two paths
cannot drift into two rules, and a drop that fails leaves the answer owed rather than gone.
The voice arm is outside this: its result has its own lifecycle and its own TTL, and an
acknowledged voice delivery still keeps its answer for the continuations that replay it.

Two limits are stated rather than designed away. A narration whose HEAD reached the transport
discharges the obligation even if its tail then raised, by the rule above, and the answer is
dropped with it — Casa does not re-narrate a turn the operator has already begun reading. That
head is the one the channel's finalize reports; a streamed draft stamps nothing, which is why a
replay cannot say what, if anything, was seen. And
a delegation still executing when a stop begins boots as a lost row, which never held an
answer: what this retains is an answer that had already been written, not one that never was.

## What Casa keeps about a finished delegation

The durable row holds the *caller's* prose — the request as it was made — the specialist's
role, the origin, the note the brief owes (`output_note`), the terminal state and its typed
failure envelope. The file is written
0600.

**It is kept until its deadline, and then it is deleted.** Every terminal write stamps the
row with a deadline: 24 h for an ordinary delegation, the shorter voice TTL for a trusted
voice-delivery row, which can be as little as thirty seconds. The first expiry pass at or
after that deadline REMOVES the row from the file — the request, its context and, on the
voice arm, the answer go with it, out of the file's bytes rather than out of a field. That
happens whatever state the delivery reached, a delivered or cancelled one included; there
is no marked-and-retained audit record, and a row with no deadline is a live job and is
never touched. This is the one statement of the retention rule; the delivery side points
here rather than restating it.

**One record is exempt: one that still owes its creator an announcement.** A terminal that
Casa has not yet been able to tell its creator about carries a durable marker until the
notice reaches the transport (INV-JOB-010), and such a row is kept — with its content —
past its deadline, so the boot replay still finds it. Its delivery still expires on time;
only the record survives. The first pass after the notice is acknowledged deletes it. This
is deliberately the same shape as INV-ENG-018's engagement-record exemption.

**Deletion is opportunistic, and this is a stated limit rather than an oversight.** The
passes are a delegation launch, the three voice job tools, and the delivery coordinator's
reconciliation — its one-second sweep, a route connecting, and each inbound job frame.
Nothing sweeps at boot or on a wall clock, so on an install where no delegation is launched
and no voice channel is running, a due row waits in the file until something runs a pass.

**It also holds the specialist's answer, for as long as that answer is owed.** The decision
was taken explicitly (#688) and then reversed explicitly: Casa used to write an empty result
on the Telegram and synchronous arms, so a delegation that finished while Casa was restarting
could only ever be announced as "it completed, the answer is gone, shall I run it again?" —
which is a worse outcome than the live path's for the same work. What that posture was
protecting against was a widened retention cost, and the cost is now bounded and stated on
both ends: an answer is written only onto a row that owes its creator an announcement, and it
is removed at the moment that announcement is acknowledged as delivered — before, and
independently of, the deadline that deletes the row itself (INV-JOB-015).

The window is therefore the delivery window, not a TTL: the answer is on the row from the
terminal write until the operator has been told, and a stop in the middle is precisely the
case it exists for. The synchronous arm still keeps nothing, because its answer went back to
the caller in-band and no notice is owed. The file is written 0600 under `/data`; the earlier
description of it here as "a file that backups and config-git snapshots reach" was never true
of it, because `config_git` versions `/config`.

**What Casa does not claim is that dropping the row's copy is forgetting.** The answer that
was delivered reached the operator's channel and the narrating resident's own turn, exactly
as a live delegation's does, and whatever those are retained in is governed by memory's rules
rather than by this one. This rule is about the durable delivery record.

The voice arm is the stated exception, and the asymmetry is deliberate rather than
accidental: a voice answer is persisted because the device delivery protocol and its
continuations replay it, which is a capability that must survive a restart and therefore
has to be in the row.

## Failure behavior

**A process is lost between the delivery and the acknowledgement.** The same outcome is
announced again at the next boot, with its answer when the row kept one — the accepted
duplicate INV-JOB-010 states. **The terminal snapshot write fails.** The registry-owned
retry lands it afterwards, carrying the same answer and the same marker. **Dropping the
answer fails.** The answer stays owed rather than gone (INV-JOB-015).

## Extension points

**A new terminal arm that posts a notification** must arm `terminal_notification_pending`
in the same snapshot as the terminal and attach the delivery acknowledgement to its notice;
an answer passed on an arm that owes no notice is not stored.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/casa_core.py::_notify_recovered_delegations`
- `casa/rootfs/opt/casa/agent.py::Agent._synthesize_delegation_turn`
- `casa/rootfs/opt/casa/job_registry.py::JobRegistry.ack_terminal_notification`

**Tests**
- `tests/test_delivery_acked_announcements.py`

**Related**
- [`architecture/jobs-and-delivery.md`](../architecture/jobs-and-delivery.md)
- [`architecture/voice-delivery.md`](../architecture/voice-delivery.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
