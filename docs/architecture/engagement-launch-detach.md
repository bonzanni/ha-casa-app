---
last_reviewed: 2026-11-09
---

# Engagement launch detach

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a background job's launch is separated from the tool call that launched it: the launch
opens the job's session, queues its brief and answers `pending`; what a launch that ends
before that answer draws — the launch-death reporter (INV-ENG-030) and the telling owed to the
engager; how the graceful stop drains the launch owners; and the one envelope every
engagement outcome travels in. Once the launch has answered, the job's session owns the job:
its turn ends, the nudge and the parking, the step ceiling and a session that ends
unexpectedly are in [`background-job-session.md`](background-job-session.md)
(INV-BGJOB-016..020). Which launch-failure arms answer the caller rather than the topic is in
[`engagement-failure-and-restart.md`](engagement-failure-and-restart.md) (INV-ENG-019); the
durable obligation to tell the engager is in
[`engagement-terminal-telling.md`](engagement-terminal-telling.md) (INV-ENG-018).

## Mental model

**The engager is not held.** The launch owner (`tools.py::_launch_interactive_engagement`)
awaits the driver's `open` — the client entered and registered, its reader and the job's
delivery worker started — then queues the brief (`InCasaDriver.enqueue_brief`) and returns
`pending`. The job's first turn runs in its session's reader, on the job's own client, after
the launching tool call has already answered `pending`. The engager's turn ends, and the job's
result reaches it the way every result does, as a notification when the job ends. A launch
once ran the whole first turn inside the engaging resident's own tool call; a reload that
replaced the resident then held the assistant for minutes and cut the turn from under it. No
edge of that kind reaches a job now: no reload scope holds a reference to the engagement
driver, the job's client belongs to no pool, and the driver's own closers are the only ones.

**Two owners, split at the answer.** Until the launch answers, a failure is the launch's. A
named fault — options that would not build, a profile that would not persist, no driver, a
client that would not open — answers the calling turn with its kind (INV-ENG-019). A
cancellation of the tool call after its record exists, or Casa's stop latching while the
session opened, carries no fault of its own, so the launch-death reporter reports it into the
topic (INV-ENG-030); a latched launch still returns `launch_cancelled` to its caller, while a
cancelled one returns nothing and the engager is told over the bus. From the answer on, the session owns the job, and
a failure of the session itself — its stream ending, a write failing — is session loss,
resumed once (INV-BGJOB-018), never a launch death. The brief is admitted at its write like
every write (INV-ENG-009), so a job that ended between `open` and the brief's write receives
nothing.

**What can end a job's session, and the healthy end that looks like one.** The graceful stop
cancels a launch still enrolled, with a recorded cause
([`engagement-failure-and-restart.md`](engagement-failure-and-restart.md)); once a job runs,
its session ends by Casa's own teardown or by its transport ending — an agent-process exit or
an EOF. Before attributing an ended session to a reload at all, eliminate the benign case
first: the finalize tail closes the job's own client, so a healthy completion ends its
session, and that close is Casa's teardown and is quiet.

**Every outcome of a launch that ends before its answer has an owner, and the owner tells the
engager.** A named fault's envelope is its telling. A launcher that was cancelled returns no
envelope, so its cancellation owner tells the engager over the bus, on the same envelope the
finalization funnel and the boot replay use, with the terminal write armed so a telling lost
with the process is replayed at boot.

## Contracts & invariants

**INV-ENG-021**: A job launch answers `pending` to the call that launched it once the job's session is open and its brief is queued; the job's first turn never runs inside the engager's turn, and cancelling the tool call after it returned does not touch the session. A launch that ends before that answer is told to the engager exactly once — by its returned envelope (a named fault, or `launch_cancelled` when a stop latched while its session opened), acknowledged on return, or, when the launcher itself was cancelled, live over the one engagement-outcome envelope with the obligation armed in the terminal write. Telling ownership is never inferred from the record: a compensating owner that loses the terminal race tells nobody, however armed the record is; an inline arm acknowledges on return only a transition it won, and hands its telling to its own abort only when the launcher was cancelled before returning the envelope. A cancellation whose terminal write did not persist leaves the record live and its topic open, posts one bounded notice saying so, and tells the engager under its own non-terminal kind, which the consumer never narrates as a failure. The graceful stop drains the launch ledger, then the reporters and the pre-record topic aborts — a launcher's that was cancelled, failed or refused between its topic and its record — together to joint quiescence, and once it has marked those drains complete none of a cancelled launcher's compensation, a named-fault abort or a pre-record topic abort mints anything; each is logged.

