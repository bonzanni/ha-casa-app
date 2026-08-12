"""#283 — EngagementRegistry integration of the agent-spawn cap.

Pins: (a) the spawn token transfers to the record AT create() success and is
released by terminal transitions (Sol design r4: never left to the caller's
post-driver-start transfer); (b) a marked record cannot be created without
its reservation; (c) boot load() restores occupancy (debt included) for live
marked rows via the injected limiter; (d) both permit fields release on
terminal transitions.
"""
import asyncio
import json

import pytest

from engagement_registry import EngagementRegistry
from specialist_limits import AgentSpawnLimiter


def _mk_registry(tmp_path, limiter=None):
    return EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"),
        bus=None,
        agent_spawn_limiter=limiter,
    )


def test_create_transfers_spawn_token_to_record_and_terminal_releases(tmp_path):
    async def run():
        lim = AgentSpawnLimiter(max_spawns=3)
        reg = _mk_registry(tmp_path, lim)
        tok = lim.try_acquire()
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"_agent_spawned": True}, topic_id=None,
            agent_spawn_permit=tok,
        )
        assert rec.agent_spawn_permit is tok
        assert lim.occupancy == 1
        await reg.mark_cancelled(rec.id)
        assert lim.occupancy == 0

    asyncio.run(run())


def test_create_refuses_marked_record_without_reservation(tmp_path):
    async def run():
        reg = _mk_registry(tmp_path, AgentSpawnLimiter(max_spawns=3))
        with pytest.raises(ValueError):
            await reg.create(
                kind="executor", role_or_type="configurator", driver="in_casa",
                task="t", origin={"_agent_spawned": True}, topic_id=None,
            )

    asyncio.run(run())


def test_create_unmarked_needs_no_reservation(tmp_path):
    async def run():
        reg = _mk_registry(tmp_path, AgentSpawnLimiter(max_spawns=3))
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={}, topic_id=None,
        )
        assert rec.agent_spawn_permit is None

    asyncio.run(run())


def test_release_both_permits_on_terminal(tmp_path):
    async def run():
        lim = AgentSpawnLimiter(max_spawns=3)
        reg = _mk_registry(tmp_path, lim)

        class _FakePermit:
            released = False

            def release(self):
                self.released = True

        tok = lim.try_acquire()
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"_agent_spawned": True}, topic_id=None,
            agent_spawn_permit=tok,
        )
        fake = _FakePermit()
        rec.permit = fake
        await reg.mark_completed(rec.id, completed_at=1.0)
        assert fake.released
        assert lim.occupancy == 0

    asyncio.run(run())


def test_load_restores_occupancy_for_live_marked_rows_as_debt(tmp_path):
    async def run():
        path = tmp_path / "engagements.json"
        rows = []
        base = dict(
            kind="executor", role_or_type="configurator", driver="in_casa",
            started_at=1.0, last_user_turn_ts=1.0, completed_at=None,
            sdk_session_id=None, task="t", topic_id=None,
        )
        # Four live marked rows (debt beyond cap 3), one terminal marked row
        # (must not count), one live unmarked row (must not count).
        for i in range(4):
            rows.append({**base, "id": f"m{i}", "status": "active",
                         "origin": {"_agent_spawned": True}})
        rows.append({**base, "id": "done", "status": "completed",
                     "completed_at": 2.0, "origin": {"_agent_spawned": True}})
        rows.append({**base, "id": "op", "status": "active", "origin": {}})
        path.write_text(json.dumps(rows), encoding="utf-8")

        lim = AgentSpawnLimiter(max_spawns=3)
        reg = _mk_registry(tmp_path, lim)
        await reg.load()
        assert lim.occupancy == 4  # debt: 4 live marked
        assert lim.try_acquire() is None
        # One reap: still >= cap live marked → still refused (r3 Terra).
        await reg.mark_cancelled("m0")
        assert lim.occupancy == 3
        assert lim.try_acquire() is None
        await reg.mark_cancelled("m1")
        assert lim.occupancy == 2
        assert lim.try_acquire() is not None

    asyncio.run(run())


def test_load_without_limiter_keeps_working(tmp_path):
    async def run():
        path = tmp_path / "engagements.json"
        path.write_text(json.dumps([{
            "id": "m0", "kind": "executor", "role_or_type": "c",
            "driver": "in_casa", "status": "active", "topic_id": None,
            "started_at": 1.0, "last_user_turn_ts": 1.0,
            "completed_at": None, "sdk_session_id": None, "task": "t",
            "origin": {"_agent_spawned": True},
        }]), encoding="utf-8")
        reg = _mk_registry(tmp_path, None)
        await reg.load()
        assert reg.get("m0") is not None

    asyncio.run(run())
