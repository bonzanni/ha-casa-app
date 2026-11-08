---
last_reviewed: 2026-09-19
---

# Engagements

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Durable engagements: their records, how one is launched, and the driver protocol. How a
turn is admitted to a live one is
[`architecture/engagement-turn-admission.md`](engagement-turn-admission.md). What a launch
abort rolls back and what survives a restart are in
[`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md). How an
engagement *ends* — the single-winner
terminal transition, the strictness that keeps creation and the terminal flip from leaving
the persisted and in-memory records disagreeing, the finalization side effects and topic
output ordering — is
[`architecture/engagement-finalization.md`](engagement-finalization.md); the completion gate
that refuses a success over input nobody read is
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md). How agents
address and launch one another — the delegation ACL, the depth cap, the agent-spawn cap —
lives in [`architecture/delegation.md`](delegation.md). What an `in_casa` launch or
follow-up turn must leave behind, and who reports one that did not, is
[`architecture/engagement-turn-outcomes.md`](engagement-turn-outcomes.md). It does not cover
the turn loop itself, nor what a driver's underlying runtime does once started.

## Mental model

**A delegated turn and an engagement are different things.** Delegation in its ordinary form
is a task handed to a specialist that runs and returns — ephemeral. An engagement is a
durable record with its own topic, which outlives the call that created it.

Three launch paths exist and they are not symmetrical. Ordinary specialist delegation runs
ephemerally. *Interactive* specialist delegation creates an engagement. Engaging an executor
always creates one. Both engagement-creating paths pass the agent-spawn gate first
(INV-ENG-008). `start_job` launches through the same interactive launch owner, as a
`specialist` record when a delegate hosts the job and as a third record kind, `plugin`,
when the declaring plugin is installed on the calling resident — a worker record whose
`role_or_type` is that resident and whose session carries neither the resident's tools nor
its prompt; what Casa then does with a job's engagement is
[`architecture/background-jobs.md`](background-jobs.md).

**A specialist's interactive engagement is a conversation, and completing it closes it.**
Its first message (`tools.py::engagement_launch_prompt`) carries the task and the engager's
context, and says that the topic stays open for the person after each reply while
`emit_completion` ends the engagement and closes the topic — to be called when the work is
done or the person says they are finished, never while they are expected to keep talking.
Without that sentence a specialist asked to "open a chat and wait" completed on its first
turn, so the topic closed at once (#1411). Which call ends it stays the model's judgement;
Casa adds no gate. A job's launch replaces this message with its own.

**An in-process executor's topic says what it was asked and shows each turn's steps.**
When an `in_casa` executor's launch is handed off, its topic gets a first line with the
title and the start of the task. Then each turn that uses tools keeps one step log in the
topic. The log is posted at the turn's first main-loop tool use and edited as steps start
(`🔧 This turn so far: 3 steps — latest: updating plugins (at 32s)`). When the turn ends,
the method that ran it hands the log its real outcome, and the log closes itself in the
background (`☑ This turn: 7 steps in 1m 12s`, or `✖ This turn stopped after 3 steps`).
Each text records steps that have already started, so a close that fails, or a topic
closed first by a completion, leaves a true record, never a claim about the present. No
write is awaited by the turn, so a slow Telegram never delays a turn or changes its
outcome; writes are coalesced and spaced, and each Telegram call is bounded. Nothing is
persisted. Because the log shows the steps, the topic is given only the text a turn wrote
after its last main-loop tool call: text written before or between calls ("now let me read
the recipe") is not posted, a turn with no text after its last call posts nothing beyond
the log, and a turn with no calls posts all its text. Which text that is follows from where
the blocks sit, never from the words. Nothing streams while the turn runs, so no interim
line is ever shown and later replaced. The closing text is posted when the turn ends the
way a streamed turn posts a single block — sent raw, then rendered in place, so a failed
render leaves the raw closing text, never an earlier line; a long text is sent one page at a
time, so that a failed page never drops a later one — or, when the turn raises or is
cancelled first, on its way out, within a bound. A cancellation that lands while the text is being sent waits for that
same send, within the same bound, and the send is never restarted, so no page repeats. A sub-agent's words are inside a call and are never the closing text. A specialist's
topic, where the operator talks with the agent turn by turn and no step log runs, still
streams every text block.

**A specialist's engagements are its open conversations, and a change to the specialist
does not reach them.** One keeps the personality and the plugin versions it started with
and reads the specialist's current settings when it resumes. So a persona apply, an upgrade,
a rollback or an uninstall warns before it changes a specialist that has any (INV-SPEC-022),
and removing a specialist closes the ones still open — after an uninstall commits and its
reload removes the specialist, and when a reload or a boot reads it disabled (INV-CFG-013) —
through the terminal funnel, outcome `cancelled`; a close that fails leaves that one open.

**Much less survives a restart than the word "durable" suggests.** The record persists;
concurrency permits, live drivers, inbound reservations and various
in-flight maps do not. A record found `active` at startup is rewritten to `idle`, because no
live driver survived to make `active` true.

**Durable is not indefinite, and engagements can speak up unprompted.** A daily sweep
suspends a live session after a day idle and posts recurring idle reminders (three days for
a specialist record, seven for every other kind, refiring weekly); terminal tombstones age
out after thirty days, bounding duplicate-task protection (one whose completion time is not a
finite number has no known age and is kept, and an `in_casa` one only once its transcripts
are reaped, per `architecture/engagement-transcript-reaping.md`). Separately, an observer watches
engagement events and may post a bounded LLM interjection into the resident chat — capped
at three per engagement and suppressible with `/silent`. The cap holds under concurrent dispatch: a
budget slot is reserved before evaluation and returned if nothing is posted.

## Contracts & invariants

The record's own contracts are defined where the behaviour is: the terminal transition and
creation strictness in [`architecture/engagement-finalization.md`](engagement-finalization.md),
turn admission in [`architecture/engagement-turn-admission.md`](engagement-turn-admission.md),
the completion gate in
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md), and what a
launch or turn must leave behind in
[`architecture/engagement-turn-outcomes.md`](engagement-turn-outcomes.md).

## Failure behavior

What a launch abort rolls back and how far it gets, which launch-failure arms answer the
calling turn rather than the operator's topic, and what a restart replays or refuses, are in
[`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md). The
invariant that behaviour is measured against is INV-ENG-011 in
[`architecture/engagement-turn-outcomes.md`](engagement-turn-outcomes.md).

