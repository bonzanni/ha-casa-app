---
last_reviewed: 2026-09-01
---

# Durable jobs

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The durable background-job ledger behind deferred and delegated work: what a job persists,
how execution and delivery are tracked separately, what a restart reconciles, what a
graceful stop does and does not do to a live row, and what a finished delegation still owes
its creator. It does not cover how a job's turn runs (the turn loop), the leased
claim/acknowledge protocol that gets an answer to a device
([`architecture/voice-delivery.md`](voice-delivery.md)), or the other durable obligation
shaped the same way — a question a resident's schedule asked the operator and is waiting
on ([`architecture/scheduled-asks.md`](scheduled-asks.md)).

## Mental model

**One job, two independent state machines.** A durable job tracks *execution* (accepted,
running, then a terminal state) and *delivery* (none or ready, then claimed, authorized,
playing, delivered) separately. Execution finishing creates a delivery obligation only when
the job has a persisted route and device; a completed job with no endpoint has nowhere to
go, and stays an audit row. A separate durable handoff latch exists for foreground
acknowledgement — it neither claims delivery nor proves execution finished.

**Disk is authoritative; everything runtime is process-local.** Every mutation writes the
complete candidate snapshot to disk before publishing it in memory, under the registry lock
— this subsystem is the origin of the disk-leads discipline that
`architecture/persistent-state.md` describes. Tasks, cancellation events, permits, timers
and the delivery coordinator's per-attempt bookkeeping are process-local and die with the
process; what survives is exactly what the snapshot holds.

**A restart reconciles; it never resumes.** Boot converts every persisted accepted or
running job to a terminal state — model execution is not resumable. A job whose durable
cancellation flag was already set finalizes as cancelled, exactly as the live cancel
paths would have; every other live job becomes a terminal orphan, and a voice-routed
orphan becomes ready so its *failure* can be delivered. In-flight delivery leases are
retained for one fresh lease rather than revoked, so a device mid-playback is not
stolen from.

## Contracts & invariants

**INV-JOB-001**: Every job mutation writes the complete snapshot to disk before publishing it in memory; a failed write publishes nothing.

Enforced by the registry's locked commit path, which also defers caller cancellation until
the write-and-publish completes. This is the strict disk-leads pattern referenced by
INV-STATE-004.

What it does not cover: runtime task ownership. The snapshot records job state, not that
any process is still executing it.

**INV-JOB-002**: A restart never resumes execution — persisted live jobs become terminal at boot (cancelled when a durable cancellation was already pending, orphaned otherwise), and a voice-routed orphan becomes ready so the failure is deliverable.

Enforced by the boot recovery pass, called once at startup. A cancel-pending job takes
the live cancel paths' terminal shape — no restart-orphan failure, no fresh delivery
sequence, no creator notice — so a creator who cancelled before the restart is not told
their job was "lost".

What it does not cover: delivery. A leased delivery attempt survives recovery with one
fresh lease instead of being orphaned, precisely so a mid-playback device is not preempted.

Recovery rebuilds the notified turn's origin **field by field** from the durable row, so
anything the live path carried as an origin marker and the row does not carry as a field is
gone by the time a resident is resumed. Scheduled-media eligibility (INV-TRIG-012) is
therefore a stored boolean rather than a marker riding the origin dict, restored only from
an exact stored true — a row written before the field existed restores nothing, and the
resumed turn stays text-only exactly as it did before that feature. Read that as the general
rule for this file: a capability that must survive a restart has to be *in the row*.

**INV-JOB-009**: A live job that a graceful stop itself settles is not settled at all — the row is left as it stands, so the boot reconciliation treats it exactly as it treats a job lost to a crash. A settling the stop did not cause, and every success or non-cancellation verdict, still commits mid-stop.

Enforced inside the registry, never at the call sites: the shutdown-deferral guard sits
at three registry transitions — the two compatibility cancellation transitions, and the
continuation compensation that settles by *deleting* the child row. What is closed is a
property, not a roster of publish points: a cancellation-reasoned settlement is deferred
to boot, while a success or non-cancellation verdict still commits mid-stop through
whichever registry transition carries it — the next paragraph narrows *which*
cancellations defer. The delivery-expiry sweeps touch delivery state alone and never
execution state.

