---
last_reviewed: 2026-07-29
---

# The turn loop

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How one inbound message becomes one agent turn: what is assembled before the model is
called, how the call is retried, and what bounds the turn. It covers the orchestration and
the warm client pool the turn runs on. It does not cover where the message came from
(the channels), what the model is allowed to do inside the turn (authorization), or how
memory is stored and recalled — only the point at which memory is read into a turn.

## Mental model

A turn is a single pass with a fixed shape: take a message, decide whether it continues an
existing conversation or starts a fresh one, assemble a system prompt and a memory block,
call the model with retry, stream the output, and record what happened.

The one structural subtlety is that the model client is **usually not created for this
turn**. Residents reuse a warm, conversation-bound client held in a pool, keyed by a scoped
session key — channel, resident role, and conversation scope together, so one channel can
carry several conversations and roles without sharing a client. A turn normally begins with
a client that has already been connected and has already seen this conversation. Everything awkward in this area follows from that reuse: the decision
about resume-versus-fresh has to be made under the pool's own lock, a cancelled turn has to
be drained before its client can be handed to anyone else, and an error result must
invalidate the entry rather than leave it warm.

When the pool cannot serve a turn it raises, and the turn creates a client for itself
instead, using it for that turn only. Both paths exist and both are exercised.

## Contracts & invariants

**INV-TURN-001**: The resume-versus-fresh decision is re-derived from a fresh session read while holding the pool entry's lock, and a cached client is reused only when its session id matches that decision exactly.

Without the lock, a freshness expiry and an interleaved turn on the same channel can fork a
conversation or hand a turn a stale client. A mismatched entry is closed and rebuilt rather
than reused. Know the lock's reach, though: it serializes the *decision* against the
registry's current contents, not a multi-step mutation of them — a `/new` reset retains the
old session and only afterwards removes the pointer, none of it under a pool entry lock.
What covers that window is the retirement claim
([`architecture/memory-lifecycle.md`](memory-lifecycle.md), INV-MEM-013): while a reset or
wipe holds one, this decision — and the bypass path's — is forced to fresh, so a turn
dispatched before the reset began starts a new conversation instead of resuming the dying
one, and the registry refuses to re-register the dying id. The decision reports such a
steer as its own reason, so observability tells the truth about why a session was fresh.

"That decision" is more than a timestamp: resume requires a decodable stored entry, an exact
role-identity match, an exact personality-binding digest, a *usable* last-active time, and
channel-specific freshness — failing any one of them means fresh, not resume. Usable is
stricter than parseable: a stored time without a timezone parses cleanly but cannot be
compared against the aware clock, so it is rejected as an invalid entry like any unparseable
value. Nothing rewrites the offending entry, so a gate that treated such a value as an error
rather than a rejection would fail the same way on every subsequent turn for that channel.

**INV-TURN-002**: An error result invalidates the pool entry before the client can return to warm; an entry is never left warm after an error.

The sequencing is looser than "before anything else": the error result is still forwarded to
the turn's message handler like any other message, and invalidation happens after the receive
loop — what is guaranteed is that the entry is invalid by the time the call returns.
Retryable errors are re-raised so the retry wrapper sees them; non-retryable ones return
with the entry marked invalid for the pool to drop.

**INV-TURN-003**: A cancelled turn interrupts, then drains its buffered messages until the terminal result or stream end before the entry may return to warm; any failure in that window — including a second cancellation — invalidates the entry instead.

This is what makes voice barge-in safe: the next turn on that channel must not inherit a
half-consumed stream. Note the "or stream end": a drain whose stream ends *without* a
terminal result still returns the entry to warm — the guarantee is a fully-consumed stream,
not a witnessed terminal result.

**INV-TURN-004**: A memory read that raises does not fail the turn. It is logged and the turn proceeds with an empty memory block.

What it does not cover: nothing downstream is told. The unavailable-versus-empty distinction
is flattened to omission at this point — observable in logs and the recall breaker, not in
the prompt — and a successfully-read profile overlay may still be present beside the missing
recall block.

