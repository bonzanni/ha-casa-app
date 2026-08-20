"""Durable specialist voice-job state machine.

The registry is the single owner of execution and voice-delivery lifecycle
state.  Runtime-only task, cooperative-cancellation, and concurrency-permit
objects are deliberately kept out of the JSON snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping

from atomic_io import PRIVATE, atomic_write_json
from personality_types import SpeakerProvenance
from speaker_provenance import provenance_from_mapping, provenance_mapping


logger = logging.getLogger(__name__)

# #671: the reason a graceful stop gives for the cancellations it causes. The
# same string the rest of Casa already uses for this — `verdict_broker`'s
# `cancel_all(reason="casa_shutdown")` and the scheduled-ask finish hook, whose
# published exemption (INV-JOB-007) says a shutdown cancel settles nothing and
# leaves the record for the next boot. Extended here, not duplicated.
CASA_SHUTDOWN_REASON = "casa_shutdown"


class ExecutionState(StrEnum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ORPHANED = "ORPHANED"


class DeliveryState(StrEnum):
    NONE = "NONE"
    READY = "READY"
    CLAIMED = "CLAIMED"
    AUTHORIZED = "AUTHORIZED"
    PLAYING = "PLAYING"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class HandoffState(StrEnum):
    """Durable acknowledgement latch for a voice specialist handoff."""

    NONE = "NONE"
    PENDING = "PENDING"
    RECEIVED = "RECEIVED"


@dataclass(frozen=True)
class JobFailure:
    """Stable failure envelope safe to persist and deliver after restart."""

    kind: str
    message: str


@dataclass(frozen=True)
class VoiceJob:
    """One durable delegated job and its delivery compare-and-set state."""

    id: str
    parent_job_id: str | None
    # Task 12: the caller's and executor's typed identity snapshots, taken at
    # construction and kept immutable for this job's lifetime — the source
    # of truth for who created/is running this job. The bare-string fields
    # below (creating_role/specialist_role/specialist_display_name) remain
    # for backward-compatible display views; they are NEVER derived FROM
    # these speaker snapshots, only alongside them.
    creating_speaker: SpeakerProvenance
    executing_speaker: SpeakerProvenance
    creating_role: str
    specialist_role: str
    specialist_display_name: str
    creator_peer: str
    creator_user_id: str | None
    scope_id: str
    origin_route_id: str | None
    origin_device_id: str | None
    task: str
    context: str
    created_at: float
    started_at: float | None
    terminal_at: float | None
    expires_at: float | None
    execution_state: ExecutionState
    delivery_state: DeliveryState
    result: str | None
    failure: JobFailure | None
    awaiting_input: bool
    continuable_until: float | None
    delivery_sequence: int
    delivery_attempt_id: str | None
    lease_until: float | None
    cancel_pending: bool
    orphan_notification_pending: bool = False
    prompted_delivery: bool = False
    # Server-bound HA route identity used for cross-satellite job control.
    # None is the backward-compatible legacy row shape, which remains scoped
    # to ``scope_id`` exactly as before this field existed.
    job_control_id: str | None = None
    # Separate from delivery state: this records that the integration has
    # acknowledged the foreground-to-background handoff frame.
    handoff_id: str | None = None
    handoff_state: HandoffState = HandoffState.NONE
    # How the ORIGIN endpoint said it can receive this answer ("audio"|"text"),
    # captured at creation from the per-utterance offer (#233/#224). None means
    # the endpoint offered nothing — or the row predates this field, in which
    # case delivery must FAIL CLOSED rather than assume audio and announce a
    # phone's answer on a speaker.
    delivery_modality: str | None = None
    # #485: was this delegation created by a turn Casa's own schedule fired?
    # The live completion path carries that as an origin marker, but a restart
    # resumes through THIS row, whose origin is rebuilt field by field — so the
    # eligibility has to be a field or it is silently gone. Defaults False: a
    # row written before this field existed restores no eligibility, which is
    # the fail-closed direction (the turn stays text-only, exactly as it did
    # before the feature).
    scheduled_delivery: bool = False


@dataclass(frozen=True)
class CancelResult:
    status: str


def _inherit_delivery_modality(parent: VoiceJob, child: VoiceJob) -> VoiceJob:
    """Carry the parent's delivery promise onto a re-delivery of that promise.

    This applies ONLY where there is no live utterance to ask. A prompted
    re-delivery replays an answer the operator was already promised, so the
    original promise is the right one to keep.

    A continuation is deliberately excluded. It has its own live turn, and a
    turn that carried no offer is not missing information — it is the endpoint
    saying it cannot currently receive a deferred answer. Reviving the parent's
    older modality there would promise audio into a room whose speaker has
    since gone away, and delivery would refuse it (#233/#224).

    Inheritance is scoped to the SAME endpoint: a promise about the kitchen
    speaker says nothing about what another device can receive.
    """
    if child.delivery_modality is not None:
        return child
    if child.origin_device_id != parent.origin_device_id:
        return child
    return replace(child, delivery_modality=parent.delivery_modality)


class JobRegistryError(RuntimeError):
    """Base class for durable job registry failures."""


class JobTransitionError(JobRegistryError):
    """A compare-and-set transition did not match the persisted state."""


class JobRouteCapacityError(JobRegistryError):
    """A voice route already owns the maximum live delivery backlog."""


class JobAuthorizationError(JobRegistryError):
    """The cancellation actor does not own the job's creation scope."""


class JobRegistry:
    """Crash-safe registry for delegated execution and voice delivery.

    A mutation is published to memory only after the complete candidate
    snapshot has been atomically replaced on disk while ``_lock`` is held.
    """

    LEASE_SECONDS = 15.0
    RESULT_TTL_SECONDS = 24 * 60 * 60.0
    CANCEL_GRACE_SECONDS = 2.0

    _TERMINAL_EXECUTION = frozenset({
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
        ExecutionState.ORPHANED,
    })
    _LEASED_DELIVERY = frozenset({
        DeliveryState.CLAIMED,
        DeliveryState.AUTHORIZED,
        DeliveryState.PLAYING,
    })

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        reconciliation_retry_interval: float = 5.0,
        result_ttl_seconds: float = RESULT_TTL_SECONDS,
    ) -> None:
        retry_interval = float(reconciliation_retry_interval)
        if not math.isfinite(retry_interval) or retry_interval <= 0:
            raise ValueError("reconciliation_retry_interval must be positive")
        result_ttl = float(result_ttl_seconds)
        if not math.isfinite(result_ttl) or result_ttl <= 0:
            raise ValueError("result_ttl_seconds must be positive")
        self._path = os.fspath(path)
        self._clock = clock
        self._jobs: dict[str, VoiceJob] = {}
        self._delivery_sequence = 0
        self._lock = asyncio.Lock()
        self._loaded = False

        # Process-local ownership.  None of these values is JSON-serializable
        # or meaningful after a restart.
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._permits: dict[str, Any] = {}
        self._cancel_timers: dict[str, asyncio.Task] = {}
        self._reconciliation_retry_interval = retry_interval
        self._voice_result_ttl_seconds = result_ttl
        self._reconciliation_tasks: dict[str, asyncio.Task] = {}
        self._reconciliation_waiters: dict[
            str, set[asyncio.Future[None]]
        ] = {}
        self._terminal_waiters: dict[
            str, set[asyncio.Future[VoiceJob]]
        ] = {}
        self._runtime_release_waiters: dict[
            str, set[asyncio.Future[None]]
        ] = {}
        # #671: the reason a cancellation write carries once the process has
        # declared a graceful stop. Process-local and never encoded — see
        # `begin_shutdown`.
        self._shutdown_reason: str | None = None

    @property
    def path(self) -> str:
        return self._path

    async def load(self) -> None:
        """Load the durable snapshot."""
        async with self._lock:
            if self._loaded:
                return

            snapshot_exists = os.path.exists(self._path)
            if snapshot_exists:
                raw = await asyncio.to_thread(self._read_json, self._path)
                jobs = self._decode_snapshot(raw)
            else:
                jobs = {}

            def publish_load() -> None:
                self._jobs = jobs
                self._delivery_sequence = max(
                    (job.delivery_sequence for job in jobs.values()), default=0,
                )
                self._loaded = True

            if not snapshot_exists:
                async def commit_load() -> None:
                    await self._write_snapshot_locked(jobs)
                    publish_load()

                await self._finish_atomic_commit(commit_load())
            else:
                publish_load()

    def get(self, job_id: str) -> VoiceJob | None:
        return self._jobs.get(job_id)

    def all(self) -> list[VoiceJob]:
        """Return a stable delivery-order snapshot."""
        return sorted(
            self._jobs.values(),
            key=lambda job: (job.delivery_sequence, job.created_at, job.id),
        )

    async def create(
        self,
        job: VoiceJob,
        *,
        max_active_ready_per_route: int | None = None,
    ) -> VoiceJob:
        async with self._lock:
            self._require_loaded()
            if not job.id:
                raise ValueError("job id must not be empty")
            if job.id in self._jobs:
                raise JobTransitionError(f"job {job.id!r} already exists")
            self._require_route_capacity(
                job, max_active_ready_per_route=max_active_ready_per_route,
            )
            candidate = dict(self._jobs)
            candidate[job.id] = job
            await self._commit_snapshot_locked(candidate)
            return job

    async def create_continuation(
        self,
        parent_job_id: str,
        child: VoiceJob,
        *,
        actor: Any,
        max_active_ready_per_route: int | None = None,
    ) -> VoiceJob:
        """Consume one live clarification and create its child atomically."""
        async with self._lock:
            self._require_loaded()
            parent = self._require_job(parent_job_id)
            self._authorize_actor(parent, actor)
            self._authorize_actor(child, actor)
            if not child.id:
                raise ValueError("job id must not be empty")
            if child.id in self._jobs:
                raise JobTransitionError(f"job {child.id!r} already exists")
            if child.parent_job_id != parent_job_id:
                raise ValueError("continuation child has the wrong parent")
            if child.specialist_role != parent.specialist_role:
                raise ValueError("continuation child has the wrong specialist")
            self._require_route_capacity(
                child,
                max_active_ready_per_route=max_active_ready_per_route,
            )
            if (child.execution_state is not ExecutionState.ACCEPTED
                    or child.delivery_state is not DeliveryState.NONE
                    or child.result is not None
                    or child.failure is not None
                    or child.awaiting_input
                    or child.continuable_until is not None):
                raise ValueError("continuation child must be newly accepted")

            now = self._now()
            continuable = (
                parent.execution_state is ExecutionState.SUCCEEDED
                and parent.awaiting_input
                and parent.continuable_until is not None
                and parent.continuable_until > now
                and (parent.expires_at is None or parent.expires_at > now)
                and parent.delivery_state not in {
                    DeliveryState.CANCELLED,
                    DeliveryState.EXPIRED,
                }
                and not parent.cancel_pending
                and parent.result is not None
            )
            if not continuable:
                raise self._transition_error(
                    parent,
                    "create_continuation",
                    expected="live awaiting-input parent",
                )

            consumed_parent = replace(
                parent,
                awaiting_input=False,
            )
            candidate = dict(self._jobs)
            candidate[parent_job_id] = consumed_parent
            candidate[child.id] = child
            await self._commit_snapshot_locked(candidate)
            return child

    async def compensate_unbound_continuation(
        self,
        parent_job_id: str,
        child_job_id: str,
        *,
        actor: Any,
    ) -> bool:
        """Remove an unbound child and restore its still-fresh parent."""
        async with self._lock:
            self._require_loaded()
            parent = self._require_job(parent_job_id)
            self._authorize_actor(parent, actor)
            child = self._jobs.get(child_job_id)
            if child is None:
                return False
            self._authorize_actor(child, actor)
            if child.parent_job_id != parent_job_id:
                raise JobTransitionError(
                    f"job {child_job_id!r} is not a child of {parent_job_id!r}"
                )
            if (child.execution_state is not ExecutionState.ACCEPTED
                    or child_job_id in self._tasks):
                return False

            now = self._now()
            restore_parent = (
                parent.execution_state is ExecutionState.SUCCEEDED
                and not parent.awaiting_input
                and parent.continuable_until is not None
                and parent.continuable_until > now
                and (parent.expires_at is None or parent.expires_at > now)
                and parent.delivery_state not in {
                    DeliveryState.CANCELLED,
                    DeliveryState.EXPIRED,
                }
                and not parent.cancel_pending
                and parent.result is not None
            )
            candidate = dict(self._jobs)
            candidate.pop(child_job_id)
            if restore_parent:
                candidate[parent_job_id] = replace(parent, awaiting_input=True)
            await self._commit_snapshot_locked(candidate)
            return restore_parent

    async def create_prompted_delivery(
        self,
        parent_job_id: str,
        child: VoiceJob,
        *,
        actor: Any,
        max_active_ready_per_route: int | None = None,
    ) -> VoiceJob:
        """Create a metadata-only child that re-delivers a stored result."""
        async with self._lock:
            self._require_loaded()
            parent = self._require_job(parent_job_id)
            self._authorize_actor(parent, actor)
            self._authorize_actor(child, actor)
            if child.id in self._jobs:
                raise JobTransitionError(f"job {child.id!r} already exists")
            if (
                child.parent_job_id != parent.id
                or child.specialist_role != parent.specialist_role
            ):
                raise ValueError("prompted delivery child does not match parent")
            now = self._now()
            available = (
                parent.execution_state is ExecutionState.SUCCEEDED
                and parent.result is not None
                and (parent.expires_at is None or parent.expires_at > now)
                and parent.delivery_state not in {
                    DeliveryState.CANCELLED,
                    DeliveryState.EXPIRED,
                }
                and not parent.cancel_pending
            )
            if not available:
                raise self._transition_error(
                    parent,
                    "create_prompted_delivery",
                    expected="live successful result",
                )
            self._require_route_capacity(
                child,
                max_active_ready_per_route=max_active_ready_per_route,
            )
            prompted = replace(
                _inherit_delivery_modality(parent, child),
                started_at=now,
                terminal_at=now,
                expires_at=parent.expires_at,
                execution_state=ExecutionState.SUCCEEDED,
                delivery_state=DeliveryState.READY,
                result=parent.result,
                delivery_sequence=self._delivery_sequence + 1,
                prompted_delivery=True,
            )
            candidate = dict(self._jobs)
            candidate[prompted.id] = prompted
            await self._commit_snapshot_locked(candidate)
            return prompted

    def owns_task(self, job_id: str, task: asyncio.Task) -> bool:
        """Return whether runtime ownership was published for exactly task."""
        return self._tasks.get(job_id) is task

    async def wait_for_terminal(self, job_id: str) -> VoiceJob:
        """Wait for a concrete durable terminal transition for one job."""
        async with self._lock:
            current = self._require_job(job_id)
            if current.execution_state in self._TERMINAL_EXECUTION:
                return current
            future = asyncio.get_running_loop().create_future()
            self._terminal_waiters.setdefault(job_id, set()).add(future)
        try:
            return await future
        finally:
            waiters = self._terminal_waiters.get(job_id)
            if waiters is not None:
                waiters.discard(future)
                if not waiters:
                    self._terminal_waiters.pop(job_id, None)

    async def wait_for_runtime_release(self, job_id: str) -> None:
        """Wait until the bound task has released its runtime ownership."""
        if job_id not in self._tasks:
            return
        future = asyncio.get_running_loop().create_future()
        self._runtime_release_waiters.setdefault(job_id, set()).add(future)
        if job_id not in self._tasks and not future.done():
            future.set_result(None)
        try:
            await future
        finally:
            waiters = self._runtime_release_waiters.get(job_id)
            if waiters is not None:
                waiters.discard(future)
                if not waiters:
                    self._runtime_release_waiters.pop(job_id, None)

    @property
    def reconciliation_count(self) -> int:
        return len(self._reconciliation_tasks)

    async def wait_for_reconciliation(self, job_id: str) -> None:
        """Wait until registry-owned terminal reconciliation is drained."""
        task = self._reconciliation_tasks.get(job_id)
        if task is None or task.done():
            return
        future = asyncio.get_running_loop().create_future()
        self._reconciliation_waiters.setdefault(job_id, set()).add(future)
        current = self._reconciliation_tasks.get(job_id)
        if (current is not task or task.done()) and not future.done():
            future.set_result(None)
        try:
            await future
        finally:
            waiters = self._reconciliation_waiters.get(job_id)
            if waiters is not None:
                waiters.discard(future)
                if not waiters:
                    self._reconciliation_waiters.pop(job_id, None)

    def schedule_failure_reconciliation(self, job_id: str) -> None:
        """Strongly own a metadata-only retry for a still-live failed write."""
        self._schedule_terminal_reconciliation(
            job_id,
            lambda: self.fail_compat(
                job_id,
                JobFailure(
                    "persistence_failed",
                    "Specialist result could not be saved.",
                ),
            ),
        )

    def schedule_completion_reconciliation(self, job_id: str) -> None:
        """#321: registry-owned retry completing a job whose result was
        already returned to the caller but whose terminal snapshot write
        failed — the answer must never be discarded by restart recovery
        (ORPHANED) just because the metadata write lost a race with disk.

        SYNC-DELEGATION ONLY (Terra r4): the empty result string here is not
        a placeholder — it reproduces ``complete_delegation``'s intended
        terminal exactly (``finish_compat(id, "")``). Sync results are
        returned synchronously to the engager and are never persisted in the
        durable job (created with ``delivery_state=NONE``), so there is no
        stored result to clobber. Voice results go through
        ``finish_voice_result`` and the ``_persist_voice_terminal`` fallback,
        never this method."""
        self._schedule_terminal_reconciliation(
            job_id, lambda: self.finish_compat(job_id, ""))

    def schedule_cancel_reconciliation(self, job_id: str) -> None:
        """#321: registry-owned retry for a cancellation whose snapshot write
        failed (voice-deadline teardown) — the job must not stay RUNNING with
        its permit accounted for until a restart."""
        self._schedule_terminal_reconciliation(
            job_id, lambda: self.cancel(job_id))

    def _schedule_terminal_reconciliation(
        self,
        job_id: str,
        op: Callable[[], Awaitable["VoiceJob | None"]],
    ) -> None:
        existing = self._reconciliation_tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._reconcile_terminal(job_id, op))
        self._reconciliation_tasks[job_id] = task
        task.add_done_callback(
            lambda done, jid=job_id: self._reconciliation_done(jid, done),
        )

    async def _reconcile_terminal(
        self,
        job_id: str,
        op: Callable[[], Awaitable["VoiceJob | None"]],
    ) -> None:
        while True:
            await asyncio.sleep(self._reconciliation_retry_interval)
            try:
                current = await op()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never render persistence content
                logger.warning(
                    "job %s terminal reconciliation retry failed",
                    job_id[:8],
                )
                continue
            if (current is None
                    or current.execution_state in self._TERMINAL_EXECUTION):
                return
            if self._shutdown_reason is not None:
                # #671 liveness: a deferred cancellation returns a still-LIVE
                # row, so this loop would retry an operation that can never
                # reach a terminal for the rest of the process's life. The boot
                # reconciliation owns the row now. This is a progress-based
                # stop, not an elapsed allowance on the work.
                logger.info(
                    "job %s terminal reconciliation stopped for boot recovery",
                    job_id[:8],
                )
                return

    def _reconciliation_done(self, job_id: str, task: asyncio.Task) -> None:
        if self._reconciliation_tasks.get(job_id) is task:
            self._reconciliation_tasks.pop(job_id, None)
        for waiter in self._reconciliation_waiters.pop(job_id, set()):
            if not waiter.done():
                waiter.set_result(None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("job %s terminal reconciliation stopped", job_id[:8])

    async def bind_task(
        self,
        job_id: str,
        task: asyncio.Task,
        permit: Any = None,
        cancel_event: asyncio.Event | None = None,
    ) -> asyncio.Event:
        """Transition ACCEPTED→RUNNING and bind runtime task ownership.

        The installed done callback is the sole release authority for the
        bound permit.  Terminal record transitions intentionally never touch
        it because cancellation can be persisted before the task has actually
        finished unwinding.
        """
        async with self._lock:
            self._require_loaded()
            current = self._require_job(job_id)
            if current.execution_state is not ExecutionState.ACCEPTED:
                raise self._transition_error(
                    current, "bind_task", expected="execution=ACCEPTED",
                )
            if job_id in self._tasks:
                raise JobTransitionError(f"job {job_id!r} already has a task")
            updated = replace(
                current,
                execution_state=ExecutionState.RUNNING,
                started_at=(current.started_at
                            if current.started_at is not None else self._now()),
            )
            candidate = self._with_job(updated)

            event = cancel_event or asyncio.Event()

            def publish_runtime_ownership() -> None:
                self._tasks[job_id] = task
                self._cancel_events[job_id] = event
                if permit is not None:
                    self._permits[job_id] = permit
                task.add_done_callback(
                    lambda done, jid=job_id: self._task_done(jid, done),
                )

            await self._commit_snapshot_locked(
                candidate, after_publish=publish_runtime_ownership,
            )
            return event

    async def mark_handoff_pending(
        self, job_id: str, handoff_id: str,
    ) -> VoiceJob:
        """Durably arm a route-bound handoff before its frame is emitted."""
        if not isinstance(handoff_id, str) or not handoff_id.strip():
            raise ValueError("handoff id must not be empty")
        async with self._lock:
            current = self._require_job(job_id)
            if not current.origin_route_id:
                raise JobTransitionError(
                    f"job {job_id!r} is not bound to a voice route"
                )
            if current.handoff_id not in {None, handoff_id}:
                raise self._transition_error(
                    current, "mark_handoff_pending", expected="matching handoff id",
                )
            if current.handoff_state is HandoffState.RECEIVED:
                raise self._transition_error(
                    current, "mark_handoff_pending", expected="handoff not received",
                )
            if current.handoff_state is HandoffState.PENDING:
                return current
            return await self._persist_job_locked(replace(
                current,
                handoff_id=handoff_id,
                handoff_state=HandoffState.PENDING,
            ))

    async def acknowledge_handoff(
        self, job_id: str, handoff_id: str,
    ) -> VoiceJob:
        """Compare-and-set a handoff acknowledgement without state regression."""
        if not isinstance(handoff_id, str) or not handoff_id.strip():
            raise ValueError("handoff id must not be empty")
        async with self._lock:
            current = self._require_job(job_id)
            if current.handoff_id != handoff_id:
                raise self._transition_error(
                    current, "acknowledge_handoff", expected="matching handoff id",
                )
            if current.handoff_state is HandoffState.RECEIVED:
                return current
            if current.handoff_state is not HandoffState.PENDING:
                raise self._transition_error(
                    current, "acknowledge_handoff", expected="handoff=PENDING",
                )
            return await self._persist_job_locked(replace(
                current, handoff_state=HandoffState.RECEIVED,
            ))

    def pending_handoffs_for_route(self, route_id: str) -> list[VoiceJob]:
        """Return pending, still-deliverable handoffs for one bound route.

        #321: DELIVERED is excluded alongside CANCELLED/EXPIRED — a job whose
        result already reached the device but whose handoff frame was lost
        has nothing left to hand off, and ``expire_due`` skips DELIVERED jobs
        so nothing else would ever retire the replay."""
        if not isinstance(route_id, str) or not route_id.strip():
            return []
        return [
            job for job in self.all()
            if (
                job.origin_route_id == route_id
                and job.handoff_state is HandoffState.PENDING
                and job.handoff_id is not None
                and not job.cancel_pending
                and job.execution_state is not ExecutionState.CANCELLED
                and job.delivery_state not in {
                    DeliveryState.CANCELLED, DeliveryState.EXPIRED,
                    DeliveryState.DELIVERED,
                }
            )
        ]

    async def finish(self, job_id: str, result: str) -> VoiceJob:
        """Persist a successful terminal execution and queue voice delivery."""
        async with self._lock:
            current = self._require_job(job_id)
            self._require_live_execution(current, "finish")
            return await self._finish_current_locked(current, result)

    async def finish_voice_result(
        self,
        job_id: str,
        result: str,
        *,
        awaiting_input: bool,
        delivery_ttl_s: int,
    ) -> VoiceJob:
        """Atomically persist a validated structured voice-job result."""
        if not isinstance(awaiting_input, bool):
            raise ValueError("awaiting_input must be a boolean")
        if (isinstance(delivery_ttl_s, bool)
                or not isinstance(delivery_ttl_s, int)
                or not 30 <= delivery_ttl_s <= 3600):
            raise ValueError("delivery_ttl_s must be an integer from 30 to 3600")

        async with self._lock:
            current = self._require_job(job_id)
            self._require_live_execution(current, "finish_voice_result")
            return await self._finish_voice_result_current_locked(
                current,
                result,
                awaiting_input=awaiting_input,
                delivery_ttl_s=delivery_ttl_s,
            )

    async def fail(
        self,
        job_id: str,
        failure: JobFailure | BaseException,
    ) -> VoiceJob:
        """Persist a failed/cancelled terminal execution."""
        async with self._lock:
            current = self._require_job(job_id)
            self._require_live_execution(current, "fail")
            return await self._fail_current_locked(current, failure)

    async def finish_compat(
        self, job_id: str, result: str = "",
    ) -> VoiceJob | None:
        """Idempotently finish a live job for legacy delegation callbacks."""
        async with self._lock:
            self._require_loaded()
            current = self._jobs.get(job_id)
            if (current is None
                    or current.execution_state not in {
                        ExecutionState.ACCEPTED, ExecutionState.RUNNING,
                    }):
                return current
            return await self._finish_current_locked(current, result)

    async def fail_compat(
        self, job_id: str, failure: JobFailure | BaseException,
    ) -> VoiceJob | None:
        """Idempotently fail a live job for legacy delegation callbacks."""
        async with self._lock:
            self._require_loaded()
            current = self._jobs.get(job_id)
            if (current is None
                    or current.execution_state not in {
                        ExecutionState.ACCEPTED, ExecutionState.RUNNING,
                    }):
                return current
            return await self._fail_current_locked(current, failure)

    async def request_cancel(self, job_id: str, *, actor: Any) -> CancelResult:
        """Authorize creator cancellation without racing playback start."""
        async with self._lock:
            current = self._require_job(job_id)
            self._authorize_actor(current, actor)

            if current.delivery_state in {
                DeliveryState.PLAYING, DeliveryState.DELIVERED,
            }:
                return CancelResult("too_late")
            if current.delivery_state in {
                DeliveryState.CANCELLED, DeliveryState.EXPIRED,
            } or current.execution_state is ExecutionState.CANCELLED:
                return CancelResult("cancelled")

            if current.delivery_state is DeliveryState.AUTHORIZED:
                updated = replace(current, cancel_pending=True)
                status = "stopping"
            elif current.delivery_state in {
                DeliveryState.READY, DeliveryState.CLAIMED,
            }:
                updated = replace(
                    current,
                    delivery_state=DeliveryState.CANCELLED,
                    delivery_attempt_id=None,
                    lease_until=None,
                    cancel_pending=False,
                )
                status = "cancelled"
            elif current.execution_state in {
                ExecutionState.ACCEPTED, ExecutionState.RUNNING,
            }:
                updated = replace(current, cancel_pending=True)
                status = "stopping"
            else:
                return CancelResult("too_late")

            event = self._cancel_events.get(job_id)
            task = self._tasks.get(job_id)

            def publish_cancel_signal() -> None:
                if event is not None:
                    event.set()
                if task is not None and not task.done():
                    self._arm_force_cancel(job_id, task)

            await self._persist_job_locked(
                updated, after_publish=publish_cancel_signal,
            )
            return CancelResult(status)

    def begin_shutdown(self, reason: str = CASA_SHUTDOWN_REASON) -> None:
        """Declare that this process is stopping gracefully (#671).

        Synchronous, no I/O, waits for nothing, idempotent — so it can be the
        FIRST statement of ``casa_core.main``'s cleanup block, ahead of every
        arm that can terminalize a live row: the bounded ``Agent.aclose()``
        loop, the agent-loop cancels, ``close()`` below, and — the edge that
        actually fires for a delegation — ``asyncio.run``'s final
        ``_cancel_all_tasks``, which runs AFTER ``main()`` has returned.

        That last point is why the reason lives here rather than in a local, a
        parameter, or a cancellation message. This registry instance is reached
        from module state (``tools._specialist_registry.job_registry``) and from
        the arms' own closures, so it is still readable once ``main()``'s frame
        is gone; a ``CancelledError`` message is not, because
        ``_cancel_all_tasks`` cancels with no message at all and a bare
        re-cancel erases one set upstream.
        """
        self._shutdown_reason = reason

    def _cancel_deferred_to_boot(self, kind: str) -> bool:
        """Whether a cancellation terminal belongs to the next boot, not to us.

        The whole policy, in one predicate, and a conjunction on purpose:

        * ``kind == "cancelled"`` is **the reason of the write**. It is what
          lets a real verdict through — a non-cancellation ``JobFailure``
          (a turn-limit abort, a persistence failure) still lands as FAILED
          even mid-stop, and a success never reaches this predicate at all.
          A guard of the shape "the process is stopping, skip the write" would
          eat both.
        * The declaration decides whose row this is. Once a graceful stop is
          declared, the boot reconciliation WILL run and WILL own the row
          (`recover_after_restart`), which is the condition itself rather than
          a proxy that resembles it.

        Deferring is also the truthful record: the job really was running when
        the process died, and it leaves the row in exactly the shape a crash
        leaves it, instead of asserting a creator cancellation that never
        happened. A row the creator HAD cancelled keeps its durable
        ``cancel_pending``, so the boot path still settles it silently as
        "Cancelled by creator" — the right message rather than this method's
        generic one.
        """
        return self._shutdown_reason is not None and kind == "cancelled"

    async def cancel(self, job_id: str) -> VoiceJob | None:
        """Compatibility terminal transition used by legacy delegation code."""
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return None
            if current.execution_state in self._TERMINAL_EXECUTION:
                return current
            if self._cancel_deferred_to_boot("cancelled"):
                logger.info(
                    "job %s cancellation deferred to boot recovery (%s)",
                    job_id[:8], self._shutdown_reason,
                )
                return current
            now = self._now()
            updated = replace(
                current,
                execution_state=ExecutionState.CANCELLED,
                terminal_at=now,
                expires_at=now + self._terminal_result_ttl_seconds(current),
                failure=JobFailure("cancelled", "Delegation cancelled"),
                delivery_state=(
                    DeliveryState.CANCELLED
                    if current.delivery_state is not DeliveryState.NONE
                    else DeliveryState.NONE
                ),
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
            return await self._persist_job_locked(updated)

    async def claim(self, job_id: str, delivery_attempt_id: str) -> VoiceJob:
        if not delivery_attempt_id:
            raise ValueError("delivery_attempt_id must not be empty")
        async with self._lock:
            current = self._require_delivery_cas(
                job_id, "claim", DeliveryState.READY, attempt_id=None,
            )
            updated = replace(
                current,
                delivery_state=DeliveryState.CLAIMED,
                delivery_attempt_id=delivery_attempt_id,
                lease_until=self._now() + self.LEASE_SECONDS,
            )
            return await self._persist_job_locked(updated)

    async def renew(self, job_id: str, delivery_attempt_id: str) -> VoiceJob:
        async with self._lock:
            current = self._require_job(job_id)
            if (current.delivery_state not in self._LEASED_DELIVERY
                    or current.delivery_attempt_id != delivery_attempt_id):
                raise self._transition_error(
                    current, "renew",
                    expected="matching attempt in CLAIMED/AUTHORIZED/PLAYING",
                )
            updated = replace(
                current, lease_until=self._now() + self.LEASE_SECONDS,
            )
            return await self._persist_job_locked(updated)

    async def authorize(self, job_id: str, delivery_attempt_id: str) -> VoiceJob:
        async with self._lock:
            current = self._require_delivery_cas(
                job_id, "authorize", DeliveryState.CLAIMED,
                attempt_id=delivery_attempt_id,
            )
            updated = replace(current, delivery_state=DeliveryState.AUTHORIZED)
            return await self._persist_job_locked(updated)

    async def mark_playing(
        self, job_id: str, delivery_attempt_id: str,
    ) -> VoiceJob:
        async with self._lock:
            current = self._require_delivery_cas(
                job_id, "mark_playing", DeliveryState.AUTHORIZED,
                attempt_id=delivery_attempt_id,
            )
            if current.cancel_pending:
                raise self._transition_error(
                    current,
                    "mark_playing",
                    expected="AUTHORIZED without cancel_pending",
                )
            updated = replace(current, delivery_state=DeliveryState.PLAYING)
            return await self._persist_job_locked(updated)

    async def mark_delivered(
        self, job_id: str, delivery_attempt_id: str,
    ) -> VoiceJob:
        async with self._lock:
            current = self._require_job(job_id)
            # CLAIMED -> DELIVERED is the authenticated HA delivered-LRU
            # acknowledgement path after Casa reoffers a lost delivered ACK.
            # It intentionally records no fake AUTHORIZED/PLAYING transition
            # and never asks HA to replay the announcement.
            if (
                current.delivery_state not in {
                    DeliveryState.CLAIMED,
                    DeliveryState.PLAYING,
                }
                or current.delivery_attempt_id != delivery_attempt_id
            ):
                raise self._transition_error(
                    current,
                    "mark_delivered",
                    expected="matching attempt in CLAIMED/PLAYING",
                )
            updated = replace(
                current,
                delivery_state=DeliveryState.DELIVERED,
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
            return await self._persist_job_locked(updated)

    async def nack(
        self,
        job_id: str,
        delivery_attempt_id: str,
        reason: str,
    ) -> VoiceJob:
        async with self._lock:
            current = self._require_job(job_id)
            if (current.delivery_state not in {
                    DeliveryState.CLAIMED, DeliveryState.AUTHORIZED,
                } or current.delivery_attempt_id != delivery_attempt_id):
                raise self._transition_error(
                    current, "nack",
                    expected="matching attempt in CLAIMED/AUTHORIZED",
                )
            cancelled = (
                current.delivery_state is DeliveryState.AUTHORIZED
                and current.cancel_pending
                and reason == "preempted_before_playback"
            )
            updated = replace(
                current,
                delivery_state=(
                    DeliveryState.CANCELLED if cancelled else DeliveryState.READY
                ),
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
            return await self._persist_job_locked(updated)

    async def expire_due(self) -> list[VoiceJob]:
        """Apply terminal result/delivery TTL without deleting audit records."""
        async with self._lock:
            now = self._now()
            changed: list[VoiceJob] = []
            candidate = dict(self._jobs)
            for job_id, current in self._jobs.items():
                if current.expires_at is None or current.expires_at > now:
                    continue
                if current.delivery_state in {
                    DeliveryState.DELIVERED,
                    DeliveryState.CANCELLED,
                    DeliveryState.EXPIRED,
                }:
                    continue
                updated = replace(
                    current,
                    delivery_state=DeliveryState.EXPIRED,
                    delivery_attempt_id=None,
                    lease_until=None,
                    cancel_pending=False,
                )
                candidate[job_id] = updated
                changed.append(updated)
            if changed:
                await self._commit_snapshot_locked(candidate)
            return changed

    async def expire_leases(self) -> list[VoiceJob]:
        """Recover lapsed delivery attempts independently from result TTL."""
        async with self._lock:
            now = self._now()
            changed: list[VoiceJob] = []
            candidate = dict(self._jobs)
            for job_id, current in self._jobs.items():
                if (current.delivery_state not in self._LEASED_DELIVERY
                        or current.lease_until is None
                        or current.lease_until > now):
                    continue
                cancelled = (
                    current.delivery_state is DeliveryState.AUTHORIZED
                    and current.cancel_pending
                )
                updated = replace(
                    current,
                    delivery_state=(
                        DeliveryState.CANCELLED
                        if cancelled else DeliveryState.READY
                    ),
                    delivery_attempt_id=None,
                    lease_until=None,
                    cancel_pending=False,
                )
                candidate[job_id] = updated
                changed.append(updated)
            if changed:
                await self._commit_snapshot_locked(candidate)
            return changed

    async def recover_after_restart(self) -> list[VoiceJob]:
        """Recover execution and retain delivery attempts for one full lease."""
        async with self._lock:
            self._require_loaded()
            now = self._now()
            recovered_ids = [
                job.id for job in self.all()
                if job.orphan_notification_pending
            ]
            candidate = dict(self._jobs)
            changed = False
            next_sequence = self._delivery_sequence

            for job_id, current in self._jobs.items():
                updated = current
                if current.execution_state in {
                    ExecutionState.ACCEPTED,
                    ExecutionState.RUNNING,
                }:
                    if current.cancel_pending:
                        # #334: the creator already cancelled this job before
                        # the restart (durable flag). Terminalize with the live
                        # cancel paths' exact shape — never as a restart orphan
                        # with a "Lost on restart" failure notice or a fresh
                        # delivery sequence.
                        updated = replace(
                            current,
                            execution_state=ExecutionState.CANCELLED,
                            terminal_at=now,
                            expires_at=(
                                now + self._terminal_result_ttl_seconds(current)
                            ),
                            failure=JobFailure(
                                "cancelled", "Cancelled by creator",
                            ),
                            delivery_state=(
                                DeliveryState.CANCELLED
                                if current.delivery_state
                                is not DeliveryState.NONE
                                else DeliveryState.NONE
                            ),
                            delivery_attempt_id=None,
                            lease_until=None,
                            cancel_pending=False,
                            orphan_notification_pending=False,
                        )
                        candidate[job_id] = updated
                        changed = True
                        continue
                    if current.origin_route_id and current.origin_device_id:
                        next_sequence += 1
                        delivery = DeliveryState.READY
                        sequence = next_sequence
                    else:
                        delivery = DeliveryState.NONE
                        sequence = current.delivery_sequence
                    updated = replace(
                        current,
                        execution_state=ExecutionState.ORPHANED,
                        terminal_at=now,
                        expires_at=(
                            now + self._terminal_result_ttl_seconds(current)
                        ),
                        failure=JobFailure(
                            "restart_orphan", "Lost on restart",
                        ),
                        delivery_state=delivery,
                        delivery_sequence=sequence,
                        delivery_attempt_id=None,
                        lease_until=None,
                        cancel_pending=False,
                        orphan_notification_pending=(
                            current.creator_peer == "telegram"
                        ),
                    )
                    recovered_ids.append(job_id)
                elif current.delivery_state in self._LEASED_DELIVERY:
                    # A restarted coordinator must not immediately steal an
                    # attempt that may still be speaking through the device.
                    updated = replace(
                        current, lease_until=now + self.LEASE_SECONDS,
                    )
                if updated != current:
                    candidate[job_id] = updated
                    changed = True

            if changed:
                await self._commit_snapshot_locked(candidate)

            seen: set[str] = set()
            return [
                self._jobs[job_id]
                for job_id in recovered_ids
                if job_id in self._jobs and not (job_id in seen or seen.add(job_id))
            ]

    async def ack_orphan_notification(self, job_id: str) -> VoiceJob:
        """Durably acknowledge a restart-orphan Telegram notification."""
        async with self._lock:
            current = self._require_job(job_id)
            if not current.orphan_notification_pending:
                return current
            return await self._persist_job_locked(replace(
                current, orphan_notification_pending=False,
            ))

    async def close(self) -> None:
        """Cancel and drain execution, cancellation, and retry ownership."""
        timers = list(self._cancel_timers.values())
        self._cancel_timers.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)

        tasks = list(self._tasks.values())
        for event in self._cancel_events.values():
            event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        reconciliations = list(self._reconciliation_tasks.values())
        for task in reconciliations:
            if not task.done():
                task.cancel()
        if reconciliations:
            await asyncio.gather(*reconciliations, return_exceptions=True)

    # -- persistence -----------------------------------------------------

    def _read_json(self, path: str) -> Any:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _decode_snapshot(self, raw: Any) -> dict[str, VoiceJob]:
        if not isinstance(raw, list):
            raise JobRegistryError(f"job snapshot {self._path!r} is not a JSON array")
        jobs: dict[str, VoiceJob] = {}
        for row in raw:
            job = self._decode_job(row)
            if job.id in jobs:
                raise JobRegistryError(f"duplicate job id {job.id!r} in snapshot")
            jobs[job.id] = job
        return jobs

    @staticmethod
    def _decode_job(row: Any) -> VoiceJob:
        if not isinstance(row, dict):
            raise JobRegistryError("job snapshot row is not an object")
        failure_raw = row.get("failure")
        failure = None
        if failure_raw is not None:
            if not isinstance(failure_raw, dict):
                raise JobRegistryError("job failure is not an object")
            failure = JobFailure(
                kind=str(failure_raw["kind"]),
                message=str(failure_raw["message"]),
            )
        try:
            return VoiceJob(
                id=str(row["id"]),
                parent_job_id=JobRegistry._optional_str(row.get("parent_job_id")),
                creating_speaker=JobRegistry._decode_speaker(row, "creating_speaker"),
                executing_speaker=JobRegistry._decode_speaker(row, "executing_speaker"),
                creating_role=str(row["creating_role"]),
                specialist_role=str(row["specialist_role"]),
                specialist_display_name=str(row["specialist_display_name"]),
                creator_peer=str(row["creator_peer"]),
                creator_user_id=JobRegistry._optional_str(row.get("creator_user_id")),
                scope_id=str(row["scope_id"]),
                origin_route_id=JobRegistry._optional_str(row.get("origin_route_id")),
                origin_device_id=JobRegistry._optional_str(row.get("origin_device_id")),
                task=str(row["task"]),
                context=str(row["context"]),
                created_at=float(row["created_at"]),
                started_at=JobRegistry._optional_float(row.get("started_at")),
                terminal_at=JobRegistry._optional_float(row.get("terminal_at")),
                expires_at=JobRegistry._optional_float(row.get("expires_at")),
                execution_state=ExecutionState(row["execution_state"]),
                delivery_state=DeliveryState(row["delivery_state"]),
                result=(None if row.get("result") is None else str(row["result"])),
                failure=failure,
                awaiting_input=bool(row["awaiting_input"]),
                continuable_until=JobRegistry._optional_float(
                    row.get("continuable_until")),
                delivery_sequence=int(row["delivery_sequence"]),
                delivery_attempt_id=JobRegistry._optional_str(
                    row.get("delivery_attempt_id")),
                lease_until=JobRegistry._optional_float(row.get("lease_until")),
                cancel_pending=bool(row["cancel_pending"]),
                orphan_notification_pending=bool(
                    row.get("orphan_notification_pending", False)
                ),
                prompted_delivery=bool(row.get("prompted_delivery", False)),
                job_control_id=JobRegistry._optional_str(
                    row.get("job_control_id")
                ),
                handoff_id=JobRegistry._optional_str(row.get("handoff_id")),
                handoff_state=HandoffState(row.get("handoff_state", "NONE")),
                delivery_modality=row.get("delivery_modality"),
                # #485: exact True only. A legacy row has no key, and a
                # malformed one must not mint eligibility — bool("false")
                # is True, which is precisely the coercion to avoid here.
                scheduled_delivery=row.get("scheduled_delivery") is True,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise JobRegistryError(f"invalid job snapshot row: {exc}") from exc

    @staticmethod
    def _decode_speaker(row: dict, key: str) -> SpeakerProvenance:
        """Decode one typed speaker snapshot from a persisted job row.

        The key is absent only for a genuinely pre-Task-12 legacy record —
        that decodes to an explicit unattributed system identity, NEVER a
        persona fabricated from the row's bare creating_role/specialist_role
        strings."""
        value = row.get(key)
        if value is None:
            return SpeakerProvenance(speaker_kind="system")
        return provenance_from_mapping(value)

    @staticmethod
    def _encode_job(job: VoiceJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "parent_job_id": job.parent_job_id,
            "creating_speaker": provenance_mapping(job.creating_speaker),
            "executing_speaker": provenance_mapping(job.executing_speaker),
            "creating_role": job.creating_role,
            "specialist_role": job.specialist_role,
            "specialist_display_name": job.specialist_display_name,
            "creator_peer": job.creator_peer,
            "creator_user_id": job.creator_user_id,
            "scope_id": job.scope_id,
            "origin_route_id": job.origin_route_id,
            "origin_device_id": job.origin_device_id,
            "task": job.task,
            "context": job.context,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "terminal_at": job.terminal_at,
            "expires_at": job.expires_at,
            "execution_state": job.execution_state.value,
            "delivery_state": job.delivery_state.value,
            "result": job.result,
            "failure": (
                None if job.failure is None else {
                    "kind": job.failure.kind,
                    "message": job.failure.message,
                }
            ),
            "awaiting_input": job.awaiting_input,
            "continuable_until": job.continuable_until,
            "delivery_sequence": job.delivery_sequence,
            "delivery_attempt_id": job.delivery_attempt_id,
            "lease_until": job.lease_until,
            "cancel_pending": job.cancel_pending,
            "orphan_notification_pending": job.orphan_notification_pending,
            "prompted_delivery": job.prompted_delivery,
            "job_control_id": job.job_control_id,
            "handoff_id": job.handoff_id,
            "handoff_state": job.handoff_state.value,
            "delivery_modality": job.delivery_modality,
            "scheduled_delivery": job.scheduled_delivery,
        }

    async def _write_snapshot_locked(
        self, jobs: Mapping[str, VoiceJob],
    ) -> None:
        snapshot = [
            self._encode_job(job)
            for job in sorted(
                jobs.values(),
                key=lambda item: (item.delivery_sequence, item.created_at, item.id),
            )
        ]
        await asyncio.to_thread(
            atomic_write_json, self._path, snapshot, indent=2,
            mode=PRIVATE,
        )

    async def _commit_snapshot_locked(
        self,
        jobs: dict[str, VoiceJob],
        *,
        after_publish: Callable[[], None] | None = None,
    ) -> None:
        """Persist and publish one candidate without a cancellation gap."""
        async def commit() -> None:
            await self._write_snapshot_locked(jobs)
            self._jobs = jobs
            self._delivery_sequence = max(
                (job.delivery_sequence for job in jobs.values()), default=0,
            )
            if after_publish is not None:
                after_publish()
            self._signal_terminal_waiters()

        await self._finish_atomic_commit(commit())

    @staticmethod
    async def _finish_atomic_commit(operation: Awaitable[None]) -> None:
        """Defer caller cancellation until a disk/publication commit finishes."""
        task = asyncio.ensure_future(operation)
        cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc

        # A persistence error wins over a simultaneous cancellation and is
        # always retrieved from the inner task. Publication only happens after
        # the blocking writer returned successfully.
        task.result()
        if cancelled is not None:
            raise cancelled

    # -- transition helpers --------------------------------------------

    @staticmethod
    def _has_trusted_voice_delivery_provenance(job: VoiceJob) -> bool:
        """Return whether a job was created from a deliverable voice route."""
        return (
            job.creator_peer == "voice"
            and isinstance(job.origin_route_id, str)
            and bool(job.origin_route_id.strip())
            and isinstance(job.origin_device_id, str)
            and bool(job.origin_device_id.strip())
        )

    def _terminal_result_ttl_seconds(self, job: VoiceJob) -> float:
        if self._has_trusted_voice_delivery_provenance(job):
            return self._voice_result_ttl_seconds
        return self.RESULT_TTL_SECONDS

    async def _finish_current_locked(
        self, current: VoiceJob, result: str,
    ) -> VoiceJob:
        now = self._now()
        if current.cancel_pending:
            updated = replace(
                current,
                execution_state=ExecutionState.CANCELLED,
                terminal_at=now,
                expires_at=now + self._terminal_result_ttl_seconds(current),
                failure=JobFailure("cancelled", "Cancelled by creator"),
                delivery_state=(
                    DeliveryState.CANCELLED
                    if current.delivery_state is not DeliveryState.NONE
                    else DeliveryState.NONE
                ),
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
        else:
            delivery, sequence = self._terminal_delivery(current)
            updated = replace(
                current,
                execution_state=ExecutionState.SUCCEEDED,
                terminal_at=now,
                expires_at=now + self._terminal_result_ttl_seconds(current),
                result=str(result),
                failure=None,
                delivery_state=delivery,
                delivery_sequence=sequence,
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
        return await self._persist_job_locked(updated)

    async def _finish_voice_result_current_locked(
        self,
        current: VoiceJob,
        result: str,
        *,
        awaiting_input: bool,
        delivery_ttl_s: int,
    ) -> VoiceJob:
        now = self._now()
        if current.cancel_pending:
            updated = replace(
                current,
                execution_state=ExecutionState.CANCELLED,
                terminal_at=now,
                expires_at=now + self._terminal_result_ttl_seconds(current),
                failure=JobFailure("cancelled", "Cancelled by creator"),
                awaiting_input=False,
                continuable_until=None,
                delivery_state=(
                    DeliveryState.CANCELLED
                    if current.delivery_state is not DeliveryState.NONE
                    else DeliveryState.NONE
                ),
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
        else:
            expires_at = now + min(
                float(delivery_ttl_s),
                self._terminal_result_ttl_seconds(current),
            )
            delivery, sequence = self._terminal_delivery(current)
            updated = replace(
                current,
                execution_state=ExecutionState.SUCCEEDED,
                terminal_at=now,
                expires_at=expires_at,
                result=str(result),
                failure=None,
                awaiting_input=awaiting_input,
                continuable_until=(expires_at if awaiting_input else None),
                delivery_state=delivery,
                delivery_sequence=sequence,
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
        return await self._persist_job_locked(updated)

    async def _fail_current_locked(
        self,
        current: VoiceJob,
        failure: JobFailure | BaseException,
    ) -> VoiceJob:
        envelope = self._failure_envelope(failure)
        cancelled = (
            isinstance(failure, asyncio.CancelledError)
            or envelope.kind == "cancelled"
        )
        if cancelled and self._cancel_deferred_to_boot("cancelled"):
            # #671: the second of the two functions every cancellation arm
            # reaches. The voice lifecycle's teardown writes its cancellation
            # as `fail_compat(JobFailure("cancelled", ...))` — a different
            # function and a different message string from `cancel` above — so
            # guarding only `cancel` would leave voice jobs silently cancelled
            # while looking defended.
            logger.info(
                "job %s cancellation deferred to boot recovery (%s)",
                current.id[:8], self._shutdown_reason,
            )
            return current
        now = self._now()
        if cancelled or current.cancel_pending:
            state = ExecutionState.CANCELLED
            delivery = (
                DeliveryState.CANCELLED
                if current.delivery_state is not DeliveryState.NONE
                else DeliveryState.NONE
            )
            sequence = current.delivery_sequence
        else:
            state = ExecutionState.FAILED
            delivery, sequence = self._terminal_delivery(current)
        updated = replace(
            current,
            execution_state=state,
            terminal_at=now,
            expires_at=now + self._terminal_result_ttl_seconds(current),
            failure=envelope,
            delivery_state=delivery,
            delivery_sequence=sequence,
            delivery_attempt_id=None,
            lease_until=None,
            cancel_pending=False,
        )
        return await self._persist_job_locked(updated)

    async def _persist_job_locked(
        self,
        updated: VoiceJob,
        *,
        after_publish: Callable[[], None] | None = None,
    ) -> VoiceJob:
        candidate = self._with_job(updated)
        await self._commit_snapshot_locked(
            candidate, after_publish=after_publish,
        )
        return updated

    def _with_job(self, updated: VoiceJob) -> dict[str, VoiceJob]:
        candidate = dict(self._jobs)
        candidate[updated.id] = updated
        return candidate

    def _require_route_capacity(
        self,
        job: VoiceJob,
        *,
        max_active_ready_per_route: int | None,
    ) -> None:
        if max_active_ready_per_route is None:
            return
        if max_active_ready_per_route <= 0:
            raise ValueError("max_active_ready_per_route must be positive")
        route_id = job.origin_route_id
        if route_id is None:
            return
        active_or_ready = sum(
            1
            for current in self._jobs.values()
            if current.origin_route_id == route_id
            and (
                current.execution_state in {
                    ExecutionState.ACCEPTED,
                    ExecutionState.RUNNING,
                }
                or current.delivery_state in {
                    DeliveryState.READY,
                    DeliveryState.CLAIMED,
                    DeliveryState.AUTHORIZED,
                    DeliveryState.PLAYING,
                }
            )
        )
        if active_or_ready >= max_active_ready_per_route:
            raise JobRouteCapacityError(
                f"voice route {route_id!r} already has "
                f"{max_active_ready_per_route} live jobs"
            )

    def _require_job(self, job_id: str) -> VoiceJob:
        self._require_loaded()
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise JobTransitionError(f"unknown job {job_id!r}") from exc

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise JobRegistryError("JobRegistry.load() must be awaited first")

    def _require_live_execution(self, job: VoiceJob, action: str) -> None:
        if job.execution_state not in {
            ExecutionState.ACCEPTED, ExecutionState.RUNNING,
        }:
            raise self._transition_error(
                job, action, expected="execution=ACCEPTED/RUNNING",
            )

    def _require_delivery_cas(
        self,
        job_id: str,
        action: str,
        state: DeliveryState,
        *,
        attempt_id: str | None,
    ) -> VoiceJob:
        current = self._require_job(job_id)
        if current.delivery_state is not state:
            raise self._transition_error(
                current, action, expected=f"delivery={state.value}",
            )
        if attempt_id is None:
            if current.delivery_attempt_id is not None:
                raise self._transition_error(
                    current, action, expected="no persisted delivery attempt",
                )
        elif current.delivery_attempt_id != attempt_id:
            raise self._transition_error(
                current, action, expected=f"attempt={attempt_id!r}",
            )
        return current

    @staticmethod
    def _transition_error(
        job: VoiceJob, action: str, *, expected: str,
    ) -> JobTransitionError:
        return JobTransitionError(
            f"{action} rejected for {job.id!r}: expected {expected}; "
            f"found execution={job.execution_state.value}, "
            f"delivery={job.delivery_state.value}, "
            f"attempt={job.delivery_attempt_id!r}",
        )

    def _terminal_delivery(
        self, job: VoiceJob,
    ) -> tuple[DeliveryState, int]:
        if not (job.origin_route_id and job.origin_device_id):
            return DeliveryState.NONE, job.delivery_sequence
        return DeliveryState.READY, self._delivery_sequence + 1

    def _task_done(self, job_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(job_id) is not task:
            return
        self._tasks.pop(job_id, None)
        self._cancel_events.pop(job_id, None)
        timer = self._cancel_timers.pop(job_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        permit = self._permits.pop(job_id, None)
        if permit is not None:
            try:
                permit.release()
            except Exception:  # noqa: BLE001 — task cleanup must finish
                logger.warning("job %s permit release failed", job_id, exc_info=True)
        waiters = self._runtime_release_waiters.pop(job_id, set())
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _signal_terminal_waiters(self) -> None:
        for job_id, waiters in list(self._terminal_waiters.items()):
            job = self._jobs.get(job_id)
            if (job is None
                    or job.execution_state not in self._TERMINAL_EXECUTION):
                continue
            self._terminal_waiters.pop(job_id, None)
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(job)

    def _arm_force_cancel(self, job_id: str, task: asyncio.Task) -> None:
        existing = self._cancel_timers.get(job_id)
        if existing is not None and not existing.done():
            return

        async def _cancel_after_grace() -> None:
            await asyncio.sleep(self.CANCEL_GRACE_SECONDS)
            if not task.done():
                task.cancel()

        timer = asyncio.create_task(_cancel_after_grace())
        self._cancel_timers[job_id] = timer

    def _authorize_actor(self, job: VoiceJob, actor: Any) -> None:
        peer = self._actor_value(actor, "creator_peer", "peer")
        scope = self._actor_value(actor, "scope_id", "scope")
        user_id = self._actor_value(actor, "creator_user_id", "user_id")
        control_id = self._actor_value(
            actor, "job_control_id", "control_id",
        )
        if peer != job.creator_peer:
            raise JobAuthorizationError(f"actor does not own job {job.id!r}")
        if user_id != job.creator_user_id:
            raise JobAuthorizationError(f"actor does not own job {job.id!r}")
        if job.job_control_id is not None:
            if control_id != job.job_control_id:
                raise JobAuthorizationError(
                    f"actor does not own job {job.id!r}"
                )
        elif scope != job.scope_id:
            raise JobAuthorizationError(f"actor does not own job {job.id!r}")

    @staticmethod
    def _actor_value(actor: Any, primary: str, fallback: str) -> Any:
        if isinstance(actor, Mapping):
            return actor.get(primary, actor.get(fallback))
        return getattr(actor, primary, getattr(actor, fallback, None))

    @staticmethod
    def _failure_envelope(failure: JobFailure | BaseException) -> JobFailure:
        if isinstance(failure, JobFailure):
            return failure
        if isinstance(failure, asyncio.CancelledError):
            return JobFailure("cancelled", "Delegation cancelled")
        kind = type(failure).__name__
        return JobFailure(kind=kind, message=str(failure))

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        return None if value is None else float(value)


__all__ = [
    "CancelResult",
    "DeliveryState",
    "ExecutionState",
    "JobAuthorizationError",
    "JobFailure",
    "JobRegistry",
    "JobRegistryError",
    "JobRouteCapacityError",
    "JobTransitionError",
    "VoiceJob",
]
