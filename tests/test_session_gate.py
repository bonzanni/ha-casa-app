"""#578/#579: the module-level, path-independent authority for session-key work.

Two mechanisms live in :mod:`session_gate` and both are pinned here:

- ``session_write_gate`` — the per-``channel_key`` write gate. Moved out of
  :mod:`agent` unchanged (it was #573's scheduled-only gate) so that
  :mod:`memory_wipe` and :mod:`session_saver` can import it without the
  lazy-import cycle dance those modules already do.
- ``TurnAdmission`` — the process-wide readers/writer barrier over TURNS. A
  retirement takes the exclusive side to drain every in-flight turn on ANY
  path; the pool could only ever join the turns it owned.

The cancellation tests are the point of this file, not decoration: the sibling
``RetainFence`` carries a specific scar (``memory_wipe.py``,
``_ExclusiveSection.__aenter__``) because the obvious readers/writer
implementation left the exclusive lock held forever when the drain was
cancelled, deadlocking every later turn AND every later wipe.
"""
import asyncio

import pytest

from session_gate import (
    TURN_ADMISSION,
    AdmissionTimeout,
    TurnAdmission,
    session_write_gate,
)


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


class TestTurnAdmission:
    async def test_exclusive_waits_for_an_admitted_turn(self):
        """The whole point: a retirement joins an in-flight turn on ANY path,
        including one the client pool never heard of."""
        events = []
        turn_running = asyncio.Event()
        finish_turn = asyncio.Event()
        adm = TurnAdmission()

        async def turn():
            async with adm.admitted():
                events.append("turn-start")
                turn_running.set()
                await finish_turn.wait()
                events.append("turn-end")

        async def retirement():
            async with adm.exclusive():
                events.append("wipe")

        t = asyncio.create_task(turn())
        await turn_running.wait()
        w = asyncio.create_task(retirement())
        await asyncio.sleep(0.01)
        assert events == ["turn-start"], "the wipe ran before the turn drained"
        finish_turn.set()
        await asyncio.gather(t, w)
        assert events == ["turn-start", "turn-end", "wipe"]

    async def test_admission_is_refused_while_exclusive_is_held(self):
        """Sol r1: closing admission BEFORE discovering keys is what covers a
        first-ever turn on a key with no registry entry to enumerate."""
        events = []
        in_wipe = asyncio.Event()
        finish_wipe = asyncio.Event()
        adm = TurnAdmission()

        async def retirement():
            async with adm.exclusive():
                in_wipe.set()
                await finish_wipe.wait()
                events.append("wipe-done")

        async def turn():
            async with adm.admitted():
                events.append("turn")

        w = asyncio.create_task(retirement())
        await in_wipe.wait()
        t = asyncio.create_task(turn())
        await asyncio.sleep(0.01)
        assert events == [], "a turn was admitted during a retirement"
        finish_wipe.set()
        await asyncio.gather(w, t)
        assert events == ["wipe-done", "turn"]

    async def test_concurrent_turns_are_not_serialized(self):
        """Admission is a barrier against retirement, NOT a global turn lock:
        distinct turns must still overlap freely."""
        adm = TurnAdmission()
        entered = []
        release = asyncio.Event()

        async def turn(tag):
            async with adm.admitted():
                entered.append(tag)
                await release.wait()

        tasks = [asyncio.create_task(turn(i)) for i in range(3)]
        await asyncio.sleep(0.01)
        assert sorted(entered) == [0, 1, 2]
        release.set()
        await asyncio.gather(*tasks)

    async def test_drain_timeout_raises_and_releases(self):
        """A turn that will not end must not hang the caller forever: the
        bounded drain reports, and the barrier stays usable afterwards."""
        adm = TurnAdmission(drain_timeout_s=0.05)
        running = asyncio.Event()
        release = asyncio.Event()

        async def wedged_turn():
            async with adm.admitted():
                running.set()
                await release.wait()

        t = asyncio.create_task(wedged_turn())
        await running.wait()
        with pytest.raises(AdmissionTimeout):
            async with adm.exclusive():
                pass
        release.set()
        await t
        # Usable again: the failed acquire must not have kept exclusivity.
        async with adm.exclusive():
            pass

    async def test_cancel_during_drain_releases_exclusive(self):
        """Terra r2 S1 — the scar the sibling RetainFence already carries
        (``_ExclusiveSection.__aenter__``): a retirement cancelled while
        draining must not leave the exclusive lock held, or every later turn
        AND every later retirement deadlocks."""
        adm = TurnAdmission()
        release = asyncio.Event()

        async def turn():
            async with adm.admitted():
                await release.wait()

        t = asyncio.create_task(turn())
        await asyncio.sleep(0)

        async def retirement():
            async with adm.exclusive():
                pass

        w = asyncio.create_task(retirement())
        await asyncio.sleep(0.02)      # parked in the drain
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
        release.set()
        await t
        # The barrier is usable: a turn is admitted and a retirement completes.
        # Both are bounded — with the release omitted these block forever, and
        # a red case that HANGS is not a red case (mutation-verified: dropping
        # the BaseException arm in RwBarrier.acquire_exclusive fails here).
        await asyncio.wait_for(_admitted_roundtrip(adm), timeout=1)
        await asyncio.wait_for(_exclusive_roundtrip(adm), timeout=1)

    async def test_cancelled_admission_acquire_leaves_the_count_clean(self):
        """A turn cancelled while WAITING for admission has incremented
        nothing; a later retirement must not wait for a turn that never ran."""
        adm = TurnAdmission()
        in_wipe = asyncio.Event()
        finish_wipe = asyncio.Event()

        async def retirement():
            async with adm.exclusive():
                in_wipe.set()
                await finish_wipe.wait()

        w = asyncio.create_task(retirement())
        await in_wipe.wait()

        async def turn():
            async with adm.admitted():
                await asyncio.sleep(3600)

        t = asyncio.create_task(turn())
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        finish_wipe.set()
        await w
        # A fresh retirement must not block on the cancelled turn's phantom count.
        await asyncio.wait_for(_exclusive_roundtrip(adm), timeout=1)

    async def test_module_singleton_exists(self):
        """One barrier per process, like memory_wipe.FENCE — every turn and
        every retirement must share exactly this instance."""
        assert isinstance(TURN_ADMISSION, TurnAdmission)


async def _exclusive_roundtrip(adm):
    async with adm.exclusive():
        pass


async def _admitted_roundtrip(adm):
    async with adm.admitted():
        pass
