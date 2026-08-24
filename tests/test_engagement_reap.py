"""D-4 auto-reap: stale engagements are cancelled through the finalize funnel.

Interrupted/abandoned engagements used to linger `active` forever (25-day
stale engagement found 2026-07-10; restart-orphan reaped manually
2026-07-11). ``tools.reap_stale_engagements`` runs in the daily engagement
sweep, BEFORE the idle-reminder pass, so a to-be-reaped record doesn't get a
pointless reminder in the same run.

v0.69.6 (codex review of D-4): the reap resolves the driver PER RECORD
(claude_code executors are torn down only by the claude_code driver — the
in-casa driver leaks their s6 subprocess), and the staleness predicate is
part of the locked terminal transition so a just-revived engagement is not
reaped.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _driver_double():
    """A driver double shaped like the REAL engagement drivers.

    A bare ``MagicMock()`` fabricates EVERY attribute, so the funnel's
    ``hasattr`` probes find ``finalize_completion_post``, ``finalize_summary``,
    ``settle_all_open_questions`` and ``drain_inbound_spool`` — none of which
    ``InCasaDriver`` has — and awaiting their non-awaitable returns raises
    ``TypeError`` into the guards around each one.

    Before #678 that was invisible: the completion post's failure was swallowed
    and the funnel painted and closed regardless, so these tests asserted a
    close that only happened because the double was broken. Now a fabricated
    ``finalize_completion_post`` tells the funnel a sequencer post was
    ATTEMPTED and failed part-way, and it correctly declines to replay the
    summary. The double was always wrong; what changed is that it is no longer
    silent.
    """
    d = MagicMock()
    d.cancel = AsyncMock()
    for hook in ("finalize_completion_post", "finalize_summary",
                 "settle_all_open_questions", "drain_inbound_spool"):
        delattr(d, hook)
    return d



@pytest.fixture(autouse=True)
def _restore_agent_drivers():
    """The reap resolves drivers off the `agent` module globals; save/restore
    them so tests don't leak driver mocks into each other."""
    import agent as agent_mod
    saved = (
        getattr(agent_mod, "active_claude_code_driver", None),
        getattr(agent_mod, "active_engagement_driver", None),
    )
    yield
    agent_mod.active_claude_code_driver, agent_mod.active_engagement_driver = saved


def _wire(tmp_path):
    """Registry + mocks + init_tools; returns (rec, registry, telegram, bus,
    driver). Also installs `driver` as BOTH agent-module driver globals so the
    per-record resolution finds it regardless of rec.driver."""
    import agent as agent_mod
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    # #678: the paged rich sender is the one the funnel PREFERS, and a bare
    # MagicMock fabricates it — so `hasattr` found it, awaiting its
    # non-awaitable return raised TypeError, the funnel swallowed that and
    # closed the topic anyway. This test's close assertion was passing because
    # the double was broken. The real TelegramChannel has this method; the
    # double now has it too, and returns a message id, which is what the
    # funnel reads as confirmation.
    telegram.send_response_to_topic = AsyncMock(return_value=4242)
    telegram.close_topic = AsyncMock()
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
    agent_mod.active_claude_code_driver = driver
    agent_mod.active_engagement_driver = driver
    return reg, telegram, bus, driver


async def _stale_record(reg, *, days: float, topic_id: int = 42,
                        status: str = "active", driver: str = "claude_code"):
    rec = await reg.create(
        "executor", "configurator", driver, "install plugin X",
        {"role": "assistant", "channel": "telegram", "chat_id": "123"}, topic_id,
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
        telegram.close_topic.assert_awaited_once_with(thread_id=42)
        bus.notify.assert_awaited_once()

    async def test_reaps_idle_past_ttl(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=9, status="idle")
        await reap_stale_engagements(ttl_days=7)
        assert rec.status == "cancelled"

    async def test_reap_uses_claude_code_driver_for_claude_code_records(self, tmp_path):
        """Finding 1 (blocker): a claude_code executor engagement's s6
        subprocess is stopped ONLY by the claude_code driver. Reaping with the
        in-casa driver closes the topic but leaks the process."""
        import agent as agent_mod
        from tools import reap_stale_engagements

        reg, telegram, bus, _ = _wire(tmp_path)
        cc_driver = _driver_double()
        in_casa = MagicMock(); in_casa.cancel = AsyncMock()
        agent_mod.active_claude_code_driver = cc_driver
        agent_mod.active_engagement_driver = in_casa
        rec = await _stale_record(reg, days=8, driver="claude_code")

        await reap_stale_engagements(ttl_days=7)

        cc_driver.cancel.assert_awaited_once_with(rec)
        in_casa.cancel.assert_not_awaited()

    async def test_reap_uses_in_casa_driver_for_in_casa_records(self, tmp_path):
        import agent as agent_mod
        from tools import reap_stale_engagements

        reg, telegram, bus, _ = _wire(tmp_path)
        cc_driver = _driver_double()
        in_casa = MagicMock(); in_casa.cancel = AsyncMock()
        agent_mod.active_claude_code_driver = cc_driver
        agent_mod.active_engagement_driver = in_casa
        rec = await _stale_record(reg, days=8, driver="in_casa")

        await reap_stale_engagements(ttl_days=7)

        in_casa.cancel.assert_awaited_once_with(rec)
        cc_driver.cancel.assert_not_awaited()

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
        telegram.close_topic.assert_not_awaited()

    async def test_skips_young_records(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=2)
        assert await reap_stale_engagements(ttl_days=7) == 0
        assert rec.status == "active"
        telegram.close_topic.assert_not_awaited()

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
        young = await _stale_record(reg, days=3, topic_id=42)
        old = await _stale_record(reg, days=8, topic_id=43)
        monkeypatch.setenv("ENGAGEMENT_REAP_DAYS", "banana")
        await reap_stale_engagements()
        assert young.status == "active"
        assert old.status == "cancelled"

    async def test_one_bad_record_does_not_stop_the_sweep(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        bad = await _stale_record(reg, days=10, topic_id=44)
        good = await _stale_record(reg, days=10, topic_id=45)
        # First close_topic explodes; the second record must still be reaped.
        telegram.close_topic.side_effect = [RuntimeError("tg down"), None]
        await reap_stale_engagements(ttl_days=7)
        assert bad.status == "cancelled"   # registry transition precedes channel I/O
        assert good.status == "cancelled"
