"""One lane and one fault per executing agent: the lane is the agent's, shared by
every chat and every bot; a fault refuses every later use until restart; a
configurator delegation shares one wait deadline with the configurator lease."""
from __future__ import annotations

import asyncio
import time

import pytest

import agent_lane
import configurator_run
import tools
from plugin_registry import PluginIssue


@pytest.fixture(autouse=True)
def fresh():
    agent_lane.reset(now=lambda: 1000.0)       # a fresh world: no lanes, no faults
    yield
    agent_lane.reset()


# --- the lane is keyed by the executing agent ----------------------------------------------

def test_one_lane_per_agent_created_once():
    a = agent_lane.LANES.get("finance")
    assert agent_lane.LANES.get("finance") is a
    assert agent_lane.LANES.get("browser") is not a
    assert isinstance(a.lock, asyncio.Lock) and a.role == "finance"
    with pytest.raises(ValueError):
        agent_lane.LANES.get("")


async def test_one_agents_uses_wait_on_each_other_and_another_agent_runs_meanwhile():
    """The lane is keyed by the agent alone: whichever chat or bot a use comes
    from, a second use of the same agent waits for the first, and another
    agent's lane is free meanwhile."""
    finance = agent_lane.LANES.get("finance")
    other = agent_lane.LANES.get("browser")
    assert other.lock is not finance.lock
    order: list = []
    async with finance.use(finance.reserve()):
        waiter = asyncio.create_task(_use(finance, order))
        for _ in range(5):
            await asyncio.sleep(0)
        assert order == [] and finance.waiting == 1 and finance.lock.locked()
        async with other.use(other.reserve()) as admitted:
            assert admitted is True
    await waiter
    assert order == ["second"]


async def _use(lane, order):
    async with lane.use(lane.reserve()):
        order.append("second")


def test_the_queue_is_the_agents_and_holds_three():
    lane = agent_lane.LANES.get("finance")
    places = [lane.reserve() for _ in range(agent_lane.QUEUE_MAX)]
    assert all(places) and lane.waiting == 3 == agent_lane.QUEUE_MAX
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

def test_a_fault_is_per_agent_until_restart():
    fault = agent_lane.FAULTS.mark("finance", "run-1", "probe", reason="run run-1: unconfirmed")
    assert agent_lane.FAULTS.faulted("finance") is fault
    assert (fault.run_id, fault.plugin, fault.since) == ("run-1", "probe", 1000.0)
    assert agent_lane.FAULTS.faulted("browser") is None
    # the agent's lane refuses, another agent's does not
    lane = agent_lane.LANES.get("finance")
    assert lane.faulted == "run run-1: unconfirmed" and lane.fault is fault
    assert agent_lane.LANES.get("browser").faulted is None
    (issue,) = agent_lane.FAULTS.issues()
    assert isinstance(issue, PluginIssue)
    assert (issue.name, issue.target, issue.reason_code) == ("probe", "specialist:finance", "agent_faulted")
    agent_lane.reset()                              # a restart clears it
    assert agent_lane.FAULTS.faulted("finance") is None and agent_lane.FAULTS.issues() == []


def test_the_faulted_line_names_the_agent():
    line = agent_lane.faulted_line("📊 Finance")
    assert line == "📊 Finance is out of service until Casa restarts."


def test_the_configurators_busy_is_a_lane_busy():
    assert issubclass(configurator_run.ConfiguratorBusy, agent_lane.LaneBusy)


# --- the configurator's lane and its lease share one deadline -------------------------------

async def test_the_configurator_lane_wait_and_the_lease_wait_share_one_deadline(monkeypatch):
    """A configurator delegation waiting for its lane spends the same
    ``LEASE_WAIT_S`` its lease wait would: past it, the use holds nothing and the
    refusal is the configurator's typed busy result."""
    from types import SimpleNamespace
    monkeypatch.setattr(configurator_run, "LEASE_WAIT_S", 0.05)
    cfg = SimpleNamespace(role="configurator", kind="executor", session=None)
    lane = agent_lane.LANES.get("configurator")
    await lane.lock.acquire()                         # another use of the configurator's lane
    place = lane.reserve()

    async def run(*a, **k):
        raise AssertionError("nothing runs past the deadline")
    monkeypatch.setattr(tools, "_run_delegated_agent_bounded", run)
    started = time.monotonic()
    with pytest.raises(configurator_run.ConfiguratorBusy):
        await tools._delegation_in_lane(lane, place, cfg, "t", "")
    assert time.monotonic() - started < 1.0
    assert lane.waiting == 0
    lane.lock.release()
    # the deadline the lane wait used is the one a lease acquire inside would read
    deadline = configurator_run.wait_deadline()
    with configurator_run.waiting_until(deadline):
        assert configurator_run.wait_deadline() == deadline