This exists because the shape a shutdown used to write was not merely lossy but false —
the same "cancelled" envelope a *creator* cancellation writes, which recovery deliberately
settles in silence, so a stop announced nothing while a crash correctly announced one lost
job. A stop was measurably worse than a crash. It is the same rule INV-JOB-007 already
publishes for a scheduled question, under the same reason: `casa_shutdown` settles
nothing, edits nothing, and leaves the record for the next boot.

**The discriminator is the cause of the settling, and cause is carried, not inferred.** A
terminal's *category* cannot carry it: a creator cancel, a turn-budget expiry, a pre-launch
bail, a launch rollback and process death all write the same cancelled shape, so keying on
that shape plus a "the process is stopping" flag defers settlings the stop never caused —
and a pre-launch bail deferred that way has the next boot announce a loss for work never
started, whose caller was already told. So a site whose cause is known where it stands
records it, and only an *unrecorded* settling defers. Unrecorded is therefore the safe
default: a site that records nothing errs toward telling the operator, and a bare
cancellation — which cannot tell a barge-in from process death — is exactly the unrecorded
case this invariant exists for.

Whether the *writer* of a terminal also needed authority to assert that outcome is a
different question, and the answer is that neither persisting ledger checks it and no
cross-ledger contract is owed — reasoned in
[`architecture/engagement-finalization.md`](engagement-finalization.md).

What it does not cover: the *announcement*. This invariant is about the durable row and
its entry into recovery; whether anyone is ever told is INV-JOB-010's subject, and the two
are settled separately because a row can be perfectly reconciled and still reach nobody.
Nor does it cover a job the creator had *already* cancelled: its durable cancel-pending
flag means a completion arriving mid-stop still writes a cancelled terminal through the
completion path, and recovery still settles such a row silently as a creator cancellation —
which is the correct outcome, and the reason silence there is not a gap.

**INV-JOB-010**: An announcement Casa owes a creator is durably owed until it has been DELIVERED — the row's pending marker is cleared only once the consuming resident's channel reports that its turn reached the transport, never when the bus accepted the notice for enqueue — so an announcement lost with the process is announced again at the next boot.

Two markers carry it, and a row can only ever hold one. `orphan_notification_pending` is
written where it always was, by the conversion of a *live* row at boot.
`terminal_notification_pending` is new, and is written **in the same snapshot as the
terminal itself** by the arm that actually posts a notification — the async and
degraded-to-pending delegation callback, in all three of its shapes (a CLI abort, an
answer, an exception). A synchronous delegation arms nothing: its answer went back to the
caller in-band and no notice is owed, so arming it would re-announce every sync delegation
of the last result-TTL window at the next boot. A cancelled terminal arms nothing either,
for the reason the paragraph above gives.

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
would mean tracking delivery of content Casa deliberately does not inspect. Repeating an
answer the operator has already read is the price of never losing one.

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
dropped with it — Casa does not re-narrate a turn the operator has already begun reading. And
a delegation still executing when a stop begins boots as a lost row, which never held an
answer: what this retains is an answer that had already been written, not one that never was.

**INV-JOB-011**: At the graceful stop's job-ledger close boundary, every delegation that has already produced a success or non-cancellation verdict has had its completion callback run, its terminal-write attempt made, and every resulting settle tail awaited — so a delegation that finished during the stop reaches the ledger as its real outcome, never as a live row the next boot converts to lost on restart — and a verdict that had already landed when the stop began is written, and its announcement enqueued, before any resident's `aclose()` is entered, while a resident can still tell it. A delegation still running at that boundary, and a terminal write whose registry-owned retry is still pending there (whenever the failed attempt occurred), are outside this guarantee and remain governed by INV-JOB-009's boot reconciliation.

Enforced in the graceful stop's cleanup, at two points, by one drain: immediately after
the engagement-launch step — while the bus consumers, every resident's SDK pool and the
channels are still up — and immediately before the job ledger closes, after every ingress
surface is quiesced. The drain re-snapshots two things until both are empty: the anchored
settle tails that are still pending, and the delegation runs that are already done but
whose completion callback is still queued on the loop — a *ripe* producer, whose tail does
not exist yet and which a snapshot of the tail set alone would miss. It gathers the former
and yields one loop turn for the latter. It is bounded by a no-output condition, never a
clock: a settle tail is one shielded registry write and at most one unbounded queue put, no
tail mints a tail, a completed tail never re-enters, and a queued callback has run by the
next snapshot. It never cancels, and never waits for, a delegated run.

