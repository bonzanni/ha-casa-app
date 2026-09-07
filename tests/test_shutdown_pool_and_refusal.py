"""#881/#882 — the container's graceful stop, against real Agents and pools.

Nothing in the repository drove ``casa_core._shutdown_cleanup``'s AR-9 agent
close loop against a real ``SdkClientPool`` before this file: the coroutine was
extracted under #698 so a red case could, and these are the first that do.

* #881 — the loop wraps each ``Agent.aclose()`` in a 15 s bound while the pool's
  own drain default is 120 s, and the pool clears its entry map and writes its
  drain records before its first lock wait. A turn holding its lock past the
  bound therefore left a client with a live, never-disconnected transport — and
  the disconnect is the AR-4 transcript flush.
* #882 — once the pool is closing every later turn is refused, and the agent
  answered that refusal by running the turn on the unpooled bypass, starting a
  CLI subprocess after the stop began and answering into a runtime whose
  channels close a few steps later.

No socket is opened (D1 2026-09-06) and no shipped file is copied.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from semantic_memory import NoOpSemanticMemory
from session_registry import SessionRegistry
from session_reg_helpers import RESIDENT_DIGEST, resident_prov, resident_role_id

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)


def _mk_text_block(text: str) -> _SDKTextBlock:
    try:
        return _SDKTextBlock(text=text)
    except TypeError:
        return _SDKTextBlock(text)  # type: ignore[call-arg]


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    block = _mk_text_block(text)
    try:
        return _SDKAssistantMessage(content=[block])
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_result(sid):
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid
    m.is_error = False
    m.result = ""
    return m


class CountingClient:
    """SDK-boundary double. Every count is its own field — a disconnect that
    STARTED is never allowed to pass for one that COMPLETED, which is the whole
    distinction #881 turns on."""

    constructed = 0
    connects = 0
    queries = 0
    disconnect_starts = 0
    disconnects = 0
    live = 0
    instances: list["CountingClient"] = []

    @classmethod
    def reset(cls) -> None:
        cls.constructed = cls.connects = cls.queries = 0
        cls.disconnect_starts = cls.disconnects = cls.live = 0
        cls.instances = []

    def __init__(self, options=None):
        CountingClient.constructed += 1
        CountingClient.instances.append(self)
        self.options = options

    async def connect(self):
        CountingClient.connects += 1
        CountingClient.live += 1

    async def disconnect(self):
        CountingClient.disconnect_starts += 1
        CountingClient.disconnects += 1
        CountingClient.live -= 1

    async def query(self, prompt, session_id="default"):
        CountingClient.queries += 1

    async def interrupt(self):
        return None

    async def receive_response(self):
        yield _mk_assistant("pong")
        yield _mk_result("sdk-sid-1")


def _make_agent(tmp_path, role: str = "assistant") -> Agent:
    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        role_id=resident_role_id(role),
        kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov(role),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / f"sessions-{role}.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        semantic_memory=NoOpSemanticMemory(),
    )


def _msg(channel: str, chat_id: str, text: str = "ping",
         *, kind: MessageType = MessageType.CHANNEL_IN) -> BusMessage:
    return BusMessage(
        type=kind,
        source="telegram" if channel == "telegram" else channel,
        target="assistant",
        content=text,
        channel=channel,
        context={"chat_id": chat_id},
    )


class _FastWaitForAsyncio:
    """casa_core's module-local ``asyncio``, with ONLY ``wait_for``'s AR-9
    bound shortened. Everything else forwards to the real module — and the
    shared ``asyncio.sleep`` is never touched (a patched global sleep spun the
    pool sweeper to 23 GB once)."""

    def __init__(self, bound: float):
        self._bound = bound

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def wait_for(self, coro, timeout=None):
        if timeout == 15:
            timeout = self._bound
        return asyncio.wait_for(coro, timeout)


