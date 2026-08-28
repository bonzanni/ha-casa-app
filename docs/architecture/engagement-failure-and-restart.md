---
last_reviewed: 2026-08-26
---

# Engagement launch failure and restart

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens when a launch does not become a live engagement, and what a restart does with
the records left behind: which gate refused a delegation, what a `claude_code` launch abort
rolls back and how far it gets, which failure arms answer the calling turn rather than the
operator's topic, and what replay resumes or refuses. The record itself, how a launch is
admitted, turn admission and the driver protocol are in
[`architecture/engagements.md`](engagements.md), which also states the invariants this
behaviour is measured against. How an engagement *ends* once it is live is
[`architecture/engagement-finalization.md`](engagement-finalization.md); the OS boundary its
workspace and uid live inside is
[`architecture/engagement-containment.md`](engagement-containment.md).

## Failure behavior

**A delegation is refused at one of its gates.** The ACL, alias, spawn-cap and
plugin-withholding refusals, and what each payload discloses, are described in
[`architecture/delegation.md`](delegation.md).

**A driver fails to start after the record exists.** The engagement is marked errored, topic
cleanup is attempted, and the caller is told the start failed.

**A launch that aborts rolls back what it provisioned, and finishes doing so.** The
`claude_code` launch removes, in order, the s6 service directory (recompiling after it), the
workspace tree, the control directory that holds `.casa-meta.json`, the uid's passwd/group
identity and its private outbox. That the sequence runs to its end even when a cancellation lands inside it is
INV-ENG-014. Each attempt keeps the best-effort floor it already had, so that one
rollback failure cannot mask the launch failure underneath it — but the floor is not uniform,
and the difference is operationally visible. Removing the service directory, pruning the
identity and tearing down the outbox each catch their failure and log it. The workspace tree
and the control directory do not: both run under `shutil.rmtree(..., ignore_errors=True)`,
which discards the error inside `rmtree` so the enclosing handler never sees one, and a
permission or I/O failure there leaves the tree in place with **no diagnostic at all**. What
cannot happen is that an attempt is skipped; a `rmtree` that fails still fails, silently for
those two. Such
a cancellation is recorded rather than propagated, no further rollback await is attempted after
it, and it is re-raised once the removals have run, carrying the launch failure it interrupted
as its context so a reader downstream holds both facts rather than one. The removals being
synchronous is what makes that cheap: after the last await there is no suspension point at
which a second cancellation could land, so no drain, shield or detached task is involved and
the launcher's compile lock is neither released early nor held across a new await. Until this
release those guards were `except Exception`, and `CancelledError` — a `BaseException` —
escaped the handler outright, leaving a workspace whose meta already read `UNDERGOING`; the
sweeper returns past `UNDERGOING` before it reads a retention deadline, and uids are never
reused (INV-CONT-001), so every such abort leaked a workspace tree, a control directory, an
identity and an outbox permanently. *Which* removals a rollback is entered to run is a separate
question this does not answer: a shutdown-caused abort is not yet distinguished from a genuine
provisioning failure, which is #698's work under the ruling that a stop and a failure are
different events. Nor is the *reporting* of the two combined: a launch that failed and was then
cancelled during its rollback reaches the caller's cancellation arm and is announced as a
cancellation, the failure surviving only as that exception's context and reaching no operator.
That routing is unchanged by this release — the escaping cancellation reached the same arm
before it — and it is the observability half of the same later work.

These launch-failure arms — a missing driver, a clearance change during launch, a superseded
plugin, a missing prompt template, an API-level fault, and a start that raised — deliberately
do NOT take the launch-death path's bounded topic notice. The reason is not that the death
path has no caller left; its post-start arm answers one too, with `launch_turn_incomplete`,
unless another writer won the terminal first, and its cancellation arm answers nobody because
the cancellation is re-raised. The reason is that these arms carry a *named fault* and the
death path carries only an *absence*. Each of the six is a fact about why the launch did not
happen, returned synchronously to the turn that asked for it, in a kind string it can act on
— one of them carries retry advice only that caller can use. The topic is aborted silently
because the launch never reached the point where the engagement is handed over as live: the
tool call still owns it, and is the one answering for it. That is not a claim that nothing
ran — the API-fault arm is raised after the stream is drained, so text the turn had already
posted progressively can be standing in the topic and cannot be retracted — it is a claim
about who answers. A launch death has no such fact to hand back: the turn ran to its end and
left nothing, or the launch was cancelled, and an operator's topic that says nothing is what
the notice exists to explain.

The claim is bounded to what the tree provides, and it is a claim about the *caller*: the
failure kind reaches the live invoking turn. Whether it then reaches the operator is that
turn's own business, and nothing here relays it. The exemption is from the notice, and from
nothing else — it does not extend to a cancellation, nor to a launch turn that ended after
starting without its terminal artifact, both of which stay on the launch-death path
(INV-ENG-011). It also does not settle whether the ledger's best-effort mark, whose result
these arms do not read, may authorize an irreversible topic close; that question is #757 and
is not answered here.

**A restart interrupts an engagement.** Persisted records load with `active` rewritten to
`idle`. Replay is attempted only for the driver kind that supports it. A record whose
workspace or recorded plugin artifact is missing is *refused* with a warning — validated
before the intact-service fast path, so an ordinary restart cannot start a service whose run
script would exit-and-respawn forever — and a missing definition is skipped with a warning.
A failed stdin-FIFO recreation and a failed service start are refusals of the same kind:
the record is marked errored and no background spool/relay machinery attaches, rather than
accepting operator messages into an engagement with no consumer (or starting one that would
crash-loop under its supervisor). A record still owing a clearance-downgrade context
rebuild (INV-MEM-011) is never *resumed*: replay drops its session pointer and archive
cache and re-renders the workspace at the clamped floor first, refusing the same way if
that fails. Every one of those decisions sits downstream of preconditions this document does
not own: INV-CONT-004 and INV-CONT-005.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/casa_core.py::replay_undergoing_engagements`
- `casa/rootfs/opt/casa/tools.py::_abort_engagement_topic`

**Tests**
- `tests/test_claude_code_driver.py`
- `tests/test_boot_replay.py`
- `tests/test_engage_executor_tool.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-containment.md`](../architecture/engagement-containment.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
<!-- END SOURCEMAP -->
