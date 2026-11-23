"""Spec §4 — VoiceSessionPool: create/touch/evict.

Keyed on (role, scope_id) since v0.80.0 (spec A2) — role is a required
keyword on every lookup so two residents sharing one device/scope can never
collide on (and therefore never resume) each other's SDK session.
"""

import asyncio

import pytest

from channels.voice.session import VoiceSession, VoiceSessionPool
from session_registry import build_scoped_session_key


@pytest.mark.asyncio
class TestPoolLifecycle:
    async def test_create_on_first_ensure(self):
        pool = VoiceSessionPool(idle_timeout=300, gate_slots=10)
        sess = pool.ensure("user-xyz", role="butler")
        assert isinstance(sess, VoiceSession)
        assert sess.scope_id == "user-xyz"
        assert sess.role == "butler"
        assert sess.session_key == build_scoped_session_key("voice", "butler", "user-xyz")

    async def test_ensure_returns_same_instance(self):
        pool = VoiceSessionPool(idle_timeout=300)
        a = pool.ensure("user-xyz", role="butler")
        b = pool.ensure("user-xyz", role="butler")
        assert a is b

    async def test_distinct_roles_distinct_sessions_same_scope(self):
        """spec A2: two residents sharing one device/scope must never
        collide on a pool entry (and therefore never share a session_key)."""
        pool = VoiceSessionPool(idle_timeout=300)
        a = pool.ensure("user-xyz", role="butler")
        b = pool.ensure("user-xyz", role="concierge")
        assert a is not b
        assert a.session_key != b.session_key
        assert pool.get("user-xyz", role="butler") is a
        assert pool.get("user-xyz", role="concierge") is b

    async def test_touch_updates_last_activity(self, monkeypatch):
        clock = [100.0]
        monkeypatch.setattr(
            "channels.voice.session.time.monotonic", lambda: clock[0],
        )
        pool = VoiceSessionPool(idle_timeout=300)
        sess = pool.ensure("user-xyz", role="butler")
        assert sess.last_activity == 100.0
        clock[0] = 150.0
        pool.touch("user-xyz", role="butler")
        assert sess.last_activity == 150.0


@pytest.mark.asyncio
class TestIdleSweep:
    async def test_evicts_after_idle_timeout(self, monkeypatch):
        clock = [100.0]
        monkeypatch.setattr(
            "channels.voice.session.time.monotonic", lambda: clock[0],
        )
        pool = VoiceSessionPool(idle_timeout=10)
        pool.ensure("user-xyz", role="butler")
        clock[0] = 200.0  # well past timeout
        evicted = pool.sweep()
        assert evicted == ["user-xyz"]
        assert pool.get("user-xyz", role="butler") is None


@pytest.mark.asyncio
class TestGate:
    async def test_gate_slots_default(self):
        pool = VoiceSessionPool(idle_timeout=300, gate_slots=10)
        sess = pool.ensure("user-xyz", role="butler")
        # Semaphore does not expose the initial value; ensure it accepts 10 acquires.
        acquired = [await sess.gate.acquire() for _ in range(10)]
        assert all(a is True for a in acquired)
        sess.gate.release()
        sess.gate.release()


@pytest.mark.asyncio
class TestSweeperHygiene:
    async def test_run_sweeper_cancels_cleanly(self):
        pool = VoiceSessionPool(idle_timeout=300)
        task = asyncio.create_task(pool.run_sweeper(interval=0.01))
        await asyncio.sleep(0.02)
        task.cancel()
        await task  # must not raise
        assert task.cancelled() or task.done()
