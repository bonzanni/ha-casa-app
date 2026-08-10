# tests/test_engagement_context_rebuild.py
"""#369: a clearance downgrade must durably mark the engagement's session
context for rebuild, so no resume path — steering, continuation, boot replay —
can ever bring the pre-clamp transcript (or the archive injected at the old
clearance) back to life.

The flag is set in the SAME locked step as the clamp itself and persisted with
the same strict write: a crash anywhere between the clamp and the rebuild
leaves a record that REFUSES resume, never one that resumes the old session.
It clears only when a fresh session has been established at the clamped floor.
"""
from __future__ import annotations

import json

import pytest

from engagement_registry import EngagementRegistry

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def _make(tmp_path, **origin_extra):
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="secret task", topic_id=7,
        origin={"channel": "telegram", "_origin_route": "telegram_dm",
                "_origin_clearance": "private", **origin_extra},
    )
    return reg, rec


class TestClampSetsRebuildPending:
    async def test_lowering_clearance_sets_the_flag_durably(self, tmp_path):
        reg, rec = await _make(tmp_path)
        assert rec.context_rebuild_pending is False
        assert await reg.lower_origin_clearance(rec.id, "public") is True
        assert rec.context_rebuild_pending is True
        # Durable: a reloaded registry still refuses to resume this context.
        reg2 = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        assert reg2.get(rec.id).context_rebuild_pending is True
        assert reg2.get(rec.id).origin["_origin_clearance"] == "public"

    async def test_clamp_bumps_the_context_generation_durably(self, tmp_path):
        """Terra diff-gate r2: the boolean flag cannot see a clamp→rebuild
        cycle that completed while a launch was suspended — the monotonic
        generation is what a stale launch's captured value is compared to."""
        reg, rec = await _make(tmp_path)
        assert rec.context_generation == 0
        await reg.lower_origin_clearance(rec.id, "friends")
        assert rec.context_generation == 1
        await reg.clear_context_rebuild_pending(rec.id)
        await reg.lower_origin_clearance(rec.id, "public")
        assert rec.context_generation == 2   # never reset by a rebuild
        reg2 = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        assert reg2.get(rec.id).context_generation == 2

    async def test_clamp_withholds_the_launch_materials(self, tmp_path):
        """The task/brief/context/world-state were authored at the CREATING
        turn's clearance — after a downgrade every later render (boot-replay
        CLAUDE.md refresh, session rebuild, resume options) re-derives from
        the record, so the record itself must stop carrying them."""
        reg, rec = await _make(
            tmp_path, brief={"goal": "secret"}, context="private ctx",
            world_state_summary="private world")
        await reg.lower_origin_clearance(rec.id, "public")
        assert "secret task" not in rec.task
        assert rec.task != ""            # a render still needs SOME text
        assert "brief" not in rec.origin
        assert "context" not in rec.origin
        assert "world_state_summary" not in rec.origin
        # Durable.
        reg2 = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        rec2 = reg2.get(rec.id)
        assert "secret task" not in rec2.task
        assert "brief" not in rec2.origin

    async def test_noop_clamp_does_not_set_the_flag(self, tmp_path):
        reg, rec = await _make(tmp_path)
        await reg.lower_origin_clearance(rec.id, "public")
        await reg.clear_context_rebuild_pending(rec.id)
        # Same floor again: record does not move, flag stays cleared.
        assert await reg.lower_origin_clearance(rec.id, "public") is False
        assert rec.context_rebuild_pending is False
        # Attempted RAISE never sets it either.
        assert await reg.lower_origin_clearance(rec.id, "private") is False
        assert rec.context_rebuild_pending is False

    async def test_clear_is_durable(self, tmp_path):
        reg, rec = await _make(tmp_path)
        await reg.lower_origin_clearance(rec.id, "friends")
        await reg.clear_context_rebuild_pending(rec.id)
        assert rec.context_rebuild_pending is False
        reg2 = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        assert reg2.get(rec.id).context_rebuild_pending is False

    async def test_loading_a_pre_flag_row_defaults_to_false(self, tmp_path):
        reg, rec = await _make(tmp_path)
        # Simulate a tombstone written by a release that predates the field.
        path = tmp_path / "e.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            row.pop("context_rebuild_pending", None)
        path.write_text(json.dumps(rows), encoding="utf-8")
        reg2 = EngagementRegistry(tombstone_path=str(path), bus=None)
        await reg2.load()
        assert reg2.get(rec.id).context_rebuild_pending is False


class TestClearSessionId:
    async def test_clear_session_id_removes_the_resume_pointer_durably(self, tmp_path):
        reg, rec = await _make(tmp_path)
        await reg.persist_session_id(rec.id, "sid-old")
        assert reg.get(rec.id).sdk_session_id == "sid-old"
        await reg.clear_session_id(rec.id)
        assert reg.get(rec.id).sdk_session_id is None
        reg2 = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        assert reg2.get(rec.id).sdk_session_id is None
