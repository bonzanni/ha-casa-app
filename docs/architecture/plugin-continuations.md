---
last_reviewed: 2026-10-18
---

# Plugin continuations and the driver sweep

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a plugin's work that outlives one turn — a browsing run waiting on the operator's tap,
a live-view login, or simply running long — is carried on in fresh turns of its
specialist: the continuation queue, the turn's origin rebuilt from a binding, the relay of
its answer back up the requester chain, and the once-a-minute driver sweep that finds work
nothing is driving. The binding and the `continue` route that feeds the queue are
[`plugin-operator-calls.md`](plugin-operator-calls.md); the desk a continuation waits for
is [`specialist-desk.md`](specialist-desk.md); the announcement its answer becomes is
[`delegation-announcements.md`](delegation-announcements.md).

## Mental model

**A run is continued, never waited on inside a turn.** A specialist turn cannot wait for
a tap on its own card: the tap is a use of the same desk the turn holds. So the plugin ends
the turn when its run must wait or has run long enough, and the run is continued by a
fresh turn. `CONTINUATIONS.request(binding, run_ref, reason)` keeps at most one pending
continuation per plugin, operator and `run_ref`; once its turn has started, a new request
for the run queues the next one, so a tap that arrives while a turn still drives the run
is applied after that turn.

**A continuation turn is rebuilt from the binding alone** (`continuation_origin`), and only
for a binding of the operator as configured now. The worker takes the specialist's desk in
the binding's chat and a concurrency slot (`_desk_turn`: the queue, the lock, a fault
recheck under the lock, the permit after it, and on the way out the desk's own guard for a
run still unwinding past its teardown bound, `Desk.hold_unwinding`), then runs one delegated turn of the
specialist in a fresh session, through the bounded runner, with the fixed task
`Continue browsing run <run_ref>.`. Its origin carries the
binding's chain, chat and operator and Casa's `synthetic: "continuation"` marker, which
gives the turn the operator's grant identity (`provenance.turn_provenance`'s
`continuation` transport) and no operator words: a run it starts goes to the operator's
tap ([`plugin-operator-calls.md`](plugin-operator-calls.md)). A role that declares
`session.desk_context: false` in its `role.yaml` is shown no earlier desk exchanges in any
desk use — a delegation, a swipe-reply or a continuation (`specialist_desk.desk_log`) — so
an earlier run's text never enters another run's context; the desk still keeps its log.

**Its answer climbs the chain one requester at a time** (`_relay_up`). The whole climb is
registered as one asynchronous delegation of the chain's first specialist, so the resident
is told its final answer exactly as it is told any delegation's — durably owed and
replayed after a restart (INV-JOB-010). The specialist's desk is held for its own turn
only; when a specialist asked (finance handing invoices to the browsing specialist), the
answer then becomes the task of a turn of that specialist under that specialist's own
desk, whose answer climbs on. A requester no longer loaded fails the climb with Casa's own
message (`RequesterUnavailable`), never with one role's answer passed off as another's. A relay's task is Casa's text and is
never banked as the requester's words (INV-MEM-024).

**A turn that only made progress tells nobody** (operator ruling 2026-10-18). The
specialist declares it itself: a continuation turn whose whole answer is exactly `<silent/>`
(`output_boundary.SILENCE_SENTINEL`; an empty answer is not a declaration) has nothing for a requester — the browsing
specialist's own progress card already told the operator. No relay turn runs, and the
record is attached with `quiet_if` naming that turn's own output, so it completes with no
announcement owed (through the same reconciled terminal write); a relay requester's own
`<silent/>` never hides the result it was given.
Any other answer is a result or a question and climbs as above; an aborted or failed run
is announced as always.

**The sweep makes a lost hand-over cost a minute** (`sweep_once`). Once a minute, for
every loaded specialist's resolved plugins that declare `casa.driverSweep`
(`plugin_store.manifest_driver_sweep`: a `safe` contract tool that consumes nothing and
calls nothing), Casa starts that plugin's stdio server itself — no model, no session —
with the environment the CLI would give it (`CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA` by
the CLI's rule for a locally loaded plugin, `plugin_data_dir`, and its `.mcp.json`
environment expanded), lists the server's tools and calls the declared one — only if the server lists it — with
no arguments, and queues a continuation
(reason `sweep`) for every returned `{run_ref, binding_ref}` whose binding belongs to that
plugin. The plugin's own predicate decides which runs need a driver; a turn cut at its
ceiling, a crash or a Casa restart leaves the run to the next sweep.

## Contracts & invariants

**INV-PLUG-053**: A continuation turn runs only for a binding of the configured operator and of the plugin that asked for it or that the sweep called, under an origin rebuilt from that binding with no operator words, as one use of its specialist's desk, and its answer reaches the binding's resident through a durably announced delegation after each specialist requester in the chain has had its turn with it under that requester's desk — or the climb fails with Casa's message.

Enforced by `Continuations.request` (one pending per plugin, operator and run), by
`run_continuation` checking the operator and taking the desk and a slot (`_desk_turn`)
before it registers the delegation record and attaches the completion callback, by
`continuation_origin` (the marker, an empty `user_text`, no `_operator_words`), by
`sweep_once` comparing each returned binding's plugin with the plugin it called and its
operator with the configured one, and by `_relay_up` running the requesters nearest first,
each under `_desk_turn`.

What it does not cover: a continuation that cannot get a desk or a slot within ten
minutes, or whose desk faults while it waits, which is dropped for the next sweep to
re-find; a relay a crash interrupts (the announced record is then reported to the
resident as lost; a graceful stop instead fails it with `RelayInterrupted`, Casa's own
message, durably announced); and whether the plugin's predicate is right — the browser
app's run record keeps the run's result either way.

**INV-PLUG-054**: A continuation turn whose answer is exactly `<silent/>` reaches no requester — no relay turn runs and its delegation record completes with no announcement owed — while any other answer, and every aborted or failed run, is announced as before.

Enforced by `_chain` in `run_continuation` returning before `_relay_up` on such an answer,
and by `tools._attach_completion_callback(..., quiet_if=...)` — true only for that turn's
own output — completing the row with `_complete_delegation_durably(id, "",
announce_creator=False)` instead of the announcing settle.

What it does not cover: a quiet terminal write that fails and is not retried before Casa
restarts — the row is then still running at boot and is reported to the resident as lost,
like any delegation a restart interrupts (one extra notice; no answer is lost, and the run's
record in the browser app is untouched).

## Failure behavior

**The sweep tool fails, times out (thirty seconds) or returns garbage.** That plugin's
pass is skipped and logged; the next minute tries again. **Casa restarts.** The queue is
empty; bindings survive, so the first sweep re-finds every run still needing a driver.
**Casa stops.** Pending and running continuations are cancelled before delegation
settlements drain; a running turn is cancelled and given its bounded teardown before its
desk and permit are released.

## Extension points

**A plugin with long work** declares `casa.driverSweep` and `callsCasa` tools, mints its
binding with the `requester` route and stores only the reference with its work.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_continuations.py`
- `casa/rootfs/opt/casa/specialist_desk.py::desk_log`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_driver_sweep`

**Tests**
- `tests/test_plugin_continuations.py`

**Related**
- [`architecture/plugin-operator-calls.md`](../architecture/plugin-operator-calls.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
- [`architecture/delegation-announcements.md`](../architecture/delegation-announcements.md)
- [`architecture/specialist-handoff.md`](../architecture/specialist-handoff.md)
<!-- END SOURCEMAP -->
