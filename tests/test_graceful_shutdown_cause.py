"""A graceful stop defers only the cancellations it CAUSED (#671).

Second red case for INV-JOB-009, from the seam review of the mechanism change.
`tests/test_graceful_shutdown_jobs.py` is frozen (specified by one reviewer,
accepted by another) and every case in it still describes behaviour this file's
subject preserves — so these are additions, not edits.

The defect these pin: the first mechanism keyed deferral on the terminal
CATEGORY `"cancelled"`, which a creator cancel, a deadline expiry, a pre-launch
bail, a launch rollback and process death all share. So the only thing actually
distinguishing process death was the ambient shutdown flag, and a cancellation
with a KNOWN non-shutdown cause landing inside the shutdown window was deferred
too — the next boot then announced "Lost on restart" for work that was never
launched and whose caller had already been told synchronously.

The fix carries the cause: `note_cancel_cause` is a per-job latch meaning "this
settling is NOT the stop", set by the arm that knows its cause statically.
Unlatched still defers, so the direction is fail-closed and an arm that latches
nothing errs toward telling the operator.

The enumeration this rests on, established mechanically rather than by reading:
of the 22 durable publish points in `JobRegistry`, exactly THREE can take a live
row out of `recover_after_restart`'s reach — `cancel`, `_fail_current_locked`,
and `compensate_unbound_continuation`, which settles by DELETION. Both expiry
sweeps touch `delivery_state` only and never `execution_state`. The two
`_finish_*_current_locked` paths write CANCELLED only for an already-durable
`cancel_pending`, which is a creator cancellation whose silence is correct.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from job_registry import (
    DeliveryState,
    ExecutionState,
    JobFailure,
    JobRegistry,
    VoiceJob,
)
from personality_types import SpeakerProvenance


SYSTEM_SPEAKER = SpeakerProvenance(speaker_kind="system")


def make_voice_job(**changes) -> VoiceJob:
    """A voice-ROUTED live row: the shape whose false orphan is spoken aloud."""
    base = VoiceJob(
        id="job-1", parent_job_id=None,
        creating_speaker=SYSTEM_SPEAKER, executing_speaker=SYSTEM_SPEAKER,
        creating_role="concierge", specialist_role="researcher",
        specialist_display_name="Researcher",
        creator_peer="voice_speaker", creator_user_id=None,
        scope_id="scope-1", origin_route_id="route-1",
        origin_device_id="device-kitchen",
        task="How long does the boiler run?", context="",
        created_at=100.0, started_at=101.0, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None,
        cancel_pending=False,
    )
    return replace(base, **changes)


def actor():
    return {
        "creator_peer": "voice_speaker",
        "creator_user_id": None,
        "scope_id": "scope-1",
    }


class Commits:
    """Counts real snapshot commits by wrapping the registry's disk writer."""

    def __init__(self, registry: JobRegistry) -> None:
        self._inner = registry._write_snapshot_locked
        self.count = 0
        registry._write_snapshot_locked = self._wrapped  # type: ignore[method-assign]

    async def _wrapped(self, jobs):
        self.count += 1
        return await self._inner(jobs)

    def reset(self) -> None:
        self.count = 0


def latch(registry: JobRegistry, job_id: str, cause: str) -> None:
    """Latch a known non-shutdown cause, tolerantly.

    Tolerant for the same reason the frozen file's declaration helper is: on the
    pre-fix tree these cases must fail on the durable OUTCOME — a row left live
    and a false orphan recovered — not on an `AttributeError`. A missing or no-op
    latch leaves every case that depends on it red, so the tolerance hides
    nothing.
    """
    note = getattr(registry, "note_cancel_cause", None)
    if note is not None:
        note(job_id, cause)


async def loaded(tmp_path, *jobs, now=200.0, name="jobs.json"):
    registry = JobRegistry(tmp_path / name, clock=lambda: now)
    await registry.load()
    for job in jobs:
        await registry.create(job)
    return registry


async def reload(tmp_path, *, now=300.0, name="jobs.json"):
    registry = JobRegistry(tmp_path / name, clock=lambda: now)
    await registry.load()
    return registry


