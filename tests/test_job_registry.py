"""Durable specialist voice-job state machine tests."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from job_registry import (
    DeliveryState,
    ExecutionState,
    HandoffState,
    JobAuthorizationError,
    JobFailure,
    JobRegistry,
    JobTransitionError,
    VoiceJob,
)
from personality_types import SpeakerProvenance


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

SYSTEM_SPEAKER = SpeakerProvenance(speaker_kind="system")


def make_job(**changes):
    base = VoiceJob(
        id="job-1", parent_job_id=None,
        creating_speaker=SYSTEM_SPEAKER, executing_speaker=SYSTEM_SPEAKER,
        creating_role="concierge", specialist_role="mtg-judge",
        specialist_display_name="Judge",
        creator_peer="voice_speaker", creator_user_id=None,
        scope_id="scope-1", origin_route_id="entry-1",
        origin_device_id="device-kitchen",
        task="Does this target?", context="",
        created_at=100.0, started_at=None, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.ACCEPTED,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None,
        cancel_pending=False,
    )
    return replace(base, **changes)


def actor_for_job():
    return {
        "creator_peer": "voice_speaker",
        "creator_user_id": None,
        "scope_id": "scope-1",
    }


async def loaded_registry(tmp_path, job=None, *, now=100.0):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: now,
    )
    await registry.load()
    if job is not None:
        await registry.create(job)
    return registry


async def ready_claimed_authorized_registry(tmp_path, *, now=100.0):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            started_at=101.0,
            terminal_at=102.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            result="It targets.",
            delivery_sequence=1,
        ),
        now=now,
    )
    await registry.claim("job-1", "attempt-1")
    await registry.authorize("job-1", "attempt-1")
    return registry


async def authorized_cancel_pending_registry(
    tmp_path, *, lease_until, now,
):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            started_at=80.0,
            terminal_at=85.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.AUTHORIZED,
            result="It targets.",
            delivery_sequence=1,
            delivery_attempt_id="attempt-1",
            lease_until=lease_until,
            cancel_pending=True,
        ),
        now=now,
    )
    return registry


async def playing_registry(tmp_path, *, now=100.0):
    registry = await ready_claimed_authorized_registry(tmp_path, now=now)
    await registry.mark_playing("job-1", "attempt-1")
    return registry


async def test_create_is_atomic_and_survives_reload(tmp_path):
    registry = JobRegistry(tmp_path / "jobs.json")
    await registry.load()
    await registry.create(make_job())
    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    assert reloaded.get("job-1") == make_job()


async def test_handoff_latch_round_trips_and_receipt_is_idempotent(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())

    pending = await registry.mark_handoff_pending("job-1", "handoff-1")
    assert pending.handoff_id == "handoff-1"
    assert pending.handoff_state is HandoffState.PENDING

    received = await registry.acknowledge_handoff("job-1", "handoff-1")
    assert received.handoff_state is HandoffState.RECEIVED
    assert await registry.acknowledge_handoff("job-1", "handoff-1") == received
    with pytest.raises(JobTransitionError):
        await registry.acknowledge_handoff("job-1", "other-handoff")

    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    assert reloaded.get("job-1") == received


async def test_legacy_snapshot_defaults_handoff_latch_to_none(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    snapshot = json.loads((tmp_path / "jobs.json").read_text())
    snapshot[0].pop("handoff_id", None)
    snapshot[0].pop("handoff_state", None)
    (tmp_path / "jobs.json").write_text(json.dumps(snapshot))

    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    job = reloaded.get("job-1")
    assert job.handoff_id is None
    assert job.handoff_state is HandoffState.NONE


async def test_pending_handoff_survives_orphan_recovery_but_cancelled_does_not(
    tmp_path,
):
    registry = await loaded_registry(tmp_path, make_job())
    await registry.mark_handoff_pending("job-1", "handoff-1")

    await registry.recover_after_restart()
    assert [job.id for job in registry.pending_handoffs_for_route("entry-1")] == [
        "job-1",
    ]
    assert registry.get("job-1").execution_state is ExecutionState.ORPHANED

    await registry.create(make_job(id="job-2"))
    await registry.mark_handoff_pending("job-2", "handoff-2")
    await registry.request_cancel("job-2", actor=actor_for_job())
    assert [job.id for job in registry.pending_handoffs_for_route("entry-1")] == [
        "job-1",
    ]


async def test_multiple_terminal_jobs_survive_reload_in_delivery_order(tmp_path):
    registry = await loaded_registry(tmp_path)
    await registry.create(make_job(id="job-1", created_at=101.0))
    await registry.create(make_job(id="job-2", created_at=102.0))

    await registry.finish("job-1", "first answer")
    await registry.fail("job-2", JobFailure("specialist_error", "second failed"))

    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    assert [job.id for job in reloaded.all()] == ["job-1", "job-2"]
    assert reloaded.get("job-1").execution_state is ExecutionState.SUCCEEDED
    assert reloaded.get("job-1").result == "first answer"
    assert reloaded.get("job-2").execution_state is ExecutionState.FAILED
    assert reloaded.get("job-2").failure == JobFailure(
        "specialist_error", "second failed",
    )


async def test_finish_voice_result_persists_clarification_contract_and_ttl(
    tmp_path,
):
    registry = await loaded_registry(tmp_path, make_job(), now=200.0)
    envelope = json.dumps({
        "status": "needs_clarification",
        "spoken_summary": "Which card do you mean?",
        "clarification": "Which card do you mean?",
        "delivery_ttl_s": 600,
    })

    finished = await registry.finish_voice_result(
        "job-1", envelope, awaiting_input=True, delivery_ttl_s=600,
    )

    assert finished.execution_state is ExecutionState.SUCCEEDED
    assert finished.delivery_state is DeliveryState.READY
    assert finished.result == envelope
    assert finished.awaiting_input is True
    assert finished.terminal_at == 200.0
    assert finished.expires_at == 800.0
    assert finished.continuable_until == 800.0

    reloaded = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 200.0,
    )
    await reloaded.load()
    assert reloaded.get("job-1") == finished


async def test_finish_voice_result_answer_has_ttl_without_continuation(tmp_path):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    finished = await registry.finish_voice_result(
        "job-1", '{"status":"answered"}',
        awaiting_input=False, delivery_ttl_s=900,
    )
    assert finished.expires_at == 1200.0
    assert finished.awaiting_input is False
    assert finished.continuable_until is None


async def test_finish_voice_result_cancel_pending_wins(tmp_path):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    result = await registry.request_cancel("job-1", actor=actor_for_job())
    assert result.status == "stopping"

    finished = await registry.finish_voice_result(
        "job-1", '{"status":"answered"}',
        awaiting_input=True, delivery_ttl_s=900,
    )

    assert finished.execution_state is ExecutionState.CANCELLED
    assert finished.result is None
    assert finished.awaiting_input is False
    assert finished.continuable_until is None
    assert finished.failure == JobFailure("cancelled", "Cancelled by creator")


async def test_finish_voice_result_write_failure_is_atomic(tmp_path, monkeypatch):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    before = registry.get("job-1")

    def fail_write(*_args, **_kwargs):
        raise OSError("voice result disk full")

    monkeypatch.setattr("job_registry.atomic_write_json", fail_write)
    with pytest.raises(OSError, match="voice result disk full"):
        await registry.finish_voice_result(
            "job-1", '{"status":"answered"}',
            awaiting_input=False, delivery_ttl_s=900,
        )

    assert registry.get("job-1") == before
    reloaded = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 300.0,
    )
    await reloaded.load()
    assert reloaded.get("job-1") == before


@pytest.mark.parametrize("ttl", [True, 29, 3601])
async def test_finish_voice_result_rejects_invalid_ttl_without_mutation(
    tmp_path, ttl,
):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    with pytest.raises(ValueError, match="delivery_ttl_s"):
        await registry.finish_voice_result(
            "job-1", "{}", awaiting_input=False, delivery_ttl_s=ttl,
        )
    assert registry.get("job-1") == make_job()


async def test_finish_voice_result_rejects_non_boolean_awaiting_without_mutation(
    tmp_path,
):
    registry = await loaded_registry(tmp_path, make_job(), now=300.0)
    with pytest.raises(ValueError, match="awaiting_input"):
        await registry.finish_voice_result(
            "job-1", "{}", awaiting_input="yes", delivery_ttl_s=900,
        )
    assert registry.get("job-1") == make_job()


async def test_cancel_after_replace_waits_for_memory_publication_under_lock(
    tmp_path, monkeypatch,
):
    import job_registry as job_registry_module

    registry = await loaded_registry(tmp_path)
    replaced = threading.Event()
    release_writer = threading.Event()
    real_write = job_registry_module.atomic_write_json

    def blocked_after_replace(*args, **kwargs):
        real_write(*args, **kwargs)
        replaced.set()
        assert release_writer.wait(timeout=5)

    monkeypatch.setattr(job_registry_module, "atomic_write_json", blocked_after_replace)
    mutation = asyncio.create_task(registry.create(make_job()))
    assert await asyncio.to_thread(replaced.wait, 5)
    mutation.cancel()

    observed = {}
    entered = asyncio.Event()

    async def observe_next_writer_view():
        async with registry._lock:
            observed["memory"] = registry.get("job-1") is not None
            observed["disk"] = [
                row["id"]
                for row in json.loads(
                    (tmp_path / "jobs.json").read_text(encoding="utf-8")
                )
            ]
            entered.set()

    observer = asyncio.create_task(observe_next_writer_view())
    try:
        await asyncio.sleep(0)
        assert not entered.is_set(), "lock released after disk replace before publication"
    finally:
        release_writer.set()

    with pytest.raises(asyncio.CancelledError):
        await mutation
    await observer
    assert observed == {"memory": True, "disk": ["job-1"]}


async def test_invalid_compare_and_set_does_not_mutate(tmp_path):
    """Pins INV-JOB-003 (registry layer; the coordinator layer is test_stale_claim_is_denied_without_mutation). Red case demonstrated: neutering BOTH the state and attempt-id comparisons in _require_delivery_cas fails this test — each alone is backstopped by the other."""
    registry = await loaded_registry(tmp_path, make_job())
    with pytest.raises(JobTransitionError):
        await registry.mark_playing("job-1", "attempt-1")
    assert registry.get("job-1").delivery_state is DeliveryState.NONE


async def test_authorized_cancel_waits_for_preplay_outcome(tmp_path):
    registry = await ready_claimed_authorized_registry(tmp_path)
    result = await registry.request_cancel("job-1", actor=actor_for_job())
    assert result.status == "stopping"
    assert registry.get("job-1").cancel_pending is True
    await registry.nack("job-1", "attempt-1", "preempted_before_playback")
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.CANCELLED
    assert job.cancel_pending is False


async def test_anonymous_creator_identity_is_exact_not_a_wildcard(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    with pytest.raises(JobAuthorizationError):
        await registry.request_cancel("job-1", actor={
            "creator_peer": "voice_speaker",
            "creator_user_id": "different-user",
            "scope_id": "scope-1",
        })
    assert registry.get("job-1").cancel_pending is False


async def test_cancel_during_persist_still_signals_and_reaps_owned_task(
    tmp_path, monkeypatch,
):
    import job_registry as job_registry_module

    registry = await loaded_registry(tmp_path, make_job())
    registry.CANCEL_GRACE_SECONDS = 0.01
    worker_cancelled = asyncio.Event()

    class PermitProbe:
        def __init__(self):
            self.releases = 0

        def release(self):
            self.releases += 1

    async def work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            worker_cancelled.set()
            raise

    permit = PermitProbe()
    worker = asyncio.create_task(work())
    cancel_event = await registry.bind_task("job-1", worker, permit=permit)

    replaced = threading.Event()
    release_writer = threading.Event()
    real_write = job_registry_module.atomic_write_json

    def blocked_after_replace(*args, **kwargs):
        real_write(*args, **kwargs)
        replaced.set()
        assert release_writer.wait(timeout=5)

    monkeypatch.setattr(job_registry_module, "atomic_write_json", blocked_after_replace)
    request = asyncio.create_task(
        registry.request_cancel("job-1", actor=actor_for_job()),
    )
    try:
        assert await asyncio.to_thread(replaced.wait, 5)
        request.cancel()
        release_writer.set()

        with pytest.raises(asyncio.CancelledError):
            await request
        assert registry.get("job-1").cancel_pending is True
        assert cancel_event.is_set()

        reloaded = JobRegistry(
            tmp_path / "jobs.json",
        )
        await reloaded.load()
        assert reloaded.get("job-1").cancel_pending is True

        await asyncio.wait_for(worker_cancelled.wait(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await worker
        await asyncio.sleep(0)
        assert permit.releases == 1
    finally:
        release_writer.set()
        if not request.done():
            request.cancel()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(request, worker, return_exceptions=True)


async def test_cancel_pending_authorized_job_rejects_playback_start(tmp_path):
    registry = await ready_claimed_authorized_registry(tmp_path)
    await registry.request_cancel("job-1", actor=actor_for_job())
    with pytest.raises(JobTransitionError):
        await registry.mark_playing("job-1", "attempt-1")
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.AUTHORIZED
    assert job.cancel_pending is True


async def test_cancel_pending_authorized_lease_lapse_cancels_not_requeues(tmp_path):
    registry = await authorized_cancel_pending_registry(
        tmp_path, lease_until=90.0, now=100.0,
    )
    await registry.expire_leases()
    assert registry.get("job-1").delivery_state is DeliveryState.CANCELLED


async def test_playing_cancel_is_too_late(tmp_path):
    registry = await playing_registry(tmp_path)
    result = await registry.request_cancel("job-1", actor=actor_for_job())
    assert result.status == "too_late"
    assert registry.get("job-1").delivery_state is DeliveryState.PLAYING


async def test_delivery_compare_and_set_happy_path(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            terminal_at=100.0,
            result="answer",
            delivery_sequence=1,
        ),
    )
    await registry.claim("job-1", "attempt-1")
    assert registry.get("job-1").lease_until == 115.0
    await registry.authorize("job-1", "attempt-1")
    await registry.mark_playing("job-1", "attempt-1")
    await registry.mark_delivered("job-1", "attempt-1")
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.DELIVERED
    assert job.delivery_attempt_id is None
    assert job.lease_until is None


async def test_nack_without_cancel_requeues_with_fresh_attempt_required(tmp_path):
    registry = await ready_claimed_authorized_registry(tmp_path)
    await registry.nack("job-1", "attempt-1", "preempted_before_playback")
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.READY
    assert job.delivery_attempt_id is None
    assert job.lease_until is None


async def test_playing_lease_lapse_requeues_for_at_least_once_delivery(tmp_path):
    """Pins INV-JOB-004. Red case demonstrated: expiring a lapsed lease to DELIVERED instead of READY fails this test."""
    registry = await loaded_registry(
        tmp_path,
        make_job(
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.PLAYING,
            terminal_at=80.0,
            result="answer",
            delivery_sequence=1,
            delivery_attempt_id="attempt-1",
            lease_until=90.0,
        ),
        now=100.0,
    )
    await registry.expire_leases()
    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.READY
    assert job.delivery_attempt_id is None


async def test_failed_snapshot_write_does_not_publish_memory_mutation(
    tmp_path, monkeypatch,
):
    """Pins INV-STATE-004. Red case demonstrated: publishing the in-memory jobs before the snapshot write fails this test. Also pins INV-JOB-001 (same property, stated from the jobs doc's side)."""
    import job_registry as job_registry_module

    registry = await loaded_registry(tmp_path, make_job())
    real_write = job_registry_module.atomic_write_json

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("job_registry.atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk full"):
        await registry.finish("job-1", "answer")
    assert registry.get("job-1") == make_job()

    monkeypatch.setattr("job_registry.atomic_write_json", real_write)
    finished = await registry.finish("job-1", "answer")
    assert finished.delivery_sequence == 1


async def test_bind_task_releases_permit_only_when_task_really_ends(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    gate = asyncio.Event()

    class PermitProbe:
        def __init__(self):
            self.releases = 0

        def release(self):
            self.releases += 1

    async def work():
        await gate.wait()

    permit = PermitProbe()
    task = asyncio.create_task(work())
    await registry.bind_task("job-1", task, permit=permit)
    assert registry.get("job-1").execution_state is ExecutionState.RUNNING
    assert permit.releases == 0
    gate.set()
    await task
    await asyncio.sleep(0)
    assert permit.releases == 1


async def test_expire_due_applies_result_delivery_ttl(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            terminal_at=80.0,
            expires_at=90.0,
            result="answer",
            delivery_sequence=1,
        ),
        now=100.0,
    )
    await registry.expire_due()
    assert registry.get("job-1").delivery_state is DeliveryState.EXPIRED


async def test_snapshot_without_control_identity_keeps_legacy_scope_auth(tmp_path):
    path = tmp_path / "jobs.json"
    registry = JobRegistry(path)
    await registry.load()
    await registry.create(make_job())
    await registry.close()
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[0].pop("job_control_id", None)
    path.write_text(json.dumps(rows), encoding="utf-8")

    reloaded = JobRegistry(path)
    await reloaded.load()
    assert reloaded.get("job-1").job_control_id is None
    with pytest.raises(JobAuthorizationError):
        await reloaded.request_cancel("job-1", actor={
            "creator_peer": "voice_speaker",
            "creator_user_id": None,
            "scope_id": "other-scope",
            "job_control_id": "entry-1",
        })
    result = await reloaded.request_cancel("job-1", actor=actor_for_job())
    assert result.status == "stopping"


async def test_atomic_replace_failure_preserves_prior_snapshot_and_memory(
    tmp_path, monkeypatch,
):
    import atomic_io

    jobs_path = tmp_path / "jobs.json"
    registry = JobRegistry(jobs_path)
    await registry.load()
    await registry.create(make_job())
    prior = json.loads(jobs_path.read_text(encoding="utf-8"))

    def fail_replace(*_args, **_kwargs):
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await registry.create(replace(make_job(), id="job-2"))
    assert json.loads(jobs_path.read_text(encoding="utf-8")) == prior
    assert registry.get("job-2") is None
    assert sorted(path.name for path in tmp_path.iterdir()) == ["jobs.json"]


async def test_restart_orphans_running_job_and_queues_voice_failure(tmp_path):
    """Pins INV-JOB-002. Red case demonstrated: recovering live jobs to RUNNING instead of ORPHANED fails this test."""
    registry = await loaded_registry(
        tmp_path,
        make_job(
            started_at=101.0,
            execution_state=ExecutionState.RUNNING,
        ),
        now=120.0,
    )
    recovered = await registry.recover_after_restart()
    job = registry.get("job-1")
    assert recovered == [job]
    assert job.execution_state is ExecutionState.ORPHANED
    assert job.delivery_state is DeliveryState.READY
    assert job.failure.kind == "restart_orphan"
    assert job.delivery_sequence == 1
    assert job.orphan_notification_pending is False


async def test_restart_orphans_accepted_job_left_before_task_binding(tmp_path):
    registry = await loaded_registry(tmp_path, make_job(), now=120.0)
    recovered = await registry.recover_after_restart()
    job = registry.get("job-1")
    assert recovered == [job]
    assert job.execution_state is ExecutionState.ORPHANED
    assert job.delivery_state is DeliveryState.READY
    assert job.failure == JobFailure("restart_orphan", "Lost on restart")


async def test_restart_finalizes_cancel_pending_job_as_cancelled(tmp_path):
    """#334: a job the creator already cancelled (durable cancel_pending) must
    not be recovered as a restart orphan — no "Lost on restart" failure, no
    delivery, no creator notice."""
    registry = await loaded_registry(
        tmp_path,
        make_job(
            started_at=101.0,
            execution_state=ExecutionState.RUNNING,
            cancel_pending=True,
            creator_peer="telegram",
        ),
        now=120.0,
    )
    recovered = await registry.recover_after_restart()
    job = registry.get("job-1")
    assert recovered == []
    assert job.execution_state is ExecutionState.CANCELLED
    assert job.failure == JobFailure("cancelled", "Cancelled by creator")
    assert job.delivery_state is DeliveryState.NONE
    assert job.delivery_sequence == 0
    assert job.cancel_pending is False
    assert job.orphan_notification_pending is False
    assert job.terminal_at == 120.0


async def test_restart_cancel_pending_with_live_delivery_marks_it_cancelled(
    tmp_path,
):
    """#334: an in-flight delivery of a cancelled job flips to CANCELLED (the
    live cancel paths' shape), never to a fresh READY sequence."""
    registry = await loaded_registry(
        tmp_path,
        make_job(
            started_at=101.0,
            execution_state=ExecutionState.RUNNING,
            delivery_state=DeliveryState.READY,
            delivery_sequence=3,
            cancel_pending=True,
        ),
        now=120.0,
    )
    recovered = await registry.recover_after_restart()
    job = registry.get("job-1")
    assert recovered == []
    assert job.execution_state is ExecutionState.CANCELLED
    assert job.delivery_state is DeliveryState.CANCELLED
    assert job.delivery_sequence == 3
    assert job.lease_until is None
    assert job.delivery_attempt_id is None


async def test_continuation_create_atomically_consumes_parent(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0,
            expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"needs_clarification"}',
            awaiting_input=True,
            continuable_until=200.0,
        ),
        now=120.0,
    )
    child = replace(
        make_job(),
        id="job-child",
        parent_job_id="job-1",
        created_at=120.0,
    )

    assert await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    ) == child
    parent = registry.get("job-1")
    assert parent.awaiting_input is False
    assert parent.continuable_until == 200.0
    assert registry.get("job-child") == child

    with pytest.raises(JobTransitionError):
        await registry.create_continuation(
            "job-1",
            replace(child, id="job-duplicate"),
            actor=actor_for_job(),
        )
    assert registry.get("job-duplicate") is None


