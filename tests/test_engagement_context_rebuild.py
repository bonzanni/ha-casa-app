# tests/test_engagement_context_rebuild.py
"""#369: the record's ``context_rebuild_pending`` flag. A memory wipe sets it
on every open in_casa record (its session transcript is gone), and the driver
refuses to resume a marked record (``tests/test_memory_wipe.py``); the
in-process tool fence refuses its tools (``test_engagement_rebuild_fences``).
Here: a record written without the field loads unmarked.
"""
from __future__ import annotations

import json

import pytest

from engagement_registry import EngagementRegistry

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def _make(tmp_path, **origin_extra):
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="secret task", topic_id=7,
        origin={"channel": "telegram", "_origin_route": "telegram_dm",
                "_origin_clearance": "private", **origin_extra},
    )
    return reg, rec


class TestRebuildPendingFlag:
    async def test_loading_a_pre_flag_row_defaults_to_false(self, tmp_path):
        reg, rec = await _make(tmp_path)
        # Simulate a tombstone written by a release that predates the field.
        path = tmp_path / "e.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            row.pop("context_rebuild_pending", None)
        path.write_text(json.dumps(rows), encoding="utf-8")
        reg2 = EngagementRegistry(tombstone_path=str(path))
        await reg2.load()
        assert reg2.get(rec.id).context_rebuild_pending is False
