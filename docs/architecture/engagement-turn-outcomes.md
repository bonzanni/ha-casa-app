---
last_reviewed: 2026-10-05
---

# Engagement turn outcomes: what an `in_casa` launch or follow-up turn leaves behind

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What an `in_casa` engagement's LAUNCH turn and its ticketed FOLLOW-UP turns must leave
behind, what the driver observes when one does not, and who reports it: a launch's death
(INV-ENG-011), the one bounded notice a cut-off or undelivered follow-up turn draws
(INV-ENG-012), and a turn that stopped at its turn limit (INV-ENG-020). The record, how an
engagement is launched and the driver protocol are in [`engagements.md`](engagements.md);
the two-call launch and its anchored owner are
[`engagement-launch-detach.md`](engagement-launch-detach.md); how a turn is admitted is
[`engagement-turn-admission.md`](engagement-turn-admission.md); what an engagement's
terminal transition posts is [`engagement-finalization.md`](engagement-finalization.md).

## Mental model

**The driver observes; an owner adjudicates.** The `in_casa` driver records what a turn left
behind — no result frame, nothing visible, text the topic refused, a turn-limit stop — and
neither raises nor reads the record's status. The launch's owner reads the launch turn's
observation once; a follow-up turn's observation is read by the owner of that turn's
admission ticket. The reasons are under each invariant below.

## Contracts & invariants

**INV-ENG-011**: An `in_casa` LAUNCH turn ends holding the turn's own terminal artifact and either a terminal engagement record or operator-visible topic output — or the launch's owner reports the death: one durable strict `error` transition, one bounded notice into the still-open topic, a bounded driver teardown, and the topic aborted, whether or not the tool call that launched the engagement is still running. It is never left `active` behind an ended transport with nothing posted, and the path never writes `completed` and never retains to the shared memory bank. A launch turn that stopped at its turn limit is never reported dead (INV-ENG-020); when its one line fails or is cancelled it is left `active`, transport live, with nothing posted.

A driver's `start()` returning has always meant *the first turn ran to its end*, never *the
engagement reported anything*, and that gap is where a launch turn could die unnoticed. The
turn's own terminal artifact is its result frame: the SDK's response iterator returns at that
frame and otherwise iterates indefinitely, so a drained stream with no result frame is a turn
that was cut off in flight — transport EOF, agent-process exit, or the reader being cancelled
— however many frames it produced first. The count of frames is *not* the predicate: a turn
cut off mid-tool-loop is indistinguishable from a finished one by frame evidence, so a check
built on evidence catches only the trivially empty turn and misses the reachable case.

The engagement's terminal artifact is its record. An interactive engagement that ends its
launch turn having posted text is legitimately awaiting the operator and is left alive; one
that posted nothing, or whose turn was cut off, has left no surface anyone can act on.

Three properties make the report safe rather than merely present. The driver only
**observes** — it records what the turn left behind and neither raises nor reads the
record's status, because a lock-free status read can catch the uncommitted window of a
strict terminal transition that a persist failure then rolls back. The launcher asks the
registry exactly **one transactional question** — flip this record terminal, strictly,
unless it already is — and its three outcomes decide everything: a durable win reports, a
lost race performs no side effect at all because the winner owns them, and a rolled-back
persist leaves the record live and its topic *open* while retiring the client, since an open
topic over a live record is recoverable and a closed one is not. And the side effects run in
one **anchored owner** the launcher awaits shielded, because a terminal transition whose
write committed keeps its durable state and re-raises on cancellation: an inline owner could
commit the flip and then never post, while a compensating second owner would correctly lose
the transition and do nothing.

The same owner reports a launch *cancelled* before its driver was confirmed live, which
previously flipped the topic to failed and closed it without posting anything at all.

