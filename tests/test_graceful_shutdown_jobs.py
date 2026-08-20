"""A graceful stop must not be worse than a crash (#671).

Red case for proposed INV-JOB-009. Specified by an independent reviewer, and
accepted by the other one, before any production change existed.

The defect, at `546d0129`: `SpecialistRegistry.cancel_delegation` is the
creator-cancel path and carries no reason, so process death reuses it. At a
graceful stop the live row is overwritten `CANCELLED` +
`JobFailure("cancelled", "Delegation cancelled")`, and because
`recover_after_restart` converts only ACCEPTED/RUNNING, the pre-terminalized row
is skipped — `orphan_notification_pending` is never set and nothing is ever
announced. A SIGKILL leaves the row `RUNNING` and correctly yields exactly one
"Lost on restart" orphan. So a clean stop is measurably worse than a crash, and
that asymmetry is what these cases pin.

What is pinned here is the durable row and its entry into boot recovery — NOT
operator delivery. The restart-orphan notice is acknowledged when it is
enqueued, not when the operator has it (`casa_core.py:3424` acks immediately
after a fire-and-forget `bus.notify`); that is a separate pre-existing defect,
reached identically by the crash path, and no case here invokes
`_notify_recovered_delegations`.
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import textwrap
from dataclasses import replace

import pytest  # noqa: F401 — pytest.ini sets asyncio_mode = auto

import tools
from job_registry import (
    DeliveryState,
    ExecutionState,
    JobFailure,
    JobRegistry,
    VoiceJob,
)
from personality_types import SpeakerProvenance
from specialist_registry import SpecialistRegistry


# `asyncio_mode = auto` (pytest.ini) runs the async cases; two cases here are
# deliberately SYNCHRONOUS because they drive `asyncio.run` themselves, so no
# module-level asyncio marker is applied.

SYSTEM_SPEAKER = SpeakerProvenance(speaker_kind="system")

# A telegram-origin delegation, which is what makes the orphan notice pending
# (`recover_after_restart` sets it from `creator_peer == "telegram"`).
def make_live_telegram_job(**changes) -> VoiceJob:
    base = VoiceJob(
        id="job-1", parent_job_id=None,
        creating_speaker=SYSTEM_SPEAKER, executing_speaker=SYSTEM_SPEAKER,
        creating_role="assistant", specialist_role="researcher",
        specialist_display_name="Researcher",
        creator_peer="telegram", creator_user_id="user-1",
        scope_id="chat-1", origin_route_id=None,
        origin_device_id=None,
        task="Find the boiler schedule.", context="",
        created_at=100.0, started_at=101.0, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None,
        cancel_pending=False,
    )
    return replace(base, **changes)


class WriteCounter:
    """Counts real snapshot commits by wrapping the registry's disk writer.

    Counting COMMITS rather than reading the final state is what makes
    "deferred" distinguishable from "wrote the same thing twice", and it is
    what the specification asked for.
    """

    def __init__(self, registry: JobRegistry) -> None:
        self._inner = registry._write_snapshot_locked
        self.count = 0
        registry._write_snapshot_locked = self._wrapped  # type: ignore[method-assign]

    async def _wrapped(self, jobs):
        self.count += 1
        return await self._inner(jobs)

    def reset(self) -> None:
        self.count = 0


def declare_graceful_stop(registry: JobRegistry) -> None:
    """Declare the graceful stop through the production seam, tolerantly.

    Deliberately tolerant of the seam not existing, so that on the PRE-FIX tree
    these cases fail on the durable OUTCOME — a `CANCELLED` row and a recovery
    that returns nothing — rather than on an `AttributeError`, which would prove
    nothing about behaviour.

    The tolerance cannot hide anything. A missing, renamed, or no-op declaration
    leaves the guard inactive, so every case that depends on deferral still
    fails; and `test_main_declares_job_shutdown_immediately_after_stop_wait`
    independently pins the exact method name and its placement in
    `casa_core.main`.
    """
    begin = getattr(registry, "begin_shutdown", None)
    if begin is not None:
        begin()


async def loaded(tmp_path, job=None, *, now=200.0, name="jobs.json"):
    registry = JobRegistry(tmp_path / name, clock=lambda: now)
    await registry.load()
    if job is not None:
        await registry.create(job)
    return registry


def facade(registry: JobRegistry, tmp_path) -> SpecialistRegistry:
    """The real `SpecialistRegistry` over the real `JobRegistry`.

    The arm under test is the production facade hop
    (`specialist_registry.py:370-372`), not a stand-in for it.
    """
    return SpecialistRegistry(str(tmp_path / "specialists"), job_registry=registry)


async def test_graceful_facade_cancel_is_recovered_as_exactly_one_orphan(tmp_path):
    """Case 1 — the defect itself, end to end.

    A live telegram delegation, a declared graceful stop, and the real facade
    cancel arm: the row must be untouched on disk so boot recovery treats it
    exactly as it treats a crash-lost job.
    """
    registry = await loaded(tmp_path, make_live_telegram_job())
    before = registry.get("job-1")
    writes = WriteCounter(registry)

    # Declared twice on purpose: the declaration is idempotent and must not
    # itself commit anything.
    declare_graceful_stop(registry)
    declare_graceful_stop(registry)
    assert writes.count == 0

    await facade(registry, tmp_path).cancel_delegation("job-1")
    assert writes.count == 0
    assert registry.get("job-1") == before

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    boot_writes = WriteCounter(reloaded)
    recovered = await reloaded.recover_after_restart()

    assert len(recovered) == 1
    assert boot_writes.count == 1
    orphan = recovered[0]
    assert orphan.id == "job-1"
    assert orphan.execution_state is ExecutionState.ORPHANED
    assert orphan.failure == JobFailure("restart_orphan", "Lost on restart")
    assert orphan.orphan_notification_pending is True


async def test_graceful_voice_cancellation_envelope_leaves_row_live(tmp_path):
    """Case 2 — the voice arm, which the issue's own fix sketch misses.

    `tools._persist_cancelled_terminal` writes a DIFFERENT message string
    through a DIFFERENT function (`fail_compat` → `_fail_current_locked`), and
    is reached from `job_registry.close()`. Pinning it here is what proves the
    waist covers both guarded sites rather than only the one the issue names.
    """
    registry = await loaded(tmp_path, make_live_telegram_job())
    before = registry.get("job-1")
    writes = WriteCounter(registry)

    declare_graceful_stop(registry)
    await tools._persist_cancelled_terminal(
        registry=registry, job_id="job-1", specialist_role="researcher",
    )

    assert writes.count == 0
    assert registry.get("job-1") == before


async def test_non_cancel_failure_during_shutdown_still_lands(tmp_path):
    """Case 3 — a non-cancellation verdict written during the same stop lands.

    This exists so a later cluster's abort verdict (#675, a
    `specialist_turn_limit` failure) is not eaten by this cluster's guard. It
    routes through `_fail_current_locked`, the site the fix touches — not an
    untouched path.

    Honest note: this case is ALREADY GREEN at the base SHA. Its evidence is
    the mutation, not its own colour — deleting the predicate's
    `kind == "cancelled"` conjunct turns it red, and no other case here detects
    that mutation.
    """
    registry = await loaded(tmp_path, make_live_telegram_job())
    writes = WriteCounter(registry)
    verdict = JobFailure(
        "specialist_turn_limit", "Specialist could not complete the voice job.",
    )

    declare_graceful_stop(registry)
    await registry.fail_compat("job-1", verdict)

    assert writes.count == 1
    row = registry.get("job-1")
    assert row.execution_state is ExecutionState.FAILED
    assert row.failure == verdict


async def test_success_during_shutdown_still_lands(tmp_path):
    """Case 4 — a success during the same stop is never suppressed.

    Catches a guard placed on all terminal writes rather than on cancellation
    writes. It is deliberately NOT credited with covering the failure-kind
    conjunct: `finish_compat` never reads the predicate at all.
    """
    registry = await loaded(tmp_path, make_live_telegram_job())
    writes = WriteCounter(registry)

    declare_graceful_stop(registry)
    await facade(registry, tmp_path).complete_delegation("job-1")

    assert writes.count == 1
    row = registry.get("job-1")
    assert row.execution_state is ExecutionState.SUCCEEDED
    assert row.result == ""
    assert row.failure is None


async def test_creator_cancel_pending_remains_silent_across_shutdown(tmp_path):
    """Case 5 — a real creator cancel stays silent across the same stop.

    This is the case that must NOT become noisy, and it is why the fix needs no
    per-task cause tracking: an authorized creator cancel is already durable as
    `cancel_pending`, so leaving the row live hands it to the boot path that
    finalizes it "Cancelled by creator" with the notice suppressed — the right
    message instead of the false "Delegation cancelled" a live cancel writes.
    """
    registry = await loaded(tmp_path, make_live_telegram_job())
    result = await registry.request_cancel("job-1", actor={
        "creator_peer": "telegram",
        "creator_user_id": "user-1",
        "scope_id": "chat-1",
    })
    assert result.status == "stopping"
    assert registry.get("job-1").cancel_pending is True

    writes = WriteCounter(registry)
    declare_graceful_stop(registry)
    await facade(registry, tmp_path).cancel_delegation("job-1")

    assert writes.count == 0
    assert registry.get("job-1").cancel_pending is True

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    boot_writes = WriteCounter(reloaded)
    recovered = await reloaded.recover_after_restart()

    assert recovered == []
    assert boot_writes.count == 1
    row = reloaded.get("job-1")
    assert row.execution_state is ExecutionState.CANCELLED
    assert row.failure == JobFailure("cancelled", "Cancelled by creator")
    assert row.orphan_notification_pending is False


async def test_cancel_without_shutdown_declaration_keeps_current_contract(tmp_path):
    """Case 6 — no declaration, no change.

    This is what asserts the predicate's SPECIFICITY: an ordinary cancel is
    still a durable CANCELLED terminal with its existing envelope, and boot
    recovery still returns nothing for it.
    """
    registry = await loaded(tmp_path, make_live_telegram_job())
    writes = WriteCounter(registry)

    await facade(registry, tmp_path).cancel_delegation("job-1")

    assert writes.count == 1
    row = registry.get("job-1")
    assert row.execution_state is ExecutionState.CANCELLED
    assert row.failure == JobFailure("cancelled", "Delegation cancelled")

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    assert await reloaded.recover_after_restart() == []


async def test_shutdown_does_not_rewrite_an_already_terminal_row(tmp_path):
    """Case 7 — a stop never revives or rewrites a terminal row.

    The non-cancellation call is what makes deleting `fail_compat`'s terminal
    early-out observable; the cancellation call covers `cancel`'s.
    """
    registry = await loaded(tmp_path, make_live_telegram_job(
        execution_state=ExecutionState.SUCCEEDED,
        terminal_at=150.0,
        result="the boiler runs at 06:00",
    ))
    before = registry.get("job-1")
    writes = WriteCounter(registry)

    declare_graceful_stop(registry)
    await registry.cancel("job-1")
    await registry.fail_compat(
        "job-1", JobFailure("specialist_turn_limit", "too many turns"),
    )

    assert writes.count == 0
    assert registry.get("job-1") == before

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    assert await reloaded.recover_after_restart() == []
    assert reloaded.get("job-1").execution_state is ExecutionState.SUCCEEDED


def test_asyncio_run_final_cancellation_uses_registry_shutdown_state(tmp_path):
    """Case 8 — the carrier survives `main()`'s frame.

    The edge that actually fires in production is NOT the agent-loop cancel: the
    delegation task is a bare `create_task` and the SDK dispatches the tool
    handler on a detached task, so the cancellation arrives from
    `asyncio.run()`'s final `_cancel_all_tasks`, AFTER the main coroutine has
    returned. So this test drives that exact topology rather than simulating it.

    It also pins WHY the carrier is registry instance state and not the
    cancellation message: measured on CPython 3.12.13, `_cancel_all_tasks`
    cancels with NO message, so a `CancelledError.args` carrier is empty in
    precisely the case this fix exists for.
    """
    seen_inside: list[tuple] = []
    seen_callback: list[tuple] = []

    async def main_like():
        registry = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
        await registry.load()
        await registry.create(make_live_telegram_job())
        writes = WriteCounter(registry)
        started = asyncio.Event()

        async def arm():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError as exc:
                seen_inside.append(exc.args)
                await tools._persist_cancelled_terminal(
                    registry=registry, job_id="job-1",
                    specialist_role="researcher",
                )
                raise

        task = asyncio.create_task(arm())

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError as exc:
                seen_callback.append(exc.args)

        task.add_done_callback(_done)
        await started.wait()
        declare_graceful_stop(registry)
        return writes

    writes = asyncio.run(main_like())

    assert seen_inside == [()]
    assert seen_callback == [()]
    assert writes.count == 0

    async def boot():
        reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
        await reloaded.load()
        return await reloaded.recover_after_restart()

    recovered = asyncio.run(boot())
    assert len(recovered) == 1
    assert recovered[0].execution_state is ExecutionState.ORPHANED
    assert recovered[0].orphan_notification_pending is True


async def test_deferred_cancel_reconciliation_stops_after_one_attempt(tmp_path):
    """Case 9 — the deferred cancel does not leave a loop that cannot progress.

    `_reconcile_terminal` is `while True: sleep; op()` and returns only once the
    row is terminal or gone. A deferred `cancel` returns a LIVE row forever, so
    without an explicit stop the retry loop spins for the rest of the process's
    life. This is the liveness half of the change and it has its own case
    because none of the cases above schedules a reconciliation.
    """
    registry = JobRegistry(
        tmp_path / "jobs.json", clock=lambda: 200.0,
        reconciliation_retry_interval=0.01,
    )
    await registry.load()
    await registry.create(make_live_telegram_job())

    attempts = 0
    inner = registry.cancel

    async def counting_cancel(job_id: str):
        nonlocal attempts
        attempts += 1
        return await inner(job_id)

    registry.cancel = counting_cancel  # type: ignore[method-assign]
    writes = WriteCounter(registry)

    declare_graceful_stop(registry)
    registry.schedule_cancel_reconciliation("job-1")
    await asyncio.wait_for(registry.wait_for_reconciliation("job-1"), timeout=5)

    assert attempts == 1
    assert writes.count == 0
    assert registry.get("job-1").execution_state is ExecutionState.RUNNING
    assert registry._reconciliation_tasks == {}


def test_main_declares_job_shutdown_immediately_after_stop_wait():
    """Case 10 — the production wiring, and its PLACEMENT, are pinned.

    The declaration must be the first statement of the cleanup block: ingress
    stays open until the runner cleanup, `Agent.aclose()` can cancel a detached
    handler earlier, and `job_registry.close()` reaches the voice arm — so any
    later placement leaves arms firing against an undeclared registry. Parsed
    from source so the whole app need not boot.
    """
    import casa_core

    tree = ast.parse(textwrap.dedent(inspect.getsource(casa_core.main)))
    body = tree.body[0].body  # type: ignore[attr-defined]

    def is_stop_wait(node: ast.stmt) -> bool:
        return "stop_event.wait()" in ast.unparse(node)

    def is_declaration(node: ast.stmt) -> bool:
        return "job_registry.begin_shutdown()" in ast.unparse(node)

    declarations = [i for i, n in enumerate(body) if is_declaration(n)]
    stop_waits = [i for i, n in enumerate(body) if is_stop_wait(n)]

    assert len(declarations) == 1
    assert len(stop_waits) == 1
    assert declarations[0] == stop_waits[0] + 1
