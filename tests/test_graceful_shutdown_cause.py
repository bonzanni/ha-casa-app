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
        # The OPERATOR is load-bearing and must be asserted, not just the
        # operands: a reviewer reproduced that turning `created and isinstance(
        # exc, Exception)` into `created or isinstance(...)` left an earlier
        # version of this test green while a shutdown cancellation was latched
        # and its row lost. So require an `and` over exactly two operands.
        if not isinstance(node.test, ast.BoolOp):
            continue
        if not isinstance(node.test.op, ast.And):
            continue
        if len(node.test.values) != 2:
            continue
        created, kind_test = node.test.values
        if not (isinstance(created, ast.Name) and created.id == "created"):
            continue
        if (isinstance(kind_test, ast.Call)
                and isinstance(kind_test.func, ast.Name)
                and kind_test.func.id == "isinstance"
                and isinstance(kind_test.args[0], ast.Name)
                and kind_test.args[0].id == handler.name
                and isinstance(kind_test.args[1], ast.Name)
                and kind_test.args[1].id == "Exception"):
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


async def test_a_shutdown_cancellation_in_the_rollback_arm_leaves_the_row(tmp_path):
    """Case T — the rollback arm, driven for REAL rather than pinned by source.

    A reviewer showed the source pin above was not enough on its own, and also
    that this path IS drivable: `_create_voice_lifecycle_task` runs after the row
    is durable and before `bind_task`, so raising a `CancelledError` there
    reproduces exactly what a graceful stop does to a launch in flight.

    The assertion that matters: the arm must NOT record a cause, so the row is
    left ACCEPTED for the boot reconciliation. Latching here would settle the row
    in a dying process and leave recovery nothing to find — the original defect,
    reintroduced through the one arm whose `except BaseException` cannot tell an
    ordinary launch failure from process death without looking.
    """
    import tools
    from specialist_registry import SpecialistRegistry

    registry = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await registry.load()
    specialists = SpecialistRegistry(
        str(tmp_path / "specialists"), job_registry=registry)

    origin = {
        "role": "concierge",
        "chat_id": "scope-1",
        "voice_route_id": "route-1",
        "origin_device_id": "device-kitchen",
    }

    def _boom(**_kwargs):
        raise asyncio.CancelledError()

    previous_registry = tools._specialist_registry
    previous_factory = tools._create_voice_lifecycle_task
    tools._specialist_registry = specialists
    tools._create_voice_lifecycle_task = _boom
    try:
        registry.begin_shutdown()
        try:
            await tools._start_voice_async_job(
                cfg=None,
                specialist_role="researcher",
                task_text="How long does the boiler run?",
                context_text="",
                origin=origin,
                resolution=None,
                permit=None,
                handoff=tools._PermitHandoff(),
            )
        except asyncio.CancelledError:
            pass
        else:  # pragma: no cover - the injected cancellation must propagate
            raise AssertionError("the rollback arm did not re-raise")
    finally:
        tools._specialist_registry = previous_registry
        tools._create_voice_lifecycle_task = previous_factory

    live = [job for job in registry.all()]
    assert len(live) == 1
    assert live[0].execution_state is ExecutionState.ACCEPTED
    assert live[0].failure is None
    # No cause was recorded, so the row is the boot reconciliation's.
    assert registry._cancel_causes == {}

    reloaded = await reload(tmp_path)
    recovered = await reloaded.recover_after_restart()
    assert len(recovered) == 1
    assert recovered[0].execution_state is ExecutionState.ORPHANED
    assert recovered[0].failure == JobFailure("restart_orphan", "Lost on restart")


def test_main_calls_the_declaration_rather_than_merely_mentioning_it():
    """Case U — the declaration must be a real, executed call.

    A reviewer reproduced that the accepted file's placement case matches
    `ast.unparse` by substring, so `False and job_registry.begin_shutdown()`
    satisfies it while the declaration never runs — and all mapped tests stayed
    green while a stop again settled a live row.

    That accepted case is FROZEN and is deliberately not edited here; this is a
    supplementary binding that closes the hole structurally. It requires the
    statement following `await stop_event.wait()` to be exactly a bare
    zero-argument call to `job_registry.begin_shutdown()` — an expression
    statement whose value is that Call and nothing else, so no boolean operand,
    no conditional and no comparison can stand in for it.
    """
    import ast
    import inspect
    import textwrap

    import casa_core

    tree = ast.parse(textwrap.dedent(inspect.getsource(casa_core.main)))
    body = tree.body[0].body  # type: ignore[attr-defined]

    waits = [
        i for i, node in enumerate(body)
        if "stop_event.wait()" in ast.unparse(node)
    ]
    assert len(waits) == 1

    statement = body[waits[0] + 1]
    assert isinstance(statement, ast.Expr)
    call = statement.value
    assert isinstance(call, ast.Call)
    assert call.args == []
    assert call.keywords == []
    assert isinstance(call.func, ast.Attribute)
    assert call.func.attr == "begin_shutdown"
    assert isinstance(call.func.value, ast.Name)
    assert call.func.value.id == "job_registry"


