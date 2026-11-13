"""Stopping a pooled turn: the SDK interrupt ends the turn with an error
result, and the pool keeps that client warm instead of discarding it.

Helpers are copied from test_sdk_client_pool_client (no cross-test imports);
the scripted client here also models what the SDK does on ``interrupt()``.
"""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

from agent import ResumeDecision

# Contextvars for testing
test_origin: ContextVar = ContextVar("test_origin", default=None)
test_cid: ContextVar = ContextVar("test_cid", default="-")
test_engagement: ContextVar = ContextVar("test_engagement", default=None)


def _mk_text_block(text: str) -> _SDKTextBlock:
    """Instantiate whatever TextBlock shape the installed SDK uses."""
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


def _mk_result(sid, *, is_error=False, result="", subtype="success"):
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid
    m.is_error = is_error
    m.result = result
    m.subtype = subtype
    return m


def _mk_interrupted_result(sid):
    """The result the SDK ends an interrupted turn with."""
    return _mk_result(
        sid, is_error=True, result="", subtype="error_during_execution",
    )


class StoppableClient:
    """Scripted client whose turn stays in flight until its result arrives.

    A ``query()`` with no script left streams nothing: the turn waits, as a
    long model answer does, until ``interrupt()`` or ``feed()`` supplies the
    rest. ``interrupt()`` does what the SDK does on an interrupt — one assistant
    message with the text so far, then an error result."""

    def __init__(self, options=None):
        self.options = options
        self.sid = "sid-1"
        self.connected = False
        self.disconnects = 0
        self.interrupts = 0
        self.interrupt_error: Exception | None = None
        self.queries: list[str] = []
        self.script: list[list] = []      # one list of messages per query()
        self._buffer: list = []
        self._more = asyncio.Event()
        #: set once a turn has streamed all it has and is waiting for more
        self.waiting = asyncio.Event()

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnects += 1

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)
        if self.script:
            self._buffer.extend(self.script.pop(0))

    def feed(self, *messages):
        self._buffer.extend(messages)
        self._more.set()

    async def interrupt(self):
        self.interrupts += 1
        if self.interrupt_error is not None:
            raise self.interrupt_error
        self.feed(_mk_assistant("so far"), _mk_interrupted_result(self.sid))

    async def receive_response(self):
        from claude_agent_sdk import ResultMessage
        while True:
            while self._buffer:
                msg = self._buffer.pop(0)
                yield msg
                if isinstance(msg, ResultMessage):
                    return
            self.waiting.set()
            await self._more.wait()
            self._more.clear()
            self.waiting.clear()


def _entry():
    from sdk_client_pool import ManagedSdkClient
    return ManagedSdkClient(
        object(),
        origin_ctxvar=test_origin, cid_ctxvar=test_cid,
        engagement_ctxvar=test_engagement,
        make_client=StoppableClient,
    )


async def _noop(_message):
    return None


def _start_turn(entry, prompt="long", *, cid="c1", on_message=_noop):
    async def turn():
        async with entry.lock:
            return await entry.run_turn_locked(
                prompt, origin={}, cid=cid, on_message=on_message,
            )
    return asyncio.create_task(turn())


class FakeRegistry:
    def get(self, key):
        return None

    def generation(self, key):
        return None


def _decide_new(channel, entry, now):
    return ResumeDecision("new", None, None, "missing")


@pytest.fixture
async def mk_pool():
    """Build pools whose clients are handed in, in connect order; every pool
    is closed at teardown so none stays in the fleet list."""
    pools = []

    def make(*clients):
        from sdk_client_pool import SdkClientPool
        queue = list(clients)
        pool = SdkClientPool(
            FakeRegistry(), decide=_decide_new,
            origin_ctxvar=test_origin, cid_ctxvar=test_cid,
            engagement_ctxvar=test_engagement,
            make_client=lambda _options: queue.pop(0),
        )
        pools.append(pool)
        return pool

    yield make
    for pool in pools:
        await pool.aclose(drain_timeout=1.0)


async def _build_options(is_fresh, resume_sid):
    return {}


async def _start_pool_turn(pool, client, key, cid):
    """Start a turn through the pool and return once it is in flight."""
    task = asyncio.create_task(pool.turn(
        channel_key=key, channel="telegram", prompt="long", origin={},
        cid=cid, build_options=_build_options, on_message=_noop,
    ))
    await asyncio.wait_for(client.waiting.wait(), timeout=1)
    return task


