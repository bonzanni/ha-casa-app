---
last_reviewed: 2026-10-06
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
closed one is not, and the next turn that resumes that record would find its topic gone. All seventeen named arms route through one owner that asks the transition exactly one
question and lets its three outcomes decide; that owner is anchored and shielded, so a
cancellation arriving after the durable commit cannot leave a terminal record behind a
permanently open topic. The caller is told its own named fault on all three outcomes, because
that fault is true of this launch whoever won the record.

The behaviour here is also measured against INV-ENG-011, stated in
[`architecture/engagement-turn-outcomes.md`](engagement-turn-outcomes.md), which keeps a launch that ended
without a terminal artifact on the launch-death path.

## Failure behavior

**A delegation is refused at one of its gates.** The ACL, alias, spawn-cap, engagement-slot
and plugin-withholding refusals, and what each payload discloses, are described in
[`architecture/delegation.md`](delegation.md).

**A driver fails to start after the record exists.** The engagement is marked errored, topic
cleanup is attempted, and the caller is told the start failed.

These launch-failure arms — a missing driver, a clearance change during launch, a superseded
plugin, a missing prompt template, and a client that would not open — deliberately do NOT
take the launch-death path's bounded topic notice. The reason is not that the death path has
nobody to answer: its post-launch arms have no live tool call left, because the launch
answered `pending` before the turn ran (INV-ENG-021), so they tell the engager over the bus
instead. The reason is that these arms carry a *named fault* and the death path carries only
an *absence*. Each of the five is a fact about why the launch did not happen, returned
synchronously to the turn that asked for it, in a kind string it can act on — one of them
carries retry advice only that caller can use. The topic is aborted silently because the
launch never reached the point where the engagement is handed over as live: the tool call
still owns it, and is the one answering for it. An API-level fault raised by the launch turn
itself used to be the sixth of these; it is not any more, because the turn runs after the
tool call has returned and there is no caller to hand the kind to — it takes the death path,
the bounded notice names the fault, and the engager is told the same kind live. Text the turn
had already posted progressively can be standing in the topic and cannot be retracted; this
is a claim about who answers, not that nothing ran. A launch death has no such fact to hand back: the turn ran to its end and
left nothing, or the launch was cancelled, and an operator's topic that says nothing is what
the notice exists to explain.

The claim is bounded to what the tree provides, and it is a claim about the *caller*: the
failure kind reaches the live invoking turn. Whether it then reaches the operator is that
turn's own business, and nothing here relays it. The exemption is from the notice, and from
nothing else — it does not extend to a cancellation, nor to a launch turn that ended after
starting without its terminal artifact, both of which stay on the launch-death path
(INV-ENG-011). What it never extended to is the *strictness* of the mark behind the abort, and
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
`idle`, because no live driver survived to make `active` true. No engagement is resumed at
boot; a live one that runs a background job is continued once channels and residents are up,
as [`architecture/background-jobs.md`](background-jobs.md) describes.

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
path has. There is no completion text, no artifacts and no next steps on the record — no such
fields exist — so the replay reports the FACT of the outcome and says plainly that the notice
does not carry the report. It does not say the report is GONE, which would be a claim about
storage this path cannot make: a finished engagement's structured summary may well have been
captured into memory. It never reconstructs an answer from the task. It does say that it is a replay: the notice tells the resident this is an
earlier outcome reached before the restart, whose announcement was never confirmed, and gives
the time the record went terminal, or says that time is unknown (INV-ENG-018).

Background-job continuation uses the periodic recovery and finalize paths described in
[`background-jobs.md`](background-jobs.md); the interactive two-strike resume rule does
not terminalize a job. Launch cancellation remains governed by the launch owner.

## Extension points

**A new launch-failure arm** decides one thing first: does it carry a named fault the calling
turn can act on, or only an absence? The first returns a kind string and aborts the topic
silently; the second belongs on the launch-death path, with its bounded notice.

**A new terminal writer** decides whether it is the one telling the engager. If it announces
the outcome, it arms the durable obligation in the same transition that commits the terminal
— the finalization funnel does (except for a quiet scheduled run's completed end, which is told
to no one, INV-BGJOB-010), and so do the detached launch owner's death report and the
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

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
<!-- END SOURCEMAP -->
