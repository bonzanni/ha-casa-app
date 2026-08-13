"""#411 — the four long-term-bank writers behind the retain fence: a wipe
completing between a writer's capture point and its retain makes the writer
DISCARD — no retain, and (for the cold-retain arms) no spool write."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

import memory_wipe
import session_saver
from memory_wipe import RetainFence
from session_registry import SessionRegistry
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_MSGS = [type("M", (), {"type": "user", "message": {"content": "hi"}})()]


@pytest.fixture()
def fresh_fence(monkeypatch):
    fence = RetainFence()
    monkeypatch.setattr(memory_wipe, "FENCE", fence)
    return fence


async def _wipe_now(fence):
    async with fence.exclusive_wipe():
        pass


def _snapshot():
    from agent import SessionEntrySnapshot
    return SessionEntrySnapshot(
        agent="resident:assistant", sdk_session_id="sid-1", last_active="x",
        scope_class=None, binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )


async def test_save_session_discards_after_wipe(tmp_path, fresh_fence, monkeypatch):
    """A wipe completing between save_session's entry (generation capture)
    and its fenced retain section makes the save discard: nothing retained,
    claim released. The wipe lands inside try_begin_save — after the capture,
    before the fence — which is exactly the window the generation closes.
    (A wipe cannot complete INSIDE the fenced section — the exclusive drain
    waits for it; the deadlock-freedom twin below pins that.)"""
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register(
        "telegram-r1", "assistant", "sid-1", binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    sem = AsyncMock()
    real_begin = reg.try_begin_save

    async def begin_then_wipe(key, **kwargs):
        claimed = await real_begin(key, **kwargs)
        await _wipe_now(fresh_fence)
        return claimed

    reg.try_begin_save = begin_then_wipe  # type: ignore[method-assign]

    async def fake_classify(content):
        return "public"

    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    with patch("session_saver.get_session_messages", return_value=_MSGS):
        ok = await session_saver.save_session(
            "telegram-r1", reg, sem, directory="/d", channel="telegram",
        )
    assert ok is False
    sem.retain.assert_not_awaited()
    assert not reg.get("telegram-r1").get("consolidated_at")   # claim released


async def test_save_session_deadlock_free_when_wipe_waits(tmp_path, fresh_fence, monkeypatch):
    """Wait — the discard above requires the wipe to COMPLETE mid-save, but
    a real wipe's exclusive drain waits for the save's shared section. This
    pins the actual interleaving: the save (shared) finishes normally, the
    concurrently-waiting wipe then proceeds — no deadlock, bounded time."""
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register(
        "telegram-r1", "assistant", "sid-1", binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    sem = AsyncMock()
    in_save = asyncio.Event()

    async def fake_classify(content):
        in_save.set()
        await asyncio.sleep(0.05)
        return "public"

    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    async def run_save():
        with patch("session_saver.get_session_messages", return_value=_MSGS):
            return await session_saver.save_session(
                "telegram-r1", reg, sem, directory="/d", channel="telegram",
            )

    save_task = asyncio.create_task(run_save())
    await in_save.wait()
    await asyncio.wait_for(_wipe_now(fresh_fence), timeout=2)
    assert await asyncio.wait_for(save_task, timeout=2) is True
    sem.retain.assert_awaited_once()


async def test_cold_retain_discards_with_no_spool(tmp_path, fresh_fence, monkeypatch):
    """Design r3 (Sol S1): the generation is the one captured at the RESUME
    DECISION; a wipe completing before the detached body runs discards —
    no retain AND no spool record."""
    sem = AsyncMock()
    gen = fresh_fence.generation()      # capture: decision time
    await _wipe_now(fresh_fence)        # wipe completes before the task body
    spool = tmp_path / "spool"
    with patch("session_saver.get_session_messages", return_value=_MSGS):
        await session_saver.retain_cold_session(
            _snapshot(), directory="/d", channel="telegram",
            semantic_memory=sem, retry_dir=spool,
            fence_generation=gen,
        )
    sem.retain.assert_not_awaited()
    assert not spool.exists() or not list(spool.glob("*.json"))


async def test_cold_retain_failure_after_wipe_does_not_respool(
    tmp_path, fresh_fence, monkeypatch,
):
    """Design r2 (Sol S-C1'): a cold retain that FAILS with the generation
    already moved by the time its failure arm runs must not write a fresh
    spool record the wipe's sweep can no longer see. The generation move is
    simulated with a direct bump — a real wipe parked on the drain resumes
    only after the failure arm's sync check-then-spool, and then sweeps any
    record it wrote; the bump covers the leg where the wipe finished first."""
    gen = fresh_fence.generation()
    spool = tmp_path / "spool"

    class ExplodingSem:
        async def retain(self, *a, **k):
            fresh_fence._generation += 1   # "a wipe completed" before the arm
            raise RuntimeError("backend down")

    async def fake_classify(content):
        return "public"

    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    with patch("session_saver.get_session_messages", return_value=_MSGS):
        await session_saver.retain_cold_session(
            _snapshot(), directory="/d", channel="telegram",
            semantic_memory=ExplodingSem(), retry_dir=spool,
            fence_generation=gen,
        )
    assert not spool.exists() or not list(spool.glob("*.json"))


async def test_spool_replay_discards_and_drops_record(tmp_path, fresh_fence, monkeypatch):
    """A wipe completing between the per-record generation capture and the
    fenced retain (simulated with a direct bump in the record-decode window)
    discards the record: no retain, record dropped rather than retried."""
    sem = AsyncMock()
    spool = tmp_path / "spool"
    spool.mkdir()
    from speaker_provenance import provenance_mapping, provenance_from_mapping
    (spool / "sid-1.json").write_text(json.dumps({
        "sdk_session_id": "sid-1", "directory": "/d", "channel": "telegram",
        "speaker_provenance": provenance_mapping(STUB_SPEAKER_PROV),
        "user_provenance": provenance_mapping(STUB_USER_PROV),
        "attempts": 0,
    }))

    def bump_then_decode(mapping):
        fresh_fence._generation += 1   # "a wipe completed" post-capture
        return provenance_from_mapping(mapping)

    monkeypatch.setattr(session_saver, "provenance_from_mapping", bump_then_decode)
    with patch("session_saver.get_session_messages", return_value=_MSGS):
        await session_saver.retry_spooled_cold_retains(sem, retry_dir=spool)
    sem.retain.assert_not_awaited()
    assert list(spool.glob("*.json")) == []   # pre-wipe record dropped, not retried


async def test_retain_delegated_discards_after_wipe(fresh_fence, monkeypatch):
    import delegated_memory
    from personality_types import RetainedTurn

    sem = AsyncMock()

    async def fake_classify(content):
        return "public"

    monkeypatch.setattr(delegated_memory, "classify_tier", fake_classify)
    gen = fresh_fence.generation()      # captured at summary assembly
    await _wipe_now(fresh_fence)        # wipe completes before the task runs
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram",
        turns=[RetainedTurn("summary", STUB_SPEAKER_PROV)],
        fence_generation=gen,
    )
    sem.retain.assert_not_awaited()


async def test_retain_delegated_carries_application_tags(fresh_fence, monkeypatch):
    import delegated_memory
    from personality_types import RetainedTurn

    sem = AsyncMock()

    async def fake_classify(content):
        return "public"

    monkeypatch.setattr(delegated_memory, "classify_tier", fake_classify)
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram",
        turns=[RetainedTurn("summary", STUB_SPEAKER_PROV)],
        application_tags=["casa-doctrine-epoch-configurator-abcdef012345"],
    )
    sem.retain.assert_awaited_once()
    items = sem.retain.await_args.args[1]
    assert "casa-doctrine-epoch-configurator-abcdef012345" in items[0]["tags"]