**What can cut a launch turn — and the healthy completion that looks like a cut.** The
launch turn runs in an owner Casa anchors, on the engagement's own client, after the
launching tool call has already answered `pending`
([`architecture/engagement-launch-detach.md`](engagement-launch-detach.md), INV-ENG-021). Until
v0.314.0 it ran inside the launching resident's own tool call, and whatever tore that
resident's warm client down reached the launch with it: the drain fuse of a reload that
replaced the resident cut the turn one drain timeout after the reload RETURNED (see
[`architecture/sdk-client-pool.md`](sdk-client-pool.md)), and evicting the resident cancelled it
at once. On 2026-09-15 that was measured as a three-minute hold on the assistant, then a cut.
Neither edge reaches the owner: no reload scope holds a reference to the engagement driver,
the engagement's client belongs to no pool, and the driver's own closers are the only ones.
What can cut the turn now is the graceful stop, which cancels every enrolled owner with a
recorded cause (INV-ENG-015), and the engagement's own transport ending — an agent-process
exit or an EOF the SDK's iterator reports as a drained stream with no result frame. Before
attributing an ended turn to a reload at all, eliminate the benign case first: the finalize tail
closes the engagement's own client, so a healthy self-emitted completion ends its turn with
no result frame, and the configurator's doctrine puts a reload immediately before
`emit_completion`; a turn that ends shortly after a reload having posted no result frame
therefore has the shape of a turn that succeeded. A launch that really was cut is reported,
as this invariant requires, but a cut from anything other than the stop is not attributed:
its detail is the undifferentiated "the tool call was cancelled during launch", because the
launch cause seam is written at the canceller and the graceful stop is the only canceller
that writes one today (#847).

**INV-ENG-012**: A ticketed FOLLOW-UP turn to an `in_casa` engagement that ends without the turn's own terminal artifact — or that finishes holding it while finalization has *established* that its streamed text was wholly undelivered — is never answered with silence: exactly one bounded operator-facing notice attempt is made in the engagement's topic, by the owner of that turn's admission ticket, and one turn's observation can never be consumed or lost by another turn's owner. The single thing that excuses the telling is that the engagement's terminal path has already told that topic why the engagement ended; the record merely being terminal is not that fact, and wherever it is not known that the topic was told, the telling is made. A follow-up turn that ends holding its terminal artifact with no established delivery failure produces no observation and no notice, unless it stopped at its turn limit — INV-ENG-020 governs that turn, and outside a batch turn tells it instead of this statement even when its text was wholly undelivered; an *ambiguous* delivery (a lost acknowledgement, or a handle off the delivery contract) is not an established failure and records nothing. The driver records, never raises, and never reads the record's status.

INV-ENG-011 covers the launch turn. The turn *after* it had the same hole and no owner at
all: cut off mid-tool-loop, a ticketed turn raises nothing, records nothing, discharges its
admission ticket and leaves the record live, so an admitted operator message was consumed
and answered with nothing. The driver's zero-frame warning does not fire either — it is
gated on having seen no assistant frame, and a mid-tool-loop cutoff has seen several.

The predicate is the same one INV-ENG-011 uses and for the same reason: the absence of the
turn's result frame. It is emphatically not the evidence latch, which is set by the first
frame of any kind and so reads identically for a cut-off turn and a finished one.

The second arm is the delivery failure. A follow-up turn can finish — its result frame
arrives — with every Telegram operation on its streamed text positively refused, and such a
turn used to read as an ordinary quiet one. The stream handle's finalize now reports a
delivery outcome (INV-TG-006 in [`telegram.md`](telegram.md)), and only an *established*
failure is recorded: a lost acknowledgement may already be on the operator's screen, and
reporting it would tell them about — or, on the launch side, kill — a delivered turn. The
notice for this arm asserts only what its site observed: the turn finished, and its response
could not be delivered to the topic. It names no cause, because the reason carries none — an
absent Telegram application produces the same established failure as a refusal.

**The driver observes; the delivery task adjudicates.** That split is not tidiness, it is
the only place the distinction can be made. A raise from the driver would be
indistinguishable from a healthy `in_casa` self-emit completion, where the finalize funnel's
tail closes the engagement's own client and the response iterator legitimately ends with no
result frame — so a driver that raised would report a failure for a turn that succeeded. The
delivery task can tell the two apart because it can ask the registry, and asking is
something the driver is separately forbidden to do (see the lock-free-read hazard under
INV-ENG-011).

That question is asked of the *settled* record, under the registry's own lock. A lock-free
read can land inside a strict transition that has committed its terminal fields in memory
and not yet survived its tombstone write; if that write fails the transition is fully rolled
back, and a reader that saw the transient value would suppress a notice that was owed. The
read is bounded, and a read that does not return in time results in the notice being posted
anyway: the failure this invariant exists to remove is silence, so the fail-open direction
is toward telling.

**A terminal status is not proof that anything was told, and the suppression turns on the
telling.** The two arms of this release compose into the state that makes the difference
matter: a ticketed turn completes its engagement, the completion post fails, the funnel's
one bounded disclosure fails too, and the tail then ends that turn's stream — so the turn
looks cut off, the record reads `completed`, and a reader that asked only for the status
would stay quiet over a topic that heard nothing at all. The funnel therefore records
whether its own telling reached the topic, and the delivery task asks for the status and
that fact together, in one locked read. Not knowing counts as not told: no record of a
telling, a read that raises, and a read that does not return in time all produce the
notice. The recorded fact is in-memory and is never persisted — it coordinates two tasks
inside one process, and a restart has no surviving turn to adjudicate.

The observation is keyed by the **admission ticket**, not by the engagement. The record is
written after the per-engagement turn lock is released, so two consecutive turns can cross
there: the first writes its reason, the second runs to completion, and one observation is
overwritten or read by the wrong turn's owner. Per-engagement keying does not merely blur
the two, it loses one of them.

**It is one bounded attempt, not an ordering guarantee.** Nothing orders this notice against
a concurrent finalization's topic operations, so a terminal writer that wins its transition
after the settled read can paint and close first, leaving the notice below the paint or
failing against a closed topic. `in_casa` turn admission now exists, and it does not close
this: admission decides whether a turn is *delivered*, not where a notice *lands*. Ordering
the notice would additionally require the finalization's own topic operations to be sequenced
against it, which is not built here.

**INV-ENG-020**: An `in_casa` engagement turn whose terminal SDK result has subtype `error_max_turns` is a limit stop. It is detected from that subtype alone — after the API-fault and evidence checks, so a refusal still ends as `ApiErrorTurn` — and recorded alongside the turn's existing observation, never raised. A limit stop never ends the engagement: a LAUNCH turn that stopped at its limit (tool-only, with text, with its text undelivered, or a job's launch turn) is not reported dead, gets no other launch notice, and the engaging resident is sent nothing; a cancellation landing during its telling never takes the launch's cancellation-abort arm. Except on a BATCH turn, Casa makes exactly one bounded attempt to post one line of its own — the resident step-limit line (INV-TURN-014) — into the engagement's topic, after whatever the turn posted and even when it posted nothing, and no attempt when the bounded settled-state read returns a terminal status; limit handling logs exactly one WARNING naming the engagement, the role, the turn count and the telling's outcome. Such a turn is never reported cut off, and the limit takes precedence over an established text-not-delivered observation, whose notice is then not posted. A batch turn — delivered by `deliver_system_turn` into an engagement whose origin carries a job — is handled exactly as before: no line, no limit WARNING, not cut off on its own. A failed or cancelled line post is logged and leaves the engagement live; nothing retries it.

## Failure behavior

Each invariant above states its own failure direction. What a launch abort rolls back, and
which launch-failure arms answer the calling turn rather than the operator's topic, are in
[`engagement-failure-and-restart.md`](engagement-failure-and-restart.md).

## Extension points

**A new reason a turn can end without its artifact** is recorded by the driver beside the
existing ones, in the slot of the arm it belongs to — launch or ticketed follow-up — and read
by that arm's owner; the driver never raises it and never reads the record's status.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.launch_turn_incomplete`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.followup_turn_incomplete`

**Tests**
- `tests/test_in_casa_launch_terminal_artifact.py`
- `tests/test_in_casa_launch_retention_guard.py`
- `tests/test_launch_death_reporter.py`
- `tests/test_pin_1141_engagement_turn_limit.py`
- `tests/test_turn_limit_engagement_regressions.py`
- `tests/test_in_casa_driver.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-launch-detach.md`](../architecture/engagement-launch-detach.md)
- [`architecture/engagement-turn-admission.md`](../architecture/engagement-turn-admission.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagement-failure-and-restart.md`](../architecture/engagement-failure-and-restart.md)
- [`architecture/telegram-failures.md`](../architecture/telegram-failures.md)
<!-- END SOURCEMAP -->
