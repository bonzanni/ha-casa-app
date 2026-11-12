"""#1545 — a delegation task left behind by a closed event loop must not
reach a later test.

`tools._delegation_settle_tasks` and `tools._delegation_callback_tasks` are
module-level sets. Each member leaves its set through a done callback, which
runs on the member's own loop. When a test's loop closes while a member is
still pending (seen on a slow CI runner: a settle tail minted during the
loop's teardown, after the cancel sweep took its snapshot), that callback
never runs and the task stays in the set. A later test on the same worker that
gathers the whole set then fails with "attached to a different loop".

Production runs one loop and its stop drain already filters by loop, so the
cure is test isolation: an autouse fixture in `tests/conftest.py` drops every
member whose loop is closed before each test.

The pair is ORDERED: the first test leaves a pending task from a closed loop
in each set, exactly as the slow runner did; the second gathers both sets the
way the delegation tests' helpers do and must complete.
"""
from __future__ import annotations

import asyncio

import pytest

import tools as tools_mod

pytestmark = pytest.mark.unit


def test_a_closed_loop_leaves_pending_delegation_tasks_behind():
    loop = asyncio.new_event_loop()
    try:
        for task_set in (tools_mod._delegation_settle_tasks,
                         tools_mod._delegation_callback_tasks):
            task = loop.create_task(asyncio.sleep(3600))
            task_set.add(task)
            task.add_done_callback(task_set.discard)
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert sum(t.get_loop() is loop
               for t in (*tools_mod._delegation_settle_tasks,
                         *tools_mod._delegation_callback_tasks)) == 2


async def test_the_next_test_gathers_both_sets_on_its_own_loop():
    await asyncio.wait_for(
        asyncio.gather(*list(tools_mod._delegation_callback_tasks),
                       *list(tools_mod._delegation_settle_tasks)),
        timeout=5)
    assert len(tools_mod._delegation_settle_tasks) == 0
    assert len(tools_mod._delegation_callback_tasks) == 0
