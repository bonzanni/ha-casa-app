---
last_reviewed: 2026-11-09
---

# Engagement launch failure and restart

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens when a launch does not become a live engagement, and what a restart does with
the records left behind: which gate refused a delegation, which failure arms answer the
calling turn rather than the operator's topic, what a graceful stop records against a launch
it cancels, and what a restart does with a record. The record itself, how a launch is
admitted and the driver protocol are in [`architecture/engagements.md`](engagements.md); how a
turn is admitted to a live engagement is
[`architecture/engagement-turn-admission.md`](engagement-turn-admission.md). How an
engagement *ends* once it is live is
[`architecture/engagement-finalization.md`](engagement-finalization.md).

## Mental model

A launch is not an engagement. Between the durable record and a live driver there is a
stretch of work that can fail, be refused at a gate, or be cancelled — and everything in this
document is about that stretch, plus the one place it is re-entered later: a restart, which
finds records whose launch succeeded and whose process did not survive.

Two questions separate the arms below. *Who is owed the answer* — a named fault goes back to
the calling turn, which still owns the launch and is the one answering for it, while an
absence goes to the operator's topic, because a topic that says nothing is what needs
explaining. And *what caused the end* — a launch failure and a graceful stop are different
events, and the record says which one it was.

## Contracts & invariants

**INV-ENG-019**: A launch-failure arm aborts its engagement's topic only on the strength of a terminal transition this call both WON and durably persisted. A transition another writer won, and a transition whose persist rolled back leaving the record live, each leave the topic open and the record to its owner. The arm still answers its caller with its own named fault, and still posts no notice.

The two refused cases are the two ways the ledger's best-effort mark used to mislead an arm.
A LOST race means another writer already committed a terminal, and that writer owns every
terminal side effect (INV-ENG-001) — so painting `failed` here contradicts the durable record
and closes a topic its owner is about to close for its own reason. A ROLLED-BACK persist means
the record is still live and disk agrees: an open topic over a live record is recoverable, a
closed one is not, and the next turn that resumes that record would find its topic gone. All four named arms route through one owner that asks the transition exactly one
question and lets its three outcomes decide; that owner is anchored and shielded, so a
cancellation arriving after the durable commit cannot leave a terminal record behind a
permanently open topic. The caller is told its own named fault on all three outcomes, because
that fault is true of this launch whoever won the record.

A launch that ends without a named fault — cancelled after its record exists, or caught by
Casa's stop — is the launch-death reporter's instead (INV-ENG-030, in
[`architecture/engagement-launch-detach.md`](engagement-launch-detach.md)), which posts the
bounded notice these arms do not.

## Failure behavior

**A delegation is refused at one of its gates.** The ACL, alias, spawn-cap, engagement-slot
and plugin-withholding refusals, and what each payload discloses, are described in
[`architecture/delegation.md`](delegation.md).

**A driver fails to start after the record exists.** The engagement is marked errored, topic
cleanup is attempted, and the caller is told the start failed.

These launch-failure arms — options that would not build (`options_build_failed`), a profile
that would not persist (`profile_persist_failed`), a missing driver (`no_driver`), and a client
that would not open (`driver_start_failed`) — deliberately do NOT take the launch-death path's
bounded topic notice. The reason is that these arms carry a *named fault* and the death path
carries only an *absence*. Each of the four is a fact about why the launch did not happen,
returned synchronously to the turn that asked for it, in a kind string it can act on. The
topic is aborted silently because the launch never reached the point where the job is handed
over as live: the tool call still owns it, and is the one answering for it. A launch death has
no such fact to hand back — the launch was cancelled — and an operator's topic that says
nothing is what the notice exists to explain. Once the launch has answered `pending`, nothing
here applies: a fault on one of the job's turns is a turn end, continued once and then parked,
and a session that ends is resumed once
([`background-job-session.md`](background-job-session.md), INV-BGJOB-017 and INV-BGJOB-018).

The claim is bounded to what the tree provides, and it is a claim about the *caller*: the
failure kind reaches the live invoking turn. Whether it then reaches the operator is that
turn's own business, and nothing here relays it. The exemption is from the notice, and from
nothing else — it does not extend to a cancellation, which stays on the launch-death path
(INV-ENG-030). What it never extended to is the *strictness* of the mark behind the abort, and
that is now settled the other way by INV-ENG-019 above: the exemption buys these arms their
silence, never the authority to close a topic on a transition they did not win or did not
persist.