async def _shutdown(runtime, *, bound: float = 0.05, bus=None,
                   semantic_memory=None):
    """Run the production ``_shutdown_cleanup`` over ``runtime``'s agents, with
    every unrelated subsystem doubled and the AR-9 bound shortened.

    ``bus`` and ``semantic_memory`` default to the doubles #881/#882 use. #895
    passes a REAL ``MessageBus`` (the defect is invisible to a bus double whose
    task lists are empty) and a real coroutine for the last cleanup await (the
    count the completion record must carry is the one true AFTER it)."""
    import casa_core
    import tools

    cm = MagicMock()
    cm.stop_all = AsyncMock()
    if bus is None:
        bus = MagicMock(begin_shutdown=MagicMock(),
                        agent_loop_tasks=MagicMock(return_value=[]),
                        fail_pending=MagicMock())
    engagement_registry = MagicMock()
    engagement_registry.begin_launch_shutdown = MagicMock(return_value=0)
    engagement_registry.drain_launches = AsyncMock()
    tools.init_tools(
        channel_manager=cm, bus=bus, specialist_registry=MagicMock(),
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=engagement_registry, executor_registry=MagicMock())
    real_asyncio = casa_core.asyncio
    casa_core.asyncio = _FastWaitForAsyncio(bound)
    try:
        with patch.object(tools, "stop_engagement_launches", AsyncMock()), \
             patch.object(tools, "drain_delegation_settlements", AsyncMock()), \
             patch.object(casa_core, "_close_tina_ha_facade", AsyncMock()), \
             patch.object(casa_core, "_drain_broker_before_channel_shutdown",
                          AsyncMock()):
            await casa_core._shutdown_cleanup(
                job_registry=MagicMock(close=AsyncMock()),
                engagement_registry=engagement_registry,
                scheduler=MagicMock(shutdown=MagicMock()),
                session_sweeper=MagicMock(stop=AsyncMock()),
                freshness_reaper=MagicMock(stop=AsyncMock()),
                runtime=runtime,
                ha_facade=None,
                bus=bus,
                loop_tasks=[],
                channel_manager=cm,
                runners=[],
                semantic_memory=(semantic_memory
                                 or MagicMock(close=AsyncMock())),
            )
    finally:
        casa_core.asyncio = real_asyncio


async def _warm_pooled_turn(agent, chat_id="chat-1"):
    """One real pooled turn, so the pool OWNS a connected client afterwards."""
    with patch("sdk_client_pool._default_make_client", CountingClient):
        text = await agent._process(_msg("voice", chat_id, "hi"))
    return text


def _held_entry(agent, key_fragment="voice"):
    """The pool's live entry, whichever key the turn resolved to."""
    entries = list(agent._pool._entries.items())
    assert len(entries) == 1, entries
    return entries[0][1]


def _drop_pool(agent):
    from sdk_client_pool import SdkClientPool
    if agent._pool in SdkClientPool._instances:
        SdkClientPool._instances.remove(agent._pool)


# --- #881 -------------------------------------------------------------------


async def test_the_real_shutdown_cuts_every_pooled_client_it_removed(tmp_path):
    """#881, through the production stop: two agents, each with a warm pooled
    client whose entry lock a turn still holds past the AR-9 bound. The bound
    fires, the pool close is cancelled — and every client it had already
    removed from the map must still be disconnected.

    Base: ``(2, 2, 0, 2)`` — nothing is disconnected and both transports stay
    live, with no later close on this path to reclaim them."""
    CountingClient.reset()
    first = _make_agent(tmp_path, role="assistant")
    second = _make_agent(tmp_path, role="butler")
    held = []
    try:
        for agent in (first, second):
            assert await _warm_pooled_turn(agent) == "pong"
            entry = _held_entry(agent)
            await entry.lock.acquire()          # the turn that outlasts the bound
            held.append(entry)
        runtime = SimpleNamespace(
            agents={"assistant": first, "butler": second},
            claude_code_driver=None,
        )
        await _shutdown(runtime)
        assert (CountingClient.constructed, CountingClient.connects,
                CountingClient.disconnects, CountingClient.live) == (2, 2, 2, 0)
    finally:
        for entry in held:
            if entry.lock.locked():
                entry.lock.release()
        _drop_pool(first)
        _drop_pool(second)


