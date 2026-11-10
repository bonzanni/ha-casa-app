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


@pytest.mark.parametrize("probe", ["turn_open", "recovering"])
async def test_a_working_job_session_is_not_suspended(tmp_path, probe):
    """One turn can run for hours: a session with a turn running, or one
    Casa is recovering, is working, not idle."""
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create("specialist", "finance", "in_casa", "t",
                           {"role": "assistant"}, topic_id=1)
    rec.last_user_turn_ts = rec.started_at
    driver = MagicMock()
    driver.is_alive = MagicMock(return_value=True)
    driver.turn_open = MagicMock(return_value=False)
    driver.recovering = MagicMock(return_value=False)
    getattr(driver, probe).return_value = True
    driver.cancel = AsyncMock()
    await reg.sweep_idle_and_suspend(driver=driver,
                                     now_override=rec.started_at + 30 * 3600)
    driver.cancel.assert_not_awaited()
    assert rec.status == "active"
