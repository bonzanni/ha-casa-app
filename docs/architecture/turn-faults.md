---
last_reviewed: 2026-11-25
---

# Turn faults: retry, cancellation and classified errors

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a turn's model call is retried, what a cancellation owes it, and when an API fault the
CLI reports as an assistant message, or a silence after consumed retries, becomes a
classified error. What is assembled before the call, the token stream
and resume are [`turn-loop.md`](turn-loop.md); the stop at a turn limit is
[`turn-limits.md`](turn-limits.md); the life of the warm client the turn runs on is
[`sdk-client-pool.md`](sdk-client-pool.md).

## Mental model

The turn loop calls the model with retry. The invariants below say which faults are
eligible for another attempt, what follows when no further attempt runs, and what the
CLI's own prose about a fault never becomes.

## Contracts & invariants

**INV-TURN-005**: Cancellation is never retried and is re-raised after a bounded interrupt-and-drain cleanup; retry covers exactly the three classified transient kinds — timeout, rate limit, and SDK error — and only while the failed attempt has seen no tool call, with exponential backoff, honouring a server-supplied retry hint when present.

"Generic model errors" are *not* a retried category: a failure that classifies to none of the
three kinds is raised immediately.

**INV-TURN-017**: Once an attempt of a turn has seen a tool call, a retryable fault or a CLI exit ends the turn as its classified error with no further attempt, and that attempt's session is registered for the conversation under the session gate; a CLI exit clears the stored session and reruns the turn fresh only when it was raised before any message of the turn arrived.