**A graceful stop cancels a launch.** This is a different event from a launch that failed,
and it is recorded as one. Casa's graceful-stop cleanup (`tools.stop_engagement_launches`)
records `casa_shutdown` against each launch it finds registered *before* it cancels that
launch, so the cause is carried on the row rather than inferred at the cancellation site from
the shape of the cancellation — the discriminator INV-JOB-009 settled for the job ledger,
applied here rather than folded into it. A terminal write that observes the cause carries
`origin.shutdown_reason = "casa_shutdown"` beside its outcome, never in place of it, and the
cancellation arm records a reason that says Casa was stopping instead of blaming a tool call
nothing cancelled. The cleanup then waits for those launches and their death reports while
Telegram, the bus and the registry are still up, so a dying launch's notice has a live
transport.

What it does not cover: a launch that becomes durable but is not yet enrolled when the stop
latches carries no cause — it is still enrolled, tagged and cancelled when it does enrol, but
the cleanup has already looked, so its death report is best-effort, as every death report is.
The notice is *attempted*, never guaranteed: a wedged or failing transport still loses it,
bounded and logged.

**A restart interrupts an engagement.** Persisted records load with `active` rewritten to
`idle`, because no live driver survived to make `active` true. A live job is resumed once
channels and residents are up — one attempt, ending the job `error` when it fails; a job
waiting on the operator with nothing journaled waits for their next input instead — as
[`architecture/background-job-session.md`](background-job-session.md) describes; no other
engagement is resumed at boot. An `executor` record an
older install left behind, from when the configurator ran as an engagement, has no config to
rebuild from: its resume refuses (`config not found`) rather than open a bare session.

**A restart replays an outcome nobody was told.** A terminal record is not only state to be
tidied: if the finalization funnel committed it, somebody is still owed the news (INV-ENG-018,
in [`architecture/engagement-terminal-telling.md`](engagement-terminal-telling.md)). Startup walks the
records that already carried that obligation when it loaded them and still carry it, and
enqueues one notice each, addressed from the
record's own persisted origin — the role that asked, on the channel it asked from, quoting the
request. It runs once the channels and the resident loops are up, because a notice enqueued
before a resident can consume it is not a telling; and it does not wait for delivery, because
a resident turn can take minutes and boot cannot. An unroutable role is logged and RETAINED
for the next boot rather than dropped. An engagement that ends during this boot, before the
walk, was told live and is not walked; only if that telling is never delivered does the next
boot replay it.

What such a notice can say is bounded by what the tombstone keeps, which is less than the live
path has. There is no completion text and no next steps on the record — no such
fields exist — so the replay reports the FACT of the outcome and says plainly that the notice
does not carry the report. It does not say the report is GONE, which would be a claim about
storage this path cannot make: a finished engagement's completion text may well still stand
in its closed topic. It never reconstructs an answer from the task. It does say that it is a replay: the notice tells the resident this is an
earlier outcome reached before the restart, whose announcement was never confirmed, and gives
the time the record went terminal, or says that time is unknown (INV-ENG-018).

A job's session that ends while it runs is resumed once by the driver's session-loss path
([`background-job-session.md`](background-job-session.md), INV-BGJOB-018); a launch cancelled
before it answered is the launch-death reporter's (INV-ENG-030).

## Extension points

**A new launch-failure arm** decides one thing first: does it carry a named fault the calling
turn can act on, or only an absence? The first returns a kind string and aborts the topic
silently; the second belongs on the launch-death path, with its bounded notice.

**A new terminal writer** decides whether it is the one telling the engager. If it announces
the outcome, it arms the durable obligation in the same transition that commits the terminal
— the finalization funnel does (except for the completed end of a quiet scheduled run nobody
interacted with, which is told to no one, INV-BGJOB-010 and INV-BGJOB-015), and so do the cancellation owner's death report and the
inline named-fault abort, the latter acknowledged by its launcher's own return when it won, and handed to the abort's own completion when the launcher was cancelled first; if it does not
— a bare mark — it leaves the default and owes nothing; a reporter that asks to arm but loses its
transition writes nothing, and so arms nothing. The
obligation follows the writer, not the record, so there is no property of a record that can
decide it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_abort_engagement_topic`
- `casa/rootfs/opt/casa/tools.py::stop_engagement_launches`
- `casa/rootfs/opt/casa/casa_core.py::_notify_recovered_engagement_outcomes`

**Tests**
- `tests/test_engage_executor_tool.py`
- `tests/test_launch_failure_terminal_authority.py`
- `tests/test_graceful_shutdown_engagement_launch.py`
- `tests/test_engagement_resume_options.py::test_an_executor_engagement_left_from_an_upgrade_fails_closed`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-launch-detach.md`](../architecture/engagement-launch-detach.md)
- [`architecture/background-job-session.md`](../architecture/background-job-session.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
<!-- END SOURCEMAP -->