async def test_compensate_unbound_continuation_restores_fresh_parent(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0,
            expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"needs_clarification"}',
            awaiting_input=True,
            continuable_until=200.0,
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
    )
    await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )

    restored = await registry.compensate_unbound_continuation(
        "job-1", "job-child", actor=actor_for_job(),
    )

    assert restored is True
    assert registry.get("job-child") is None
    parent = registry.get("job-1")
    assert parent.awaiting_input is True
    assert parent.continuable_until == 200.0


async def test_compensate_unbound_continuation_does_not_restore_expired_parent(
    tmp_path,
):
    now = [120.0]
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: now[0],
    )
    await registry.load()
    await registry.create(make_job(
        terminal_at=100.0,
        expires_at=121.0,
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        result='{"status":"needs_clarification"}',
        awaiting_input=True,
        continuable_until=121.0,
    ))
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
    )
    await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )
    now[0] = 122.0

    restored = await registry.compensate_unbound_continuation(
        "job-1", "job-child", actor=actor_for_job(),
    )

    assert restored is False
    assert registry.get("job-child") is None
    parent = registry.get("job-1")
    assert parent.awaiting_input is False
    assert parent.continuable_until == 121.0


async def test_compensation_never_rewinds_a_bound_running_child(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0,
            expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"needs_clarification"}',
            awaiting_input=True,
            continuable_until=200.0,
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
    )
    await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )
    release = asyncio.Event()

    async def work():
        await release.wait()

    worker = asyncio.create_task(work())
    await registry.bind_task("job-child", worker)
    try:
        restored = await registry.compensate_unbound_continuation(
            "job-1", "job-child", actor=actor_for_job(),
        )
        assert restored is False
        assert registry.get("job-child").execution_state is ExecutionState.RUNNING
        assert registry.get("job-1").awaiting_input is False
        assert registry.owns_task("job-child", worker) is True
    finally:
        release.set()
        await worker


