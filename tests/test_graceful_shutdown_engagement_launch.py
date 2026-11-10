"""The graceful stop's cleanup step, ``casa_core._shutdown_cleanup``.

Two things are pinned here: that ``casa_core.main`` awaits the cleanup step
unconditionally after its stop wait (a structural pin), and INV-JOB-011's
delegation settle tail at the job ledger's close boundary (#767).

The final cancellation is delivered by the edge that actually fires at a
graceful stop and by no other: ``asyncio.run``'s ``_cancel_all_tasks``, which
runs AFTER the main coroutine has returned — ``job_registry.begin_shutdown``'s
own docstring names it as such. That is why these tests are SYNCHRONOUS and
call ``asyncio.run`` themselves: an ``asyncio``-marked test runs inside a loop
somebody else shuts down, and the final cancellation sweep is exactly the
thing under test.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


async def _spin_until(predicate, *, what: str, limit: int = 4000):
    """Yield until ``predicate()`` — no sleeps, no wall clock.

    A bound on ITERATIONS is not an elapsed allowance (D21): it fires when the
    loop has run ``limit`` times without the awaited state appearing, which is
    a no-output condition, and it fails loudly instead of hanging the suite.
    """
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"never observed: {what}")


def test_main_awaits_the_shutdown_cleanup_unconditionally_after_the_stop_wait():
    """The one edge the behavioural cases cannot witness: `main` calling it.

    The #767 cases below EXECUTE `casa_core._shutdown_cleanup` — they do not
    re-implement it, and every mutant inside it turns them red. What they
    cannot see is whether `main` still calls it: they obtain the coroutine and
    await it themselves, so deleting the call from `main`'s stop block leaves
    them green while a real SIGTERM falls through to bare task cancellation.
    An acceptance round named exactly that mutant.

    This is a STRUCTURAL pin and it is weaker than the cases above, which is
    said here rather than left to be discovered: it reads `main`'s AST. It is
    written to kill the three mutants that shape admits — the call DELETED, the
    call made unreachable (`if False:`, or any other conditional nesting), and
    the call moved ahead of the stop-event wait — by requiring the await to sit
    as a DIRECT, unconditional statement of `main`'s own body, after the
    statement that waits for the stop signal. It cannot see a reordering WITHIN
    the stop block.
    """
    import ast
    import inspect

    import casa_core

    tree = ast.parse(inspect.getsource(casa_core))
    main = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                 and n.name == "main"), None)
    assert main is not None, "casa_core.main is gone"

    def _mentions(node, needle: str) -> bool:
        return any(isinstance(n, ast.Name) and n.id == needle
                   or isinstance(n, ast.Attribute) and n.attr == needle
                   for n in ast.walk(node))

    stop_wait_at = [i for i, st in enumerate(main.body)
                    if _mentions(st, "stop_event") and _mentions(st, "wait")]
    cleanup_at = [i for i, st in enumerate(main.body)
                  if isinstance(st, ast.Expr)
                  and isinstance(st.value, ast.Await)
                  and _mentions(st.value, "_shutdown_cleanup")]

    assert stop_wait_at, "main no longer waits on the stop event"
    assert cleanup_at, (
        "main does not await _shutdown_cleanup as a direct, unconditional "
        "statement of its own body — a call nested under any conditional, or "
        "deleted outright, leaves a real graceful stop falling through to bare "
        "task cancellation while every behavioural case below stays green")
    assert min(cleanup_at) > max(stop_wait_at), (
        "main awaits _shutdown_cleanup before it waits for the stop signal")


# ---------------------------------------------------------------------------
# #767 — the delegation SETTLE tail and the graceful stop's ledger-close boundary
# ---------------------------------------------------------------------------
#
# Red case for **INV-JOB-011**, declared under D34: at the graceful stop's
# job-ledger close boundary, every delegation that has already produced a
# success or non-cancellation verdict has had its completion callback run, its
# terminal-write attempt made, and every resulting settle tail awaited — so a
# delegation that finished during the stop reaches the ledger as its real
# outcome, never as a settle tail killed before its first step.
#
# Specified externally (terra, MODE: SPECIFY) against
# 560cf22af557df7eb15fead24691f7a718daa40d; accepted by sol. Written before any
# production change.
#
# What is REAL here: the ``JobRegistry`` on disk, the ``SpecialistRegistry``
# over it, the production ``tools._attach_completion_callback`` (the ONLY
# producer of a settle tail) over real ``asyncio.Task``s, and the production
# ``casa_core._shutdown_cleanup`` executed inside its own ``asyncio.run`` so
# that ``_cancel_all_tasks`` is the post-cleanup canceller, exactly as in
# ``main``. What is faked completes WITHOUT yielding unless the arm needs a
# window, so a missed tail is never handed an incidental extra turn.
#
# Every assertion is on durable facts — the rows at the ledger-close boundary
# and the rows a fresh registry recovers — and on notice counts. Nothing here
# reads a task set, calls a drain, or asserts a trace of the chosen
# implementation: an arrangement assertion would pass a fix that arranged
# without delivering.
#
# The three interleavings, each a falsifier the design named:
#
# * EARLY — its verdict landed BEFORE the stop; its tail is pending when the
#   cleanup begins. Its notice must be enqueued before any resident's
#   ``aclose()`` is entered (a live pool is what lets the notice be told now).
# * LATE-A / LATE-B — A finishes inside a resident's ``aclose()`` window; A's
#   notice is held until the statement immediately before the ledger-close
#   boundary, then releases B's RUN and awaits it. B's tail is therefore born
#   strictly after any snapshot that saw A, and its write has not published
#   when A's tail completes: a drain that gathers ONE snapshot closes the
#   ledger with B still live.
# * LATE-C — released by the statement immediately before the boundary, on a
#   loop turn that lets C's run complete but leaves C's production done
#   callback QUEUED: at the boundary C is done and has no tail yet. A drain
#   that looks only at the tail set closes the ledger with C still live. Run
#   with a successful verdict and with a raised ``RuntimeError``, whose typed
#   kind must survive as itself.


class _StopOutput:
    """The shape the production completion callback reads off a finished
    delegated run (``run_aborted``, ``text``)."""

    def __init__(self, text: str):
        self.text = text
        self.run_aborted = False
        self.run_subtype = "success"
        self.answer_incomplete = False


def _run_delegation_stop(monkeypatch, tmp_path, *, late_pair: bool,
                         queued: bool, queued_raises: bool = False) -> dict:
    """One graceful stop over real delegation tails, and the facts it leaves."""
    import casa_core
    import tools
    from job_registry import ExecutionState, JobRegistry
    from specialist_registry import DelegationRecord, SpecialistRegistry

    jobs_path = tmp_path / "jobs.json"
    ids = ["early"] + (["late-a", "late-b"] if late_pair else []) \
        + (["late-c"] if queued else [])
    origin = {"role": "assistant", "channel": "telegram", "_tg_bot": 111, "chat_id": "42",
              "cid": "x"}

    flags = {"aclose_entered": False, "begin_shutdown": False}
    gates: dict[str, asyncio.Event] = {}
    tasks: dict[str, asyncio.Task] = {}
    notices: list[tuple] = []          # (delegation_id, aclose_entered)
    at_close: dict = {}

    class _Bus:
        def begin_shutdown(self):
            flags["begin_shutdown"] = True

        def agent_loop_tasks(self):
            return []

        # #895: the stop reads the bus's live DISPATCH tasks too, at the
        # completion record. This double has no consumer and no dispatch;
        # the record's count is pinned on a REAL bus in
        # tests/test_shutdown_pool_and_refusal.py, never here.
        def live_dispatch_tasks(self):
            return []

        def fail_pending(self):
            pass

        async def notify(self, msg):
            did = msg.context["delegation_id"]
            if did == "late-a":
                # Held until the statement before the boundary; then A's
                # notice releases B's RUN and waits for the run alone — never
                # for B's tail, which does not exist yet.
                await gates["boundary"].wait()
                gates["late-b"].set()
                await tasks["late-b"]
            notices.append((did, flags["aclose_entered"]))

    class _Agent:
        async def aclose(self):
            flags["aclose_entered"] = True
            if late_pair:
                gates["late-a"].set()
                await _spin_until(lambda: tasks["late-a"].done(),
                                  what="LATE-A's run finished in the "
                                       "aclose window")
            else:
                # The window a real pool drain is: one turn, nothing more.
                await asyncio.sleep(0)

    class _Runner:
        async def cleanup(self):
            # The statement immediately before the ledger-close boundary:
            # the last ingress runner's cleanup.
            gates["boundary"].set()
            if queued:
                gates["late-c"].set()
                # C's run is queued AHEAD of this continuation: it completes
                # on this turn, and its production done callback lands
                # BEHIND the continuation — so the boundary is reached with
                # C done and no tail yet.
                await asyncio.sleep(0)

    async def _run(did: str):
        await gates[did].wait()
        if did == "late-c" and queued_raises:
            raise RuntimeError("boom")
        return _StopOutput(f"answer from {did}")

    async def _main():
        registry = JobRegistry(jobs_path)
        await registry.load()
        spec = SpecialistRegistry(str(tmp_path / "specialists"),
                                  job_registry=registry)
        bus = _Bus()
        cm = MagicMock()
        cm.stop_all = AsyncMock()
        tools.init_tools(
            channel_manager=cm, bus=bus, specialist_registry=spec,
            mcp_registry=MagicMock(), trigger_registry=MagicMock(),
            engagement_registry=MagicMock())

        for did in ids:
            gates[did] = asyncio.Event()
        gates["boundary"] = asyncio.Event()
        for did in ids:
            rec = DelegationRecord(id=did, agent="researcher",
                                   started_at=time.time(), origin=origin)
            await spec.register_delegation(rec)
            tasks[did] = asyncio.create_task(_run(did))
            # The PRODUCTION callback — the only thing that mints a tail.
            tools._attach_completion_callback(tasks[did], rec)

        # EARLY's verdict lands before the stop. Its run is awaited to
        # completion; its done callback has run; its tail is pending.
        gates["early"].set()
        await tasks["early"]
        assert tasks["early"].done()
        assert registry.get("early").execution_state is ExecutionState.RUNNING

        real_close = registry.close

        async def _close():
            # The ledger-close boundary: durable facts only.
            at_close.update({
                did: registry.get(did).execution_state for did in ids})
            return await real_close()

        monkeypatch.setattr(registry, "close", _close)

        engagement_registry = MagicMock()
        engagement_registry.begin_launch_shutdown = MagicMock(return_value=0)
        engagement_registry.drain_launches = AsyncMock()

        # main's own two statements after the stop wait, in order.
        registry.begin_shutdown()
        await casa_core._shutdown_cleanup(
            job_registry=registry,
            engagement_registry=engagement_registry,
            scheduler=MagicMock(shutdown=MagicMock()),
            session_sweeper=MagicMock(stop=AsyncMock()),
            runtime=MagicMock(agents={"assistant": _Agent()}),
            ha_facade=None,
            bus=bus,
            loop_tasks=[],
            channel_manager=cm,
            runners=[_Runner()],
        )
        # main() returns here; asyncio.run's _cancel_all_tasks is next.

    asyncio.run(_main())

    async def _boot():
        restarted = JobRegistry(jobs_path)
        await restarted.load()
        await restarted.recover_after_restart()
        return {did: restarted.get(did) for did in ids}

    rows = asyncio.run(_boot())
    return {
        "ids": ids,
        "rows": rows,
        "converted": sum(1 for r in rows.values()
                         if r.execution_state is ExecutionState.ORPHANED),
        "restart_orphans": sum(
            1 for r in rows.values()
            if r.failure is not None and r.failure.kind == "restart_orphan"),
        "at_close": at_close,
        "notices": notices,
    }


def _assert_settled(facts: dict, *, failed: dict | None = None) -> None:
    """The durable promise, in counts first and states second."""
    from job_registry import ExecutionState

    failed = failed or {}
    ids = facts["ids"]
    rows = facts["rows"]
    # 1. Recovery converted NOTHING — no row was live at boot.
    assert facts["converted"] == 0, facts
    assert facts["restart_orphans"] == 0, facts
    # 2. Every row is its own verdict, and still owes its announcement.
    for did in ids:
        row = rows[did]
        if did in failed:
            assert row.execution_state is ExecutionState.FAILED, (did, row)
            assert row.failure is not None and row.failure.kind == failed[did], (
                did, row.failure)
        else:
            assert row.execution_state is ExecutionState.SUCCEEDED, (did, row)
            assert row.failure is None, (did, row)
        assert row.terminal_notification_pending is True, (did, row)
        assert row.orphan_notification_pending is False, (did, row)
    # 3. At the ledger-close boundary every row was already terminal.
    live = {ExecutionState.RUNNING, ExecutionState.ACCEPTED}
    assert set(facts["at_close"]) == set(ids), facts["at_close"]
    assert sum(1 for s in facts["at_close"].values() if s in live) == 0, (
        facts["at_close"])
    assert sum(1 for s in facts["at_close"].values()
               if s is ExecutionState.SUCCEEDED) == len(ids) - len(failed)
    assert sum(1 for s in facts["at_close"].values()
               if s is ExecutionState.FAILED) == len(failed)
    # 4. Exactly one notice per delegation.
    assert sorted(d for d, _ in facts["notices"]) == sorted(ids), facts["notices"]
    # 5. A verdict that landed BEFORE the stop is told while the resident
    #    pools are still up — before any aclose() was entered.
    early = [entered for d, entered in facts["notices"] if d == "early"]
    assert early == [False], facts["notices"]


def test_a_graceful_stop_settles_a_verdict_that_landed_before_it(
    monkeypatch, tmp_path,
):
    """EARLY alone: the tail that was pending when the stop began."""
    facts = _run_delegation_stop(monkeypatch, tmp_path, late_pair=False,
                                 queued=False)
    _assert_settled(facts)


def test_a_graceful_stop_settles_a_verdict_born_while_it_was_already_draining(
    monkeypatch, tmp_path,
):
    """LATE-A / LATE-B: B's tail is born after any snapshot that saw A."""
    facts = _run_delegation_stop(monkeypatch, tmp_path, late_pair=True,
                                 queued=False)
    _assert_settled(facts)


@pytest.mark.parametrize("raises", [False, True])
def test_a_graceful_stop_settles_a_run_whose_callback_is_still_queued(
    monkeypatch, tmp_path, raises,
):
    """LATE-C: done at the boundary, callback queued, no tail yet — as a
    success and as a typed failure whose kind must survive as itself."""
    import tools

    facts = _run_delegation_stop(monkeypatch, tmp_path, late_pair=False,
                                 queued=True, queued_raises=raises)
    failed = ({"late-c": tools._classify_error(RuntimeError("boom")).value}
              if raises else {})
    assert "restart_orphan" not in failed.values()
    _assert_settled(facts, failed=failed)