async def test_stopped_turn_returns_sid_and_stays_warm():
    entry = _entry()
    await entry.open()
    inner = entry._client
    seen = []

    async def on_message(message):
        seen.append(type(message).__name__)

    task = _start_turn(entry, on_message=on_message)
    await asyncio.wait_for(inner.waiting.wait(), timeout=1)
    assert entry.state == "in_turn"

    assert await entry.interrupt_turn() is True
    sid = await asyncio.wait_for(task, timeout=1)

    assert sid == "sid-1"
    assert entry.state == "warm"
    assert entry.stopped is True
    assert inner.interrupts == 1
    assert inner.disconnects == 0
    assert entry._client is inner
    assert seen == ["AssistantMessage", "ResultMessage"]


async def test_same_client_answers_the_next_turn():
    entry = _entry()
    await entry.open()
    inner = entry._client
    task = _start_turn(entry)
    await asyncio.wait_for(inner.waiting.wait(), timeout=1)
    assert await entry.interrupt_turn() is True
    await asyncio.wait_for(task, timeout=1)

    inner.script = [[_mk_assistant("answer"), _mk_result("sid-1")]]
    marks = []

    async def on_message(_message):
        marks.append(entry.stopped)

    sid = await asyncio.wait_for(
        _start_turn(entry, "next", cid="c2", on_message=on_message), timeout=1,
    )

    assert sid == "sid-1"
    assert inner.queries == ["long", "next"]
    assert marks == [False, False]
    assert entry.stopped is False
    assert entry.state == "warm"
    assert inner.disconnects == 0


async def test_error_result_without_stop_still_invalidates():
    entry = _entry()
    await entry.open()
    inner = entry._client
    inner.script = [[_mk_assistant("so far"), _mk_interrupted_result("sid-1")]]

    sid = await asyncio.wait_for(_start_turn(entry), timeout=1)

    assert sid == "sid-1"
    assert entry.stopped is False
    assert entry.state == "invalid"
    assert inner.interrupts == 0
    assert inner.disconnects == 1


async def test_a_stop_after_the_result_arrived_is_refused(mk_pool):
    """A stop that lands while the turn's own error result is being handed
    on comes too late: it is refused, and the result counts as the fault it
    is — the client is discarded, not kept warm."""
    from sdk_client_pool import interrupt_turns

    client = StoppableClient()
    client.script = [[_mk_assistant("so far"), _mk_interrupted_result("sid-1")]]
    pool = mk_pool(client)
    at_result = asyncio.Event()
    release = asyncio.Event()

    async def on_message(message):
        if isinstance(message, _SDKResultMessage):
            at_result.set()
            await release.wait()

    turn = asyncio.create_task(pool.turn(
        channel_key="telegram-1", channel="telegram", prompt="long",
        origin={}, cid="stop-a", build_options=_build_options,
        on_message=on_message,
    ))
    try:
        await asyncio.wait_for(at_result.wait(), timeout=1)
        entry = pool._entries["telegram-1"]
        assert entry.state == "in_turn"

        assert await entry.interrupt_turn() is False
        assert await interrupt_turns("stop-a") == 0
        assert client.interrupts == 0
        assert entry.stopped is False
    finally:
        release.set()
    result = await asyncio.wait_for(turn, timeout=1)

    assert result.stopped is False
    assert result.sid == "sid-1"
    # The turn invalidated the entry and the pool has dropped it: closed,
    # out of the map, its transport cut once.
    assert entry.state == "closed"
    assert "telegram-1" not in pool._entries
    assert client.interrupts == 0
    assert client.disconnects == 1


async def test_interrupt_on_idle_entry_is_refused():
    entry = _entry()
    await entry.open()

    assert await entry.interrupt_turn() is False

    assert entry._client.interrupts == 0
    assert entry.stopped is False
    assert entry.state == "warm"