async def test_the_real_shutdown_closes_the_agents_concurrently(tmp_path):
    """#881's bound: the AR-9 loop must not spend one full window per agent, or
    the stop's close portion grows with the number of specialists and can pass
    the container's own stop timeout. Both closes must be in flight at once.

    Base: ``1`` — the second close has not started when the first is cancelled."""
    CountingClient.reset()
    first = _make_agent(tmp_path, role="assistant")
    second = _make_agent(tmp_path, role="butler")
    entered: list[str] = []
    at_first_cancellation: list[int] = []
    held = []
    try:
        for role, agent in (("assistant", first), ("butler", second)):
            assert await _warm_pooled_turn(agent) == "pong"
            entry = _held_entry(agent)
            await entry.lock.acquire()
            held.append(entry)
            real = agent.aclose

            async def counted(_real=real, _role=role):
                entered.append(_role)
                try:
                    await _real()
                except asyncio.CancelledError:
                    # The AR-9 bound expired on THIS agent. How many closes
                    # had been entered by then is the concurrency fact: a
                    # serial loop cannot have started the second one yet.
                    at_first_cancellation.append(len(entered))
                    raise

            agent.aclose = counted
        runtime = SimpleNamespace(
            agents={"assistant": first, "butler": second},
            claude_code_driver=None,
        )
        await _shutdown(runtime)
        assert (len(entered), at_first_cancellation[:1]) == (2, [2])
    finally:
        for entry in held:
            if entry.lock.locked():
                entry.lock.release()
        _drop_pool(first)
        _drop_pool(second)


async def test_agent_close_cancelled_before_it_reaches_the_pool_still_cuts(
    tmp_path,
):
    """#881, the ordering neither issue names: ``Agent.aclose`` cancels and
    gathers its background cold-retains BEFORE it reaches the pool close. A
    cancellation delivered at that gather — which is exactly what the loop's
    final sweep does to a reload's background close — used to leave the pool
    untouched, so no salvage inside the pool could ever see the client.

    Base: ``(1, 1, 0, 1)`` — connected, never disconnected, still live."""
    CountingClient.reset()
    agent = _make_agent(tmp_path, role="assistant")
    try:
        assert await _warm_pooled_turn(agent) == "pong"
        entered = asyncio.Event()

        async def slow_retain():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                entered.set()                    # the settle tail is running
                await asyncio.Event().wait()     # and it does not finish
                raise

        agent._bg_tasks.add(asyncio.create_task(slow_retain()))
        closing = asyncio.create_task(agent.aclose())
        await asyncio.wait_for(entered.wait(), timeout=2)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        assert (CountingClient.connects, CountingClient.disconnect_starts,
                CountingClient.disconnects, CountingClient.live) == (1, 1, 1, 0)
    finally:
        for task in list(agent._bg_tasks):
            task.cancel()
        _drop_pool(agent)


# --- #882 -------------------------------------------------------------------


async def test_a_turn_arriving_after_the_stop_is_refused(tmp_path):
    """#882: once the container has declared its graceful stop, a turn the pool
    refuses BECAUSE IT IS CLOSING must not be served on the unpooled bypass —
    which would start a CLI subprocess after the stop began and answer into a
    runtime whose channels close a few steps later.

    Base: ``(1, 1, 1)`` — a client is constructed, queried and answered."""
    CountingClient.reset()
    agent = _make_agent(tmp_path, role="assistant")
    try:
        assert await _warm_pooled_turn(agent) == "pong"
        runtime = SimpleNamespace(agents={"assistant": agent},
                                  claude_code_driver=None)
        await _shutdown(runtime)                 # declares the stop, closes it
        before = (CountingClient.constructed, CountingClient.queries)
        with patch("sdk_client_pool._default_make_client", CountingClient):
            with pytest.raises(asyncio.CancelledError):
                await agent._process(_msg("voice", "chat-1", "again"))
        after = (CountingClient.constructed, CountingClient.queries)
        assert (after[0] - before[0], after[1] - before[1]) == (0, 0)
    finally:
        _drop_pool(agent)


async def test_the_stop_is_a_process_fact_not_an_instance_one(tmp_path):
    """#882: the stop outlives the agents that were alive when it was declared.
    A specialist installed — or a generation swapped in — after the cleanup ran
    must meet the same refusal, so the fact cannot be per-``Agent`` state.

    Base: the later agent is served. Kills an instance-local stop marker."""
    CountingClient.reset()
    first = _make_agent(tmp_path, role="assistant")
    later = None
    try:
        assert await _warm_pooled_turn(first) == "pong"
        runtime = SimpleNamespace(agents={"assistant": first},
                                  claude_code_driver=None)
        await _shutdown(runtime)
        later = _make_agent(tmp_path, role="butler")     # built AFTER the stop
        assert await _warm_pooled_turn(later, chat_id="chat-2") == "pong"
        await later.aclose()                             # now its pool refuses
        before = CountingClient.constructed
        with patch("sdk_client_pool._default_make_client", CountingClient):
            with pytest.raises(asyncio.CancelledError):
                await later._process(_msg("voice", "chat-2", "again"))
        assert CountingClient.constructed - before == 0
    finally:
        _drop_pool(first)
        if later is not None:
            _drop_pool(later)