async def test_continuation_create_rejects_parent_that_expired_before_commit(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0,
            expires_at=119.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            result='{"status":"needs_clarification"}',
            awaiting_input=True,
            continuable_until=119.0,
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
    )

    with pytest.raises(JobTransitionError):
        await registry.create_continuation(
            "job-1", child, actor=actor_for_job(),
        )
    assert registry.get("job-child") is None


async def test_terminal_waiter_observes_durable_terminal_transition(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    waiter = asyncio.create_task(registry.wait_for_terminal("job-1"))

    await registry.fail("job-1", JobFailure("safe", "safe failure"))

    terminal = await asyncio.wait_for(waiter, timeout=1)
    assert terminal.execution_state is ExecutionState.FAILED


async def test_failure_reconciliation_eventually_terminalizes_live_job(
    tmp_path, caplog,
):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 120.0,
        reconciliation_retry_interval=0.01,
    )
    await registry.load()
    await registry.create(make_job(
        started_at=110.0,
        execution_state=ExecutionState.RUNNING,
    ))
    real_fail = registry.fail_compat
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("PRIVATE_RECONCILE_CANARY")
        return await real_fail(*args, **kwargs)

    registry.fail_compat = fail_once
    registry.schedule_failure_reconciliation("job-1")

    terminal = await asyncio.wait_for(
        registry.wait_for_terminal("job-1"), timeout=1,
    )
    await asyncio.wait_for(
        registry.wait_for_reconciliation("job-1"), timeout=1,
    )
    assert attempts == 2
    assert terminal.execution_state is ExecutionState.FAILED
    assert terminal.failure == JobFailure(
        "persistence_failed", "Specialist result could not be saved.",
    )
    assert registry.reconciliation_count == 0
    assert "PRIVATE_RECONCILE_CANARY" not in caplog.text