async def test_an_ordinary_launch_failure_still_terminalizes_during_a_stop(tmp_path):
    """Case V — the ordinary-`Exception` counterpart to case T, DRIVEN.

    Both reviewers showed the source pin on this arm claims coverage it does not
    have: nesting the latch under `if False`, or latching a wrong job id, left
    every mapped case green while an ordinary launch failure during a stop was
    deferred and boot recovered it as a false restart orphan. So the arm's
    ordinary path is driven here rather than asserted about.

    A `RuntimeError` from `_create_voice_lifecycle_task` — raised after the row
    is durable and before `bind_task` — is a launch failure the arm knows the
    cause of. It must terminalize now, and boot must recover nothing.
    """
    import tools
    from specialist_registry import SpecialistRegistry

    registry = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await registry.load()
    specialists = SpecialistRegistry(
        str(tmp_path / "specialists"), job_registry=registry)

    origin = {
        "role": "concierge",
        "chat_id": "scope-1",
        "voice_route_id": "route-1",
        "origin_device_id": "device-kitchen",
    }

    def _boom(**_kwargs):
        raise RuntimeError("the lifecycle task could not be created")

    previous_registry = tools._specialist_registry
    previous_factory = tools._create_voice_lifecycle_task
    tools._specialist_registry = specialists
    tools._create_voice_lifecycle_task = _boom
    try:
        registry.begin_shutdown()
        try:
            await tools._start_voice_async_job(
                cfg=None,
                specialist_role="researcher",
                task_text="How long does the boiler run?",
                context_text="",
                origin=origin,
                resolution=None,
                permit=None,
                handoff=tools._PermitHandoff(),
            )
        except RuntimeError:
            pass
        else:  # pragma: no cover - the injected failure must propagate
            raise AssertionError("the rollback arm did not re-raise")
    finally:
        tools._specialist_registry = previous_registry
        tools._create_voice_lifecycle_task = previous_factory

    rows = list(registry.all())
    assert len(rows) == 1
    # The cause was recorded for THIS row, which is what a wrong-id latch breaks.
    assert list(registry._cancel_causes) == [rows[0].id]
    assert rows[0].execution_state is ExecutionState.CANCELLED

    reloaded = await reload(tmp_path)
    assert await reloaded.recover_after_restart() == []


async def test_the_prelaunch_bail_terminalizes_its_own_row_during_a_stop(
    monkeypatch, tmp_path,
):
    """Case W — the pre-launch bail, DRIVEN through the real tool.

    A reviewer showed the source pin on this branch cannot see WHICH row is
    latched: replacing `delegation_id` with a literal wrong id left all 22 mapped
    cases green, while the real row stayed RUNNING and boot recovered it as a
    false `ORPHANED`/`READY` restart orphan for a specialist that never started.
    A pin that cannot see the receiver is not evidence about the receiver, so the
    branch is driven here.

    `_voice_wait_from_deadline` is called at exactly two sites: once before
    `register_delegation` and once after, precisely because registration consumes
    wall-clock time. Returning a budget first and `None` second is therefore the
    exact condition of the POST-registration bail, and nothing else reaches it.
    """
    import json
    from unittest.mock import MagicMock

    import agent as agent_mod
    import tools as tm
    from config import AgentConfig, DelegateEntry
    from specialist_registry import SpecialistRegistry

    try:
        from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
    except ImportError:  # pragma: no cover - path shape differs by invocation
        from role_artifact_stub import STUB_ROLE_ARTIFACT

    registry = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await registry.load()
    specialists = SpecialistRegistry(
        str(tmp_path / "specialists"), job_registry=registry)

    def cfg(role: str, delegates: tuple[str, ...] = ()) -> AgentConfig:
        built = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
        built.delegates = [
            DelegateEntry(agent=d, purpose="p", when="w") for d in delegates
        ]
        return built

    tm.init_tools(
        channel_manager=MagicMock(), bus=MagicMock(),
        specialist_registry=specialists, mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_role_map={
            "assistant": cfg("assistant", delegates=("researcher",)),
            "researcher": cfg("researcher"),
        },
    )

    budgets = iter([5.0, None])
    monkeypatch.setattr(
        tm, "_voice_wait_from_deadline", lambda *_a, **_k: next(budgets))

    async def _must_not_launch(*_a, **_k):
        raise AssertionError("the specialist must not be started")

    monkeypatch.setattr(tm, "_run_delegated_agent_bounded", _must_not_launch)

    # A plain voice turn from a NON-concierge caller: the concierge role is the
    # one that must use WS handoff delivery, and a sync concierge voice call with
    # no reservation is refused before it ever reaches the deadline recompute.
    # `is_voice` is what matters here, and the budget is supplied by the patch
    # above rather than by a real clock.
    origin = {
        "role": "assistant", "execution_role": "assistant",
        "channel": "voice", "chat_id": "c1", "cid": "t", "user_text": "hi",
    }

    registry.begin_shutdown()
    token = agent_mod.origin_var.set(origin)
    try:
        result = await tm.delegate_to_agent.handler({
            "agent": "researcher",
            "task": "How long does the boiler run?",
            "mode": "sync",
        })
    finally:
        agent_mod.origin_var.reset(token)

    payload = json.loads(result["content"][0]["text"])
    assert payload["kind"] == "deadline_exceeded"

    rows = list(registry.all())
    assert len(rows) == 1
    # The cause is recorded against the row that exists — the assertion a source
    # pin cannot make.
    assert list(registry._cancel_causes) == [rows[0].id]
    assert rows[0].execution_state is ExecutionState.CANCELLED

    reloaded = await reload(tmp_path)
    assert await reloaded.recover_after_restart() == []
