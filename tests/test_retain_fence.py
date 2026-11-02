"""The retain fence: the generation-checked shared/exclusive gate every
long-term bank writer passes (#411). It stays until the bank goes (S11); the
wipe itself now runs in the container-start step (``memory.maintenance``)."""

import asyncio

import pytest

import memory_wipe
from memory_wipe import RetainFence, StaleGeneration, WipeAborted

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestRetainFence:
    async def test_stale_generation_discards(self, ):
        fence = RetainFence()
        gen = fence.generation()
        async with fence.exclusive_wipe():
            pass
        with pytest.raises(StaleGeneration):
            async with fence.retaining(gen):
                pytest.fail("stale writer must not enter the shared section")

    async def test_current_generation_retains(self):
        fence = RetainFence()
        async with fence.exclusive_wipe():
            pass
        gen = fence.generation()   # captured AFTER the wipe: post-wipe writer
        entered = False
        async with fence.retaining(gen):
            entered = True
        assert entered

    async def test_exclusive_drains_inflight_shared(self):
        """The wipe must not delete under a live retain: exclusive waits for
        the shared section to exit."""
        fence = RetainFence()
        order: list[str] = []
        release = asyncio.Event()

        async def writer():
            async with fence.retaining(fence.generation()):
                order.append("writer-in")
                await release.wait()
                order.append("writer-out")

        async def wiper():
            await asyncio.sleep(0)   # let the writer enter first
            async with fence.exclusive_wipe():
                order.append("wipe")

        w = asyncio.create_task(writer())
        x = asyncio.create_task(wiper())
        await asyncio.sleep(0.05)
        assert order == ["writer-in"]      # wipe blocked on the drain
        release.set()
        await asyncio.gather(w, x)
        assert order == ["writer-in", "writer-out", "wipe"]

    async def test_exclusive_timeout_aborts_nothing_deleted(self, monkeypatch):
        monkeypatch.setattr(memory_wipe, "_EXCLUSIVE_DRAIN_TIMEOUT_S", 0.05)
        fence = RetainFence()
        release = asyncio.Event()

        async def writer():
            async with fence.retaining(fence.generation()):
                await release.wait()

        w = asyncio.create_task(writer())
        await asyncio.sleep(0)
        gen_before = fence.generation()
        with pytest.raises(WipeAborted):
            async with fence.exclusive_wipe():
                pytest.fail("must not enter after drain timeout")
        assert fence.generation() == gen_before   # nothing changed
        release.set()
        await w
        # The fence is not wedged: a wipe succeeds once the writer drained.
        async with fence.exclusive_wipe():
            pass

    async def test_shared_blocked_during_exclusive_then_discards(self):
        fence = RetainFence()
        gen = fence.generation()
        entered_exclusive = asyncio.Event()
        release_exclusive = asyncio.Event()
        outcome: list[str] = []

        async def wiper():
            async with fence.exclusive_wipe():
                entered_exclusive.set()
                await release_exclusive.wait()

        async def writer():
            await entered_exclusive.wait()
            try:
                async with fence.retaining(gen):
                    outcome.append("retained")
            except StaleGeneration:
                outcome.append("discarded")

        x = asyncio.create_task(wiper())
        w = asyncio.create_task(writer())
        await entered_exclusive.wait()
        await asyncio.sleep(0.02)
        assert outcome == []               # writer parked behind the wipe
        release_exclusive.set()
        await asyncio.gather(x, w)
        assert outcome == ["discarded"]    # pre-wipe work never lands


async def _retain_roundtrip(fence):
    async with fence.retaining(fence.generation()):
        pass


class TestDiffR1Fixes:
    async def test_cancel_during_drain_releases_exclusive(self):
        """Sol diff-r1: a wipe cancelled while draining must not leave the
        exclusive lock held — later retains and wipes would deadlock."""
        fence = RetainFence()
        release = asyncio.Event()

        async def writer():
            async with fence.retaining(fence.generation()):
                await release.wait()

        w = asyncio.create_task(writer())
        await asyncio.sleep(0)

        async def wiper():
            async with fence.exclusive_wipe():
                pass

        x = asyncio.create_task(wiper())
        await asyncio.sleep(0.02)      # parked in the drain
        x.cancel()
        with pytest.raises(asyncio.CancelledError):
            await x
        release.set()
        await w
        # The fence is usable: a writer retains and a wipe completes. BOTH are
        # bounded — with the release omitted the retain blocks forever, and a
        # red case that hangs is not a red case (it wedges CI instead of
        # reporting). #578: the release now lives in RwBarrier.acquire_exclusive,
        # shared with TurnAdmission; mutating it there fails this test too.
        await asyncio.wait_for(_retain_roundtrip(fence), timeout=1)
        await asyncio.wait_for(fence.exclusive_wipe().__aenter__(), timeout=1)
