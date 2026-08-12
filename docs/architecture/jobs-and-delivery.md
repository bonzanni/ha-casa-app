---
last_reviewed: 2026-07-31
---

# Durable jobs and voice delivery

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The durable background-job lifecycle behind deferred voice work: what a job persists, how
execution and delivery are tracked separately, what a restart reconciles, and the leased
claim/acknowledge protocol that gets an answer to a device. It does not cover how a job's
turn runs (the turn loop), the socket transport itself (the voice channel), or what the
result may disclose beyond where that decision is enforced.

## Mental model

**One job, two independent state machines.** A durable job tracks *execution* (accepted,
running, then a terminal state) and *delivery* (none or ready, then claimed, authorized,
playing, delivered) separately. Execution finishing creates a delivery obligation only when
the job has a persisted route and device; a completed job with no endpoint has nowhere to
go, and stays an audit row. A separate durable handoff latch exists for foreground
acknowledgement — it neither claims delivery nor proves execution finished.

**Disk is authoritative; everything runtime is process-local.** Every mutation writes the
complete candidate snapshot to disk before publishing it in memory, under the registry lock
— this subsystem is the origin of the disk-leads discipline that
`architecture/persistent-state.md` describes. Tasks, cancellation events, permits, timers
and the delivery coordinator's per-attempt bookkeeping are process-local and die with the
process; what survives is exactly what the snapshot holds.

**A restart reconciles; it never resumes.** Boot converts every persisted accepted or
running job to a terminal state — model execution is not resumable. A job whose durable
cancellation flag was already set finalizes as cancelled, exactly as the live cancel
paths would have; every other live job becomes a terminal orphan, and a voice-routed
orphan becomes ready so its *failure* can be delivered. In-flight delivery leases are
retained for one fresh lease rather than revoked, so a device mid-playback is not
stolen from.

**Delivery is leased, attempt-scoped, at-least-once.** An offer is process-local until the
client claims it; a claim durably records an attempt id and a lease before anything is
authorized to play. Every subsequent transition must present the same attempt id against
the expected durable state. "Ready" was offered; among *successful* endings only a matching
delivered acknowledgement retires the obligation — a lapsed playing lease requeues, so
audible playback may repeat. Cancellation and TTL expiry also end a delivery obligation,
as their own terminal states. Exactly-once is deliberately not the contract.

**Ordering is per device, not global.** For each origin device, only the head of the
durable delivery order is offered, and a non-deliverable head blocks that device's queue
until it expires. Different devices progress independently, and the same device stays
serialized even across route reconnects.

**The bounds are fixed and small.** A disconnected route stays fresh for sixty seconds; a
completed result is retained at most fifteen minutes (a specialist's shorter privacy
expiry wins); a route holds at most five live-or-ready jobs; a claim leases for fifteen
seconds with five-second renewals, and a nacked endpoint parks re-offers for thirty. These
decide admission, expiry and redelivery latency — none is operator-configurable.

**Cancellation has a physical boundary.** Ready or claimed work cancels; authorized work
enters a stopping/revocation handshake; playing or delivered is too late — a cancellation
request does not promise an already-authorized answer goes unheard.

## Contracts & invariants

**INV-JOB-001**: Every job mutation writes the complete snapshot to disk before publishing it in memory; a failed write publishes nothing.

Enforced by the registry's locked commit path, which also defers caller cancellation until
the write-and-publish completes. This is the strict disk-leads pattern referenced by
INV-STATE-004.

What it does not cover: runtime task ownership. The snapshot records job state, not that
any process is still executing it.

**INV-JOB-002**: A restart never resumes execution — persisted live jobs become terminal at boot (cancelled when a durable cancellation was already pending, orphaned otherwise), and a voice-routed orphan becomes ready so the failure is deliverable.

Enforced by the boot recovery pass, called once at startup. A cancel-pending job takes
the live cancel paths' terminal shape — no restart-orphan failure, no fresh delivery
sequence, no creator notice — so a creator who cancelled before the restart is not told
their job was "lost".

