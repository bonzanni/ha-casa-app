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

**INV-JOB-009**: A live job that a graceful stop itself settles is not settled at all — the row is left as it stands, so the boot reconciliation treats it exactly as it treats a job lost to a crash. A settling the stop did not cause, and every success or non-cancellation verdict, still commits mid-stop.

Enforced inside the registry, never at the call sites: the shutdown-deferral guard sits
at three registry transitions — the two compatibility cancellation transitions, and the
continuation compensation that settles by *deleting* the child row. What is closed is a
property, not a roster of publish points: a cancellation-reasoned settlement is deferred
to boot, while a success or non-cancellation verdict still commits mid-stop through
whichever registry transition carries it — the next paragraph narrows *which*
cancellations defer. The delivery-expiry sweeps touch delivery state alone and never
execution state.

This exists because the shape a shutdown used to write was not merely lossy but false —
the same "cancelled" envelope a *creator* cancellation writes, which recovery deliberately
settles in silence, so a stop announced nothing while a crash correctly announced one lost
job. A stop was measurably worse than a crash. It is the same rule INV-JOB-007 already
publishes for a scheduled question, under the same reason: `casa_shutdown` settles
nothing, edits nothing, and leaves the record for the next boot.

**The discriminator is the cause of the settling, and cause is carried, not inferred.** A
terminal's *category* cannot carry it: a creator cancel, a turn-budget expiry, a pre-launch
bail, a launch rollback and process death all write the same cancelled shape, so keying on
that shape plus a "the process is stopping" flag defers settlings the stop never caused —
and a pre-launch bail deferred that way has the next boot announce a loss for work never
started, whose caller was already told. So a site whose cause is known where it stands
records it, and only an *unrecorded* settling defers. Unrecorded is therefore the safe
default: a site that records nothing errs toward telling the operator, and a bare
cancellation — which cannot tell a barge-in from process death — is exactly the unrecorded
case this invariant exists for.

What it does not cover: **delivery**, and terminal work. The restart-orphan notice is
acknowledged when it is *enqueued*, not when the operator has it, so this invariant is
about the durable row and its entry into recovery — a notice already handed to the bus can
still die with the process, exactly as it can after a crash. And a job that reaches a
terminal state *during* the stop is outside recovery's reach altogether: recovery converts
only live rows, so a delegation that succeeds mid-stop is announced by the live path or not
at all. Nor does it cover a job the creator had *already* cancelled: its durable
cancel-pending flag means a completion arriving mid-stop still writes a cancelled terminal
through the completion path, and recovery still settles such a row silently as a creator
cancellation — which is the correct outcome, and the reason silence there is not a gap.

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

One asymmetry is worth stating, because it is the seam between the two halves of this
design. Revocation reads the broker, never the record file (INV-JOB-008) — but from process
start until this reconcile runs, records exist on disk and the broker is empty, so a
revocation landing in that window cancels nothing. Each revocation therefore leaves a
process-local MARKER carrying the selector it used — a role, a role and a trigger's labels,
or a chat — and the reconciler settles, rather than restores, a record that matches one.
The markers are retired once the pass completes, after which every surviving record is in
the broker and a revocation's own scan sees it. The rule stays intact — no live decision
reads the store — and the one state where the broker cannot speak for it is handled where
the store is legitimately read. The selector has to be the revocation's OWN: keying it on
the role's lifecycle epoch, which a single-trigger revocation also bumps, discarded every
other question that role was waiting on.

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

**Ordinary conversation is not a claim on that lane.** The rule that a plain DM message
resolves a pending question — the text *is* the answer — is true of a question this
conversation asked and false of a machine-timed one, whose answer routes to the session that
asked it and can never be carried by a turn of the operator's own session. Since the
scheduled question moved into the operator's plain-ask scope, a message retired it there
along with everything else, under an ending the promise made for scheduled questions does
not contain. Plain text now retires only the human-raised asks in that DM, selected by the
same `scheduled` marker the boot restore rebuilds — so a restored question is protected by
exactly the rule a live one gets, with no second mechanism to keep in step. The direction
above is unchanged: a human question still supersedes a machine one, never the reverse. What
changed is when — the displacement happens once the replacement is DELIVERED and still live,
not when it is merely registered, so a keyboard whose post fails, or which is cancelled while
that post is in flight, no longer takes a waiting question with it. The authorization
challenge is the stated exception and keeps clearing the lane at admission, before its own
keyboard is posted.

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

**The process is stopping.** A cancellation caused by the stop is not written at all
(INV-JOB-009): the row stays live, and the retry that would otherwise chase a terminal it
can never reach stops for the same reason. Read INV-JOB-009 for what that does and does not
promise — a stop makes a live row recoverable, and nothing more than that.

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
- `tests/test_graceful_shutdown_jobs.py`
- `tests/test_graceful_shutdown_cause.py`
- `tests/test_voice_delivery.py`
- `tests/test_voice_job_result.py`
- `tests/test_scheduled_ask_user.py`
- `tests/test_scheduled_ask_attention_lane.py`

**Related**
- [`architecture/voice.md`](../architecture/voice.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
<!-- END SOURCEMAP -->
