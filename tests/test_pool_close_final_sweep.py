"""#881 — a pool close cancelled by the event loop's OWN final sweep.

These tests are deliberately SYNCHRONOUS and drive ``asyncio.run`` themselves:
the defect they pin lives in ``asyncio.run``'s closing sequence, which cancels
every task still alive when the main coroutine returns and then gathers their
unwinds. A test running *inside* a loop pytest-asyncio owns cannot reach it.

The entry point is the real one: ``reload._schedule_agent_close``, the
background ``agent-pool-close`` task a reload creates for a replaced Agent,
which ``casa_core._shutdown_cleanup`` never joins.

No socket is opened anywhere here (D1 2026-09-06: the reviewer sandbox and the
gate's read-only materialization both deny it), and no file is copied.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar

sweep_origin: ContextVar = ContextVar("sweep_origin", default=None)
sweep_cid: ContextVar = ContextVar("sweep_cid", default="-")
sweep_engagement: ContextVar = ContextVar("sweep_engagement", default=None)


class SweepClient:
    """SDK-boundary fake. Counts disconnect STARTS, CANCELLATIONS and
    COMPLETIONS separately — a cut the sweep interrupted must never be able to
    pass for a completed one — and tracks ``live`` as its own field."""

    def __init__(self, options=None):
        self.options = options
        self.connect_completions = 0
        self.disconnect_starts = 0
        self.disconnect_cancellations = 0
        self.disconnect_completions = 0
        self.live = 0
        self.disconnect_started = asyncio.Event()
        self.park_first_disconnect = False

    async def connect(self):
        self.connect_completions += 1
        self.live = 1

    async def disconnect(self):
        self.disconnect_starts += 1
        self.disconnect_started.set()
        if self.park_first_disconnect:
            self.park_first_disconnect = False      # only the first one parks
            try:
                await asyncio.Event().wait()        # never set; only cancel
            except asyncio.CancelledError:
                self.disconnect_cancellations += 1
                raise
        self.live = 0
        self.disconnect_completions += 1

    async def query(self, prompt, session_id="default"):
        return None

    async def interrupt(self):
        return None

    async def receive_response(self):
        if False:                                    # pragma: no cover
            yield None


class _Decide:
    def __call__(self, channel, entry, now):
        from agent import ResumeDecision
        return ResumeDecision("new", None, False, None, "missing")


class _Registry:
    def get(self, key):
        return None

    def generation(self, key):
        return None

    async def touch(self, key):
        return None


class _AgentStub:
    """The only thing ``reload._start_agent_close`` needs of an Agent: an
    ``aclose`` coroutine function. ``Agent.aclose``'s own pre-pool ordering is
    pinned separately (tests/test_agent_process.py)."""

    def __init__(self, pool):
        self._pool = pool

    async def aclose(self):
        await self._pool.aclose()


def _mk_pool(client):
    from sdk_client_pool import SdkClientPool
    return SdkClientPool(
        _Registry(), decide=_Decide(),
        origin_ctxvar=sweep_origin, cid_ctxvar=sweep_cid,
        engagement_ctxvar=sweep_engagement,
        make_client=lambda _options: client,
    )


async def _warm_entry(pool, key, client):
    """One connected, warm entry at ``key``, opened through the pool's own
    client lifecycle (so the connect really ran)."""
    entry = await pool._entry_stub(key)
    await entry.open()
    return entry


def _forget(pool):
    from sdk_client_pool import SdkClientPool
    if pool in SdkClientPool._instances:
        SdkClientPool._instances.remove(pool)


def test_final_sweep_cancels_the_reload_close_before_it_can_cut() -> None:
    """The sweep cancels the background close while it is still waiting for a
    wedged turn's entry lock. The close must still disconnect the client it had
    already removed from the map, before the loop finishes closing.

    Base: ``(1, 0, 0, 1)`` — the client stays connected forever."""
    client = SweepClient()
    pool = None

    async def main():
        nonlocal pool
        pool = _mk_pool(client)
        entry = await _warm_entry(pool, "voice-sweep", client)
        await entry.lock.acquire()                   # the wedged turn
        import reload as reload_mod
        reload_mod._schedule_agent_close(_AgentStub(pool))
        while "voice-sweep" in pool._entries:        # the close has enumerated
            await asyncio.sleep(0)
        # return with the lock still held and the close still waiting on it

    asyncio.run(main())
    _forget(pool)
    assert (client.connect_completions, client.disconnect_starts,
            client.disconnect_completions, client.live) == (1, 1, 1, 0)


def test_final_sweep_cancels_a_disconnect_already_running() -> None:
    """The harder ordering: the cut has already STARTED when the sweep runs, so
    the sweep cancels the disconnect itself. A repair that only shields, or that
    treats the interrupted cut as completed, leaves the transport open — the cut
    has to be started again against the retained client.

    Base: ``(1, 1, 0, 0, 1)`` — one start, no completion, one live transport."""
    client = SweepClient()
    client.park_first_disconnect = True
    pool = None

    async def main():
        nonlocal pool
        pool = _mk_pool(client)
        await _warm_entry(pool, "voice-cut", client)   # lock left FREE
        import reload as reload_mod
        reload_mod._schedule_agent_close(_AgentStub(pool))
        await asyncio.wait_for(client.disconnect_started.wait(), timeout=2)
        # return with the disconnect parked mid-flight

    asyncio.run(main())
    _forget(pool)
    assert (client.connect_completions, client.disconnect_starts,
            client.disconnect_cancellations, client.disconnect_completions,
            client.live) == (1, 2, 1, 1, 0)