async def test_close_cancels_and_drains_sleeping_reconciliation(tmp_path):
    registry = JobRegistry(
        tmp_path / "jobs.json",
        reconciliation_retry_interval=3600.0,
    )
    await registry.load()
    await registry.create(make_job(
        started_at=110.0,
        execution_state=ExecutionState.RUNNING,
    ))
    registry.schedule_failure_reconciliation("job-1")
    assert registry.reconciliation_count == 1

    await asyncio.wait_for(registry.close(), timeout=1)

    assert registry.reconciliation_count == 0
    assert registry.get("job-1").execution_state is ExecutionState.RUNNING


async def test_persistent_reconciliation_failure_stays_restart_recoverable(
    tmp_path, caplog,
):
    attempted = asyncio.Event()
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 120.0,
        reconciliation_retry_interval=0.01,
    )
    await registry.load()
    await registry.create(make_job(
        started_at=110.0,
        execution_state=ExecutionState.RUNNING,
    ))

    async def fail_forever(*_args, **_kwargs):
        attempted.set()
        raise OSError("PRIVATE_RECONCILE_CANARY")

    registry.fail_compat = fail_forever
    registry.schedule_failure_reconciliation("job-1")
    await asyncio.wait_for(attempted.wait(), timeout=1)
    await registry.close()
    assert registry.reconciliation_count == 0
    assert registry.get("job-1").execution_state is ExecutionState.RUNNING
    assert "PRIVATE_RECONCILE_CANARY" not in caplog.text

    restarted = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 121.0,
    )
    await restarted.load()
    recovered = await restarted.recover_after_restart()
    assert [job.id for job in recovered] == ["job-1"]
    assert restarted.get("job-1").execution_state is ExecutionState.ORPHANED


