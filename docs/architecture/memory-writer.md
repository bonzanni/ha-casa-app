---
last_reviewed: 2026-11-02
---

# Memory writer: filing captured events into the ledgers

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the events capture stores become a ledger: the one background worker that runs every
writer job, the one pending predicate that says what is still owed, the commit that files a
batch with its three checks, correction marking, crash recovery, tier classification of
captured events, failures, the nightly pass and the size meter. What is captured and when is
[`architecture/memory-capture.md`](memory-capture.md); the store, its tables and the registry
are [`architecture/memory-store.md`](memory-store.md); how a ledger is served to a session
is [`architecture/memory-serving.md`](memory-serving.md); the `correct_memory` tool that
marks a correction is [`architecture/memory-tools.md`](memory-tools.md).

## Mental model

**One predicate says what is owed.** An event is *pending* in a ledger while it is eligible
for that ledger (its tier at or below the ledger's, and classified unless the ledger is
`private`) and has no terminal disposition there committed after its latest correction mark.
`patched` and `no_change` are terminal; `deferred` is not. The predicate is the `pending`
view in `memory.db`, and every consumer reads it — writer scheduling and input assembly,
correction marking, the meter. No other record of whether an event was handled exists. Only
the `private` ledger is maintained for now.

**Two counters order everything, and neither is stored.** Each writer commit takes the next
`commit_no`: one more than the highest number any disposition, revision or correction mark
carries, read inside the commit's own `BEGIN IMMEDIATE` transaction. A correction mark takes
the next `mark_no` the same way, and records as `mark_commit_no` the `commit_no` current in
its transaction. A store that holds the rows holds its counters.

**One worker, one job at a time.** Every change to a ledger is a job — `ordinary` and
`correction` file pending events, `condense` shortens entries over 1 024 tokens, `regroup`
arranges entries into groups and topics — and only the writer task in the Casa process runs
them, a correction job first. It never runs on a turn's path: it is a background task, and
its SQLite work runs in threads. Cadence: once a ledger's pending events that no run of this
process was given have been quiet for 10 minutes, or the oldest of them has waited 60 minutes;
at the end of a session (an explicit reset); and right after a correction mark. "Given" is
measured by `seq`, which the store assigns inside its single write transaction, so an event a
run's read snapshot missed always counts as not given, whenever it was stamped. Nightly, at 03:30 in
the operator's zone, a pass queues `condense` (an entry over 1 024 tokens), `regroup` (a
ledger whose view overflows a served role's budget even at its coarsest index level, so
groups and topics are added only when the level below no longer fits) and a retry of every
failed job. The nightly pass writes no
ledger row itself.

**A run assembles, asks, then commits once.** Every run of a job — first run, requeue,
backoff retry, nightly retry — assembles its input afresh from the pending view, in one read
transaction on one connection: the pending events, each one's correction mark, a short
window of earlier events from the same sessions, the ledger's objects, and the revision of
every object (the *base*). The job row's `input_seqs`, `base_revisions` and `mark_seen`
record that run only. In the `private` job each provisional event is classified with Casa's
tier classifier, each call bounded to a minute. The model is then called through the model
port (Opus 5.5, effort medium, no tools), outside any transaction; the worker bounds that
call to 600 seconds itself, whatever port it is given, so no model call can hold the one
worker indefinitely. A ledger over 40 000 tokens is given as its index instead — every
object without its body — and a filing run's first answer may name up to 80 objects to open,
which a second call gives in full from the same snapshot. Its answer is a list of id-addressed
operations — `add`, `replace`, `retire`, `split`, `merge`, `add_cue`, `set_line`, `move` —
and an outcome per event. What the conversation means, and how the ledger should change, is
the model's judgement; code checks only that each operation is well formed and stays in the
job's ledger. Cues are add-only.

