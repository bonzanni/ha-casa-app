"""D-4 auto-reap: stale engagements are cancelled through the finalize funnel.

Interrupted/abandoned engagements used to linger `active` forever (25-day
stale engagement found 2026-07-10; restart-orphan reaped manually
2026-07-11). ``tools.reap_stale_engagements`` runs in the daily engagement
sweep, BEFORE the idle-reminder pass, so a to-be-reaped record doesn't get a
pointless reminder in the same run.

v0.69.6 (codex review of D-4): the staleness predicate is part of the
locked terminal transition so a just-revived engagement is not reaped. The
cancel goes through the in-casa engagement driver.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _driver_double():
    """A driver double whose ``cancel`` is awaitable, like ``InCasaDriver``'s."""
    d = MagicMock()
    d.cancel = AsyncMock()
    return d


@pytest.fixture(autouse=True)
def _restore_agent_drivers():
    """The reap resolves the driver off the `agent` module global; save/restore
    it so tests don't leak driver mocks into each other."""
    import agent as agent_mod
    saved = getattr(agent_mod, "active_engagement_driver", None)
    yield
    agent_mod.active_engagement_driver = saved


def _wire(tmp_path):
    """Registry + mocks + init_tools; returns (rec, registry, telegram, bus,
    driver). Installs `driver` as the agent-module engagement driver."""
    import agent as agent_mod
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    telegram = MagicMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    driver = _driver_double()
    agent_mod.active_engagement_driver = driver
    return reg, telegram, bus, driver


async def _stale_record(reg, *, days: float,
                        status: str = "active", driver: str = "in_casa"):
    rec = await reg.create(
        "executor", "configurator", driver, "install plugin X",
        {"role": "assistant", "channel": "telegram", "chat_id": "123"},
    )
    rec.started_at = time.time() - days * 86400
    rec.last_user_turn_ts = time.time() - days * 86400
    if status != "active":
        rec.status = status
    return rec


class TestReapStaleEngagements:
    async def test_reaps_active_past_ttl(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=8)
        assert await reap_stale_engagements(ttl_days=7) == 1
        assert rec.status == "cancelled"
        bus.notify.assert_awaited_once()

    async def test_reaps_idle_past_ttl(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=9, status="idle")
        await reap_stale_engagements(ttl_days=7)
        assert rec.status == "cancelled"

    async def test_reap_uses_in_casa_driver_for_in_casa_records(self, tmp_path):
        import agent as agent_mod
        from tools import reap_stale_engagements

        reg, telegram, bus, _ = _wire(tmp_path)
        in_casa = _driver_double()
        agent_mod.active_engagement_driver = in_casa
        rec = await _stale_record(reg, days=8, driver="in_casa")

        await reap_stale_engagements(ttl_days=7)

        in_casa.cancel.assert_awaited_once_with(rec)

    async def test_revived_record_is_not_reaped(self, tmp_path):
        """Finding 2 (major): the staleness predicate must be part of the
        locked transition. A record whose last_user_turn_ts is refreshed to
        'now' after the reap's cutoff snapshot must NOT be cancelled."""
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=8, status="idle")
        # Simulate a user turn reviving it right at the cutoff boundary:
        # try_transition_terminal(stale_before=cutoff) must see it fresh.
        rec.status = "active"
        rec.last_user_turn_ts = time.time()  # fresh — newer than any cutoff

        reaped = await reap_stale_engagements(ttl_days=7)
        # Reaped count is 0 and the engagement is left live.
        assert reaped == 0
        assert rec.status == "active"
        bus.notify.assert_not_awaited()

    async def test_skips_young_records(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=2)
        assert await reap_stale_engagements(ttl_days=7) == 0
        assert rec.status == "active"
        bus.notify.assert_not_awaited()

    async def test_ttl_zero_disables_reap(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=100)
        await reap_stale_engagements(ttl_days=0)
        assert rec.status == "active"

    async def test_ttl_defaults_from_env(self, tmp_path, monkeypatch):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=3)
        monkeypatch.setenv("ENGAGEMENT_REAP_DAYS", "2")
        await reap_stale_engagements()
        assert rec.status == "cancelled"

    async def test_garbage_env_falls_back_to_default_7d(self, tmp_path, monkeypatch):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        young = await _stale_record(reg, days=3)
        old = await _stale_record(reg, days=8)
        monkeypatch.setenv("ENGAGEMENT_REAP_DAYS", "banana")
        await reap_stale_engagements()
        assert young.status == "active"
        assert old.status == "cancelled"

    async def test_one_bad_record_does_not_stop_the_sweep(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        bad = await _stale_record(reg, days=10)
        good = await _stale_record(reg, days=10)
        # The first teardown explodes; the second record must still be reaped.
        driver.cancel.side_effect = [RuntimeError("driver down"), None]
        await reap_stale_engagements(ttl_days=7)
        assert bad.status == "cancelled"   # registry transition precedes teardown
        assert good.status == "cancelled"


class TestAWorkingJobIsNeverStale:
    """A job's turn can run for hours with no turn end, and its session can
    be in recovery: the reap skips a job whose driver reports either, as the
    idle suspension does."""

    @pytest.mark.parametrize("probe", ["turn_open", "recovering"])
    async def test_a_working_job_is_not_reaped(self, tmp_path, probe):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=30)
        getattr(driver, probe).return_value = True
        assert await reap_stale_engagements(ttl_days=7) == 0
        assert rec.status == "active"
        driver.cancel.assert_not_awaited()

    async def test_an_idle_waiting_job_past_its_expiry_is_reaped(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=30)
        driver.turn_open.return_value = False
        driver.recovering.return_value = False
        assert await reap_stale_engagements(ttl_days=7) == 1
        assert rec.status == "cancelled"
