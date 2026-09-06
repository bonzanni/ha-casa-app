---
last_reviewed: 2026-09-07
---

# Memory wipe: consent, draining, and what it leaves behind

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The operator-consented erasure of long-term memory: the two doors that can ask
for it and the consent each demands, the order the orchestrator works in, the
fence every bank writer passes through so that nothing pre-wipe survives it, and
what a wipe deliberately does not cover. How a conversation becomes long-term
memory in the first place — the freshness windows, the claim-and-guard save
protocol, the explicit reset and the durable retry spool a wipe drops — is
[`architecture/memory-lifecycle.md`](memory-lifecycle.md); what a caller may
claim from a recalled fact is [`architecture/memory.md`](memory.md), and who may
read one back is [`architecture/memory-scoping.md`](memory-scoping.md).

## Mental model

**The wipe is one operation, two doors, one consent posture.** The
orchestrator first closes *turn admission* and drains every in-flight turn
(INV-CONC-005) — until that holds, a turn the client pool never owned can
re-arm a session pointer after the wipe has reported completion, and a
first-ever turn on a key is not even visible to enumerate. It then claims
every key, drains in-flight bank writers behind an exclusive fence, drops the
retry spool, drops every session pointer *without retaining it* (retiring
saves first — exactly the residue a wipe must not leave), deletes the bank,
and reports counts. Both drains are bounded and fail closed: turns or writers
that do not finish in time abort the wipe with nothing deleted, because a wipe
that cannot prove it drained everything must not report that it deleted
everything. The terminal door
(`casactl memory-wipe --yes` → `POST /admin/memory/wipe`) is root-gated by
the same peer-credential check as every admin route; the agent door (the
`wipe_memory` tool) posts an Approve/Cancel keyboard to the configured
operator and executes only on the operator's own tap, from the broker's
finish hook — never inside the invoking turn, whose pool lock the wipe's
flush-close must be able to take. With no operator configured, nobody can
consent and the tool door refuses everyone. At most one wipe runs at a time,
and shutdown freezes new admissions before draining a running one.

**Which door a shipped install actually has.** The terminal door, and only
that one. A Casa tool reaches an agent's MCP server only when that agent's
resolved grants name it, and no shipped runtime, role or executor artifact
names the wipe tool — so the agent door exists, is correct, and is held by
nobody. An operator's own agent configuration can grant it, and the tool's
gates then apply unchanged. Because the door is unreachable by default, every
shipped resident's doctrine carries the matching refusal: claim the capability
only when the tool is actually present, and otherwise say plainly that this
agent cannot do it and name the terminal command, rather than delegating the
request or promising a confirmation that will never arrive.

**Writers that straddle a wipe discard.** Every bank writer captures a
*fence generation* in the same no-await block as the decision that commits
it to its source data — beside the pool's resume decision for a superseded
session's retain, at summary assembly for an engagement's, at entry for the
save and the spool replay — and enters a shared fence section that compares
it. A wipe bumps the generation under the exclusive side after draining the
in-flight sections, so a pre-wipe writer that resumes later retains nothing
and, crucially, spools nothing: the operator consented to deleting exactly
that content.

## Contracts & invariants

**INV-MEM-014**: The wipe executes only on the configured operator's explicit consent at its consent-bearing door, and no durable pre-wipe writer survives it: the spool is dropped, every claimed pointer is dropped without retention, and a bank writer that straddles the wipe discards — retaining nothing and spooling nothing.

Enforced by the tool door binding its keyboard to the configured operator's
identity (the broker refuses any other actor's tap, and a broker cancel can
only finish as *cancelled*, never as an answer); by the admin door sitting
behind the root peer-credential gate plus an explicit confirm field; by the
orchestrator's order — claims first, then the exclusive fence drain and
generation bump, then the spool, then sid-guarded pointer removals, then the
bank — with claims released in a `finally`; and by every writer's
generation check sitting in front of both its retain *and* its
failure-spool arm.

What it does not cover, deliberately disclosed: a turn or engagement already
in flight when the wipe runs may contribute one post-wipe item (its retain
enters the fence with a post-wipe generation); a session pointer registered
by a steered-fresh turn mid-wipe survives (its conversation is
post-wipe-initiation — the sid-guarded remove protects it on purpose); the
backend applies retains it accepted before the delete on its own schedule;
and the bank deletion itself is the backend's — Casa does not verify
emptiness afterwards.

## Failure behavior

**The wipe's writer drain times out.** The wipe aborts with nothing deleted
and says so — a stuck retain must not be raced, and an abort is retryable.
A wipe interrupted by shutdown is drained to completion (or a truthful
failure report) before the channels and the memory backend close; a consent
approval landing during shutdown is refused, never half-run.

**The bank delete itself fails.** The failure propagates to the door that
asked (an error edit on the consent keyboard, a non-2xx with the reason on
the admin route) — by then the spool and the pointers are already gone,
and the report says exactly that rather than claiming a deletion that did
not happen.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory_wipe.py::RetainFence`
- `casa/rootfs/opt/casa/memory_wipe.py::wipe_long_term_memory`
- `casa/rootfs/opt/casa/memory_wipe.py::start_wipe_task`
- `casa/rootfs/opt/casa/memory_wipe.py::freeze_wipes`
- `casa/rootfs/opt/casa/memory_wipe.py::drain_wipe_task`
- `casa/rootfs/opt/casa/tools.py::wipe_memory`
- `casa/rootfs/opt/casa/internal_handlers.py::build_admin_memory_wipe_handler`
- `casa/rootfs/opt/casa/semantic_memory.py::SemanticMemory.delete_bank`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.delete_bank`

**Tests**
- `tests/test_memory_wipe.py`
- `tests/test_retain_fence_writers.py`
- `tests/test_wipe_memory_tool.py`
- `tests/test_admin_memory_wipe_route.py`

**Related**
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
