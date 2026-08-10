---
last_reviewed: 2026-07-31
---

# The concurrency model

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What actually runs at the same time: bus dequeue versus handler execution, where turn
serialization really comes from, which locks exist and what each one guards, and what runs
threaded rather than on the event loop. It does not restate the subsystems' own contracts —
where a lock's guarantee is already an invariant elsewhere, this file points at it.

## Mental model

**The bus orders dequeue; it never serializes execution.** Each registered role has one
consumer task draining a priority queue, and priority decides *dequeue* order — but the
consumer immediately spawns a task per message and never awaits it. Two messages to the
same agent run concurrently the moment they are dequeued. Any sentence of the form "the
agent's queue serializes its turns" is the single most likely wrong belief in this area.

**Serialization comes later, from the pool, at session-key granularity.** A pooled turn
takes its entry's lock keyed by channel, role and conversation scope together — the
resume decision, the SDK turn and the session publication all happen under it
(INV-TURN-001). Two turns on the *same* key serialize there; the same agent on two keys
runs concurrently. Turn types outside the pool have no such gate.

**The lock inventory is small, and each lock guards one thing.** The pool has two layers —
a pool-wide bookkeeping lock (entries, invalidation barriers, closing state) that is
deliberately released before any entry lock is awaited, and a per-entry lock for the
client itself. The engagement registry mutates and persists under one registry lock
(reads take nothing). Reload holds a per-scope lock plus a global read-write lock in which
a full reload is the writer excluding every other scope (INV-CFG-002). Plugin mutations
share one tool-level lock (INV-TOOL-003); the full-reload entry points take it through a
task-reentrant guard, because a full reload that includes the environment refresh reaches
the plugin-env handler's health block, which serializes on the same lock — one logical
operation, one acquisition, while distinct tasks still exclude each other (a task spawned
inside the guarded region does not inherit the hold). The specialist lifecycle serializes
under the materialize lock, and a bundle rollback's tuple-and-symlink restoration takes
that same lock so a concurrent reconcile cannot re-materialize what the rollback just
removed. Agents keep a small first-publication lock for their plugin-resolution
snapshot — it does not serialize turns. The Telegram channel re-imposes ordering per scope
above the bus: a per-topic handler lock for engagement topics and a per-chat lock
serializing `/new` with same-chat enqueue (both documented in the Telegram map). Two
cross-*thread* locks exist, both `threading.Lock`: plugin health's report lock, because
its writers run both on the loop and in the thread offload; and the s6 compile-worker
lock, which serializes the compile/swap/reap worker threads themselves — a cancelled
compile abandons its worker mid-run, and the successor's worker must queue behind the
abandoned one rather than race it.

**Blocking work goes to threads; coordination stays on the loop.** Filesystem and config
I/O is pushed through the thread offload helper (well over a hundred call sites);
dispatch, orchestration, lock management and registry mutation are loop work. A registry
holding its lock across offloaded disk I/O is a deliberate choice: the lock's critical
section includes the write.

## Contracts & invariants

**INV-CONC-001**: The bus dequeues per role in priority order and spawns one task per message; it never serializes handler execution.

Enforced by the consumer loop's spawn-without-await. Priority orders the queue tuples;
completion order is unspecified.

What it does not cover: per-conversation ordering. That is the pool's job, and only for
pooled turns on one session key.

**INV-CONC-002**: A role has at most one live consumer task, and re-registration replaces the handler while preserving the queue.

Enforced by the loop tracker returning the existing task and by registration rebinding in
place — a live consumer is never orphaned by a reload.

What it does not cover: a handler captured by an already-running dispatch (replacement is
prospective; in-flight dispatches finish on the handler they started with), and the
unregister path — unregister cancels the consumer *and* the role's in-flight dispatch
tasks without awaiting either, handing the consumer task back; the awaiting variant
(`unregister_and_wait`, the reload-teardown path) gathers them all, so only an
unregister-then-re-register that does not await can briefly overlap the old loop with the
new one.

**INV-CONC-003**: Turns sharing a session key serialize under the pool entry's lock, decision through publication; distinct keys are concurrent.

Enforced in the pool's turn path. This is INV-TURN-001's concurrency face: what the lock
reads and decides is documented there.

What it does not cover: unpooled turn types, and the bus layer above (INV-CONC-001).

## Failure behavior

**A message targets an unknown role.** The checked send reports no-target; the plain send
drops silently. Nothing queues.

**A request gets no answer.** The caller times out and its pending future is removed —
but a slow handler is *not* cancelled by the timeout and runs to completion; the dispatch
task is cancelled by exactly two things: the caller's own cancellation (which also marks
a not-yet-dequeued message so the consumer drops it instead of running the handler for a
caller that is gone), and eviction of the target role, which resolves a still-waiting
caller with a handler-error response rather than leaving it to time out. A handler
returning nothing produces an empty response rather than a hang, and a handler that
raises produces an error response.

**The process is shutting down.** Once the shutdown gate is set, new requests fail
immediately with a typed shutdown error rather than enqueueing for consumers that are
about to be cancelled, and after the consumers are gone every still-pending request
future is resolved with the same error so no ingress handler waits out the bus timeout.
Notifications and plain sends are deliberately not gated — outbound operator messages
keep flowing during the drain.

**The pool cannot serve.** The turn raises pool-unavailable and the agent falls back to a
one-shot client (the turn loop's contract); a failure mid-publication drops that pool
generation rather than leaving it warm.

**A reload handler fails.** The dispatch returns an error envelope; the scope and RW locks
release normally.

## Extension points

**Anything that must serialize per conversation** belongs under the pool entry lock via
the session key — not in the bus, which will not serialize it, and not in a new global
lock, which would serialize strangers.

**A new lock** should guard one named thing and be documented in the subsystem that owns
it; this file's inventory is a map, and a lock that appears nowhere else has no contract.

**New blocking I/O** goes through the thread offload helper. Holding an existing lock
across it is sometimes right (the persistence sections above) — but that choice widens the
critical section and belongs to the owning subsystem's contract.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/bus.py::MessageBus.run_agent_loop`
- `casa/rootfs/opt/casa/bus.py::MessageBus.register`
- `casa/rootfs/opt/casa/bus.py::MessageBus.start_agent_loop`
- `casa/rootfs/opt/casa/sdk_client_pool.py::SdkClientPool.turn`
- `casa/rootfs/opt/casa/reload.py::_RWLock`

**Tests**
- `tests/test_bus.py`
- `tests/test_sdk_client_pool_pool.py`

**Related**
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
<!-- END SOURCEMAP -->
