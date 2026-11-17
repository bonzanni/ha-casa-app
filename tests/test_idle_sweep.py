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
                               {"role": "assistant"})
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
                               {"role": "assistant"})
        await reg.mark_completed(rec.id, completed_at=rec.started_at + 1)
        now = rec.started_at + 10 * 86400
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=True)
        driver.cancel = AsyncMock()
        await reg.sweep_idle_and_suspend(driver=driver, now_override=now)
        driver.cancel.assert_not_awaited()
        assert rec.status == "completed"


async def _sweep_with_facts(tmp_path, facts):
    """One job, 30 h after its last operator input, whose driver reports
    *facts* (a function of the sweep's time)."""
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create("specialist", "finance", "in_casa", "t",
                           {"role": "assistant"})
    rec.last_user_turn_ts = rec.started_at
    now = rec.started_at + 30 * 3600
    driver = MagicMock()
    driver.is_alive = MagicMock(return_value=True)
    driver.get_session_id = MagicMock(return_value="sess-x")
    driver.job_facts = MagicMock(return_value=facts(now))
    driver.cancel = AsyncMock()
    await reg.sweep_idle_and_suspend(driver=driver, now_override=now)
    return rec, driver


@pytest.mark.parametrize("state", ["working", "silent", "recovering"])
async def test_a_job_whose_session_is_in_use_is_not_suspended(tmp_path, state):
    """One turn can run for hours: a session with a turn running — whether
    it shows activity or has been silent past the limit — or one Casa is
    recovering is in use, not idle."""
    import background_jobs as bj
    from drivers.in_casa_driver import JobFacts

    facts = {
        "working": lambda now: JobFacts(True, False, now - 10),
        "silent": lambda now: JobFacts(True, False, now - bj.SILENT_AFTER_S - 60),
        "recovering": lambda now: JobFacts(False, True, None),
    }[state]
    rec, driver = await _sweep_with_facts(tmp_path, facts)
    assert bj.job_state(rec, driver.job_facts(rec.id),
                        rec.started_at + 30 * 3600) == state
    driver.cancel.assert_not_awaited()
    assert rec.status == "active"


async def test_a_job_between_turns_is_suspended(tmp_path):
    """No open turn, however old its session's last activity: idle."""
    from drivers.in_casa_driver import JobFacts

    rec, driver = await _sweep_with_facts(
        tmp_path, lambda now: JobFacts(False, False, now - 29 * 3600))
    driver.cancel.assert_awaited_once()
    assert rec.status == "idle"