async def test_a_reload_closed_pool_still_serves_its_turn(tmp_path):
    """#882's sharp edge: the pool's closing flag cannot tell a container stop
    from a configuration reload, and reloads are routine. With no stop declared,
    a turn meeting a closed pool is served on the bypass exactly as today —
    a refusal keyed on the flag alone would silently drop reload-window turns.
    Green at the base and after the fix."""
    CountingClient.reset()
    agent = _make_agent(tmp_path, role="assistant")
    try:
        assert await _warm_pooled_turn(agent) == "pong"
        await agent.aclose()                     # what reload does, no stop
        before = CountingClient.constructed
        with patch("sdk_client_pool._default_make_client", CountingClient):
            text = await agent._process(_msg("voice", "chat-1", "again"))
        assert text == "pong" and CountingClient.constructed - before == 1
    finally:
        _drop_pool(agent)


@pytest.mark.parametrize("kind,channel,chat_id", [
    (MessageType.SCHEDULED, "telegram", "chat-1"),
    (MessageType.CHANNEL_IN, "webhook",
     "3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
])
async def test_the_unconditional_bypass_is_still_served_during_a_stop(
    tmp_path, kind, channel, chat_id,
):
    """A scheduled heartbeat and a webhook one-shot take the bypass because of
    WHAT THEY ARE, never because the pool refused them, so the stop-time
    refusal must not reach them: silencing them would silence reminders and
    triggers. Green at the base and after the fix."""
    CountingClient.reset()
    agent = _make_agent(tmp_path, role="assistant")
    try:
        assert await _warm_pooled_turn(agent) == "pong"
        runtime = SimpleNamespace(agents={"assistant": agent},
                                  claude_code_driver=None)
        await _shutdown(runtime)
        before = CountingClient.constructed
        with patch("sdk_client_pool._default_make_client", CountingClient):
            text = await agent._process(
                _msg(channel, chat_id, "beat", kind=kind))
        assert text == "pong" and CountingClient.constructed - before == 1
    finally:
        _drop_pool(agent)


async def test_a_transient_pool_refusal_is_still_served_during_a_stop(tmp_path):
    """``PoolUnavailable("entry unstable after retry")`` is a transient
    eviction fallback with nothing to do with shutdown. A refusal that cannot
    tell it from "pool closing" refuses ordinary turns. Green at the base and
    after the fix."""
    from sdk_client_pool import PoolUnavailable

    CountingClient.reset()
    agent = _make_agent(tmp_path, role="assistant")
    try:
        assert await _warm_pooled_turn(agent) == "pong"
        runtime = SimpleNamespace(agents={"assistant": agent},
                                  claude_code_driver=None)
        await _shutdown(runtime)
        before = CountingClient.constructed

        async def unstable(**_kwargs):
            raise PoolUnavailable("entry unstable after retry")

        agent._pool.turn = unstable
        with patch("sdk_client_pool._default_make_client", CountingClient):
            text = await agent._process(_msg("voice", "chat-1", "again"))
        assert text == "pong" and CountingClient.constructed - before == 1
    finally:
        _drop_pool(agent)


# --- #895 -------------------------------------------------------------------
#
# The stop cancels and gathers the bus's CONSUMER tasks only. Every turn runs
# in a DISPATCH task, in a structurally disjoint map the stop never reads — so
# "Casa core shutdown complete" is written while admitted turns are still
# running. INV-CONC-006 makes that record truthful: the stop's completion
# record carries how many turns the bus had dispatched and had not finished at
# the instant it was written, together with the reason they were left — that
# they are neither awaited nor cancelled — and exactly one record in a stop
# carries that count.
#
# What that deliberately does NOT say, after four acceptance rounds: that no
# other record could be read as announcing completion. A record is recognisable
# to a test only by its wording or by its fields, so "no second completion
# record, identified by neither" is not a property any test can hold — and a
# declaration that cannot be pinned is one this loop must not make.
#
# These drive a REAL ``MessageBus``: the four ``test_graceful_shutdown_*.py``
# files all double the bus with an empty task list, so a step that reads the
# dispatch map is invisible to them.


class _RecordAt(logging.Handler):
    """Capture, AT EMIT TIME, every record, plus a caller-supplied probe.

    A caplog assertion made after the fact cannot tell whether a callback ran
    between the snapshot and the log call; this can, because ``probe`` is read
    inside ``emit``."""

    def __init__(self, needle: str, probe=lambda: None):
        super().__init__()
        self.needle, self.probe = needle, probe
        self.all: list[logging.LogRecord] = []
        self.records: list[logging.LogRecord] = []
        self.probed: list[object] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.all.append(record)
        if self.needle in record.getMessage():
            self.records.append(record)
            self.probed.append(self.probe())

    @property
    def enriched(self) -> list[logging.LogRecord]:
        """Every record carrying a turn count, whatever its message says.

        Counting only records that match the needle would let an
        implementation emit the enriched line AND a second completion record
        with different wording — "exactly one completion record" would be false
        while the test stayed green."""
        return [r for r in self.all if hasattr(r, "abandoned_turns")]


@contextlib.contextmanager
def _completion_watch(needle="Casa core shutdown complete", probe=lambda: None):
    """Attach a ``_RecordAt`` to casa_core's own logger for the block's
    duration, at a level that lets the completion line through whichever
    severity it is written at (the count arm and the zero arm differ)."""
    import casa_core
    watch = _RecordAt(needle, probe)
    log = logging.getLogger(casa_core.__name__)
    previous = log.level
    log.setLevel(logging.INFO)
    log.addHandler(watch)
    try:
        yield watch
    finally:
        log.removeHandler(watch)
        log.setLevel(previous)


def _bus_msg(target, kind):
    return BusMessage(type=kind, source="tester", target=target, content="x")


async def _blocking_role(bus, name, *, entered, release, unwound=None):
    """Register a role whose handler blocks until *release*, recording its own
    dispatch task. When *unwound* is given the handler also parks INSIDE its
    cancellation unwind — the state ``unregister`` leaves a dispatch in."""
    box: dict[str, asyncio.Task] = {}

    async def handler(msg):
        box["task"] = asyncio.current_task()
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if unwound is not None:
                unwound[0].set()
                await unwound[1].wait()
            raise
        return None

    bus.register(name, handler)
    bus.start_agent_loop(name)
    return box


async def test_the_completion_record_counts_the_turns_the_stop_leaves_running():
    """#895's red case, on a real bus: four dispatched turns, one of them
    parked inside the unwind of a reload eviction and one finishing during the
    stop's LAST await. The completion record must say THREE.

    Base: zero enriched completion records — the line is written with no count
    and no reason while three turns are still running."""
    from bus import MessageBus

    bus = MessageBus()
    boxes, entered, release = {}, {}, {}
    unwound = [asyncio.Event(), asyncio.Event()]
    plan = [("req", MessageType.REQUEST), ("chan", MessageType.CHANNEL_IN),
            ("evicted", MessageType.NOTIFICATION),
            ("finisher", MessageType.NOTIFICATION)]
    for name, _kind in plan:
        entered[name], release[name] = asyncio.Event(), asyncio.Event()
        boxes[name] = await _blocking_role(
            bus, name, entered=entered[name], release=release[name],
            unwound=unwound if name == "evicted" else None)
    try:
        for name, kind in plan:
            await bus.send(_bus_msg(name, kind))
        for name, _kind in plan:
            await asyncio.wait_for(entered[name].wait(), timeout=5)

        # The evict: `unregister` pops the role's dispatch set and cancels the
        # task, which then parks in its unwind. Still unfinished work this bus
        # owns — and invisible to every per-role enumeration.
        bus.unregister("evicted")
        await asyncio.wait_for(unwound[0].wait(), timeout=5)
        assert bus.dispatch_tasks_for("evicted") == []

        # One turn finishes during the stop's last cleanup await, so a count
        # taken any earlier is a different (wrong) number.
        async def _close_and_let_one_finish():
            release["finisher"].set()
            await asyncio.wait_for(boxes["finisher"]["task"], timeout=5)

        with _completion_watch() as watch:
            await _shutdown(
                SimpleNamespace(agents={}, claude_code_driver=None),
                bus=bus,
                semantic_memory=SimpleNamespace(close=_close_and_let_one_finish))

        # Exactly one record carries a count, and it is the one carrying the
        # completion phrase.
        assert len(watch.records) == 1, watch.records
        assert watch.enriched == watch.records, watch.enriched
        record = watch.records[0]
        assert getattr(record, "abandoned_turns", None) == 3
        # The NEGATED meaning, not merely the two words: "the stop awaits and
        # cancels them" contains both and contradicts the invariant.
        assert "neither awaited nor cancelled" in getattr(
            record, "abandoned_reason", ""), record.__dict__

        # The scenario really does separate the two enumerations: a fix that
        # unioned the per-role maps instead would have reported 2.
        by_role = {t for s in bus._dispatch_by_agent.values() for t in s}
        assert len([t for t in by_role if not t.done()]) == 2

        # Nothing was awaited and nothing was cancelled by the stop: the three
        # are still blocked, and only the pre-existing evict asked for a cancel.
        still = [boxes[n]["task"] for n in ("req", "chan", "evicted")]
        assert [t.done() for t in still] == [False, False, False]
        assert sum(t.cancelling() for t in still) == 1
    finally:
        for name, _kind in plan:
            release[name].set()
        unwound[1].set()
        tasks = [b["task"] for b in boxes.values() if "task" in b]
        tasks += [t for t in bus.agent_loop_tasks()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_the_completion_record_says_zero_when_no_turn_was_dispatched():
    """The ordinary stop still writes the completion line, with a count of 0 —
    the record is unconditional, so its absence is never ambiguous.

    Base: zero enriched completion records."""
    from bus import MessageBus

    bus = MessageBus()
    with _completion_watch() as watch:
        await _shutdown(SimpleNamespace(agents={}, claude_code_driver=None),
                        bus=bus)
    assert len(watch.records) == 1, watch.records
    assert watch.enriched == watch.records, watch.enriched
    assert getattr(watch.records[0], "abandoned_turns", None) == 0
    assert "Casa core shutdown complete" in watch.records[0].getMessage()


async def test_a_completed_dispatch_is_retained_until_its_callback_and_uncounted():
    """A finished task's removal callback is only SCHEDULED, so the raw set
    still holds it while ``done()`` is already true. The accessor filters it;
    the set releases it when the callback runs.

    Base: the accessor does not exist."""
    from bus import MessageBus

    bus = MessageBus()
    seen: list[int] = []
    done_evt = asyncio.Event()

    async def handler(msg):
        # Observe from a call_soon scheduled in the handler's last synchronous
        # segment: it runs after the task finishes, before its done callbacks.
        asyncio.get_running_loop().call_soon(
            lambda: (seen.append(len(bus.live_dispatch_tasks())),
                     seen.append(len([t for t in bus._live_dispatches
                                      if t.done()])),
                     done_evt.set()))
        return None

    bus.register("solo", handler)
    loop_task = bus.start_agent_loop("solo")
    try:
        await bus.send(_bus_msg("solo", MessageType.NOTIFICATION))
        await asyncio.wait_for(done_evt.wait(), timeout=5)
        assert seen == [0, 1], seen           # filtered 0, retained-done 1
        await asyncio.sleep(0)
        assert len(bus._live_dispatches) == 0
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_nothing_runs_between_the_snapshot_and_the_completion_record():
    """The snapshot is a copy, and no scheduled callback can interleave between
    taking it and writing the record — a yield in between would let a turn
    finish and make the number stale.

    Base: the accessor does not exist."""
    from bus import MessageBus

    bus = MessageBus()
    entered, release = asyncio.Event(), asyncio.Event()
    box = await _blocking_role(bus, "solo", entered=entered, release=release)
    marker: list[int] = []
    try:
        await bus.send(_bus_msg("solo", MessageType.NOTIFICATION))
        await asyncio.wait_for(entered.wait(), timeout=5)

        # A returned snapshot is the caller's own list.
        first = bus.live_dispatch_tasks()
        first.clear()
        assert len(bus.live_dispatch_tasks()) == 1

        real = bus.live_dispatch_tasks

        def _observed():
            result = real()
            asyncio.get_running_loop().call_soon(lambda: marker.append(1))
            return result

        bus.live_dispatch_tasks = _observed
        with _completion_watch(probe=lambda: len(marker)) as watch:
            await _shutdown(SimpleNamespace(agents={}, claude_code_driver=None),
                            bus=bus)
        assert watch.probed == [0], watch.probed
        await asyncio.sleep(0)
        assert marker == [1]
        assert getattr(watch.records[0], "abandoned_turns", None) == 1
    finally:
        release.set()
        tasks = [box["task"]] + list(bus.agent_loop_tasks())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
