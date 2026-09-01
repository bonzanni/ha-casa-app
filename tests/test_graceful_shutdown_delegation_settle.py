"""#767 — regressions around INV-JOB-011's two declared limits and its bus-less path.

The red case (``tests/test_graceful_shutdown_engagement_launch.py``, the
``test_a_graceful_stop_settles_*`` cases) pins the guarantee. These pin what the
guarantee deliberately does NOT do, so a later change that widens the drain into
waiting on a delegated run, or on the registry's unbounded retry, is caught as a
behaviour change rather than slipping in as an improvement:

* a delegation still RUNNING when the ledger closes is left live — the stop
  returns without waiting for it — and boots as exactly one restart orphan
  (INV-JOB-009's crash equivalence, and "lost on restart" is then true);
* a terminal write that FAILED before the stop leaves a registry-owned retry
  that the ledger's close cancels; the row boots as an orphan and the notice
  already sent stands contradicted — the second limit INV-JOB-011 names;
* with no bus at all the bare settle tail is drained the same way and the
  durable row is its real verdict, announced to nobody.

Same shape as the red case: a real ``JobRegistry`` on ``tmp_path``, the
production completion callback over real tasks, the production
``casa_core._shutdown_cleanup`` inside its own ``asyncio.run``; assertions on
durable rows and counts only.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


class _Output:
    def __init__(self, text: str):
        self.text = text
        self.run_aborted = False
        self.run_subtype = "success"
        self.answer_incomplete = False


def _stop(monkeypatch, tmp_path, *, release: bool, with_bus: bool,
          fail_first_write_after_stop: bool) -> dict:
    import casa_core
    import tools
    from job_registry import ExecutionState, JobRegistry
    from specialist_registry import DelegationRecord, SpecialistRegistry

    jobs_path = tmp_path / "jobs.json"
    notices: list[str] = []
    at_close: dict = {}
    retries_at_close: list[int] = []
    gate_holder: dict = {}

    class _Bus:
        def begin_shutdown(self):
            pass

        def agent_loop_tasks(self):
            return []

        def fail_pending(self):
            pass

        async def notify(self, msg):
            notices.append(msg.context["delegation_id"])

    async def _run():
        await gate_holder["gate"].wait()
        return _Output("the answer")

    async def _main():
        registry = JobRegistry(jobs_path)
        await registry.load()
        spec = SpecialistRegistry(str(tmp_path / "specialists"),
                                  job_registry=registry)
        bus = _Bus() if with_bus else None
        cm = MagicMock()
        cm.stop_all = AsyncMock()
        tools.init_tools(
            channel_manager=cm, bus=bus, specialist_registry=spec,
            mcp_registry=MagicMock(), trigger_registry=MagicMock(),
            engagement_registry=MagicMock(), executor_registry=MagicMock())
        gate_holder["gate"] = asyncio.Event()
        rec = DelegationRecord(
            id="d-1", agent="researcher", started_at=time.time(),
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "42", "cid": "x"})
        await spec.register_delegation(rec)
        task = asyncio.create_task(_run())
        tools._attach_completion_callback(task, rec)
        if release:
            gate_holder["gate"].set()
            await task

        if fail_first_write_after_stop:
            # The registry's own disk writer, failing exactly once — the
            # terminal write of the settle tail the first drain gathers.
            inner = registry._write_snapshot_locked
            armed = {"fail": True}

            async def _flaky(jobs):
                if armed["fail"]:
                    armed["fail"] = False
                    raise OSError("disk full")
                return await inner(jobs)

            registry._write_snapshot_locked = _flaky  # type: ignore[method-assign]

        real_close = registry.close

        async def _close():
            at_close["state"] = registry.get("d-1").execution_state
            retries_at_close.append(len(registry._reconciliation_tasks))
            return await real_close()

        monkeypatch.setattr(registry, "close", _close)
        engagement_registry = MagicMock()
        engagement_registry.begin_launch_shutdown = MagicMock(return_value=0)
        engagement_registry.drain_launches = AsyncMock()

        registry.begin_shutdown()
        # A liveness bound on the TEST, not a budget on the work: the stop
        # must return without waiting for a run that never finishes.
        async with asyncio.timeout(10):
            await casa_core._shutdown_cleanup(
                job_registry=registry,
                engagement_registry=engagement_registry,
                scheduler=MagicMock(shutdown=MagicMock()),
                session_sweeper=MagicMock(stop=AsyncMock()),
                freshness_reaper=MagicMock(stop=AsyncMock()),
                runtime=MagicMock(agents={}, claude_code_driver=None),
                ha_facade=None,
                bus=bus if bus is not None else MagicMock(
                    begin_shutdown=MagicMock(),
                    agent_loop_tasks=MagicMock(return_value=[]),
                    fail_pending=MagicMock()),
                loop_tasks=[],
                channel_manager=cm,
                runners=[],
                semantic_memory=MagicMock(close=AsyncMock()),
            )

    asyncio.run(_main())

    async def _boot():
        restarted = JobRegistry(jobs_path)
        await restarted.load()
        recovered = await restarted.recover_after_restart()
        return restarted.get("d-1"), len(recovered)

    row, recovered = asyncio.run(_boot())
    return {"row": row, "recovered": recovered, "notices": notices,
            "at_close": at_close.get("state"),
            "retries_at_close": retries_at_close}


def test_a_run_still_running_at_the_boundary_is_left_live_and_boots_as_one_orphan(
    monkeypatch, tmp_path,
):
    from job_registry import ExecutionState

    facts = _stop(monkeypatch, tmp_path, release=False, with_bus=True,
                  fail_first_write_after_stop=False)
    # The stop returned (the liveness bound did not fire) with the row live.
    assert facts["at_close"] is ExecutionState.RUNNING
    assert facts["notices"] == []
    assert facts["recovered"] == 1
    row = facts["row"]
    assert row.execution_state is ExecutionState.ORPHANED
    assert row.failure is not None and row.failure.kind == "restart_orphan"
    assert row.orphan_notification_pending is True
    assert row.terminal_notification_pending is False


def test_a_terminal_write_that_failed_leaves_its_retry_to_the_ledgers_close(
    monkeypatch, tmp_path,
):
    """The second limit INV-JOB-011 names, pinned as a limit: the retry is
    unbounded by design, the stop does not wait for it, `close()` cancels it,
    and the row boots as an orphan — with the notice already sent for it now
    contradicted. A change that starts awaiting the retry turns this red."""
    from job_registry import ExecutionState

    facts = _stop(monkeypatch, tmp_path, release=True, with_bus=True,
                  fail_first_write_after_stop=True)
    # The tail ran: it announced anyway, and its failed write scheduled ONE
    # retry that was still pending when the ledger closed.
    assert facts["notices"] == ["d-1"]
    assert facts["retries_at_close"] == [1]
    assert facts["at_close"] is ExecutionState.RUNNING
    assert facts["recovered"] == 1
    row = facts["row"]
    assert row.execution_state is ExecutionState.ORPHANED
    assert row.failure is not None and row.failure.kind == "restart_orphan"


def test_without_a_bus_the_bare_settle_tail_is_still_drained(
    monkeypatch, tmp_path,
):
    from job_registry import ExecutionState

    facts = _stop(monkeypatch, tmp_path, release=True, with_bus=False,
                  fail_first_write_after_stop=False)
    assert facts["notices"] == []
    assert facts["at_close"] is ExecutionState.SUCCEEDED
    row = facts["row"]
    assert row.execution_state is ExecutionState.SUCCEEDED
    assert row.failure is None
    assert row.terminal_notification_pending is True
    assert row.orphan_notification_pending is False
    # It still owes its announcement, so recovery returns it — unconverted.
    assert facts["recovered"] == 1