async def test_telegram_orphan_notification_retries_until_durable_ack(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    first = JobRegistry(jobs_path, clock=lambda: 120.0)
    await first.load()
    await first.create(make_job(
        creator_peer="telegram",
        scope_id="chat-1",
        origin_route_id="route-1",
        origin_device_id=None,
        started_at=101.0,
        execution_state=ExecutionState.RUNNING,
    ))

    boot_one = await first.recover_after_restart()
    assert [job.id for job in boot_one] == ["job-1"]
    assert first.get("job-1").orphan_notification_pending is True

    second = JobRegistry(jobs_path, clock=lambda: 121.0)
    await second.load()
    boot_two = await second.recover_after_restart()
    assert [job.id for job in boot_two] == ["job-1"]
    await second.ack_orphan_notification("job-1")
    assert second.get("job-1").orphan_notification_pending is False

    third = JobRegistry(jobs_path, clock=lambda: 122.0)
    await third.load()
    assert await third.recover_after_restart() == []


async def test_snapshot_without_orphan_ack_field_decodes_as_not_pending(tmp_path):
    registry = await loaded_registry(tmp_path, make_job())
    jobs_path = tmp_path / "jobs.json"
    snapshot = json.loads(jobs_path.read_text(encoding="utf-8"))
    snapshot[0].pop("orphan_notification_pending", None)
    jobs_path.write_text(json.dumps(snapshot), encoding="utf-8")

    reloaded = JobRegistry(jobs_path)
    await reloaded.load()
    assert reloaded.get("job-1").orphan_notification_pending is False


@pytest.mark.parametrize("marked", [True, False])
async def test_recovered_orphan_carries_scheduled_delivery_across_restart(
    tmp_path, marked,
):
    """#485 durable round trip: a scheduled turn delegates, Casa restarts, and
    the resident is resumed from the JSON snapshot — not from the live record.
    The completion origin is rebuilt field by field here, so eligibility has to
    come off the durable field or the resumed turn silently cannot send media.
    An unmarked job must gain nothing."""
    from casa_core import _notify_recovered_delegations

    registry = await loaded_registry(tmp_path)
    await registry.create(make_job(
        id="job-sched",
        creator_peer="telegram",
        scope_id="cron-weekly-invoice",
        origin_device_id=None,
        execution_state=ExecutionState.ORPHANED,
        failure=JobFailure("restart_orphan", "Lost on restart"),
        orphan_notification_pending=True,
        delivery_sequence=1,
        scheduled_delivery=marked,
    ))

    # Reload from disk — this is the restart.
    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    assert reloaded.get("job-sched").scheduled_delivery is marked

    sent = []

    class BusProbe:
        queues = {"concierge": object()}

        async def notify(self, message):
            sent.append(message)

    await _notify_recovered_delegations(
        reloaded.all(), reloaded, BusProbe(), assistant_role="concierge",
    )

    assert len(sent) == 1
    origin = sent[0].content.origin
    assert origin.get("_scheduled_delivery", False) is marked
    # The session label survives either way; eligibility never becomes an address.
    assert origin["chat_id"] == "cron-weekly-invoice"


@pytest.mark.parametrize("failure_phase", ["notify", "ack"])
async def test_recovered_orphan_failure_isolated_before_later_success(
    tmp_path, caplog, failure_phase,
):
    import logging

    from casa_core import _notify_recovered_delegations

    registry = await loaded_registry(tmp_path)
    failed_job = make_job(
        id="job-fail",
        creator_peer="telegram",
        scope_id="chat-1",
        origin_device_id=None,
        execution_state=ExecutionState.ORPHANED,
        failure=JobFailure("restart_orphan", "Lost on restart"),
        orphan_notification_pending=True,
        delivery_sequence=1,
    )
    next_job = replace(
        failed_job, id="job-next", scope_id="chat-2", delivery_sequence=2,
    )
    await registry.create(failed_job)
    await registry.create(next_job)
    events = []
    secret = "SECRET-notification-detail"

    class RegistryProbe:
        async def ack_orphan_notification(self, job_id):
            events.append(("ack", job_id))
            if failure_phase == "ack" and job_id == "job-fail":
                raise RuntimeError(secret)
            await registry.ack_orphan_notification(job_id)

    class BusProbe:
        queues = {"concierge": object()}

        async def notify(self, message):
            assert message.content.text == ""
            events.append(("notify", message.content.delegation_id))
            if (failure_phase == "notify"
                    and message.content.delegation_id == "job-fail"):
                raise RuntimeError(secret)

    with caplog.at_level(logging.ERROR, logger="casa_core"):
        await _notify_recovered_delegations(
            registry.all(), RegistryProbe(), BusProbe(),
            assistant_role="concierge",
        )
    expected = [("notify", "job-fail")]
    if failure_phase == "ack":
        expected.append(("ack", "job-fail"))
    expected.extend([("notify", "job-next"), ("ack", "job-next")])
    assert events == expected
    assert registry.get("job-fail").orphan_notification_pending is True
    assert registry.get("job-next").orphan_notification_pending is False
    assert secret not in caplog.text

    reloaded = JobRegistry(
        tmp_path / "jobs.json",
    )
    await reloaded.load()
    assert reloaded.get("job-fail").orphan_notification_pending is True
    assert reloaded.get("job-next").orphan_notification_pending is False


async def test_recovered_orphan_notification_does_not_swallow_cancellation():
    from casa_core import _notify_recovered_delegations

    job = make_job(
        creator_peer="telegram",
        execution_state=ExecutionState.ORPHANED,
        failure=JobFailure("restart_orphan", "Lost on restart"),
        orphan_notification_pending=True,
    )

    class RegistryProbe:
        async def ack_orphan_notification(self, _job_id):
            raise AssertionError("ack must not run")

    class CancelledBus:
        queues = {"concierge": object()}

        async def notify(self, _message):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _notify_recovered_delegations(
            [job], RegistryProbe(), CancelledBus(), assistant_role="concierge",
        )


@pytest.mark.parametrize(
    "delivery_state",
    [DeliveryState.CLAIMED, DeliveryState.AUTHORIZED, DeliveryState.PLAYING],
)
async def test_restart_retains_delivery_attempt_for_one_full_lease(
    tmp_path, delivery_state,
):
    now = [100.0]
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: now[0],
    )
    await registry.load()
    await registry.create(make_job(
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=delivery_state,
        terminal_at=80.0,
        result="answer",
        delivery_sequence=1,
        delivery_attempt_id="attempt-1",
        lease_until=90.0,
    ))
    await registry.recover_after_restart()
    assert registry.get("job-1").delivery_state is delivery_state
    assert registry.get("job-1").lease_until == 115.0
    now[0] = 116.0
    await registry.expire_leases()
    assert registry.get("job-1").delivery_state is DeliveryState.READY


