"""Tests for engagement_registry.sweep_idle_and_suspend."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestSweep:
    async def test_suspends_live_client_after_24h_idle(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               {"role": "assistant"}, topic_id=1)
        now = rec.started_at + 30 * 3600  # 30h later
        rec.last_user_turn_ts = rec.started_at
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=True)
        driver.get_session_id = MagicMock(return_value="sess-x")
        driver.cancel = AsyncMock()

        await reg.sweep_idle_and_suspend(driver=driver, now_override=now)
        driver.cancel.assert_awaited_once()
        assert rec.sdk_session_id == "sess-x"
        assert rec.status == "idle"

    async def test_skips_non_active_records(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               {"role": "assistant"}, topic_id=1)
        await reg.mark_completed(rec.id, completed_at=rec.started_at + 1)
        now = rec.started_at + 10 * 86400
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=True)
        driver.cancel = AsyncMock()
        await reg.sweep_idle_and_suspend(driver=driver, now_override=now)
        driver.cancel.assert_not_awaited()
        assert rec.status == "completed"