**INV-TURN-005**: Cancellation is never retried and is re-raised after a bounded interrupt-and-drain cleanup; retry covers exactly the three classified transient kinds — timeout, rate limit, and SDK error — with exponential backoff, honouring a server-supplied retry hint when present.

"Generic model errors" are *not* a retried category: a failure that classifies to none of the
three kinds is raised immediately.

**INV-TURN-006**: A key reset joins any in-flight pool invalidation for that key — the reset-hook close returns only after the displaced generation has fully closed, not merely after its lock handed off.

The distinction from an ordinary replacement turn is deliberate and asymmetric: a
replacement turn waits only for the invalidation's lock-handoff barrier (a slow disconnect
stays off its path), while a reset waits for the whole close, because the disconnect is
what flushes the CLI transcript the reset's save is about to read — and because a still-
finishing old turn could otherwise re-register the session the reset just removed.

What it does not cover: it joins *invalidation* generations only, and only for clients this
pool owns — a turn on a path the pool never saw has no entry lock to join. Ordering against
turns in general is not this hook's job: a reset takes the per-key write gate and turn
admission for its whole body (INV-CONC-004, INV-CONC-005), which is what orders it against
turns dispatched before or after it on the same key, on any path.

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
conversation the next turn could continue.

What it does not cover: a fault scoped to a sub-agent rather than the main loop is suppressed
without ending the turn, because the resident may still answer; the result message's stop
reason is read as a second carrier, so a refusal reported only there is still classified; text
already streamed to a channel before the fault stays delivered, since it cannot be recalled;
and this is a statement about the *turn* boundary. The other read loops that fold assistant
text apply the same suppression and then report *their own* failure rather than a turn error:
a delegated specialist run raises, so all four of its consumers fail the delegation with the
carried kind through the exception paths they already had; engager synthesis raises rather
than answering with an empty string that would read as "the engager remembers nothing"; and
an engagement turn raises too, so a fault on the launch turn reaches the engagement's terminal
record with its own kind instead of flattening into one generic driver-start failure, where a
refusal and a crash read identically. A fault on a *later* turn of a live engagement raises
the same way but deliberately does not end it: the topic is told the turn failed and the
engagement stays open, because one declined turn is not the engagement's verdict. That whole
half needed the engagement lifecycle to be ready for it rather than the suppression alone, so
it followed a release later. Its detection deliberately sits outside the
branch that streams text: an interactive specialist's output is capped, and once the cap
freezes the accumulator that branch stops running, so a fault arriving after the freeze would
otherwise end the turn as a truncated success.

**INV-TURN-008**: A turn carrying server-stamped trusted user ingress that consumes SDK retries and still ends in silence ends as a classified error instead of silence; and a conversation whose resumed trusted-ingress turns repeatedly evidence SDK faults is not resumed past a bounded streak — it starts fresh with the old session retained.

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
a real answer or a clean no-retry finish. A terminal rate-limit or timeout is
congestion-shaped and records no verdict either way. The note follows the session-id
chain (a resumed turn may publish a successor id), so the recorded fault id is always the
id the next ask would resume. At the streak bound the resume decision comes out fresh
*with retain*, exactly like an expired entry — continuity is saved, never cleared, and
the refusal-only registration clearing of INV-TURN-007 is untouched.

What it does not cover: the notice does not extend to delegation-completion synthesis
turns (no trusted ingress; a delegation fault surfaces through the delegation machinery's
own consumers), and text a channel streamed before classification stays streamed — a
final edit supersedes a streamed literal sentinel on the classified path, but a failed
edit leaves what the stream already showed.

## Failure behavior

**The model call fails transiently.** Retried with exponential backoff up to a small attempt
limit. A retry hint from the server overrides the computed delay.

**Retries are consumed and the turn still ends silent.** On a trusted-ingress turn this is
surfaced as the mapped error, never as silence, and — when the turn was resuming — counted
against the conversation's fault streak; at the bound the next ask starts fresh with the
old session retained (INV-TURN-008).