# ---------------------------------------------------------------------------
# Task 12: typed creating/executing speaker provenance
# ---------------------------------------------------------------------------

CALLER_SPEAKER = SpeakerProvenance(
    speaker_kind="resident", role_id="resident:concierge",
    persona_id="casa/gary", persona_version="0.1.0",
    display_name="Gary", binding_digest="sha256:" + "a" * 64,
)
EXECUTOR_SPEAKER = SpeakerProvenance(
    speaker_kind="specialist", role_id="specialist:mtg-judge",
    persona_id="casa/judge", persona_version="0.1.0",
    display_name="Judge", binding_digest="sha256:" + "b" * 64,
)


async def test_voice_job_round_trip_preserves_both_speaker_snapshots(tmp_path):
    registry = JobRegistry(tmp_path / "jobs.json")
    await registry.load()
    job = make_job(
        creating_speaker=CALLER_SPEAKER, executing_speaker=EXECUTOR_SPEAKER,
    )
    await registry.create(job)

    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    restored = reloaded.get("job-1")
    assert restored.creating_speaker == CALLER_SPEAKER
    assert restored.executing_speaker == EXECUTOR_SPEAKER
    assert restored == job


async def test_continuation_child_keeps_its_own_speaker_snapshots(tmp_path):
    """job_registry.create_continuation is pure data plumbing — it must
    persist whatever creating_speaker/executing_speaker the caller (tools.py)
    already computed for the child, not derive or overwrite them: the parent
    keeps its own creator, while a re-bound specialist's current executing
    identity travels forward onto the child unchanged."""
    registry = await loaded_registry(
        tmp_path,
        make_job(
            creating_speaker=CALLER_SPEAKER,
            executing_speaker=SpeakerProvenance(speaker_kind="system"),
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            awaiting_input=True,
            continuable_until=200.0,
            terminal_at=101.0,
            result="It targets.",
            delivery_sequence=1,
        ),
    )
    current_executor = SpeakerProvenance(
        speaker_kind="specialist", role_id="specialist:mtg-judge",
        persona_id="casa/judge", persona_version="0.2.0",
        display_name="Judge", binding_digest="sha256:" + "c" * 64,
    )
    child = make_job(
        id="job-2", parent_job_id="job-1",
        creating_speaker=CALLER_SPEAKER, executing_speaker=current_executor,
        execution_state=ExecutionState.ACCEPTED, delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False, continuable_until=None,
    )
    stored = await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )
    assert stored.creating_speaker == CALLER_SPEAKER
    assert stored.executing_speaker == current_executor

    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    assert reloaded.get("job-2").creating_speaker == CALLER_SPEAKER
    assert reloaded.get("job-2").executing_speaker == current_executor


