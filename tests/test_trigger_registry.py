"""Tests for trigger_registry.py — per-agent scheduling + webhooks."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bus():
    bus = MagicMock()
    bus.send = AsyncMock()
    return bus


def _make_scheduler():
    sched = MagicMock()
    sched.add_job = MagicMock()
    return sched


def _trigger_interval(name="hb", minutes=60, channel="telegram"):
    from config import TriggerSpec
    return TriggerSpec(name=name, type="interval", minutes=minutes,
                       channel=channel, prompt="tick")


def _trigger_cron(name="morning", schedule="0 7 * * *", channel="telegram"):
    from config import TriggerSpec
    return TriggerSpec(name=name, type="cron", schedule=schedule,
                       channel=channel, prompt="morning digest")


def _trigger_webhook(name="gh", path="/webhook/gh"):
    from config import TriggerSpec
    return TriggerSpec(name=name, type="webhook", path=path)


# ---------------------------------------------------------------------------
# TestInterval
# ---------------------------------------------------------------------------


class TestInterval:
    async def test_interval_registers_apscheduler_job(self):
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            role="assistant",
            triggers=[_trigger_interval()],
            channels=["telegram", "webhook"],
        )

        sched.add_job.assert_called_once()
        kwargs = sched.add_job.call_args.kwargs
        assert kwargs["trigger"] == "interval"
        assert kwargs["minutes"] == 60
        assert kwargs["id"] == "assistant:hb"

    async def test_interval_job_sends_scheduled_message(self):
        from trigger_registry import TriggerRegistry
        from bus import MessageType

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent("assistant", [_trigger_interval()],
                           channels=["telegram"])

        # Extract the async callable APScheduler would invoke.
        func = sched.add_job.call_args.args[0]
        await func()

        bus.send.assert_awaited_once()
        msg = bus.send.call_args.args[0]
        assert msg.type == MessageType.SCHEDULED
        assert msg.target == "assistant"
        assert msg.channel == "telegram"
        assert msg.content == "tick"

    async def test_interval_chat_id_is_honcho_compliant(self):
        """Spec §3.1: scheduled trigger chat_id must satisfy Honcho's
        `[A-Za-z0-9_-]+` regex so build_session_key + honcho_session_id
        do not raise. Producer-validator drift was the v0.17.1
        regression — protect with a roundtrip assertion."""
        from trigger_registry import TriggerRegistry
        from session_registry import build_session_key

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            "assistant",
            [_trigger_interval(name="heartbeat")],
            channels=["telegram"],
        )

        func = sched.add_job.call_args.args[0]
        await func()

        msg = bus.send.call_args.args[0]
        assert msg.context["chat_id"] == "interval-heartbeat"

        # Roundtrip: must not raise. This is the load-bearing check —
        # producer hyphen must match honcho_session_id's regex.
        key = build_session_key(msg.channel, msg.context["chat_id"])
        assert key == "telegram-interval-heartbeat"


# ---------------------------------------------------------------------------
# TestCron
# ---------------------------------------------------------------------------


class TestCron:
    async def test_cron_registers_apscheduler_job(self):
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            "assistant", [_trigger_cron()], channels=["telegram"],
        )

        sched.add_job.assert_called_once()
        kwargs = sched.add_job.call_args.kwargs
        assert kwargs["trigger"] == "cron"
        assert kwargs["id"] == "assistant:morning"


# ---------------------------------------------------------------------------
# #343: cron day-of-week numbering — cron says 0/7=Sunday, APScheduler 3.x
# says 0=Monday. Numeric DOW fields must be translated (as day names, which
# mean the same days in both conventions).
# ---------------------------------------------------------------------------


class TestCronDayOfWeekTranslation:
    def test_translate_tokens(self):
        from trigger_registry import _translate_cron_dow
        assert _translate_cron_dow("*") == "*"
        assert _translate_cron_dow("0") == "sun"
        assert _translate_cron_dow("7") == "sun"       # cron allows 7=Sunday
        assert _translate_cron_dow("1") == "mon"
        assert _translate_cron_dow("6") == "sat"
        assert _translate_cron_dow("1-5") == "mon,tue,wed,thu,fri"
        assert _translate_cron_dow("0,3,6") == "sun,wed,sat"
        assert _translate_cron_dow("*/2") == "sun,tue,thu,sat"
        assert _translate_cron_dow("1-5/2") == "mon,wed,fri"
        assert _translate_cron_dow("sun") == "sun"     # names pass through
        assert _translate_cron_dow("MON-FRI") == "mon,tue,wed,thu,fri"
        # Terra r1-3: ranges ending in the Sunday alias 7 must not
        # collapse — 0-7 and 1-7 are full ascending ranges.
        assert _translate_cron_dow("0-7") == "sun,mon,tue,wed,thu,fri,sat"
        assert (_translate_cron_dow("1-7")
                == "mon,tue,wed,thu,fri,sat,sun")
        assert _translate_cron_dow("5-7") == "fri,sat,sun"
        # Terra r3-1: "n/step" = n-7/step (the raw Sunday alias is the
        # field max) — "7/2" is Sunday only, not a week wraparound.
        assert _translate_cron_dow("7/2") == "sun"
        assert _translate_cron_dow("5/2") == "fri,sun"
        assert _translate_cron_dow("1/2") == "mon,wed,fri,sun"
        # cron wrap range (Sat-Sun) expands rather than hitting
        # APScheduler's no-wrap range rule.
        assert _translate_cron_dow("6-0") == "sat,sun"

    def test_translate_invalid_raises(self):
        from trigger_registry import TriggerError, _translate_cron_dow
        for bad in ("8", "boo", "1-", "-3", "1-5/0", "1//2", ""):
            with pytest.raises(TriggerError):
                _translate_cron_dow(bad)

    async def test_numeric_sunday_registers_as_sun(self):
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        reg = TriggerRegistry(
            scheduler=sched, app=web.Application(), bus=_make_bus())
        reg.register_agent(
            "assistant", [_trigger_cron(schedule="0 9 * * 0")],
            channels=["telegram"],
        )
        kwargs = sched.add_job.call_args.kwargs
        assert kwargs["day_of_week"] == "sun"

    def test_translated_sunday_fires_on_sunday_real_apscheduler(self):
        """Empirical regression pin against the INSTALLED APScheduler:
        cron `0 9 * * 0` must next fire on a Sunday, not a Monday."""
        from datetime import datetime, timezone

        from apscheduler.triggers.cron import CronTrigger

        from trigger_registry import _translate_cron_dow

        trig = CronTrigger(
            minute="0", hour="9", day="*", month="*",
            day_of_week=_translate_cron_dow("0"), timezone=timezone.utc,
        )
        # 2026-08-03 is a Monday.
        start = datetime(2026, 8, 3, tzinfo=timezone.utc)
        nxt = trig.get_next_fire_time(None, start)
        assert nxt.weekday() == 6, f"expected Sunday, got {nxt:%A}"


# ---------------------------------------------------------------------------
# TestWebhook
# ---------------------------------------------------------------------------


class TestWebhook:
    async def test_webhook_registers_no_per_path_route(self):
        """Release A: webhook triggers are served ONLY by the authenticated
        wildcard /webhook/{name} handler. Registration adds the name to the
        allowlist but registers NO per-path aiohttp route (the old unauth'd
        per-path route — and, for v2 path="", an open POST / — is removed)."""
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            "assistant", [_trigger_webhook()], channels=["webhook"],
        )

        # No per-path route was registered (serving is wildcard-only).
        assert list(app.router.routes()) == []
        # But the name IS in the dispatch allowlist.
        assert reg.get_webhook_target("gh") == "assistant"

    async def test_v2_empty_path_registers_no_root_route(self):
        """A v2 webhook trigger (path="") must NOT register an open POST /."""
        from config import TriggerSpec
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)
        reg.register_agent(
            "assistant",
            [TriggerSpec(name="a", type="webhook"),
             TriggerSpec(name="b", type="webhook")],  # two empty-path v2 triggers
            channels=["webhook"],
        )
        assert list(app.router.routes()) == []
        assert reg.get_webhook_target("a") == "assistant"
        assert reg.get_webhook_target("b") == "assistant"

    async def test_get_clearance_returns_declared_and_defaults_public(self):
        """Release A: per-trigger clearance is stored and readable; unknown
        names default to public; reregister eviction clears it."""
        from config import TriggerSpec
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)
        reg.register_agent(
            "assistant",
            [TriggerSpec(name="vm", type="webhook", path="/webhook/vm",
                         clearance="family")],
            channels=["webhook"],
        )
        assert reg.get_clearance("vm") == "family"
        assert reg.get_clearance("never-registered") == "public"
        reg.reregister_for("assistant", [], channels=["webhook"])
        assert reg.get_clearance("vm") == "public"  # evicted → default

    # NOTE (Release A): the per-path route is removed, so path is no longer a
    # collision axis (duplicate-name rejection is covered by
    # TestCrossRoleWebhookNameCollision below), and the per-path handler's
    # context-safety test moved to test_webhook_handler (the wildcard handler).


class TestCrossRoleWebhookNameCollision:
    """L79/L28: two agents each declaring a webhook trigger with the same
    NAME but different PATHs must be rejected — otherwise the wildcard
    /webhook/{name} dispatch silently reroutes all traffic to whichever
    role registered last."""

    async def test_cross_role_webhook_name_collision_rejected(self):
        from trigger_registry import TriggerError, TriggerRegistry

        reg = TriggerRegistry(
            scheduler=_make_scheduler(), app=web.Application(), bus=_make_bus(),
        )
        reg.register_agent(
            role="assistant",
            triggers=[_trigger_webhook(name="doorbell", path="/hooks/assist-doorbell")],
            channels=["telegram", "webhook"],
        )
        with pytest.raises(TriggerError, match="doorbell"):
            reg.register_agent(
                role="security",
                triggers=[_trigger_webhook(name="doorbell", path="/hooks/sec-doorbell")],
                channels=["telegram", "webhook"],
            )
        # first owner keeps the wildcard dispatch target
        assert reg.get_webhook_target("doorbell") == "assistant"

    async def test_same_role_reregister_same_webhook_name_allowed(self):
        from trigger_registry import TriggerRegistry

        reg = TriggerRegistry(
            scheduler=_make_scheduler(), app=web.Application(), bus=_make_bus(),
        )
        reg.register_agent(
            "assistant",
            [_trigger_webhook(name="doorbell", path="/hooks/a1")],
            ["telegram", "webhook"],
        )
        reg.reregister_for(
            "assistant",
            [_trigger_webhook(name="doorbell", path="/hooks/a2")],
            ["telegram", "webhook"],
        )
        assert reg.get_webhook_target("doorbell") == "assistant"


class TestWebhookAllowlist:
    """N-1 + N-2 (v0.36.0). Webhook triggers are dispatched via the
    wildcard ``/webhook/{name}`` handler in casa_core; the per-trigger
    ``router.add_post`` call is best-effort because aiohttp freezes the
    router after startup. The registry exposes a name-to-role allowlist
    so the wildcard handler can 404 unknowns and dispatch knowns to the
    correct role.
    """

    async def test_get_webhook_target_returns_role_after_register(self):
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            "assistant", [_trigger_webhook(name="probe")],
            channels=["webhook"],
        )
        assert reg.get_webhook_target("probe") == "assistant"

    async def test_get_webhook_target_unknown_returns_none(self):
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        assert reg.get_webhook_target("never-registered") is None

    async def test_reregister_for_clears_then_repopulates(self):
        """Removing a webhook trigger via reregister_for makes the name
        invalid (returns None); adding a new one makes it valid."""
        from config import TriggerSpec
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            "assistant",
            [TriggerSpec(name="old", type="webhook", path="/webhook/old")],
            channels=["webhook"],
        )
        assert reg.get_webhook_target("old") == "assistant"

        reg.reregister_for(
            "assistant",
            [TriggerSpec(name="new", type="webhook", path="/webhook/new")],
            channels=["webhook"],
        )
        assert reg.get_webhook_target("old") is None
        assert reg.get_webhook_target("new") == "assistant"

    async def test_register_tolerates_frozen_router(self):
        """N-1 fix: post-boot ``register_agent`` for a webhook trigger
        must NOT raise even though aiohttp's router is frozen. The route
        add is best-effort; the wildcard handler dispatches via the
        allowlist."""
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        # Drive the app through AppRunner.setup() which is what production
        # does — that's the lifecycle step that freezes the router and is
        # what made post-boot register_agent raise pre-N-1.
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

            # No raise:
            reg.register_agent(
                "assistant", [_trigger_webhook(name="post-boot")],
                channels=["webhook"],
            )
            assert reg.get_webhook_target("post-boot") == "assistant"
        finally:
            await runner.cleanup()

    async def test_get_webhook_target_returns_specialist_role(self):
        """Webhook triggers can be registered to non-assistant roles
        (specialists, other residents). Allowlist preserves the role."""
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            "butler", [_trigger_webhook(name="b1", path="/webhook/b1")],
            channels=["webhook"],
        )
        assert reg.get_webhook_target("b1") == "butler"


# ---------------------------------------------------------------------------
# TestValidation
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_duplicate_trigger_name_same_agent_rejected(self):
        from trigger_registry import TriggerRegistry, TriggerError

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        t1 = _trigger_interval(name="dup")
        t2 = _trigger_interval(name="dup")
        with pytest.raises(TriggerError, match="duplicate"):
            reg.register_agent("assistant", [t1, t2],
                               channels=["telegram"])

    async def test_interval_channel_must_be_registered_on_agent(self):
        """Pins INV-TRIG-001. Red case demonstrated: neutering register_agent's scheduled-channel membership check fails this test."""
        from trigger_registry import TriggerRegistry, TriggerError

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        trig = _trigger_interval(channel="unknown_channel")
        with pytest.raises(TriggerError, match="channel"):
            reg.register_agent("assistant", [trig],
                               channels=["telegram"])
