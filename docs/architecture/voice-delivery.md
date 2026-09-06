---
last_reviewed: 2026-09-01
---

# Voice delivery

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The leased claim/acknowledge protocol that gets a deferred voice answer to a device: how an
offer becomes a claim, what a lease promises and for how long, per-device ordering, the
fixed bounds, and what the result may disclose only where that decision is enforced. It does
not cover the durable job row itself, its restart reconciliation or what a stop does to it
([`architecture/jobs-and-delivery.md`](jobs-and-delivery.md)), or the socket transport
(the voice channel).

## Mental model

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
completed result is deliverable for at most fifteen minutes (a specialist's shorter privacy
expiry wins), and that same deadline is when the record is deleted — see
[what Casa keeps about a finished delegation](jobs-and-delivery.md#what-casa-keeps-about-a-finished-delegation),
which owns the retention rule; a route holds at most five live-or-ready jobs; a claim leases
for fifteen seconds with five-second renewals, and a nacked endpoint parks re-offers for
thirty. These decide admission, expiry and redelivery latency — none is
operator-configurable.

**Cancellation has a physical boundary.** Ready or claimed work cancels; authorized work
enters a stopping/revocation handshake; playing or delivered is too late — a cancellation
request does not promise an already-authorized answer goes unheard.

## Contracts & invariants

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

**A send fails.** Only the local offer is removed; the durable row stays ready and is
re-offered. A failed revocation stays locally pending for a later sweep.

**A frame is malformed, stale, or races a CAS.** Old-protocol and unknown frames are
ignored; current-protocol frames that fail validation or the CAS receive a revoke denial,
and the *requested* transition mutates nothing — though the handler's pre-validation
reconciliation pass may independently expire or requeue jobs that were already due.

**The result outlives its TTL.** The next expiry pass deletes the durable row outright,
with the request, its context and the answer; a live attempt on it is reconciled exactly as
a non-deliverable one is, with one revoke and the attempt reclaimed. Nothing is kept as an
expired audit record, so `voice_job_status`, `cancel_voice_job` and `continue_voice_job`
answer for that id with *job not found* rather than reporting an expired job — the record
is gone, and saying so is the truthful answer. The one exception is a row that still owes
its creator an announcement, which is kept until the notice is delivered; the retention
rule and its exemption are stated in
[what Casa keeps about a finished delegation](jobs-and-delivery.md#what-casa-keeps-about-a-finished-delegation).

## Extension points

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
- `casa/rootfs/opt/casa/channels/voice/delivery.py::VoiceDeliveryCoordinator`
- `casa/rootfs/opt/casa/voice_job_result.py::parse_voice_job_result`
- `casa/rootfs/opt/casa/voice_job_result.py::spoken_text_for`

**Tests**
- `tests/test_voice_delivery.py`
- `tests/test_voice_job_result.py`

**Related**
- [`architecture/jobs-and-delivery.md`](../architecture/jobs-and-delivery.md)
- [`architecture/voice.md`](../architecture/voice.md)
<!-- END SOURCEMAP -->
