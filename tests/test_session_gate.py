"""#573/#579: the module-level, path-independent authority for session-key work.

``session_write_gate`` — the per-``channel_key`` write gate — lives in
:mod:`session_gate`. It moved out of :mod:`agent` unchanged (it was #573's
scheduled-only gate) so that :mod:`session_registry`'s reset can import it
without a lazy-import cycle.
"""
import asyncio

import pytest

from session_gate import session_write_gate


class TestSessionWriteGate:
    async def test_same_key_serializes(self):
        order = []

        async def worker(tag, hold):
            async with session_write_gate("k"):
                order.append(f"enter-{tag}")
                await hold.wait()
                order.append(f"exit-{tag}")

        h1, h2 = asyncio.Event(), asyncio.Event()
        a = asyncio.create_task(worker("a", h1))
        await asyncio.sleep(0)
        b = asyncio.create_task(worker("b", h2))
        await asyncio.sleep(0)
        # b must not have entered while a holds the gate.
        assert order == ["enter-a"]
        h1.set()
        h2.set()
        await asyncio.gather(a, b)
        assert order == ["enter-a", "exit-a", "enter-b", "exit-b"]

    async def test_distinct_keys_are_concurrent(self):
        entered = []
        release = asyncio.Event()

        async def worker(key):
            async with session_write_gate(key):
                entered.append(key)
                await release.wait()

        tasks = [asyncio.create_task(worker(k)) for k in ("k1", "k2")]
        await asyncio.sleep(0.01)
        assert sorted(entered) == ["k1", "k2"]
        release.set()
        await asyncio.gather(*tasks)

    async def test_entry_is_reclaimed_when_idle(self):
        import session_gate

        async with session_write_gate("transient"):
            assert "transient" in session_gate._SESSION_GATES
        assert "transient" not in session_gate._SESSION_GATES

    async def test_cancelled_waiter_does_not_strand_the_entry(self):
        """#578 Q8: a dispatch task cancelled while WAITING for the gate (bus
        eviction cancels in-flight dispatches) must not leave a refcount that
        never reaches zero — that would block the key for the process's life."""
        import session_gate

        holding = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with session_write_gate("k"):
                holding.set()
                await release.wait()

        h = asyncio.create_task(holder())
        await holding.wait()
        waiter = asyncio.create_task(_take_gate("k"))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await h
        assert "k" not in session_gate._SESSION_GATES


async def _take_gate(key):
    async with session_write_gate(key):
        await asyncio.sleep(3600)
