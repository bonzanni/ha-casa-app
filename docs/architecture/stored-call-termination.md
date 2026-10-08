---
last_reviewed: 2026-10-05
---

# Stored-call buttons: the pinned turn's termination and the faulted desk

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the pinned one-call turn of a stored-call tap ends: the `PinnedRun` controller that owns
the run's processes, the one release path every end goes through, and the faulted desk when
termination cannot be confirmed. The proposal, the tap's admission chain and re-checks, the
pin, the capture and the receipt are [`stored-call-buttons.md`](stored-call-buttons.md); the
desk a tap uses is [`specialist-desk.md`](specialist-desk.md); the bounded delegation
wrapper's own process tree is [`delegation.md`](delegation.md). Telegram only.

## Mental model

**The controller owns the run's processes.** The pinned turn is not driven by the bounded
delegation wrapper. A `PinnedRun` enters the SDK client, pins a pidfd on the CLI and on
every descendant it finds at start, and owns the client to the end; the runner never exits
it. During a termination it walks the descendants of every pinned process again — when the
termination begins, before the execution task is cancelled (a server may exit during that wait
and reparent its child), and again just before the signals — and pins what a tool call started since —
a plugin server's ordinary child, such as a shelled-out command — so that it is killed and
counted like the rest (#1205: a child started after the start-time snapshot had survived the
kill, reparented to init, with no fault and no notice). A walk that cannot be complete is not
a confirmation: a proven pinned process other than the CLI that is found exited before Casa
signalled anything — already dead when the termination began, or dying while the walk ran —
may have left a child reparented before any walk could see it, so the run is unconfirmed and
the desk is faulted and told rather than released over a survivor Casa cannot see. On the turn's normal end the controller starts the SDK's close as a detached task and
confirms every pinned process's exit by pidfd readability under one deadline — nothing
inside the SDK's teardown is awaited. On the desk turn's ceiling, or when a process is still
alive after that, the operator is told at once (`✖ failed`), and the controller cancels the
execution task (5 s), signals the CLI through its pidfd (SIGTERM, 5 s, SIGKILL) and the
descendants (SIGKILL), seals its own hook callbacks — a callback entering after the seal has
no effect; one already inside is drained — waits for every pidfd under 10 s, discards the
confirmed-dead CLI from the SDK's reaper set, and only then releases the desk and the
permit. A receipt captured during the hold is still posted — or, for a tap that answered
`in_place`, shown as the edited card ([`stored-call-next-card.md`](stored-call-next-card.md)).

**One release path.** Every end of a pinned run — the normal end, the ceiling and a
cancellation (a Casa stop cancels the turn's task) — goes through one settle function, run
shielded from any cancellation, whose only input is the confirmation predicate: every pinned
fd's own pidfd readable, every callback drained, every identity established. On it, together
and in order, the function decides the fault (unconfirmed — a teardown itself cancelled by a
loop shutdown included — faults the desk, tells, refreshes the health report), deletes the
pinned run's transcript — at once when the exit is confirmed, otherwise through a detached
waiter that runs after the pinned fds report exit, since an unconfirmed writer may still
flush it — closes the fds and releases the permit; nothing else in the tap performs any of
these. A pinned fd is dropped as extinct only on its own pidfd's evidence,
never on a `/proc` read's outcome: a descendant that is unreadable or whose descent from the
CLI cannot be proven is kept, awaited, never signalled, and leaves the run unconfirmed. The
controller owns the client from before its entry is awaited, so a ceiling that fires while
the CLI's initialisation hangs still finds the process; an entry cancelled with no process
to pin is unconfirmed. A tap cancelled in-process after its call ran is never silent: an ERROR
names the run, the exchange is logged and the resident's line records the applied tap. The
process's own exit — the event loop's shutdown sweep cancelling every task at once — is outside
this: in-memory state and pidfds do not outlive the process, a restart starts clean, and a CLI
child or an in-flight reply lost at that moment is what every delegation and resident turn
already accepts today.

**Bounded best effort, told.** If a pinned process's exit stayed unconfirmed at the
deadline, or its identity could not be established while pinned, or a callback was still
inside, the desk is *faulted*: every later use of it — reply, tap, delegation — is refused
at once with a labelled notice (`📊 Finance's desk is faulted; a Casa restart clears it.`),
the permit is released, an ERROR is logged, and the standing plugin-health report carries a
`desk_faulted` row against the plugin and the specialist until a restart. A worker that a
plugin deliberately detaches — `setsid`, a double fork — is not a descendant and is outside
the pin: that is the plugin's own responsibility, an accepted risk rather than a Casa
guarantee; an ordinary child started after the start-time snapshot is not (it is re-walked at
termination). What stays uncovered is narrower still, and accepted by the operator's ruling
under §14.8 (#1205, 2026-10-03): a child started in the instant between the last walk and the
signals, and three windows in which a server dies around the walks themselves — during a
still-pending SDK entry when the cancellation reaps it, before Casa's own close began on the
normal-end path, or while the walk's `/proc` enumeration runs so that it is listed and gone
before its pidfd is taken — in each of which an ordinary child can be reparented unseen. The
stricter rule that would close them (any such death leaves the run unconfirmed) was declined
because it would fault desks until restart on false alarms; these windows are documented, with
tests marked as expected failures, not fixed. A desk is also refused, without
being faulted, while a swipe-reply's or a delegation's run that outlived its teardown bound is
still unwinding ([`specialist-desk.md`](specialist-desk.md)): the same notice and typed result,
no health row, and it ends when that run does.

## Contracts & invariants

The release this document describes is part of INV-PROP-001, defined in
[`stored-call-buttons.md`](stored-call-buttons.md): a desk is not released to a later use until
the CLI, the plugin servers it started and the ordinary children those servers started by
termination time are terminated, or the desk is faulted.

## Failure behavior

**Termination unconfirmed at the deadline.** The desk is faulted (above); the plugin-health
report says so until restart.

**A Casa stop while a tap runs.** The channel's stop cancels the turn's task in-process: the
settle function runs before the cancellation propagates — on the run, or on the receipt's send; an unconfirmed exit faults the desk for the
rest of the process's life; a receipt captured meanwhile cannot be posted: an ERROR names the
run, the exchange and the applied echo line are still recorded.

## Extension points

**Reusing the controller** for the desk-reply early-release overlap (#1197) is possible but
not done here; the bounded termination path stays simple.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/pinned_run.py::PinnedRun`
- `casa/rootfs/opt/casa/specialist_desk.py::_settle_pinned_run`

**Tests**
- `tests/test_pinned_run.py`

**Related**
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/plugin-health.md`](../architecture/plugin-health.md)
<!-- END SOURCEMAP -->