async def test_prelaunch_budget_bail_still_terminalizes_during_a_stop(tmp_path):
    """Case J — the regression the seam review reproduced.

    `delegate_to_agent`'s pre-launch voice-budget bail (`tools.py:5066`)
    registered the row, launched NO task, and reports the deadline to the caller
    synchronously. Deferring it makes the next boot speak "Lost on restart" for
    work that never started, on top of a report the caller already has. The base
    SHA correctly says nothing here, so a deferral is strictly worse.
    """
    registry = await loaded(tmp_path, make_voice_job())
    commits = Commits(registry)

    registry.begin_shutdown()
    latch(registry, "job-1", "voice_budget_prelaunch")
    await registry.cancel("job-1")

    assert commits.count == 1
    row = registry.get("job-1")
    assert row.execution_state is ExecutionState.CANCELLED
    assert row.failure == JobFailure("cancelled", "Delegation cancelled")

    reloaded = await reload(tmp_path)
    boot = Commits(reloaded)
    assert await reloaded.recover_after_restart() == []
    assert boot.count == 0


async def test_voice_deadline_stays_silent_during_a_stop(tmp_path):
    """Case K — `_voice_deadline_exceeded`'s silence is preserved.

    The cluster brief requires it: the caller gets the typed deadline result and
    must not ALSO be told the work was lost to a restart. A voice-routed row is
    used deliberately — a false orphan here becomes a READY delivery, i.e. a
    device speaking it out loud.
    """
    registry = await loaded(tmp_path, make_voice_job())
    commits = Commits(registry)

    registry.begin_shutdown()
    latch(registry, "job-1", "voice_deadline")
    await registry.cancel("job-1")

    assert commits.count == 1
    assert registry.get("job-1").execution_state is ExecutionState.CANCELLED

    reloaded = await reload(tmp_path)
    assert await reloaded.recover_after_restart() == []


async def test_a_latch_is_per_job_and_not_a_blanket_switch(tmp_path):
    """Case L — one row's known cause must not un-defer another's.

    This is what stops the latch degenerating into a second ambient flag.
    """
    registry = await loaded(
        tmp_path, make_voice_job(), make_voice_job(id="job-2"),
    )
    commits = Commits(registry)

    registry.begin_shutdown()
    latch(registry, "job-1", "voice_deadline")
    await registry.cancel("job-1")
    await registry.cancel("job-2")

    assert commits.count == 1
    assert registry.get("job-1").execution_state is ExecutionState.CANCELLED
    assert registry.get("job-2").execution_state is ExecutionState.RUNNING

    reloaded = await reload(tmp_path)
    recovered = await reloaded.recover_after_restart()
    assert [job.id for job in recovered] == ["job-2"]
    assert recovered[0].execution_state is ExecutionState.ORPHANED


async def test_an_unlatched_cancellation_during_a_stop_still_defers(tmp_path):
    """Case M — the arms that CANNOT know their cause still defer.

    A `CancelledError` reaching `_attach_completion_callback` or either sync
    `asyncio.wait` cannot tell a barge-in from process death, and those are the
    arms #671 exists for. Unlatched must stay deferred, or the fix is undone.
    """
    registry = await loaded(tmp_path, make_voice_job(creator_peer="telegram"))
    commits = Commits(registry)

    registry.begin_shutdown()
    await registry.cancel("job-1")

    assert commits.count == 0
    assert registry.get("job-1").execution_state is ExecutionState.RUNNING

    reloaded = await reload(tmp_path)
    recovered = await reloaded.recover_after_restart()
    assert len(recovered) == 1
    assert recovered[0].failure == JobFailure("restart_orphan", "Lost on restart")
    assert recovered[0].orphan_notification_pending is True


def continuable_parent(**changes) -> VoiceJob:
    return make_voice_job(
        terminal_at=150.0, expires_at=400.0,
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        result='{"status":"needs_clarification"}',
        awaiting_input=True, continuable_until=400.0, **changes,
    )


async def test_unlatched_continuation_compensation_defers_to_boot(tmp_path):
    """Case N — the THIRD settling site, which settles by DELETION.

    `compensate_unbound_continuation` removes the child row outright, so an
    enumeration of "cancellation terminals" misses it — and deletion is strictly
    worse than a terminal write, because the boot reconciliation then has no row
    to find at all. A crash never runs this compensation, so the child survives
    as ACCEPTED and is orphaned; a graceful stop must reproduce exactly that.
    """
    registry = await loaded(tmp_path, continuable_parent())
    child = replace(
        make_voice_job(), id="job-child", parent_job_id="job-1",
        created_at=200.0, started_at=None,
        execution_state=ExecutionState.ACCEPTED,
    )
    await registry.create_continuation("job-1", child, actor=actor())
    commits = Commits(registry)

    registry.begin_shutdown()
    restored = await registry.compensate_unbound_continuation(
        "job-1", "job-child", actor=actor(),
    )

    assert restored is False
    assert commits.count == 0
    assert registry.get("job-child") is not None
    assert registry.get("job-child").execution_state is ExecutionState.ACCEPTED

    reloaded = await reload(tmp_path, now=500.0)
    recovered = await reloaded.recover_after_restart()
    assert [job.id for job in recovered] == ["job-child"]
    assert recovered[0].execution_state is ExecutionState.ORPHANED
    assert recovered[0].failure == JobFailure("restart_orphan", "Lost on restart")