**The launch enrols itself with the ledger the moment its record exists.** The tool call
enrols its own task under the engagement's id right after `create()` and drops the handle on
every exit, so the graceful stop's ledger walk sees the launch for its whole stretch between
record and answer. A stop that began during `open()` finds the ledger latched: a tool call
that resumes from `open()` to find the latch set queues no brief — it reports the cancellation
inline, armed, and acknowledges on return like every other inline abort.

**A launch that answered has nothing left to report.** Once the brief is queued and `pending`
returned, the launch owner holds nothing: the session's reader owns every turn, the job's
turn-end handler continues, nudges or parks it, and the driver's session-loss path resumes a
session that ended. A first turn that ends with nothing visible is a turn end like any other
(INV-BGJOB-017), not a death.

**Cancellation has an owner at every point.** A launcher cancelled after its record exists
takes the cancellation arm: the compensation (`tools.py::_abort_launch_on_cancel`) is anchored
in the reporter set the stop drains, reports `launch_cancelled` with the stop's recorded cause
through the launch-death reporter, armed, and tells the engager what the report's outcome
allows — the fact of the outcome on a win, the uncommitted notice on a rolled-back persist,
nothing on a lost race. Its telling runs inside that anchored task, which the launcher's own
cancellation never reaches, so nothing cuts it and nothing spawns a second one. A compensating second reporter that finds the record
already terminal tells nobody: the winner owns the telling, and the record's armed bit is not
permission to send, because a surviving finalization funnel leaves it armed while its own
delivery is pending (reading the bit as permission told the engager twice). The one case where
a winner cannot tell — an inline abort whose launcher was cancelled before it returned the
envelope — is handed over explicitly by that launcher: its cancellation arm attaches the
telling to the abort's own completion, and the abort tells live only if it won.

After the stop has marked its drains complete nothing is minted — not by a launcher cancelled
after the mark, not by a named-fault arm a launcher reaches before its cancellation is
delivered, and not by a launcher cancelled while still creating its topic, which the ledger
walk could not see — because the interpreter's final sweep is next and would destroy the
report pending; the residual is logged and boot re-idles the live record. A launcher that
fails between its topic and its record — a `create()` that could not persist — is in no ledger
either, so it does not close its topic inline: it hands the abort to the same anchored owner as
the cancelled launcher and returns its own error envelope at once. After the mark that owner
mints nothing and the topic, which has no record behind it, is logged as left open.

The stop's order is the ledger drain, then the anchors — the reporters with their acks and
tellings, and the pre-record topic aborts — drained TOGETHER to joint quiescence, then the
mark. Joint, because a task in one anchor can mint into another (a finished topic abort
releases a launcher whose named abort then mints a telling into the reporter set), so drains in
sequence let work into an already-drained set; and quiescence is confirmed only after one yield
to the loop, because a task that has just completed reads as done while the done callback that
mints its successor is still queued (a stop that marked on the first empty snapshot, or after
sequential drains, left one telling to be minted after it).

**A persist failure is not a failure.** A cancellation whose strict terminal write rolled back
leaves a live record and an open topic — an open topic over a live record is recoverable and a
closed one is not, exactly as INV-ENG-019 reasons for the inline aborts. Telling the engager
"error" over the bus would have the consumer narrate a failure for an engagement that is still
open and resumable. So the owner posts one bounded notice into the topic saying the outcome
could not be recorded, the reporter has already retired the client, and the engager gets an
UNARMED notice of kind `launch_outcome_uncommitted` — there is no terminal write to carry an
obligation. The consumer has a branch for that kind that says the engagement is still open and
did not fail, and never routes it through the failure narration.

**INV-ENG-030**: A launch that ends after its record exists and before it answered — its launcher cancelled, or Casa's stop latched while its session opened — is reported by one owner, the launch-death reporter, which asks the registry one strict terminal question and lets its three outcomes decide: a durable win posts one bounded notice into the still-open topic, tears the driver down under a bound, settles the job's message journal and aborts the topic; a lost race performs no side effect; a rolled-back persist leaves the record live and its topic open with the client retired. The path writes only `error`, never `completed`.