async def test_pre_task_12_legacy_record_decodes_to_a_system_speaker(tmp_path):
    """A record persisted before this task shipped has neither key present —
    it must decode to an explicit system-kind snapshot, never a fabricated
    persona derived from the bare creating_role/specialist_role strings."""
    registry = await loaded_registry(tmp_path, make_job(
        creating_speaker=CALLER_SPEAKER, executing_speaker=EXECUTOR_SPEAKER,
    ))
    snapshot = json.loads((tmp_path / "jobs.json").read_text())
    snapshot[0].pop("creating_speaker", None)
    snapshot[0].pop("executing_speaker", None)
    (tmp_path / "jobs.json").write_text(json.dumps(snapshot))

    reloaded = JobRegistry(tmp_path / "jobs.json")
    await reloaded.load()
    job = reloaded.get("job-1")
    assert job.creating_speaker == SpeakerProvenance(speaker_kind="system")
    assert job.executing_speaker == SpeakerProvenance(speaker_kind="system")
    # Never derived from the still-present legacy display strings.
    assert job.creating_role == "concierge"
    assert job.specialist_role == "mtg-judge"


async def test_a_continuation_does_not_revive_the_parents_promise(tmp_path):
    """A live turn that offered no endpoint is an ANSWER, not a gap.

    The continuation has its own utterance. If that utterance carried no
    delivery offer, the endpoint is telling us it cannot currently receive a
    deferred answer — the speaker was unplugged, the app was removed. Reviving
    the parent's older modality would promise audio into a room that can no
    longer play it, and delivery would refuse it (#233/#224).
    """
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0,
            expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"needs_clarification"}',
            awaiting_input=True,
            continuable_until=200.0,
            delivery_modality="audio",
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
        delivery_modality=None,
    )

    created = await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )
    assert created.delivery_modality is None
    assert registry.get("job-child").delivery_modality is None


