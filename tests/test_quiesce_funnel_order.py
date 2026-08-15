"""#599 — the kill happens before anything the operator can see.

The defect was an ORDERING one: the terminal record committed, then the funnel
spent the broker drain, the summary finalize, the open-question settle and four
Telegram round-trips before it reached an unbounded ``s6-rc -d change`` — with
the CLI alive and holding its native tools for that whole span. These tests
assert the order, and that the funnel still finishes when the ladder misbehaves.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from engagement_registry import EngagementRegistry
from engagement_uids import UID_BASE

pytestmark = [pytest.mark.asyncio]

UID = UID_BASE + 31


async def _wire(tmp_path, order, *, owner=None, driver=None):
    """A registry + channel manager that record their call order."""
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="dev", driver="claude_code", task="t",
        origin={"role": "assistant", "channel": "telegram"}, topic_id=42,
    )
    rec.allocated_uid = UID
    if owner is not None:
        reg.set_quiesce_owner(owner)

    tch = MagicMock()

    async def rec_send(*a, **k):
        order.append("telegram:send")
    async def rec_resp(*a, **k):
        order.append("telegram:send")
    async def rec_state(*a, **k):
        order.append("telegram:state")
    async def rec_close(*a, **k):
        order.append("telegram:close")
    tch.send_to_topic = rec_send
    tch.send_response_to_topic = rec_resp
    tch.update_topic_state = rec_state
    tch.close_topic = rec_close
    cm = MagicMock(); cm.get.return_value = tch
    bus = MagicMock(); bus.notify = AsyncMock()
    init_tools(channel_manager=cm, bus=bus, specialist_registry=MagicMock(),
               mcp_registry=MagicMock(), trigger_registry=MagicMock(),
               engagement_registry=reg)
    return reg, rec, bus


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "error"])
async def test_the_kill_precedes_every_operator_visible_effect(tmp_path, outcome):
    """EVERY terminal outcome, not just cancellation: a single model turn can
    emit parallel tool calls, so a completing CLI is not necessarily idle."""
    from tools import _finalize_engagement

    order: list[str] = []

    async def owner(record):
        # The ladder is SCHEDULED by the terminal commit, so merely starting
        # early proves nothing — it has to have FINISHED before anything the
        # operator can see. Yielding here is what separates the two: without the
        # funnel's wait, the Telegram effects run while this is still parked.
        order.append("quiesce:start")
        # Long enough that no number of incidental awaits in the funnel can let
        # this finish by coincidence: only an actual WAIT orders it before the
        # sends. (An earlier version yielded a few times and passed even with
        # the wait deleted — the funnel's own awaits covered it.)
        await asyncio.sleep(0.2)
        order.append("quiesce:end")
        await reg.clear_quiesce_pending(record.id)
        return True

    import tools
    tools._QUIESCE_FUNNEL_TIMEOUT_S = 5.0
    reg, rec, bus = await _wire(tmp_path, order, owner=owner)
    await _finalize_engagement(rec, outcome=outcome, text="done", artifacts=[],
                               next_steps=[], driver=None)

    assert "quiesce:end" in order, "the uid was never quiesced"
    first_visible = next((i for i, e in enumerate(order)
                          if e.startswith("telegram:")), None)
    if first_visible is not None:
        assert order.index("quiesce:end") < first_visible, (
            f"an operator-visible effect ran before the kill finished: {order}")


async def test_the_funnel_finishes_when_the_ladder_overruns(tmp_path):
    """A ladder that hangs must not wedge the notification behind it — that
    would be strictly worse than the defect being fixed."""
    import tools
    from tools import _finalize_engagement

    order: list[str] = []
    entered = asyncio.Event()

    async def hanging_owner(record):
        entered.set()
        await asyncio.sleep(60)
        return True

    reg, rec, bus = await _wire(tmp_path, order, owner=hanging_owner)
    tools._QUIESCE_FUNNEL_TIMEOUT_S = 0.05
    await asyncio.wait_for(
        _finalize_engagement(rec, outcome="cancelled", text="x", artifacts=[],
                             next_steps=[], driver=None),
        timeout=5)
    assert entered.is_set()
    assert bus.notify.await_count == 1, "the bus notification was wedged"
    task = reg._quiesce_tasks.get(rec.id)
    if task is not None:
        assert not task.done(), "the ladder was cancelled by the funnel's bound"
        task.cancel()


async def test_the_funnel_finishes_when_driver_cancel_hangs(tmp_path):
    """Sol, design round 3: bounding ``stop_service`` alone is not enough —
    ``cancel`` also waits on the compile lock, the logger stop and a recompile,
    all ahead of the bus notification and the retains."""
    import tools
    from tools import _finalize_engagement

    order: list[str] = []
    reg, rec, bus = await _wire(tmp_path, order)

    class HangingDriver:
        async def cancel(self, engagement):
            await asyncio.sleep(60)

    tools._DRIVER_CANCEL_TIMEOUT_S = 0.05
    # The outer bound is the assertion: an UNBOUNDED teardown makes this raise
    # rather than merely finish late (a slow pass would pin nothing).
    await asyncio.wait_for(
        _finalize_engagement(rec, outcome="cancelled", text="x", artifacts=[],
                             next_steps=[], driver=HangingDriver()),
        timeout=5)
    assert bus.notify.await_count == 1, "a hung teardown wedged the funnel"


async def test_a_record_with_no_uid_does_not_wait(tmp_path):
    """An in-casa specialist has no OS identity — nothing to wait for."""
    from tools import _finalize_engagement

    order: list[str] = []
    reg, rec, bus = await _wire(tmp_path, order)
    rec.driver = "in_casa"
    rec.allocated_uid = -1
    await _finalize_engagement(rec, outcome="completed", text="x", artifacts=[],
                               next_steps=[], driver=None)
    assert "quiesce" not in order
    assert bus.notify.await_count == 1