async def test_latched_continuation_compensation_still_compensates(tmp_path):
    """Case O — an ORDINARY launch failure must still roll back.

    The counterpart to case N, and what stops the third guard becoming a blanket
    "never compensate during a stop": a launch that failed for its own reasons
    still deletes the unbound child and restores the parent's awaiting_input,
    exactly as before.
    """
    registry = await loaded(tmp_path, continuable_parent())
    child = replace(
        make_voice_job(), id="job-child", parent_job_id="job-1",
        created_at=200.0, started_at=None,
        execution_state=ExecutionState.ACCEPTED,
    )
    await registry.create_continuation("job-1", child, actor=actor())
    commits = Commits(registry)

    registry.begin_shutdown()
    latch(registry, "job-child", "launch_rollback")
    restored = await registry.compensate_unbound_continuation(
        "job-1", "job-child", actor=actor(),
    )

    assert restored is True
    assert commits.count == 1
    assert registry.get("job-child") is None
    assert registry.get("job-1").awaiting_input is True

    reloaded = await reload(tmp_path, now=500.0)
    assert await reloaded.recover_after_restart() == []


async def test_compensation_outside_a_stop_is_untouched(tmp_path):
    """Case P — no declaration, no change to the compensation contract."""
    registry = await loaded(tmp_path, continuable_parent())
    child = replace(
        make_voice_job(), id="job-child", parent_job_id="job-1",
        created_at=200.0, started_at=None,
        execution_state=ExecutionState.ACCEPTED,
    )
    await registry.create_continuation("job-1", child, actor=actor())
    commits = Commits(registry)

    restored = await registry.compensate_unbound_continuation(
        "job-1", "job-child", actor=actor(),
    )

    assert restored is True
    assert commits.count == 1
    assert registry.get("job-child") is None


def test_launch_rollback_does_not_latch_a_cancellation():
    """Case Q — the launch-rollback arms must not claim a shutdown cancellation.

    Both seam reviewers reproduced this as the one way the amended mechanism
    could silently restore the original defect: `_start_voice_async_job`'s
    rollback handler is `except BaseException`, so latching unconditionally would
    claim a shutdown `CancelledError` as an ordinary launch failure, settle the
    row here, and leave the boot reconciliation nothing to find.

    This is a SOURCE pin, not a behavioural one, and that is a deliberate
    trade rather than an oversight: driving the real function needs a bound task,
    a route-capacity check, a handoff reservation and a config object, and a
    fragile ten-mock fixture asserting on a rollback path is worse evidence than
    an exact assertion about the guard that makes the decision. It follows the
    AST precedent already accepted in this cluster for pinning `casa_core.main`'s
    declaration placement. What it guarantees is narrow and exactly the mutation
    the reviewers named: the latch is reachable only under an
    `isinstance(<exc>, Exception)` test, and `CancelledError`, `KeyboardInterrupt`
    and `SystemExit` all derive from `BaseException` without deriving from
    `Exception`.
    """
    import ast
    import inspect
    import textwrap

    import tools

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(tools._start_voice_async_job)))

    latching_handlers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if "note_cancel_cause" not in ast.unparse(node):
            continue
        latching_handlers.append(node)

    assert len(latching_handlers) == 1
    handler = latching_handlers[0]

    # The handler must bind the exception, and it must be the BaseException one.
    assert handler.name is not None
    assert isinstance(handler.type, ast.Name)
    assert handler.type.id == "BaseException"

    # Every latch inside it must sit under an `isinstance(<bound>, Exception)`
    # test — asserted structurally rather than by substring, so a comment
    # mentioning isinstance cannot satisfy it.
    guarded = []
    for node in ast.walk(handler):
        if not isinstance(node, ast.If):
            continue
        if "note_cancel_cause" not in ast.unparse(node.body):
            continue
        tests = [node.test] if not isinstance(node.test, ast.BoolOp) else node.test.values
        for test in tests:
            if (isinstance(test, ast.Call)
                    and isinstance(test.func, ast.Name)
                    and test.func.id == "isinstance"
                    and isinstance(test.args[0], ast.Name)
                    and test.args[0].id == handler.name
                    and isinstance(test.args[1], ast.Name)
                    and test.args[1].id == "Exception"):
                guarded.append(node)

    assert len(guarded) == 1
    # ...and no latch escapes that `if`.
    assert ast.unparse(handler).count("note_cancel_cause") == 1