async def test_a_childs_own_endpoint_offer_wins_over_the_parents(tmp_path):
    """The live turn is the better evidence — never overwrite it."""
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0, expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"needs_clarification"}',
            awaiting_input=True, continuable_until=200.0,
            delivery_modality="audio",
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
        delivery_modality="text",
    )
    created = await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )
    assert created.delivery_modality == "text"


async def test_a_different_endpoint_does_not_inherit_the_promise(tmp_path):
    """Promising audio because some OTHER device could speak is the bug."""
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0, expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"needs_clarification"}',
            awaiting_input=True, continuable_until=200.0,
            delivery_modality="audio",
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
        origin_device_id="device-hallway", delivery_modality=None,
    )
    created = await registry.create_continuation(
        "job-1", child, actor=actor_for_job(),
    )
    assert created.delivery_modality is None


async def test_prompted_delivery_inherits_the_delivery_promise(tmp_path):
    registry = await loaded_registry(
        tmp_path,
        make_job(
            terminal_at=100.0, expires_at=200.0,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.DELIVERED,
            result='{"status":"ok"}',
            delivery_modality="text",
        ),
        now=120.0,
    )
    child = replace(
        make_job(), id="job-child", parent_job_id="job-1", created_at=120.0,
        delivery_modality=None,
    )
    prompted = await registry.create_prompted_delivery(
        "job-1", child, actor=actor_for_job(),
    )
    assert prompted.delivery_modality == "text"


# ---------------------------------------------------------------------------
# #321 — DELIVERED handoff replay + generalized terminal reconciliation
# ---------------------------------------------------------------------------


async def test_delivered_job_with_pending_handoff_is_not_replayed(tmp_path):
    """#321: a job whose result was DELIVERED but whose handoff frame was lost
    (handoff still PENDING) must NOT re-send voice_handoff on every route
    reconnect forever — expire_due skips DELIVERED jobs, so nothing else ever
    retires it."""
    registry = await loaded_registry(tmp_path, make_job(
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        handoff_id="handoff-1",
        handoff_state=HandoffState.PENDING,
        result="done",
        started_at=101.0,
        terminal_at=110.0,
        expires_at=500.0,
    ))
    assert registry.pending_handoffs_for_route("entry-1") == []


async def test_completion_reconciliation_eventually_terminalizes_live_job(
    tmp_path, caplog,
):
    """#321: a successful sync delegation whose terminal write failed keeps
    its RESULT (returned to the engager) — the durable record is completed by
    a registry-owned retry, mirroring the failure-reconciliation owner."""
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 120.0,
        reconciliation_retry_interval=0.01,
    )
    await registry.load()
    await registry.create(make_job(
        started_at=110.0,
        execution_state=ExecutionState.RUNNING,
    ))
    real_finish = registry.finish_compat
    attempts = 0

    async def finish_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("PRIVATE_RECONCILE_CANARY")
        return await real_finish(*args, **kwargs)

    registry.finish_compat = finish_once
    registry.schedule_completion_reconciliation("job-1")

    terminal = await asyncio.wait_for(
        registry.wait_for_terminal("job-1"), timeout=1,
    )
    await asyncio.wait_for(
        registry.wait_for_reconciliation("job-1"), timeout=1,
    )
    assert attempts == 2
    assert terminal.execution_state is ExecutionState.SUCCEEDED
    assert registry.reconciliation_count == 0
    assert "PRIVATE_RECONCILE_CANARY" not in caplog.text


async def test_cancel_reconciliation_eventually_terminalizes_live_job(tmp_path):
    """#321: a voice-deadline teardown whose cancel snapshot write failed
    leaves the job RUNNING — the registry-owned retry cancels it."""
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: 120.0,
        reconciliation_retry_interval=0.01,
    )
    await registry.load()
    await registry.create(make_job(
        started_at=110.0,
        execution_state=ExecutionState.RUNNING,
    ))
    real_cancel = registry.cancel
    attempts = 0

    async def cancel_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk hiccup")
        return await real_cancel(*args, **kwargs)

    registry.cancel = cancel_once
    registry.schedule_cancel_reconciliation("job-1")

    terminal = await asyncio.wait_for(
        registry.wait_for_terminal("job-1"), timeout=1,
    )
    await asyncio.wait_for(
        registry.wait_for_reconciliation("job-1"), timeout=1,
    )
    assert attempts == 2
    assert terminal.execution_state is ExecutionState.CANCELLED
    assert registry.reconciliation_count == 0


async def test_fail_compat_cancelled_kind_persists_cancelled_state(tmp_path):
    """Terra r7 refutation pin (#321): the lifecycle cancellation handler
    persists via fail_compat(JobFailure("cancelled", ...)) — the registry's
    kind-aware terminal maps that to ExecutionState.CANCELLED (delivery
    CANCELLED), NOT FAILED, so the primary write and the cancel
    reconciliation retry converge on the same cancelled shape."""
    registry = await loaded_registry(tmp_path, make_job(
        started_at=101.0, execution_state=ExecutionState.RUNNING,
    ))
    finished = await registry.fail_compat(
        "job-1", JobFailure("cancelled", "Specialist job was cancelled."))
    assert finished.execution_state is ExecutionState.CANCELLED
    assert finished.delivery_state is DeliveryState.NONE
