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
The Telegram channel serializes `/new` against same-chat message *enqueue*, and the reset's
save and removal are generation-checked (INV-MEM-006) — but a turn already dispatched
before the reset began can still read the old session id fresh and resume it once more.

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

What it does not cover: it joins *invalidation* generations only. A reset has no ordering
relationship with a turn dispatched after it on the same key beyond what the entry lock and
the channel's own serialization provide.

## Failure behavior

**The model call fails transiently.** Retried with exponential backoff up to a small attempt
limit. A retry hint from the server overrides the computed delay.

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
unboundedly. A new bound belongs alongside these rather than in the turn body, so that
eviction stays in one place.

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

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
<!-- END SOURCEMAP -->
