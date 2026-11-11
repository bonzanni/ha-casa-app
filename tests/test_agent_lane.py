"""One lane and one fault per executing agent: the lane is the agent's, shared by
every chat and every desk of it; a fault refuses every later use until restart;
a configurator use shares one wait deadline with the configurator lease."""
from __future__ import annotations

import asyncio
import time

import pytest

import agent_lane
import configurator_run
import specialist_desk as sd
from plugin_registry import PluginIssue


@pytest.fixture(autouse=True)
def fresh():
    sd_registry = sd.DeskRegistry(now=lambda: 1000.0)       # a fresh world: no lanes, no faults
    yield sd_registry
    agent_lane.reset()


# --- the lane is keyed by the executing agent ----------------------------------------------

def test_one_lane_per_agent_created_once():
    a = agent_lane.LANES.get("finance")
    assert agent_lane.LANES.get("finance") is a
    assert agent_lane.LANES.get("browser") is not a
    assert isinstance(a.lock, asyncio.Lock) and a.role == "finance"
    with pytest.raises(ValueError):
        agent_lane.LANES.get("")


async def test_the_lane_is_shared_across_two_chat_ids_and_two_bots(fresh):
    """The operator's private chat id is the same on every bot: two desks of one
    agent — another chat, another bot — are one lane."""
    d1 = fresh.get_or_create(111, 42, "finance")
    d2 = fresh.get_or_create(111, 43, "finance")
    d3 = fresh.get_or_create(222, 42, "finance")
    other = fresh.get_or_create(111, 42, "browser")
    assert d1 is not d2 and d1.lane is d2.lane is d3.lane is agent_lane.LANES.get("finance")
    assert d1.lock is d2.lock is d3.lock
    assert other.lock is not d1.lock
    # a use held through one chat's desk keeps the other chat's waiting
    order: list = []
    async with d1.use(d1.reserve()):
        waiter = asyncio.create_task(_use(d2, order))
        for _ in range(5):
            await asyncio.sleep(0)
        assert order == [] and d2.waiting == 1 == d1.waiting and d2.lock.locked()
        # another agent's lane is free meanwhile
        async with other.use(other.reserve()) as admitted:
            assert admitted is True
    await waiter
    assert order == ["second"]
    # the logs stay per desk
    d1.append("operator", "q", 1.0)
    assert d2.log == [] and d3.log == []


async def _use(desk, order):
    async with desk.use(desk.reserve()):
        order.append("second")


def test_the_queue_is_the_agents_and_holds_three():
    lane = agent_lane.LANES.get("finance")
    places = [lane.reserve() for _ in range(agent_lane.QUEUE_MAX)]
    assert all(places) and lane.waiting == 3 == sd.DESK_QUEUE_MAX
    assert lane.reserve() is None
    places[0].release()
    places[0].release()                       # idempotent
    assert lane.waiting == 2
    assert lane.reserve() is not None


async def test_a_use_past_its_deadline_holds_nothing():
    lane = agent_lane.LANES.get("finance")
    await lane.lock.acquire()
    place = lane.reserve()
    async with lane.use(place, time.monotonic() + 0.01) as admitted:
        assert admitted is False
    assert place.released and lane.waiting == 0
    lane.lock.release()


async def test_an_unwinding_run_refuses_the_lane_only_until_it_ends():
    lane = agent_lane.LANES.get("finance")
    gate = asyncio.Event()
    run = asyncio.create_task(gate.wait())
    lane.hold_unwinding([run])
    assert lane.faulted == agent_lane.UNWINDING
    assert agent_lane.FAULTS.issues() == []        # not a fault: no health row
    gate.set()
    await run
    assert lane.faulted is None


# --- the fault is the agent's ------------------------------------------------------------

def test_a_fault_is_per_agent_until_restart(fresh):
    fault = agent_lane.FAULTS.mark("finance", "run-1", "probe", reason="run run-1: unconfirmed")
    assert agent_lane.FAULTS.faulted("finance") is fault
    assert (fault.run_id, fault.plugin, fault.since) == ("run-1", "probe", 1000.0)
    assert agent_lane.FAULTS.faulted("browser") is None
    # every desk of the agent, in every chat, refuses
    for desk in (fresh.get_or_create(111, 42, "finance"), fresh.get_or_create(222, 7, "finance")):
        assert desk.faulted == "run run-1: unconfirmed" and desk.fault is fault
    assert fresh.get_or_create(111, 42, "browser").faulted is None
    (issue,) = agent_lane.FAULTS.issues()
    assert isinstance(issue, PluginIssue)
    assert (issue.name, issue.target, issue.reason_code) == ("probe", "specialist:finance", "agent_faulted")
    assert sd.faulted_desk_issues() == [issue]
    agent_lane.reset()                              # a restart clears it
    assert agent_lane.FAULTS.faulted("finance") is None and agent_lane.FAULTS.issues() == []


def test_the_faulted_line_names_no_desk():
    line = agent_lane.faulted_line("📊 Finance")
    assert line == "📊 Finance is out of service until Casa restarts."
    assert "desk" not in line and sd.faulted_line is agent_lane.faulted_line


def test_the_desk_names_are_the_lanes():
    assert sd.DeskBusy is agent_lane.LaneBusy and sd.DeskFaulted is agent_lane.LaneFaulted
    assert sd.DeskFault is agent_lane.Fault and sd.Reservation is agent_lane.Reservation
    assert sd.UNWINDING == agent_lane.UNWINDING
    assert issubclass(configurator_run.ConfiguratorBusy, agent_lane.LaneBusy)


def test_the_modules_own_registry_keeps_the_lanes_it_finds():
    agent_lane.FAULTS.mark("finance", "run-1", "probe")
    sd.DeskRegistry(adopt_lanes=True)
    assert agent_lane.FAULTS.faulted("finance") is not None
    sd.DeskRegistry()
    assert agent_lane.FAULTS.faulted("finance") is None


# --- the configurator's lane and its lease share one deadline -------------------------------

async def test_the_configurator_lane_wait_and_the_lease_wait_share_one_deadline(fresh, monkeypatch):
    """A configurator delegation waiting for its lane spends the same
    ``LEASE_WAIT_S`` its lease wait would: past it, the use holds nothing and the
    refusal is the configurator's typed busy result."""
    from types import SimpleNamespace
    monkeypatch.setattr(configurator_run, "LEASE_WAIT_S", 0.05)
    cfg = SimpleNamespace(role="configurator", kind="executor", session=None)
    desk = fresh.get_or_create(111, 42, "configurator")
    await desk.lock.acquire()                         # another use of the configurator's lane
    place = desk.reserve()

    async def run(*a, **k):
        raise AssertionError("nothing runs past the deadline")
    started = time.monotonic()
    with pytest.raises(configurator_run.ConfiguratorBusy):
        await sd.delegation_use(desk, reservation=place, run=run, cfg=cfg,
                                task_text="t", context_text="")
    assert time.monotonic() - started < 1.0
    assert desk.waiting == 0
    desk.lock.release()
    # the deadline the lane wait used is the one a lease acquire inside would read
    deadline = configurator_run.wait_deadline()
    with configurator_run.waiting_until(deadline):
        assert configurator_run.wait_deadline() == deadline