The reporter (`tools.py::_report_launch_death`) is the finalization funnel's order applied to
a launch — mark strictly, tell, then tear down under a bound — and deliberately not the funnel
itself: the funnel tells the engager the outcome as the engagement reported it, so a launch
death recorded as a completion with empty text would tell it of a success that never happened,
and the funnel hard-codes a completion error kind that would erase `launch_cancelled`. One
transactional question, because a lock-free status read can catch the uncommitted window of a
strict transition that a persist failure then rolls back, and the best-effort marker returns
true over a swallowed tombstone-write failure, which would authorize an irreversible close over
a record disk still calls `active`. The reporter runs as an anchored task the launcher awaits
shielded: a terminal transition whose write committed keeps its durable state and re-raises on
cancellation, so an inline owner could commit the flip and then never post, while a
compensating second owner would correctly lose the transition and do nothing. Each topic
operation and the teardown carry their own bound, so one wedged counterparty never strands the
rest. The notice names the launch's fault only and lists no inbound message: a message the
operator sent meanwhile is the journal's to tell, once (INV-ENG-029).

**One envelope.** The finalization funnel, the boot replay and the cancellation owner each
build their own `DelegationComplete` — they differ in text, result availability, elapsed time
and message — and hand it to one sender (`tools.py::send_engagement_outcome`) that carries only
the routing: the target role from the origin with the caller's fallback (the boot replay passes
the configured assistant role; a hard-coded fallback sent a role-less record to a queue that
need not exist, and the bus drops an unknown target silently), the channel, the
cid/chat-id/engagement-id context merged with whatever the caller adds (the funnel's next
steps), and the delivery acknowledgement.

## Failure behavior

| Failure | Behaviour |
|---|---|
| `open()` raises, or another named fault before it | The tool call answers its named fault, the abort armed and acknowledged on return when it won its transition; a lost transition leaves the winner's obligation untouched; no brief is queued. |
| The tool call is cancelled inside an inline abort | The abort finishes anchored; if it won, its completion tells the engager live with the acknowledgement; the cancellation owner finds the record terminal and tells nobody. |
| The stop latches while `open()` is suspended | The ledger cancels the tool call; if the client's connect consumed that cancellation, the latch check reports `launch_cancelled` inline, armed, acknowledged on return, and queues no brief. |
| The tool call is cancelled after its record exists | The cancellation owner reports through the launch-death reporter (strict transition, bounded notice, bounded teardown, journal settled, topic aborted) and tells the engager live with the acknowledgement that clears the obligation. |
| The bus drops the engager's role | The message is dropped silently by the bus; the obligation stays armed; boot replays it (INV-ENG-018). |
| The terminal write of the cancellation rolls back | Record live, topic open, one bounded notice, client retired, unarmed `launch_outcome_uncommitted` to the engager; the consumer says "still open". |
| A launcher is cancelled after the drains are marked complete | Its compensation is not minted; logged; boot re-idles the live record. A named-fault arm it reaches before the cancellation lands spawns no abort either: it is refused as the cancellation it is. |
| A launcher is cancelled or fails between creating its topic and its record | Its topic abort is anchored and the stop drains it (it is in no ledger, having no record); after the drains are marked complete it is not minted and the topic left open without a record is logged. |
| The tool call's task is cancelled after it returned | Nothing: the session belongs to the job. |
| The session ends after the launch answered | Session loss, not a launch death: resumed once, then the job ends `error` (INV-BGJOB-018). |

## Extension points

**A new step in the launch** goes before the brief is queued, and decides what its failure is:
a named fault the calling turn can act on takes an inline abort (INV-ENG-019); anything that
can only be cancelled is already covered by the cancellation arm.

**A new outcome the launch must tell** decides first whether the record is terminal. If it is,
the telling is armed in the transition and cleared by delivery; if it is not, the notice is
unarmed and carries a kind the consumer narrates as non-terminal, with its own branch.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_abort_launch_on_cancel`
- `casa/rootfs/opt/casa/tools.py::_report_launch_death`
- `casa/rootfs/opt/casa/tools.py::_tell_engager_launch_outcome`
- `casa/rootfs/opt/casa/tools.py::send_engagement_outcome`
- `casa/rootfs/opt/casa/tools.py::_schedule_terminal_ack`
- `casa/rootfs/opt/casa/tools.py::drain_launch_death_reports`
- `casa/rootfs/opt/casa/tools.py::stop_engagement_launches`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.open`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.launch_shutdown_active`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.mark_launch_drains_complete`

**Tests**
- `tests/test_in_casa_launch_detach.py`
- `tests/test_launch_death_reporter.py`
- `tests/test_in_casa_launch_retention_guard.py`
- `tests/test_in_casa_launch_terminal_artifact.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/background-job-session.md`](../architecture/background-job-session.md)
- [`architecture/engagement-failure-and-restart.md`](../architecture/engagement-failure-and-restart.md)
- [`architecture/engagement-terminal-telling.md`](../architecture/engagement-terminal-telling.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
<!-- END SOURCEMAP -->