A rerun starts from the prompt, on a session that does not show the tool ran, so the model
runs it again: a delegation launched twice, a message sent twice (#1654). The failed
attempt's own session holds the call and its result, so it is registered as a successful
turn's is, its memory view bound to it ([`memory-serving.md`](memory-serving.md)), and the
next message resumes it.

What it does not cover: any other fault keeps no session, and a refusal still clears one
(INV-TURN-007); a CLI exit after a message but before any tool call fails the turn with no
rerun and no registration; a kept session the CLI cannot resume is cleared at the next
connect; and nothing reruns an unattended turn.

**INV-TURN-007**: An API-level fault the CLI reports as an assistant message never becomes agent text; it ends the turn as a classified error, and a safety refusal is not retried.

The CLI does not raise for an API-level fault. It synthesizes an ordinary assistant message
whose single content block holds its own user-facing error string — an Anthropic request id
and terminal-UI advice among it — stamps the envelope with an `error` value, and for a
safety decline sets that message's stop reason to `refusal`. Both arrive through the SDK as a
normal assistant message, which is why the accumulator that folds assistant text into the
reply is where CLI prose would otherwise be spoken in persona position.

The gate is the *truthiness* of the envelope's error field, never membership in a known set:
the set of values the CLI can emit is open, and at least one it does emit is absent from the
SDK's own literal type for that field. A refusal is classified to its own kind and is
deliberately outside the retried set — the decline is deterministic, so further attempts buy
nothing but latency — while a fault the CLI names as transient maps back onto the retried
kinds.

A refused turn also gives up the conversation it was in. Dropping the pool entry unbinds the
*client*, not the *conversation*: the session registry would still name the session the turn
resumed, so the next turn on that key would resume the very conversation that was declined,
with the declined message still in it. The refusal therefore clears that registration —
under the same guards as the stale-resume recovery, so a concurrent turn's newer session
survives, and for a refusal only, since a transient fault is no reason to discard a
conversation the next turn could continue (INV-TURN-017).

What it does not cover: a fault scoped to a sub-agent rather than the main loop is suppressed
without ending the turn, because the resident may still answer; the result message's stop
reason is read as a second carrier, so a refusal reported only there is still classified; text
a channel already showed while streaming, before the fault, is not recalled;
and this is a statement about the *turn* boundary. The other read loops that fold assistant
text apply the same suppression and then report *their own* failure rather than a turn error:
a delegated specialist run raises, so all four of its consumers fail the delegation with the
carried kind through the exception paths they already had; engager synthesis raises rather
than answering with an empty string that would read as "the engager remembers nothing"; and
a job's session reader records the fault's kind on the turn and hands it to the job's
turn-end handler at the turn's result, which waits and continues the job once and parks it on
a second consecutive fault — never ends it, because one declined turn is not the job's verdict
([`background-job-session.md`](background-job-session.md)). Its detection runs however much
the turn has already written.

**INV-TURN-008**: A turn carrying server-stamped trusted user ingress that consumes SDK retries and still ends in silence ends as a classified error instead of silence, unless it stopped at its turn limit (INV-TURN-014); and a conversation whose resumed trusted-ingress turns repeatedly evidence SDK faults is not resumed past a bounded streak — it starts fresh with the old session retained.

Two shapes of a doomed ask motivate this. Retries can exhaust and raise, which was always
visible; or the final attempt can return nothing but whitespace or the literal silence
sentinel, which the suppression gate would otherwise treat as chosen silence — consumed
retries and all. The second shape is reclassified before delivery into the mapped message
of the last retried kind, and rides the ordinary classified-error path end to end: plain
rendering, the voice error line, a non-empty response for request turns. The scope
predicate is the server-created trusted-ingress stamp — the fact that the turn has an
author — not humanity: a signed `/invoke` automation is in scope, while heartbeats, event
wakes, scheduled work and setup dispatches stay doctrinally silent through any upstream
congestion window.

The streak half guards against a poisoned resume: each trusted turn that resumed its
conversation commits a health note — while still holding the per-key session write gate,
which is what orders notes against the decisions that read them — striking on a terminal
SDK-error raise or on silence whose consumed retries included an SDK error, resetting on
a real answer, a clean no-retry finish, or a turn that stopped at its turn limit whatever
retries preceded it (INV-TURN-014). A terminal rate-limit or timeout is
congestion-shaped and records no verdict either way. The note follows the session-id
chain (a resumed turn may publish a successor id), so the recorded fault id is always the
id the next ask would resume. At the streak bound the resume decision comes out fresh,
exactly like an expired entry — what the session said was captured as it happened — and
the refusal-only registration clearing of INV-TURN-007 is untouched.

What it does not cover: the notice does not extend to delegation-completion synthesis
turns (no trusted ingress; a delegation fault surfaces through the delegation machinery's
own consumers), and text a channel showed while streaming, before classification, is not
recalled. The
silence sentinel is no longer part of that residue — it is never streamed at all
(INV-TURN-009), so the reclassified reply is one fresh send rather than a posted literal
and a superseding edit.

## Failure behavior

**The model call fails transiently.** Retried with exponential backoff up to a small attempt
limit, unless the failed attempt saw a tool call (INV-TURN-017). A retry hint from the server
overrides the computed delay.

**Retries are consumed and the turn still ends silent.** On a trusted-ingress turn this is
surfaced as the mapped error, never as silence, and — when the turn was resuming — counted
against the conversation's fault streak (INV-TURN-008).

**The session id is stale.** The CLI exits before any message of the turn (INV-TURN-017),
and the stored session is cleared — but only while the registry entry still carries the id
that failed *and* no registration has landed since this attempt's snapshot: each
registration stamps a process-local generation the decision captures, so even a concurrent
turn that successfully re-resumed the *same* id keeps its registration (and its save-time
retention). The retry resumes whatever survives — and the turn re-enters the normal fresh
retry policy — up to the standard attempt limit, not a single extra try.

**The pool cannot serve the turn.** It raises, and the turn creates its own client for this
turn only. The two failures compose: a stale session id hit on that per-turn fallback
re-enters the same stale-id recovery above, rather than surfacing raw.

## Extension points

Retry and the resume-fault streak are tuned by environment, alongside the pool's own
bounds and in the same place a new bound belongs; the knobs and their defaults are
listed in [`sdk-client-pool.md`](sdk-client-pool.md).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent.py::Agent._process`
- `casa/rootfs/opt/casa/agent.py::Agent._make_on_message`
- `casa/rootfs/opt/casa/retry.py::retry_sdk_call`
- `casa/rootfs/opt/casa/retry.py::compute_backoff_ms`

**Tests**
- `tests/test_retry.py`
- `tests/test_agent_api_error_message.py`

**Related**
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
- [`architecture/turn-limits.md`](../architecture/turn-limits.md)
- [`architecture/memory-serving.md`](../architecture/memory-serving.md)
<!-- END SOURCEMAP -->