## Extension points

**A new driver** implements the driver protocol: start, send, cancel, resume, liveness —
plus the downgrade-recovery seams (invalidate the live session with confirmed teardown;
rebuild fresh at the record's current clearance). `start()` — or `open()`, for a driver
that declares the split launch of INV-ENG-021 — may raise `StaleLaunchError` at its last
suspension point; the launcher then aborts rather than deliver a prompt rendered from
pre-downgrade materials. A driver without the split keeps its whole first turn inside the
launching tool call. The protocol says nothing about OS identity or filesystem reach: a
driver that runs its agent as a separate OS process owes its own containment.

**`start()`'s `prompt` is the first turn.** For the in-process driver that is the opening
user message; a memory-enabled launch interpolates the prior-engagement archive into it (see
[`architecture/memory-scoping.md`](memory-scoping.md)). Nothing after the launch fetches the
archive again: a clearance rebuild *clears* the cached block rather than refetching — a
refetch there would read at the clearance the rebuild has just left, which is the thing the
rebuild exists to stop.

**A new durable field** must be added to the record, its load path and its write path
together — otherwise it exists at runtime and silently vanishes across a restart. A field that
records an OBLIGATION owes three more decisions: what arms it (and, if the answer depends on
which caller wrote the terminal rather than on any property of the record, it is an explicit
argument to the transition, not a predicate), whether the strict rollback restores it, and
whether a record carrying it survives the terminal-retention expiry. One does so today:
expiring the row would delete the obligation along with the state needed to discharge it.
`terminal_notification_pending` is the telling a finalized outcome still owes the party that
asked for the work (INV-ENG-018, in
[`architecture/engagement-terminal-telling.md`](engagement-terminal-telling.md)). A legacy row
missing the key decodes as owing NOTHING — the opposite default would discharge the whole
tombstone's worth of obligations at the first boot after an upgrade.

**A new origin value that may hold a live object** must be registered as non-persistable, or
serialization will either fail or persist something meaningless.

**A new terminal path, and a new topic output**, belong with the finalize funnel in
[`architecture/engagement-finalization.md`](engagement-finalization.md).
Whether a terminal writer needed authority to assert *that* outcome — and why neither
persisting ledger checks it — is answered in the same document.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRecord`
- `casa/rootfs/opt/casa/drivers/driver_protocol.py::DriverProtocol`
- `casa/rootfs/opt/casa/tools.py::engage_executor`
- `casa/rootfs/opt/casa/tools.py::_post_topic_opening`
- `casa/rootfs/opt/casa/tools.py::engagement_launch_prompt`
- `casa/rootfs/opt/casa/drivers/turn_progress.py::TurnProgressLine`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::_fold_closing_text`

**Tests**
- `tests/test_engagement_stays_open.py`
- `tests/test_turn_progress.py`
- `tests/test_in_casa_turn_progress.py`
- `tests/test_in_casa_executor_closing_text.py`
- `tests/test_engage_executor_tool.py::TestEngageHandoffLines`
- `tests/test_delegate_to_agent.py`
- `tests/test_delegate_to_agent_interactive.py`
- `tests/test_engagement_registry.py`
- `tests/test_observer.py`
- `tests/test_in_casa_inbound_admission.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/engagement-turn-admission.md`](../architecture/engagement-turn-admission.md)
- [`architecture/engagement-turn-outcomes.md`](../architecture/engagement-turn-outcomes.md)
<!-- END SOURCEMAP -->
