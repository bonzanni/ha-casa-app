---
last_reviewed: 2026-09-01
---

# Engagement turn admission

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a turn is admitted to a live engagement: the registry decision taken immediately before
the call that hands the turn over, where each driver can place that decision, and what it
does and does not fence. The record itself, how an engagement is launched and the driver
protocol are in [`architecture/engagements.md`](engagements.md); the completion gate that
refuses a success over input nobody read, and the account of why the two drivers' admissions
differ in strength, are in
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md); what a
terminal transition does and does not do to a turn already begun is
[`architecture/engagement-finalization.md`](engagement-finalization.md)'s.

## Mental model

**Admission is a decision about the first byte, not about the turn.** The registry says,
synchronously and immediately before the hand-off, whether this record may receive a turn —
a terminal record never may — and the guarantee ends where delivery begins: bytes in the
engagement's pipe cannot be revoked, and stopping a turn already running is the finalize
path's driver teardown, never the admission's.

## Contracts & invariants

**INV-ENG-009**: A turn is admitted by the registry immediately before the call that hands it to the engagement, with no suspension point in between — a record found idle is `active` by then, and a terminal record is never handed a turn. For `claude_code` the admission precedes the first BYTE; for `in_casa` it precedes the awaited client hand-off, and covers follow-up turns only — the launch turn is INV-ENG-011's. Neither fences the hand-off itself: a terminal transition landing once a turn has begun cannot revoke it, and stopping an in-flight turn stays `driver.cancel`'s job. A refused turn is never reported as delivered and never told twice.

**One decision, asked at two different instants.** The registry answers it synchronously and
identically for both drivers — terminal refuses, `idle` delivers and becomes `active` without
re-stamping the last-turn time, `active` delivers, and an unknown record delivers rather than
refusing. What differs is *where the answer can be placed*, and that placement is what makes
the invariant's two halves differ in strength: for `claude_code` there is an instant at which
the engagement has provably seen nothing, and for `in_casa` there is not.
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md) carries that
account, next to the driver inbound surface it belongs to.

The `in_casa` admission covers FOLLOW-UP turns only, which is a decision rather than an
omission. A refusal on the launch turn would surface through the launch owners' error-marking
and death-reporting path, telling the operator a launch died for an engagement that a racing
writer — or their own cancellation — had just deliberately ended, which INV-ENG-013 forbids;
and the only writer racing that window is the coroutine that created the record moments
earlier. The launch turn's owner stays INV-ENG-011.

The admission sits between opening the engagement's stdin FIFO for writing — which succeeds
only once its CLI is reading — and writing the first byte, which is the first thing that CLI
can observe. That is the only instant at which the delivery is certain and the engagement
has seen nothing of it. It is deliberately synchronous: with no suspension point between the
open, the decision and the first write, no inbound tool call and no terminal transition can
interleave, which is what makes the ordering a guarantee rather than a race. Durability
follows behind it — the authority check reads the in-memory record, and a persist that never
lands costs only that a later restart re-idles the record, after which the same redelivery
admits it again.

A turn that fits the pipe is written by a single non-blocking write, so in practice the
admission covers the whole of it rather than only its first byte. That is not luck: a payload
larger than the pipe's capacity would be written in pieces, and the suspension between them
is a real scheduling point at which an ungated terminal transition can commit, leaving the
remainder to be written to a record that is already terminal. Since a delivery cannot be
revoked, the fix is to remove the suspension rather than guard it — the pipe is grown to fit
the payload before the first write. The growth is strictly best-effort: a kernel that refuses
it, or a payload beyond the maximum pipe size, simply falls back to writing in pieces.

What it does not cover: the bytes after the first, when the payload does not fit. A terminal
transition landing mid-turn cannot revoke a delivery — closing the writer is itself an
end-of-input the CLI acts on — so a turn already begun runs until the finalize path's driver
teardown *attempts* to stop it, and what that teardown does and does not establish about the
OS process is in [`architecture/engagement-containment.md`](engagement-containment.md). A *completion*
cannot land in that window, because a message is still counted as unread throughout its own
write and INV-ENG-003 refuses; a cancellation can, and what becomes of the truncated turn it
produces is teardown's business rather than the write path's — INV-CONT-001 states what that
does and does not settle. The
admission also expresses no opinion on a record the registry does not know, which is
unreachable for a live engagement and where the dispatch gate already fails closed.

## Failure behavior

**The registry refuses the turn.** For `claude_code` the refusal lands before the first byte
is written; for `in_casa` before the awaited client hand-off. A refused turn is never
reported as delivered and never told twice (INV-ENG-009 above); how the delivery task
settles the refused turn's admission ticket is described with the driver inbound surface in
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md).

## Extension points

**A new driver** places its admission at the last synchronous instant before the
engagement can observe the turn — the placement, not the decision, is what differs per
driver. Everything else a driver implements is in
[`architecture/engagements.md`](engagements.md).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.begin_turn_delivery`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver._write_to_fifo`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver`

**Tests**
- `tests/test_claude_code_driver.py`
- `tests/test_engagement_registry.py`
- `tests/test_c1_continuation_admission.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
<!-- END SOURCEMAP -->