**The session id is stale.** The stored session is cleared — but only while the registry
entry still carries the id that failed *and* no registration has landed since this
attempt's snapshot: each registration stamps a process-local generation the decision
captures, so even a concurrent turn that successfully re-resumed the *same* id keeps its
registration (and its save-time retention). The retry resumes whatever survives — and the
turn re-enters the normal fresh retry policy — up to the standard attempt limit, not a
single extra try.

**The pool cannot serve the turn.** It raises, and the turn creates its own client for this
turn only. The two failures compose: a stale session id hit on that per-turn fallback
re-enters the same stale-id recovery above, rather than surfacing raw.

**The CLI does not match its pin.** Boot verifies the effective Claude CLI against a pinned
path and exact pinned version, and a mismatch is *fatal at startup* — replacing or
upgrading the CLI without moving the pin prevents Casa from starting rather than merely
degrading turns.

**Memory is unavailable.** The turn proceeds without it, with a warning. An unavailable
memory is not an empty memory — but at this point the distinction lives only in the logs and
the recall breaker. The model and the turn's caller see the same thing either way: no recall
block (see INV-TURN-004).

**The turn is cancelled.** The cancellation is re-raised after a bounded, shielded
interrupt-and-drain cleanup of the pool entry — drained back to warm, or invalidated.

What the loop does *not* do: it does not save long-term memory per turn. Saving happens at
session granularity, in the background, elsewhere.

## Extension points

Know where "every turn" actually runs: options assembly happens when a client generation is
*built* — a warm reuse skips it entirely, prompt, memory block and all. Anything that must
be true for literally every turn belongs on the message-processing path around the query,
not in the options assembly; anything that only needs to hold per client generation belongs
in the options assembly, which is the one place that sees the fully-resolved context.

The pool is bounded three ways — a per-agent cap, a fleet-wide cap shared across agents, and
an idle/age sweeper — and all three are environment-tunable: `SDK_POOL_MAX_PER_AGENT`
(default 4), `SDK_POOL_FLEET_CAP` (8), `SDK_POOL_IDLE_SECONDS` (1800) and
`SDK_POOL_MAX_AGE_SECONDS` (43200). Retry is tunable the same way —
`SDK_RETRY_MAX_ATTEMPTS` (3), `SDK_RETRY_INITIAL_MS` (500), `SDK_RETRY_CAP_MS` (8000) —
and a server-supplied retry hint is honoured only up to ten times the backoff cap, never
unboundedly — as is the resume-fault streak bound, `SDK_RESUME_FAULT_LIMIT` (2). A new
bound belongs alongside these rather than in the turn body, so that eviction stays in one
place.

Turn types that must never reuse a client are excluded by the eligibility gate. Scheduled
work and one-shot webhook scopes are excluded there today; that gate is the place to add
another exclusion, not the pool internals.

Teardown is asynchronous on purpose: draining the pool synchronously from inside a turn that
holds one of its own entry locks deadlocks. A new teardown path must background the drain
the same way.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent.py::Agent._process`
- `casa/rootfs/opt/casa/agent.py::Agent._build_options`
- `casa/rootfs/opt/casa/agent.py::Agent.aclose`
- `casa/rootfs/opt/casa/sdk_client_pool.py::SdkClientPool.turn`
- `casa/rootfs/opt/casa/sdk_client_pool.py::SdkClientPool.close_key`
- `casa/rootfs/opt/casa/sdk_client_pool.py::ManagedSdkClient`
- `casa/rootfs/opt/casa/sdk_client_pool.py::PoolUnavailable`
- `casa/rootfs/opt/casa/retry.py::retry_sdk_call`
- `casa/rootfs/opt/casa/retry.py::compute_backoff_ms`

**Tests**
- `tests/test_agent_process.py::test_session_id_is_channel_plus_role`
- `tests/test_agent_process.py::test_telegram_channel_autorecalls_on_fresh_session`
- `tests/test_sdk_client_pool_pool.py`
- `tests/test_retry.py`
- `tests/test_agent_api_error_message.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
<!-- END SOURCEMAP -->
