"""ManagedSdkClient unit tests (sdk_client_pool.py). Spec: docs/superpowers/
specs/2026-07-11-resident-sdk-client-pooling-design.md (AR-1..AR-10)."""
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

pytestmark = pytest.mark.asyncio


def test_cidbox_str_and_default():
    from sdk_client_pool import _CidBox
    box = _CidBox()
    assert str(box) == "-"
    box.value = "abc123"
    assert str(box) == "abc123"


async def test_cid_filter_coerces_box_to_str():
    from log_cid import CidFilter, cid_var
    from sdk_client_pool import _CidBox
    box = _CidBox()
    box.value = "turn-2-cid"
    token = cid_var.set(box)  # type: ignore[arg-type]
    try:
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "m", (), None)
        CidFilter().filter(rec)
        assert rec.cid == "turn-2-cid"
        assert isinstance(rec.cid, str)
    finally:
        cid_var.reset(token)


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


def _mk_result(sid, usage=None, *, is_error=False, result=""):
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid
    m.is_error = is_error
    m.result = result
    if usage is not None:
        m.usage = usage
    return m


class ScriptedClient:
    def __init__(self, options):
        self.options = options
        self.connected = False
        self.disconnected = False
        self.interrupts = 0
        self.queries: list[str] = []
        self.script: list[list] = []      # one list of messages per query()
        self._buffer: list = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)
        if self.script:
            self._buffer.extend(self.script.pop(0))

    async def interrupt(self):
        self.interrupts += 1

    async def receive_response(self):
        from claude_agent_sdk import ResultMessage
        while self._buffer:
            msg = self._buffer.pop(0)
            yield msg
            if isinstance(msg, ResultMessage):
                return


def _client(options=None, **kw):
    from sdk_client_pool import ManagedSdkClient
    return ManagedSdkClient(
        options or object(),
        origin_ctxvar=test_origin, cid_ctxvar=test_cid,
        engagement_ctxvar=test_engagement,
        make_client=ScriptedClient, **kw,
    )


async def test_open_binds_holder_and_connects():
    c = _client()
    await c.open()
    assert c.state == "warm"
    assert c._client.connected            # ScriptedClient
    # The bindings happened in open()'s context copy; we can't read them
    # from here — assert the structural pieces instead:
    assert isinstance(c.origin_holder, dict)
    assert str(c.cid_box) == "-"


async def test_open_connect_failure_invalidates_and_disconnects():
    """Finding 3 (final-review): a failed connect() must not leave
    self._client set with no disconnect — open() should best-effort
    disconnect + null the client via the same _invalidate() path a
    mid-turn failure uses, keeping state == 'invalid'."""
    made: list[ScriptedClient] = []

    class FailingConnectClient(ScriptedClient):
        def __init__(self, options):
            super().__init__(options)
            made.append(self)

        async def connect(self):
            raise RuntimeError("connect failed")

    from sdk_client_pool import ManagedSdkClient
    c = ManagedSdkClient(
        object(), origin_ctxvar=test_origin, cid_ctxvar=test_cid,
        engagement_ctxvar=test_engagement, make_client=FailingConnectClient,
    )
    with pytest.raises(RuntimeError):
        await c.open()
    assert c.state == "invalid"
    assert c._client is None
    assert made[0].disconnected


async def test_open_refuses_inside_engagement():
    tok = test_engagement.set(object())
    try:
        c = _client()
        with pytest.raises(AssertionError):
            await c.open()
    finally:
        test_engagement.reset(tok)


async def test_run_turn_returns_sid_and_dispatches_messages():
    c = _client()
    await c.open()
    c._client.script = [[_mk_assistant("hello"), _mk_result("sid-1")]]
    seen = []

    async def on_message(m):
        seen.append(m)

    async with c.lock:
        sid = await c.run_turn_locked(
            "hi", origin={"cid": "c1", "channel": "voice"}, cid="c1",
            on_message=on_message,
        )
    assert sid == "sid-1"
    assert c.sid == "sid-1"
    assert c.state == "warm"
    assert len(seen) == 2
    assert c.origin_holder["channel"] == "voice"   # rewritten in place
    assert str(c.cid_box) == "c1"


async def test_second_turn_reuses_same_transport():
    c = _client()
    await c.open()
    c._client.script = [
        [_mk_assistant("a"), _mk_result("sid-1")],
        [_mk_assistant("b"), _mk_result("sid-1")],
    ]
    async def on_message(m): pass
    async with c.lock:
        await c.run_turn_locked("t1", origin={}, cid="c1", on_message=on_message)
    inner = c._client
    async with c.lock:
        await c.run_turn_locked("t2", origin={}, cid="c2", on_message=on_message)
    assert c._client is inner
    assert inner.queries == ["t1", "t2"]


async def test_origin_holder_contents_replaced_not_rebound():
    c = _client()
    await c.open()
    holder = c.origin_holder
    c._client.script = [[_mk_result("s")], [_mk_result("s")]]
    async def on_message(m): pass
    async with c.lock:
        await c.run_turn_locked("t1", origin={"cid": "one", "user_text": "a"},
                                cid="one", on_message=on_message)
    async with c.lock:
        await c.run_turn_locked("t2", origin={"cid": "two"}, cid="two",
                                on_message=on_message)
    assert c.origin_holder is holder          # same object (read-task visibility)
    assert holder == {"cid": "two"}           # cleared + rewritten, no stale keys


