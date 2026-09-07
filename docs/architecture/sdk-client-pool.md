---
last_reviewed: 2026-09-07
---

# The warm client pool

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The life of the warm, conversation-bound model client a turn runs on: how the pool keys and
reuses one, what invalidates it, what a cancelled turn owes it, and what a close, a key
reset or a container stop guarantees about the clients the pool has opened. It does not
cover what a turn assembles or how the model call is retried
([`architecture/turn-loop.md`](turn-loop.md)), nor how turns are ordered against
configuration writes ([`architecture/concurrency-model.md`](concurrency-model.md)).

## Mental model

The pool exists so that a conversation's client outlives the turn that opened it:
connecting is expensive, and a warm client already holds the conversation.

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

**INV-TURN-010**: The pool's drain timeout bounds every generation, invalidated ones included: an entry whose lock an invalidation closer still cannot acquire when the drain window ends is force-closed — its transport disconnected — and the closer is neither cancelled nor waited on further. The bound is on the lock wait, not on transport I/O.

A generation already handed to an in-flight invalidation has left the pool's map, and the
closer that owns it takes the entry lock with no timeout — that is what lets a replacement
turn start the moment the old one releases. So the pool retains what each closer owns, and
shutdown, after its serial pass over the live entries, waits one further drain window for
every closer together, then disconnects whatever a closer still could not lock.
The stop runs its agents' closes together rather than one after another, so that
bound is one window for the fleet rather than one per resident and specialist.
Stated as a number, because a stop that overruns its container's patience is
killed rather than graceful: an agent close is given its graceful window, and a
close cancelled at the end of it is given one further forced-cut window on top,
so the stop's close step lasts at most the sum of the two — once, whatever the
number of residents and specialists — against a container stop timeout several
times that. The closer
is not cancelled: it finishes its handoff bookkeeping when the wedged turn releases, and a
key reset that arrives meanwhile still joins it. Both force-close sites — the live-entry
drain and the invalidated arm — go through one helper, which first fires the pool's
injectable force-close hook (a no-op by default, and a raising hook is contained) so a
later consumer can record why a turn is about to be cut. A second close overlapping the
first, or one that resumes after an outer bound cancelled an earlier one, re-drains that
call's live entries with its own window rather than cutting them early. What the transport
cut itself waits for is unchanged: a client's disconnect is awaited, and a closer already
inside its disconnect is joined rather than skipped. What that force-close can reach when
the turn holding the lock is hosting an engagement launch is in
[`architecture/engagements.md`](engagements.md).

A client the pool has opened is recorded before it exists. The replacement a turn
builds goes into the entry map — replacing the entry that turn was serialized
against — BEFORE its connect starts, and completing the connect never re-creates
that membership. Otherwise a close, an invalidation or a key reset that
enumerated the map during the connect closes the placeholder it found there,
returns, and leaves the real client and its CLI subprocess connected with nothing
able to reclaim them: outside the map, outside what the closers own, and outside
the drain bound above, which quantifies over generations the pool recorded. Two
of the three close paths never set the closing flag, so the record is keyed on
map identity rather than on that flag, and the window is not a cold connect's
alone — it spans the registry read, the flush of a stale warm client, the retain
spawn and the options build, so a warm entry being replaced enters it too. A
close that takes the record while the connect is in flight owns that client: it
is blocked on the entry lock the turn holds, so the turn hands the client over
closed rather than running a query on a generation something has already retired,
and a shutdown force-closing it cancels the connect and waits for it to stop — a
connect that swallows the cancellation and finishes anyway would otherwise
establish a transport after the close had returned. The record replaces rather
than adds, so a drain pass still meets one lock per key. A turn whose record is
taken retries, and during shutdown that retry meets the same closing refusal
every other post-shutdown turn meets.

What a refused turn then does is a decision, not a default. Ordinarily the
refusal falls to the per-turn bypass, which builds a one-shot client of its
own — the right answer while a configuration reload swaps a generation, because
the runtime is not going anywhere and the refusal is momentary. Once the
container has declared its graceful stop it is the wrong answer: it starts a
fresh CLI subprocess after the stop began, on a turn nothing in the shutdown
sequence bounds, whose answer is delivered onto channels the stop closes a few
steps later. So the stop is declared to the turn path as a fact about the
running runtime — not per agent, since a retired generation may still be serving
a dispatched turn and a specialist may be installed after the stop — and from
that point a refusal that means *this pool is closing* ends the turn instead of
serving it. It ends as silence: these turns are notifications, and a caller
waiting on a reply is resolved by the bus rather than left to time out.

