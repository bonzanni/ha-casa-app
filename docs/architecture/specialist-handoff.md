---
last_reviewed: 2026-10-18
---

# Specialist hand-off

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist that is answering a request hands part of it to another specialist and
gets the answer back: the `hand_off` tool, the chain of requesters every delegated run
carries, the operator's own words that travel down it, the mechanical bounds, the permit a
hand-off holds, and the requester's ceiling a hand-off pauses. How a
resident delegates — the ACL, the depth cap on `delegate_to_agent`, the spawn cap, the
desk — is [`delegation.md`](delegation.md); what a delegated run banks in memory is
[`memory-lifecycle.md`](memory-lifecycle.md); how a plugin running under a hand-off learns
whom it serves is [`plugin-operator-calls.md`](plugin-operator-calls.md), and how its
later turns carry the answer back up the chain is
[`plugin-continuations.md`](plugin-continuations.md).

## Mental model

**A delegated run knows whom it serves.** Every run `_run_delegated_agent` starts stamps
`_handoff_chain` on its origin: the requesters root first, ending with the run's own role
(`handoff.child_chain`). A resident's direct turn is a chain of one; a desk turn is the
resident and the specialist the operator replied to; a resident's delegation adds its
target; a hand-off adds the next specialist. The run's `<delegation_context>` names the
chain's element before it as the caller (`handoff.requester_of`) — the resident on every
path except a hand-off, where it is the specialist that handed the work on — and, when the
chain is longer than two, lists the earlier requesters as `requested_through`.

**The operator's words travel with the request.** The Telegram channel stamps the text
the configured operator typed (or a file's caption) on the reserved `_operator_words` key,
and the specialist desk stamps it for a reply he typed (not for a file it routes, whose
task is Casa's composition); nothing else does — not a button
or approval continuation, a stored-call tap, a setup turn, a narration, a continuation, a
scheduled, webhook, voice or engagement turn (a tapped suggested reply's dispatch will be
the third stamper). A child origin copies its parent's, so the words of the turn that
started the chain reach every run in it. `handoff.operator_words` reads that key only, on
an `_operator_turn` Telegram origin, and run admission reads nothing else
([`plugin-operator-calls.md`](plugin-operator-calls.md)).

**A hand-off is a tool call that waits.** `hand_off` is offered to every specialist's
delegated or desk build in code (`tools._handoff_grants`), never declared by a bundle —
the bundle allowlist keeps spawn tools out — and that build mounts Casa's framework server
exposing it even when the role declares no servers. The call runs the target ephemerally through
the same bounded runner as any delegation and returns its answer to the caller as the
tool result: the result goes back to whoever asked, and the operator hears of it through
the requester's own answer. The target is any loaded specialist, by role id or by a
persona display name exactly one specialist carries; no declaration is needed. The run
writes no job row (the chain's root delegation owns the durable record), posts no hand-off
line, and carries a fresh send-quota key.

**One chain, one global slot.** The chain's root delegation holds a global concurrency
slot for as long as the chain runs, so a hand-off takes only its per-scope permit
(`SpecialistLimiter.try_acquire_scope_only`): one target per chat at a time, and a
three-hand-off chain is not refused at the default cap of two.

**A requester waiting on a hand-off is not running away.** Each bounded run has a ceiling
clock (`tools._CeilingClock`); `hand_off` pauses its requester's clock for exactly the
wait, so the requester's ceiling counts only its own running time.

## Contracts & invariants

**INV-ENG-025**: `hand_off` runs only inside a specialist's own delegated or desk run, to a loaded specialist that is not already in the request's chain, and never past three hand-offs after the resident the chain started from; every refusal happens before any run, permit or record exists.

Enforced in `tools.hand_off` with the pure bounds in `handoff.handoff_refusal`: the caller
must be a specialist at delegation depth one or more, not in an engagement, a job or a
tapped stored call, and not on voice; the target resolves among loaded specialists only, a
display name shared by two is refused rather than guessed; a target already in the chain
is `handoff_cycle`, a fourth hand-off is `handoff_chain_too_long`.

What it does not cover: `delegate_to_agent`, whose depth cap (INV-ENG-004) still stops
delegated work from chaining that tool; an engagement or job, which has no hand-off; and
whether the target's answer is correct — the requester checks it against what it said a
correct result looks like (`expected_result`), by its own judgement.

**INV-ENG-026**: The time a delegated run spends waiting on its own hand-offs does not count against its wall-clock ceiling.

Enforced by the clock `_run_delegated_agent_bounded` binds around its inner task
(`_ceiling_clock`); `_wait_under_ceiling` re-reads the clock while it is paused and
expires only on running time. Pauses nest, so parallel hand-offs from one run each pause
and resume it once.

What it does not cover: a pinned stored-call turn, which keeps its own ceiling; the
target's ceiling, which is the module default like any delegation's; and a chain whose
every link waits, which is bounded by the deepest run's ceiling, not by a chain-wide
clock.

## Failure behavior

**The target fails, is aborted or times out.** The caller gets a typed error with the
kind (`timeout` for its ceiling), never a partial answer; its own run continues.
**The caller's run is cancelled.** The hand-off's run is cancelled with it and its permit
is released when that run actually ends. **The target already runs for this chat.** The
call is `busy`. **Casa restarts.** The whole chain is lost with the process; the root
delegation's row is reported to its resident as any lost delegation is
([`delegation-announcements.md`](delegation-announcements.md)).

## Extension points

**A new session kind that should hand off** needs `_handoff_grants` to offer it and
`hand_off`'s availability check to admit it; an engagement or job would also need a home
for the hand-off's answer, which today is the waiting tool call.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/handoff.py`
- `casa/rootfs/opt/casa/tools.py::hand_off`
- `casa/rootfs/opt/casa/tools.py::_handoff_grants`
- `casa/rootfs/opt/casa/tools.py::_CeilingClock`
- `casa/rootfs/opt/casa/tools.py::_wait_under_ceiling`
- `casa/rootfs/opt/casa/specialist_limits.py::SpecialistLimiter.try_acquire_scope_only`

**Tests**
- `tests/test_handoff.py`

**Related**
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/plugin-operator-calls.md`](../architecture/plugin-operator-calls.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
<!-- END SOURCEMAP -->