async def test_the_deadline_arm_itself_records_its_cause(tmp_path):
    """Case R — the ARM's enrolment, not just the registry's honouring of it.

    Cases K and J latch by calling `note_cancel_cause` directly, so they pin what
    the registry does with a recorded cause and say nothing about whether the arm
    records one. Measured: with only those cases, deleting the latch call from
    `_voice_deadline_exceeded` left every test green — which would have let a
    later edit silently reinstate the reproduced regression. So this drives the
    real function.
    """
    import tools
    from specialist_registry import SpecialistRegistry

    registry = await loaded(tmp_path, make_voice_job())
    specialists = SpecialistRegistry(
        str(tmp_path / "specialists"), job_registry=registry)
    previous = tools._specialist_registry
    tools._specialist_registry = specialists
    try:
        async def never():
            await asyncio.sleep(3600)

        task = asyncio.create_task(never())
        await asyncio.sleep(0)

        registry.begin_shutdown()
        result = await tools._voice_deadline_exceeded(
            task, "job-1", "researcher")
    finally:
        tools._specialist_registry = previous

    # The caller is told synchronously, which is why a boot notice would be a
    # second and false telling.
    assert result is not None

    row = registry.get("job-1")
    assert row.execution_state is ExecutionState.CANCELLED
    assert row.failure == JobFailure("cancelled", "Delegation cancelled")

    reloaded = await reload(tmp_path)
    assert await reloaded.recover_after_restart() == []


def test_the_prelaunch_bail_itself_records_its_cause():
    """Case S — the pre-launch bail's enrolment.

    A source pin, for the same reason as case Q: the bail sits inside
    `delegate_to_agent`, whose driving needs the whole tool surface, and a
    large-fixture test asserting on a pre-launch branch is weaker evidence than
    an exact assertion about the statement that must precede the cancel. What it
    guarantees is the mutation that matters: the arm records a cause, and it does
    so BEFORE it cancels — recording afterwards would leave the guard already
    consulted and the row already deferred.
    """
    import ast
    from pathlib import Path

    import tools

    # `delegate_to_agent` is registered as an SdkMcpTool and its runtime
    # `.handler` is a decorated wrapper, so the function is located by name in
    # the module source rather than through the tool object.
    module = ast.parse(Path(tools.__file__).read_text(encoding="utf-8"))
    functions = [
        node for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "delegate_to_agent"
    ]
    assert len(functions) == 1

    # Find the branch that bails because the voice budget went during
    # registration: it both records a cause and cancels the delegation.
    # Match STRUCTURALLY on the branch's own statements. Matching on text is
    # wrong here: `ast.unparse` of a nested `if` renders its whole body, so the
    # enclosing `if is_voice:` matches any substring its child contains, and a
    # count asserted against that describes the wrong node.
    def calls(node):
        """Attribute names called directly by this branch's own statements."""
        names = []
        for stmt in node.body:
            if not isinstance(stmt, (ast.Expr, ast.Await)):
                continue
            inner = stmt.value if isinstance(stmt, ast.Expr) else stmt
            while isinstance(inner, ast.Await):
                inner = inner.value
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                names.append(inner.func.attr)
        return names

    def direct(node):
        return [ast.unparse(stmt) for stmt in node.body]

    branches = [
        node for node in ast.walk(functions[0])
        if isinstance(node, ast.If)
        and "note_cancel_cause" in calls(node)
        and "cancel_delegation" in calls(node)
    ]
    assert len(branches) == 1

    statements = direct(branches[0])
    noted = next(
        i for i, text in enumerate(statements) if "note_cancel_cause" in text)
    cancelled = next(
        i for i, text in enumerate(statements) if "cancel_delegation" in text)
    assert noted < cancelled
