"""#411 — the retain fence (generation-checked shared/exclusive gate) and the
wipe orchestrator (claims → drain → spool → sid-guarded removes → bank)."""

import asyncio
import json

import pytest

import memory_wipe
from memory_wipe import (
    FENCE, RetainFence, StaleGeneration, WipeAborted, wipe_long_term_memory,
)
from session_registry import SessionRegistry
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class FakeSem:
    def __init__(self):
        self.deleted: list[str] = []
        self.retained: list[tuple] = []

    async def delete_bank(self, bank: str) -> bool:
        self.deleted.append(bank)
        return True

    async def retain(self, bank, items, *, async_=True):
        self.retained.append((bank, items))


def _reg(tmp_path) -> SessionRegistry:
    return SessionRegistry(str(tmp_path / "sessions.json"))


async def _register(reg, key, sid):
    await reg.register(
        key, "resident:assistant", sid, binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )


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


class TestWipeOrchestrator:
    async def test_full_wipe_report_and_order(self, tmp_path):
        reg = _reg(tmp_path)
        await _register(reg, "telegram-v2-aaa", "sid-1")
        await _register(reg, "telegram-v2-bbb", "sid-2")
        spool = tmp_path / "spool"
        spool.mkdir()
        (spool / "sid-1.json").write_text(json.dumps({"sdk_session_id": "sid-1"}))
        (spool / "sid-2.json").write_text(json.dumps({"sdk_session_id": "sid-2"}))
        sem = FakeSem()
        report = await wipe_long_term_memory(
            registry=reg, semantic_memory=sem, fence=RetainFence(),
            bank="casa", retry_dir=spool,
        )
        assert report.bank_deleted is True
        assert report.spool_records_dropped == 2
        assert report.session_entries_dropped == 2
        assert sem.deleted == ["casa"]
        assert list(spool.glob("*.json")) == []
        assert reg.all_entries() == {}
        # No claim leaks: a new registration proceeds unimpeded.
        assert not reg.retirement_pending("telegram-v2-aaa")
        await _register(reg, "telegram-v2-aaa", "sid-1")
        assert reg.get("telegram-v2-aaa")["sdk_session_id"] == "sid-1"

    async def test_removes_without_any_retain(self, tmp_path):
        """The pointers are dropped WITHOUT retention — retiring saves first,
        which is exactly the #411 residue the wipe exists to avoid."""
        reg = _reg(tmp_path)
        await _register(reg, "k", "sid-1")
        sem = FakeSem()
        await wipe_long_term_memory(
            registry=reg, semantic_memory=sem, fence=RetainFence(),
            bank="casa", retry_dir=tmp_path / "nospool",
        )
        assert sem.retained == []

    async def test_steered_fresh_session_survives(self, tmp_path):
        """Design r2 red case (Sol/Terra convergent): a racing turn steered
        fresh registers sid-new mid-wipe; the unconditional remove of v2
        deleted it — the sid-guarded remove must leave it standing."""
        reg = _reg(tmp_path)
        await _register(reg, "k", "sid-old")
        sem = FakeSem()

        real_notify = reg.notify_reset

        async def notify_and_race(key):
            await real_notify(key)
            # The racing turn publishes its steered-fresh session between the
            # flush-close and the remove.
            await _register(reg, "k", "sid-new")

        reg.notify_reset = notify_and_race  # type: ignore[method-assign]
        report = await wipe_long_term_memory(
            registry=reg, semantic_memory=sem, fence=RetainFence(),
            bank="casa", retry_dir=tmp_path / "nospool",
        )
        assert reg.get("k")["sdk_session_id"] == "sid-new"
        assert report.session_entries_dropped == 0

    async def test_dying_sid_republish_refused_during_wipe(self, tmp_path):
        """An in-flight old turn republishing the dying sid mid-wipe is
        refused at register(), so the wipe's remove leaves no pre-wipe sid."""
        reg = _reg(tmp_path)
        await _register(reg, "k", "sid-old")
        sem = FakeSem()

        real_notify = reg.notify_reset

        async def notify_and_republish(key):
            await real_notify(key)
            await _register(reg, "k", "sid-old")   # refused: claimed dying sid

        reg.notify_reset = notify_and_republish  # type: ignore[method-assign]
        await wipe_long_term_memory(
            registry=reg, semantic_memory=sem, fence=RetainFence(),
            bank="casa", retry_dir=tmp_path / "nospool",
        )
        assert reg.get("k") is None

    async def test_claims_cleared_on_bank_delete_failure(self, tmp_path):
        """A mid-wipe exception must not leave keys steering fresh forever."""
        reg = _reg(tmp_path)
        await _register(reg, "k", "sid-1")

        class ExplodingSem(FakeSem):
            async def delete_bank(self, bank):
                raise RuntimeError("backend down")

        with pytest.raises(RuntimeError):
            await wipe_long_term_memory(
                registry=reg, semantic_memory=ExplodingSem(),
                fence=RetainFence(), bank="casa",
                retry_dir=tmp_path / "nospool",
            )
        assert not reg.retirement_pending("k")

    async def test_spool_dropped_before_bank_delete(self, tmp_path):
        """Order pin: a spool record must be gone before the bank is —
        otherwise a replay window reopens between the two."""
        reg = _reg(tmp_path)
        spool = tmp_path / "spool"
        spool.mkdir()
        (spool / "r.json").write_text("{}")
        order: list[str] = []

        class OrderSem(FakeSem):
            async def delete_bank(self, bank):
                order.append(("spool_empty", not list(spool.glob("*.json"))))
                return True

        await wipe_long_term_memory(
            registry=reg, semantic_memory=OrderSem(), fence=RetainFence(),
            bank="casa", retry_dir=spool,
        )
        assert order == [("spool_empty", True)]