The refusal is narrow in three ways, each of which is the difference between it
and a turn nobody meant to drop. It is keyed on the stop, so every configuration
reload keeps serving. It is keyed on the closing refusal specifically, so the
transient *entry unstable after retry* refusal — an eviction race with nothing
to do with teardown — keeps serving. And it lives where the pooled path's
refusal is handled, which scheduled work and webhook one-shots never reach:
those take the bypass because of what they are, so no heartbeat, reminder or
trigger can be silenced by it. A turn already admitted when the stop is declared
is outside this: it runs to its end as before.

**INV-TURN-011**: A pool turn records its replacement client in the pool's entry map before it connects, and completing that connect never re-creates the membership; the pool additionally retains every client it has opened until that client's transport cut has settled. So every client the pool has opened is inside the enumeration of any close, invalidation or key reset that follows; no such path returns while a client it removed the key for is still connected; a cancelled pool close attempts a bounded concurrent cut of what it removed before propagating, rather than abandoning it; and whatever that window could not finish — like whatever a cancelled reset, eviction or invalidation worker leaves — stays retained by the pool and discoverable by every later close, never forgotten.

The cancelled close is the second half of that, and it used to be the hole. A
close of the pool clears the entry map and writes its drain records before its
first lock wait, so a caller that cancels it — the container bounding each
agent's close well below the pool's own drain default, or the runtime's final
task sweep reaching a reload's background close — left what it had already
removed for a later call that, on those paths, does not come. A cancelled close
now stops waiting for locks and cuts, concurrently and inside one bounded
window, every client it still owes, and only then propagates the cancellation.
That window is a real bound and it is where the promise stops: a stop that
cannot be finished must still finish, so a cut the window cannot complete is
abandoned with a warning rather than held open, and its client stays in what the
pool retains for the next close to take. The alternative — waiting until every
transport is provably cut — is a stop with no bound at all, which is the one
thing teardown may not be.
The same forced cut is what an agent close reaches for when its own cancellation
arrives before it ever entered the pool. What is promised is completion of the
SDK's close protocol — the disconnect that flushes the transcript is started and
awaited — not that the protocol never escalates to terminating the CLI, which is
the SDK's own behaviour and not Casa's to promise.

Retaining what the pool opened is what makes that cover routes no drain record
reaches: a key reset or an eviction cancelled between removing its entry and
closing it, an invalidation worker the final sweep cancels, the flush of a warm
client a turn is replacing. Recording each of those sites separately was tried
and dropped — four such routes were found one at a time, which is the shape of a
mechanism that enumerates rather than encloses. And one transport cut runs per
client, in its own task, so a cancelled closer cannot truncate it: completion is
recorded when the disconnect actually returned, and a cut that was cancelled is
started again by the next closer rather than reported as done.

## Failure behavior

**The turn is cancelled.** The cancellation is re-raised after a bounded, shielded
interrupt-and-drain cleanup of the pool entry — drained back to warm, or invalidated.

## Extension points

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
the same way. That drain is bounded per entry, though not pool-wide: each entry's lock is
awaited up to a drain timeout — a default the caller may override, spent serially per
entry — and an entry still locked when it expires is force-closed rather than waited on
further. WHEN that window opens depends on where the agent was replaced. A swap performed
inside a reload dispatch does not start the drain: the dispatcher records the replaced agent
and starts every drain it recorded as it returns, after its locks are released and after its
post-lock secret report — so the window is counted from the reload's return, not from the
swap, and the reload's own remaining work is never spent out of it. That start happens on
every exit of the dispatcher, a failing or cancelled reload included, so a replaced pool is
never left unclosed; it happens before the caller's post-reload plugin-health regeneration,
which then runs concurrently with the drain. A swap outside a dispatch — shutdown, a direct
handler call — starts its drain at the swap, as before.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent.py::Agent.aclose`
- `casa/rootfs/opt/casa/sdk_client_pool.py::SdkClientPool.turn`
- `casa/rootfs/opt/casa/sdk_client_pool.py::SdkClientPool.close_key`
- `casa/rootfs/opt/casa/sdk_client_pool.py::SdkClientPool.invalidate_all`
- `casa/rootfs/opt/casa/sdk_client_pool.py::SdkClientPool.aclose`
- `casa/rootfs/opt/casa/sdk_client_pool.py::ManagedSdkClient`
- `casa/rootfs/opt/casa/sdk_client_pool.py::PoolUnavailable`
- `casa/rootfs/opt/casa/sdk_client_pool.py::PoolClosing`

**Tests**
- `tests/test_sdk_client_pool_pool.py`
- `tests/test_sdk_client_pool_client.py`
- `tests/test_pool_close_final_sweep.py`
- `tests/test_shutdown_pool_and_refusal.py`

**Related**
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/concurrency-model.md`](../architecture/concurrency-model.md)
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