What it does not cover: delivery. A leased delivery attempt survives recovery with one
fresh lease instead of being orphaned, precisely so a mid-playback device is not preempted.

**INV-JOB-003**: Every delivery transition is a compare-and-set against both the expected durable state and the recorded attempt id; a stale or mismatched frame is denied without mutation.

Enforced in the registry's transition methods — some through the shared CAS helper, some
(renew, mark-delivered, nack) with their own equivalent state-and-attempt checks; the
semantics are uniform even where the helper is not. The delivery coordinator is the only
production caller, through its frame handler — plus one extra nack site for a revoked
acknowledgement.

What it does not cover: endpoint authenticity. Route and connection matching happen in the
coordinator before the CAS; the CAS itself trusts the coordinator's identification.

**INV-JOB-004**: Delivery is at-least-once — only a matching delivered acknowledgement retires the obligation, and a lapsed playing lease requeues the job.

Enforced by lease expiry (back to ready, new attempt required) and by mark-delivered's
acceptance of only a matching claimed or playing attempt.

What it does not cover: physical playback. Requeue-after-lapse means a person may hear an
answer twice; that is the accepted cost of never losing one.

**INV-JOB-005**: Per-device FIFO — only the durable-order head of each device's queue is offered, and devices progress independently.

Enforced in the coordinator's offer pass. A head with no recorded deliverable endpoint or
modality is never offered and blocks its queue until TTL expiry sweeps it (the
non-starvation half is INV-VOICE-006's territory).

## Failure behavior

**Execution fails — an exception, an aborted run, or an invalid structured result.** A safe
failed envelope is persisted, with no model or result text interpolated into the failure
message; a routed job becomes ready so the failure itself is delivered.

**The terminal write fails.** The voice lifecycle falls back to a compatibility failure
write; if that also fails, registry-owned reconciliation retries in the background and the
live row stays restart-recoverable. A synchronous delegation whose *successful* terminal
write fails still returns its result — the record is completed by the same background
reconciliation rather than raising the answer away — and a cancellation whose write fails is
likewise retried in the background. Runtime ownership (the permit) is released either way.

**A send fails.** Only the local offer is removed; the durable row stays ready and is
re-offered. A failed revocation stays locally pending for a later sweep.

**A frame is malformed, stale, or races a CAS.** Old-protocol and unknown frames are
ignored; current-protocol frames that fail validation or the CAS receive a revoke denial,
and the *requested* transition mutates nothing — though the handler's pre-validation
reconciliation pass may independently expire or requeue jobs that were already due.

**The result outlives its TTL.** Delivery becomes expired, the attempt and lease are
cleared, and the audit row is preserved — expiry deletes nothing.

## Extension points

**A new durable field or state** touches the job dataclass, both snapshot codecs, and the
transitions and recovery that must understand it — the codecs are where forward
compatibility is decided.

**A wire-protocol change** touches the protocol constant, the inbound frame set, the
handler, offer construction and job matching; the voice channel forwards every job frame to
the coordinator without interpreting it.

**A result-shape or disclosure change** belongs in the result module — the closed schema,
the parser, and the spoken-text policy — not in the coordinator, which only renders what
policy selects.

**A scheduling change** belongs in the offer pass; **a lease or TTL change** in the
registry's expiry methods. They are deliberately separate: what to offer next versus how
long an attempt may hold.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/job_registry.py::JobRegistry`
- `casa/rootfs/opt/casa/job_registry.py::VoiceJob`
- `casa/rootfs/opt/casa/job_registry.py::JobRegistry.recover_after_restart`
- `casa/rootfs/opt/casa/channels/voice/delivery.py::VoiceDeliveryCoordinator`
- `casa/rootfs/opt/casa/voice_job_result.py::parse_voice_job_result`
- `casa/rootfs/opt/casa/voice_job_result.py::spoken_text_for`

**Tests**
- `tests/test_job_registry.py`
- `tests/test_voice_delivery.py`
- `tests/test_voice_job_result.py`

**Related**
- [`architecture/voice.md`](../architecture/voice.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
