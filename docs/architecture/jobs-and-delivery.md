---
last_reviewed: 2026-07-31
---

# Durable jobs and voice delivery

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The durable background-job lifecycle behind deferred voice work: what a job persists, how
execution and delivery are tracked separately, what a restart reconciles, and the leased
claim/acknowledge protocol that gets an answer to a device — and the other durable
obligation shaped the same way, a question a resident's schedule asked the operator and is
waiting on. It does not cover how a job's turn runs (the turn loop), the socket transport
itself (the voice channel), or what the result may disclose beyond where that decision is
enforced.

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

**A scheduled question is a durable obligation, not a message.** When a resident's own
schedule asks the operator something, the question outlives the turn that asked it: the
broker holding it is in-memory, so a record on disk is what keeps a keyboard on screen
honest across a restart, and what guarantees the waiting session is eventually told
*something*. It is the same disk-leads discipline as a job, with the opposite duplicate
policy (INV-JOB-006), and it is deliberately timid about the operator's attention —
a machine-timed question yields to a human one in both directions (INV-JOB-008).

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

Recovery rebuilds the notified turn's origin **field by field** from the durable row, so
anything the live path carried as an origin marker and the row does not carry as a field is
gone by the time a resident is resumed. Scheduled-media eligibility (INV-TRIG-012) is
therefore a stored boolean rather than a marker riding the origin dict, restored only from
an exact stored true — a row written before the field existed restores nothing, and the
resumed turn stays text-only exactly as it did before that feature. Read that as the general
rule for this file: a capability that must survive a restart has to be *in the row*.

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

**INV-JOB-006**: A scheduled question's durable record is written before its keyboard is posted and moved to settling before any terminal action, so a restart restores an unexpired question and never replays a settled one.

Enforced by the record's compare-and-set state machine: `posting` before the post, `live`
once the message id is known, `settling` before the first terminal edit, dropped after the
terminal continuation is dispatched. Deletion is not the acknowledgement — `settling` is.
The boot reconciler restores a `live` record with its remaining timeout and the identical
broker binding, settles an expired, unconfirmed or operator-changed one, and drops a
`settling` one in silence.

What it does not cover: exactly-once. The crash window between "decided" and "dispatched"
resolves toward at-most-once — the opposite of INV-JOB-004's choice, and deliberately so: a
duplicated answer makes a resident act twice on one confirmation, while a lost one leaves an
unanswered question in a session that keeps working. A record still `posting` at boot may
also leave an orphaned keyboard on screen, which a tap answers with "expired".

**INV-JOB-007**: Every terminal outcome of a scheduled question — answered, expired, or cancelled for any reason — is delivered back to the session that asked, as a machine-authored scheduled turn.

Enforced by the scheduled ask's finish hook, the single owner of the keyboard edit, the
continuation and the record. The continuation reproduces the *firing* turn's shape (the
session label as chat id, the same scheduled-delivery marker, the epoch the question was
asked under) and carries no trusted user origin: the operator's tap is reported in the
turn's content, never as its speaker, so it cannot relabel a machine-authored session.

What it does not cover: the shutdown cancel, which settles nothing, edits nothing and leaves
the record for the next boot — the keyboard is still on screen and the question is still
honest. Nor does it cover a bus enqueue that is accepted and then never runs.

**INV-JOB-008**: A scheduled question never displaces a live operator question: it is admitted only into an idle attention lane, and an authorization challenge cancels a live scheduled one before registering its own.

Enforced synchronously in the broker: registration with an idle requirement across both
halves of the lane (plain asks and authorization challenges are separate scopes), and a
predicate cancel from the challenge's own no-await block. Refused, the tool answers
`operator_busy` and asks nothing. The direction is one-way by design — a human question
supersedes a machine one, never the reverse.

What it does not cover: selection from the durable record file. Live decisions read the
broker, which is synchronous; the record file is written after an await and would miss an
ask that had just won its lane.

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
- `casa/rootfs/opt/casa/scheduled_asks.py::ScheduledAskStore`
- `casa/rootfs/opt/casa/scheduled_asks.py::make_finish_hook`
- `casa/rootfs/opt/casa/scheduled_asks.py::reconcile_at_boot`

**Tests**
- `tests/test_job_registry.py`
- `tests/test_voice_delivery.py`
- `tests/test_voice_job_result.py`
- `tests/test_scheduled_ask_user.py`

**Related**
- [`architecture/voice.md`](../architecture/voice.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
<!-- END SOURCEMAP -->