class TestSingleFlight:
    @pytest.fixture(autouse=True)
    def _reset_module_state(self, monkeypatch):
        monkeypatch.setattr(memory_wipe, "_wipe_task", None)
        monkeypatch.setattr(memory_wipe, "_wipes_frozen", False)

    async def test_second_wipe_refused_while_running(self):
        release = asyncio.Event()

        async def slow():
            await release.wait()

        t1 = memory_wipe.start_wipe_task(slow())
        assert t1 is not None
        assert memory_wipe.start_wipe_task(slow()) is None
        release.set()
        await t1
        # After completion a new wipe is admitted again.
        t2 = memory_wipe.start_wipe_task(slow())
        assert t2 is not None
        await t2

    async def test_freeze_refuses_new_wipes(self):
        async def w():
            return None

        memory_wipe.freeze_wipes()
        assert memory_wipe.start_wipe_task(w()) is None

    async def test_drain_waits_out_running_wipe(self):
        """Design r3 (Terra): shutdown drains a running wipe before channels
        and the memory backend go away."""
        done: list[str] = []
        release = asyncio.Event()

        async def slow():
            await release.wait()
            done.append("wipe-finished")

        memory_wipe.start_wipe_task(slow())
        memory_wipe.freeze_wipes()
        drain = asyncio.create_task(memory_wipe.drain_wipe_task())
        await asyncio.sleep(0.02)
        assert not drain.done()
        release.set()
        await drain
        assert done == ["wipe-finished"]


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

    async def test_unremovable_spool_record_aborts_before_deletion(
        self, tmp_path, monkeypatch,
    ):
        """Sol diff-r1: a spool record that cannot be removed is a surviving
        durable pre-wipe writer — the wipe must abort with the pointers and
        the bank untouched, never report success."""
        from pathlib import Path as _P

        reg = _reg(tmp_path)
        await _register(reg, "k", "sid-1")
        spool = tmp_path / "spool"
        spool.mkdir()
        (spool / "r.json").write_text("{}")
        sem = FakeSem()

        real_unlink = _P.unlink

        def failing_unlink(self, *a, **k):
            if self.suffix == ".json" and self.parent == spool:
                raise OSError("EPERM")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(_P, "unlink", failing_unlink)
        with pytest.raises(WipeAborted, match="could not be removed"):
            await wipe_long_term_memory(
                registry=reg, semantic_memory=sem, fence=RetainFence(),
                bank="casa", retry_dir=spool,
            )
        assert sem.deleted == []                       # bank untouched
        assert reg.get("k") is not None                # pointer untouched
        assert not reg.retirement_pending("k")         # claims released

    async def test_unreadable_spool_dir_aborts_before_deletion(
        self, tmp_path, monkeypatch,
    ):
        """Terra diff-r2: Path.glob() swallows the scandir OSError (an
        unreadable dir yields [] silently), so enumeration must use scandir
        directly — an unreadable spool dir aborts the wipe with nothing
        deleted."""
        reg = _reg(tmp_path)
        await _register(reg, "k", "sid-1")
        spool = tmp_path / "spool"
        spool.mkdir()
        (spool / "r.json").write_text("{}")
        sem = FakeSem()

        real_scandir = memory_wipe.os.scandir

        def failing_scandir(path):
            if str(path) == str(spool):
                raise PermissionError(13, "Permission denied", str(path))
            return real_scandir(path)

        monkeypatch.setattr(memory_wipe.os, "scandir", failing_scandir)
        with pytest.raises(WipeAborted, match="enumerate"):
            await wipe_long_term_memory(
                registry=reg, semantic_memory=sem, fence=RetainFence(),
                bank="casa", retry_dir=spool,
            )
        assert sem.deleted == []
        assert reg.get("k") is not None
        assert not reg.retirement_pending("k")

    async def test_missing_spool_dir_is_the_legitimate_empty(self, tmp_path):
        reg = _reg(tmp_path)
        sem = FakeSem()
        report = await wipe_long_term_memory(
            registry=reg, semantic_memory=sem, fence=RetainFence(),
            bank="casa", retry_dir=tmp_path / "never-created",
        )
        assert report.bank_deleted is True
        assert report.spool_records_dropped == 0