This exists because the settle tail was the one hop between a verdict and the registry
that the stop never awaited. The code's own comment justified leaving it to the loop's
final cancellation sweep by citing INV-JOB-009 — which promises the opposite for exactly
this case. Measured, the sweep killed the tail before its write: the SUCCEEDED delegation
booted as a live row, was converted to lost on restart, and the operator was told that a
delegation had been lost which had in fact completed — the alarming report in place of the
reassuring one, and a typed failure laundered into the generic one the same way.

What it does not cover, and why each limit is where it is. A delegation still *running*
when the ledger closes: the stop does not wait on delegated work, and that run is
INV-JOB-009's crash equivalence at boot — "lost on restart" is then true. A terminal write
that *failed* and left a registry-owned retry pending, whenever the failed attempt
happened: the retry is unbounded by design, so a stop that awaited it could never complete,
and the ledger's close cancels it as it cancels every retry — the row boots as lost, and the
notice already sent for it is contradicted at the next boot. A *cancelled* run is not a
verdict: its cancellation is deferred to boot exactly as INV-JOB-009 says, and draining its
deferred no-op would change nothing.

## What Casa keeps about a finished delegation

The durable row holds the *caller's* prose — the request as it was made — the specialist's
role, the origin, the terminal state and its typed failure envelope. The file is written
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

**Execution fails — an exception, an aborted run, or an invalid structured result.** A safe
failed envelope is persisted, with no model or result text interpolated into the failure
message; a routed job becomes ready so the failure itself is delivered. This is true of
every delegation arm, voice and non-voice alike: a CLI-aborted run fails the row with its
specific abort kind before the success write is reachable, and a run the terminal result
itself reports as faulted fails the row with the same classified kind its caller was told —
the envelope carries a kind, never an exception class name.

**The terminal write fails.** The voice lifecycle falls back to a compatibility failure
write; if that also fails, registry-owned reconciliation retries in the background and the
live row stays restart-recoverable. A synchronous delegation whose *successful* terminal
write fails still returns its result — the record is completed by the same background
reconciliation rather than raising the answer away — and a cancellation whose write fails is
likewise retried in the background. A failed *failure* write retries only a failure
transition, carrying the original typed failure when its caller had one: an abort or fault
can never be completed by a background retry, and the retry does not launder the kind into
the generic persistence fallback. Runtime ownership (the permit) is released either way.

**The process is stopping.** A cancellation caused by the stop is not written at all
(INV-JOB-009): the row stays live, and the retry that would otherwise chase a terminal it
can never reach stops for the same reason. A verdict that had already landed is different:
the stop waits for its settle tail — first while a resident can still announce it, and
again just before the ledger closes — so a delegation that finished during the stop is
recorded and told as its real outcome (INV-JOB-011). Read the two invariants for what that
does and does not promise — a stop makes a live row recoverable and lands every verdict it
already holds, and nothing more than that.

## Extension points

**A new durable field or state** touches the job dataclass, both snapshot codecs, and the
transitions and recovery that must understand it — the codecs are where forward
compatibility is decided.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/job_registry.py::JobRegistry`
- `casa/rootfs/opt/casa/job_registry.py::VoiceJob`
- `casa/rootfs/opt/casa/job_registry.py::JobRegistry.recover_after_restart`

**Tests**
- `tests/test_job_registry.py`
- `tests/test_delivery_acked_announcements.py`
- `tests/test_graceful_shutdown_jobs.py`
- `tests/test_graceful_shutdown_cause.py`
- `tests/test_graceful_shutdown_engagement_launch.py`
- `tests/test_graceful_shutdown_delegation_settle.py`

**Related**
- [`architecture/voice-delivery.md`](../architecture/voice-delivery.md)
- [`architecture/scheduled-asks.md`](../architecture/scheduled-asks.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
<!-- END SOURCEMAP -->