async def test_failed_interrupt_clears_the_mark(caplog):
    entry = _entry()
    await entry.open()
    inner = entry._client
    inner.interrupt_error = RuntimeError("transport gone")
    task = _start_turn(entry)
    await asyncio.wait_for(inner.waiting.wait(), timeout=1)

    with caplog.at_level(logging.WARNING, logger="sdk_client_pool"):
        assert await entry.interrupt_turn() is False

    assert inner.interrupts == 1
    assert entry.stopped is False
    warnings = [r for r in caplog.records if r.name == "sdk_client_pool"]
    assert len(warnings) == 1
    assert "transport gone" in warnings[0].getMessage()
    # The turn goes on and ends the ordinary way.
    assert entry.state == "in_turn" and not task.done()
    inner.feed(_mk_assistant("answer"), _mk_result("sid-1"))
    assert await asyncio.wait_for(task, timeout=1) == "sid-1"
    assert entry.state == "warm"
    assert entry.stopped is False


async def test_interrupt_turns_matches_by_cid_across_pools(mk_pool):
    from sdk_client_pool import interrupt_turns

    client_a, client_b, client_blank = (StoppableClient() for _ in range(3))
    pool_one = mk_pool(client_a)
    pool_two = mk_pool(client_b, client_blank)
    turn_a = await _start_pool_turn(pool_one, client_a, "telegram-1", "stop-a")
    turn_b = await _start_pool_turn(pool_two, client_b, "telegram-2", "stop-b")
    # A turn with no cid runs under the "-" placeholder; neither spelling of
    # "no cid" may reach it.
    turn_blank = await _start_pool_turn(pool_two, client_blank, "telegram-3", "")
    assert pool_two._entries["telegram-3"].cid_box.value == "-"

    assert await interrupt_turns("-") == 0
    assert await interrupt_turns("") == 0
    assert await interrupt_turns("stop-unknown") == 0
    assert [c.interrupts for c in (client_a, client_b, client_blank)] == [0, 0, 0]

    assert await interrupt_turns("stop-a") == 1
    assert [c.interrupts for c in (client_a, client_b, client_blank)] == [1, 0, 0]
    assert (await asyncio.wait_for(turn_a, timeout=1)).stopped is True
    assert not turn_b.done() and not turn_blank.done()
    # The stopped turn is over, so the same cid matches nothing now.
    assert await interrupt_turns("stop-a") == 0
    assert client_a.interrupts == 1

    client_b.feed(_mk_result("sid-1"))
    client_blank.feed(_mk_result("sid-1"))
    assert (await asyncio.wait_for(turn_b, timeout=1)).stopped is False
    assert (await asyncio.wait_for(turn_blank, timeout=1)).stopped is False


async def test_interrupt_turns_reaches_a_turn_an_invalidation_waits_on(mk_pool):
    """``invalidate_all`` takes every entry out of the pool's map at once and
    then waits for the turn each one is running. A stop asked for in that
    wait still reaches the turn."""
    from sdk_client_pool import interrupt_turns

    client = StoppableClient()
    pool = mk_pool(client)
    turn = await _start_pool_turn(pool, client, "telegram-1", "stop-a")
    entry = pool._entries["telegram-1"]
    invalidation = asyncio.create_task(pool.invalidate_all())
    try:
        while "telegram-1" in pool._entries:
            await asyncio.sleep(0)
        # The entry is the invalidation's now, and its turn is still running.
        assert entry in pool._draining
        assert entry.state == "in_turn"
        assert not invalidation.done() and not turn.done()

        assert await interrupt_turns("stop-a") == 1
        assert client.interrupts == 1

        result = await asyncio.wait_for(turn, timeout=1)
        assert result.stopped is True
        assert result.sid == "sid-1"
        await asyncio.wait_for(invalidation, timeout=1)
    finally:
        if not turn.done():
            client.feed(_mk_result("sid-1"))
        await asyncio.gather(turn, invalidation, return_exceptions=True)

    assert client.interrupts == 1
    assert entry.state == "closed"
    assert client.disconnects == 1
    assert not pool._draining
    assert "telegram-1" not in pool._entries


async def test_pool_turn_result_reports_stopped(mk_pool):
    from sdk_client_pool import interrupt_turns

    client = StoppableClient()
    pool = mk_pool(client)
    turn = await _start_pool_turn(pool, client, "telegram-1", "stop-a")
    entry = pool._entries["telegram-1"]

    assert await interrupt_turns("stop-a") == 1
    result = await asyncio.wait_for(turn, timeout=1)

    assert result.stopped is True
    assert result.sid == "sid-1"
    assert pool._entries["telegram-1"] is entry
    assert entry.state == "warm"
    assert client.disconnects == 0