**The commit is one transaction with three checks.** It re-reads the ledger's revisions and
each input event's mark: if the ledger changed or an input event's mark is not the one the
run read, nothing is written and the job goes back to the queue. Otherwise it applies the
operations (each object's next revision plus a history row), checks that every current
object still hangs from current parents a level up (entry, group, topic) — the view's own
reachability rule, so no entry is ever beyond a view's reach — writes the
classification verdicts, one disposition for every input event, and the job's `committed`
state, all under one `commit_no`. Its input must still be exactly what is owed: the commit
re-reads the pending view, and if another commit filed some of the run's events since the read
(a second writer process, whose `no_change` changes no revision and no mark), the answer is
stale — it may cite an event already filed — so nothing is written and the job is requeued to
reassemble from what survives. Each such requeue follows another commit's terminal filing,
so it cannot repeat without that progress. If every event is gone, the run has nothing left to
file: it writes nothing and ends committed. A run whose fresh input is empty commits with no
disposition.

**A mark re-opens.** `memory.corrections.mark` marks the operator's event in its own
`BEGIN IMMEDIATE` transaction. Because a disposition counts only when committed after the
mark, the event is pending again in every ledger that had filed it, whether that commit landed
just before the mark (the mark waited behind it for the write lock) or long ago. The same
transaction queues a correction job for each such ledger, so a mark and its jobs are stored
together or not at all: a store failure right after the mark cannot leave a correction with
no job, which neither a retried mark (idempotent) nor the cadence (the event was already
given to a run) would queue again. A run still holding the event is rejected
at its commit. While the mark is still pending somewhere, marking again (a retried call)
changes nothing.

## Contracts & invariants

**INV-MEM-027**: Whether an event is still owed to a ledger is decided only by the `pending` view: it is eligible for the ledger (tier at or below it, classified unless the ledger is `private`) and has no `patched` or `no_change` disposition there committed after its latest correction mark, so `deferred` keeps it pending and a mark re-opens an event every ledger had filed; the memory package and its store record no other handled, processed or watermark state, and `commit_no` and `mark_no` are derived from the rows that carry them.

Writer scheduling, input assembly, correction marking and the meter all read the view. A
mark records the `commit_no` current inside its own write transaction, so SQLite's single
writer orders it against every commit: a commit that filed the event from a read without
the mark carries that same number at most, and so no longer counts. A pinning scan reads the
package's code and a fresh store's tables, views and columns for any other such state.

What it does not cover: the meaning of an outcome. A model that answers `no_change` for an
event it should have filed files it; that is the model's judgement, not this rule's. Lower
ledgers are not maintained yet, so an event classified below `private` is pending in them
and nothing files it there.

**INV-MEM-028**: Only the writer's commit marks a job `committed`, in the same transaction that writes its patches, revisions, classification verdicts and a disposition for every event of the run's input, and only after checking that the ledger's revisions and every input event's correction mark are those the run's one read transaction saw and that the pending view still shows every input event pending; a stale base, a new mark or an input event filed meanwhile writes nothing and requeues the job, a run whose every input event was filed meanwhile writes nothing and ends committed, every run assembles its input afresh, and nothing but that commit writes `objects`, `revisions` or `dispositions`.

So a crash at any point leaves each event filed or pending, never both and never neither: a
process killed after the model replied, or inside the commit before COMMIT, leaves the job
`running` under its dead process and no row written; the next process sends every `running`
job of an earlier process back to `queued`, and the rerun files each event once. A job's
state changes after `running` only while it still runs under this process's id. Condense and
regroup take no events, so they write no dispositions, and go through the same commit and
checks.

What it does not cover: scheduling between two Casa processes writing one store at once.
The worker is one task in one process. A second writer's commit cannot file an event twice
or write an answer made from events the other already filed (the pending re-read and the
base check), but the two may spend a model call each on the same events.

## Failure behavior

**A run fails** (the model call raises or times out, the answer is not one JSON object, an
outcome is unknown, an operation is malformed or names an object outside the ledger, or the
commit would leave an object unreachable): nothing is written, the attempt is counted and the
job is retried after a backoff (one minute, then two). After three failures the job is
`failed`, its events stay pending, and the operator gets one private notice: "Memory
couldn't file what was said at 14:05; it's kept and I'll try again tonight." The nightly
pass sends the job back to the queue once a night; failing again sends no second notice.

**The answer leaves an event out.** An event the answer neither names nor cites stays
`deferred`, so pending; the next batch shows it again. An outcome for an event the run was
not given is ignored: a disposition is written only for the run's own input.

**Classification falls back** (the classifier's own fallback, an error, or a call still
running after a minute). The event stays provisional `private`: pending in no lower ledger,
counted by the meter, never re-classified. The run goes on to the model and commits.

**The store fails under the worker.** Recovery is the worker's own, in the running
process, on every pass:

- a store that could not be opened at boot is opened again after 30 seconds, then at
  doubling intervals up to 15 minutes; meanwhile capture keeps spooling
  ([`memory-capture.md`](memory-capture.md));
- once the store is available, a spool that capture left is drained, so what was said during
  the outage is stored even when no later capture comes to start a drain;
- a job whose move out of `running` could not be written (its commit and its failure count
  both hit the failing store) is remembered, and once the store takes writes it is sent back
  to `queued` by boot recovery's rule, applied only to that job and only while it is still
  `running` under this process: a job whose commit did land is left alone. Its next run
  assembles afresh like any requeued run, and the outage counts as no attempt.

Requeueing was chosen over replaying the lost transition: the transition it would replay (a
failure count and possibly a notice) describes the store, not the job, and a requeue needs
no state beyond the job's id.

**The meter.** After every commit and every failure, `memory.meter.persist` stores in
`meta` the per-ledger entry count and tokens, unreachable objects, each served role's view of
the ledger (whole or index, its size, and whether it overflows the role's budget at the
coarsest level), the twenty largest entries (ids and sizes), the week's growth, pending events with the oldest, pending corrections and
failed jobs, plus provisional events, the spool's size, the transcript runs an earlier
process left unconfirmed (unresolved, INV-MEM-031), each open session's
`base_commit_no` and whether a maintenance intent is pending. A meter that cannot be stored
never fails a job.

## Extension points

**A new job kind** is assembled by `memory.writer.assemble_input` and committed by its
commit; nothing else may write a ledger. **A new consumer of what is owed** reads the
`pending` view through `memory.pending`, never a state of its own. **A new model call**
goes through a model port, with the utility one-shot settings (INV-MEM-021).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory/writer.py`
- `casa/rootfs/opt/casa/memory/pending.py`
- `casa/rootfs/opt/casa/memory/corrections.py`
- `casa/rootfs/opt/casa/memory/patches.py`
- `casa/rootfs/opt/casa/memory/classify.py`
- `casa/rootfs/opt/casa/memory/model_port.py`
- `casa/rootfs/opt/casa/memory/nightly.py`
- `casa/rootfs/opt/casa/memory/meter.py`

**Tests**
- `tests/test_memory_writer.py`
- `tests/test_memory_writer_crash.py`
- `tests/test_memory_writer_failures.py`
- `tests/test_memory_pending.py`
- `tests/test_memory_patches.py`
- `tests/test_memory_classify.py`

**Related**
- [`architecture/memory-store.md`](../architecture/memory-store.md)
- [`architecture/memory-capture.md`](../architecture/memory-capture.md)
- [`architecture/memory-labelling.md`](../architecture/memory-labelling.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/memory.md`](../architecture/memory.md)
<!-- END SOURCEMAP -->