async def test_aclose_idempotent_and_disconnects():
    c = _client()
    await c.open()
    inner = c._client
    await c.aclose()
    await c.aclose()
    assert inner.disconnected
    assert c.state == "closed"


async def test_turn_exception_invalidates():
    c = _client()
    await c.open()

    class Boom(Exception): pass

    async def bad_query(prompt, session_id="default"):
        raise Boom("subprocess died")
    c._client.query = bad_query
    async def on_message(m): pass
    with pytest.raises(Boom):
        async with c.lock:
            await c.run_turn_locked("t", origin={}, cid="c", on_message=on_message)
    assert c.state == "invalid"
    assert c._client is None


async def test_error_result_retryable_raises_sdkturnerror_and_invalidates():
    c = _client()
    await c.open()
    c._client.script = [[_mk_result("sid-e", is_error=True,
                                    result="API Error: 529 overloaded_error")]]
    async def on_message(m): pass
    from sdk_client_pool import SdkTurnError
    with pytest.raises(SdkTurnError):
        async with c.lock:
            await c.run_turn_locked("t", origin={}, cid="c", on_message=on_message)
    assert c.state in ("invalid", "closed")


async def test_error_result_nonretryable_returns_but_invalidates():
    """Pins INV-TURN-002. Red case demonstrated: leaving the non-retryable error path warm instead of invalid fails this test."""
    c = _client()
    await c.open()
    c._client.script = [[_mk_assistant("partial"),
                         _mk_result("sid-e", is_error=True,
                                    result="invalid_request: bad tool schema")]]
    async def on_message(m): pass
    async with c.lock:
        sid = await c.run_turn_locked("t", origin={}, cid="c",
                                      on_message=on_message)
    assert sid == "sid-e"
    assert c.state == "invalid"        # never warm after is_error (AR-5)


async def test_cancel_interrupts_drains_and_stays_warm():
    """AR-1: the aborted turn's buffered tail (incl. its ResultMessage) is
    consumed during cleanup, so the NEXT turn cannot go off-by-one.

    Pins INV-TURN-003. Red case demonstrated: skipping _cleanup_after_cancel in the CancelledError handler fails this test.
    """
    c = _client()
    await c.open()
    hang = asyncio.Event()

    async def hanging_query(prompt, session_id="default"):
        c._client._buffer.extend([_mk_assistant("partial")])
        hang.set()

    real_receive = c._client.receive_response

    async def slow_receive():
        async for m in real_receive():
            yield m
        await asyncio.sleep(30)         # never yields Result: simulates mid-turn

    c._client.query = hanging_query
    c._client.receive_response = slow_receive

    async def interrupt():
        c._client.interrupts += 1
        # CLI aborts: the stale result lands in the buffer
        c._client._buffer.append(_mk_result("sid-x"))
        c._client.receive_response = real_receive   # drain reads the real buffer

    c._client.interrupt = interrupt
    async def on_message(m): pass

    async def turn():
        async with c.lock:
            await c.run_turn_locked("long", origin={}, cid="c",
                                    on_message=on_message)

    t = asyncio.create_task(turn())
    await hang.wait()
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert c._client.interrupts == 1
    assert c.state == "warm"
    assert c._client._buffer == []      # drained through the stale Result
    assert c.sid == "sid-x"


async def test_cancel_with_failing_interrupt_invalidates():
    c = _client()
    await c.open()
    started = asyncio.Event()

    async def hanging_query(prompt, session_id="default"):
        started.set()

    async def slow_receive():
        await asyncio.sleep(30)
        yield  # pragma: no cover

    async def bad_interrupt():
        raise RuntimeError("transport gone")

    c._client.query = hanging_query
    c._client.receive_response = slow_receive
    c._client.interrupt = bad_interrupt
    async def on_message(m): pass

    async def turn():
        async with c.lock:
            await c.run_turn_locked("long", origin={}, cid="c",
                                    on_message=on_message)

    t = asyncio.create_task(turn())
    await started.wait()
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert c.state == "invalid"


async def test_aclose_joins_a_disconnect_already_in_flight():
    """#853: a second aclose() — or an aclose() after _invalidate() detached
    the client — does not return while the first disconnect is still running;
    the transport is disconnected exactly once."""
    release = asyncio.Event()
    starts = []

    class SlowDisconnect(ScriptedClient):
        async def disconnect(self):
            starts.append(1)
            await release.wait()
            await super().disconnect()

    c = _client()
    c._make_client = SlowDisconnect
    await c.open()
    inner = c._client
    first = asyncio.create_task(c._invalidate())
    await asyncio.sleep(0)
    assert c.state == "invalid" and c._client is None and starts == [1]
    second = asyncio.create_task(c.aclose())
    done, _pending = await asyncio.wait({first, second}, timeout=0.1)
    assert len(done) == 0 and c.state == "closed" and not inner.disconnected
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert inner.disconnected and starts == [1]
    await c.aclose()                       # idempotent after completion
    assert starts == [1]
