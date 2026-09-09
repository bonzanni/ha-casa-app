"""Tests for the engagement observer — classifier + rate limit + silent."""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


class TestClassifier:
    @pytest.mark.parametrize("event,expected", [
        ("started", "peek"),
        ("user_turn", "silent"),
        ("tool_call", "silent"),
        ("progress", "peek"),
        ("warn", "trigger"),
        ("error", "trigger"),
        ("idle_detected", "trigger"),
    ])
    def test_default_classifier(self, event, expected):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._classify(event, {}) == expected

    def test_tool_result_ok_is_silent(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._classify("tool_result", {"status": "ok"}) == "silent"

    def test_tool_result_error_is_trigger(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._classify("tool_result", {"status": "error"}) == "trigger"

    def test_query_engager_unknown_is_trigger(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._classify("query_engager", {"status": "unknown"}) == "trigger"


class TestRateLimit:
    def test_third_interject_still_allowed(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._rate_limit_ok("e1") is True
        obs._count_interjection("e1")
        assert obs._rate_limit_ok("e1") is True
        obs._count_interjection("e1")
        obs._count_interjection("e1")
        assert obs._rate_limit_ok("e1") is False


class TestSilent:
    def test_silence_suppresses_triggers(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        obs.silence("e1")
        assert obs.is_silenced("e1") is True


class TestInterject:
    async def test_interject_posts_notification_when_llm_says_yes(self, monkeypatch):
        from observer import Observer

        bus = MagicMock(); bus.notify = AsyncMock()
        registry = MagicMock()
        registry.get.return_value = MagicMock(
            id="e1", task="Plan Q2", origin={"role": "assistant",
                                              "channel": "telegram",
                                              "chat_id": "c1"},
            role_or_type="finance",
        )
        obs = Observer(bus=bus, engagement_registry=registry, model_name="haiku")

        async def _fake_decide(event_type, payload, rec):
            return {"interject": True, "text": "You may want to check on this."}
        monkeypatch.setattr(obs, "_decide_interjection", _fake_decide)

        await obs._interject("e1", "error", {"kind": "sdk_error"})
        bus.notify.assert_awaited_once()
        notif = bus.notify.await_args.args[0]
        assert notif.target == "assistant"
        assert "You may want to check on this" in getattr(notif.content, "suggested_text", "") or \
            "You may want to check on this" in str(notif.content)

    async def test_interject_skipped_when_llm_says_no(self, monkeypatch):
        from observer import Observer

        bus = MagicMock(); bus.notify = AsyncMock()
        registry = MagicMock()
        registry.get.return_value = MagicMock(
            id="e1", task="t", origin={"role": "assistant", "channel": "telegram",
                                       "chat_id": "c1"},
            role_or_type="finance",
        )
        obs = Observer(bus=bus, engagement_registry=registry, model_name="haiku")
        monkeypatch.setattr(obs, "_decide_interjection",
                            AsyncMock(return_value={"interject": False, "text": ""}))
        await obs._interject("e1", "warn", {})
        bus.notify.assert_not_called()


class TestObserverSdkOptions:
    async def test_decider_uses_verified_cli_path(self, monkeypatch):
        import sdk_logging
        from claude_runtime import CLAUDE_CLI_PATH
        from observer import Observer

        captured = {}
        fake_sdk = types.ModuleType("claude_agent_sdk")

        class ClaudeAgentOptions:  # noqa: N801 — mirrors SDK name
            def __init__(self, **kwargs):
                captured["options"] = self
                for key, value in kwargs.items():
                    setattr(self, key, value)

        class TextBlock:  # noqa: N801 — mirrors SDK name
            def __init__(self, text):
                self.text = text

        class AssistantMessage:  # noqa: N801 — mirrors SDK name
            def __init__(self, text):
                self.content = [TextBlock(text)]

        class ClaudeSDKClient:  # noqa: N801 — mirrors SDK name
            def __init__(self, options):
                captured["client_options"] = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def query(self, prompt):
                captured["prompt"] = prompt

            async def receive_response(self):
                yield AssistantMessage('{"interject": false, "text": ""}')

        fake_sdk.ClaudeAgentOptions = ClaudeAgentOptions
        fake_sdk.TextBlock = TextBlock
        fake_sdk.AssistantMessage = AssistantMessage
        fake_sdk.ClaudeSDKClient = ClaudeSDKClient
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
        monkeypatch.setattr(
            sdk_logging,
            "with_stderr_callback",
            lambda options, engagement_id=None: options,
        )

        observer = Observer(
            bus=MagicMock(),
            engagement_registry=MagicMock(),
            model_name="haiku",
        )
        record = MagicMock(
            id="engagement-1",
            task="check the kitchen",
            role_or_type="configurator",
        )

        result = await observer._decide_interjection("warn", {}, record)

        assert result == {"interject": False, "text": ""}
        assert getattr(captured["options"], "cli_path", None) == CLAUDE_CLI_PATH
        assert captured["client_options"] is captured["options"]


class TestBudgetCountsOnlyPostedInterjections:
    """L68/L17: declined-trigger evaluations must not consume the
    per-engagement interjection budget — otherwise 3 declined evaluations
    silence every later genuine alert."""

    async def test_declined_triggers_do_not_consume_budget(self, monkeypatch):
        from observer import Observer

        bus = MagicMock(); bus.notify = AsyncMock()
        registry = MagicMock()
        registry.get.return_value = MagicMock(
            id="e1", task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "c1"},
            role_or_type="finance",
        )
        obs = Observer(bus=bus, engagement_registry=registry, model_name="haiku")

        # Three trigger events the LLM declines must not consume the budget.
        monkeypatch.setattr(obs, "_decide_interjection",
                            AsyncMock(return_value={"interject": False, "text": ""}))
        err = MagicMock()
        err.content = {"event": "tool_result", "status": "error", "engagement_id": "e1"}
        for _ in range(3):
            await obs._handle_event(err)
        assert obs._rate_limit_ok("e1") is True   # FAILS on current code (count==3)

        # A later genuine idle event must still produce a notification.
        monkeypatch.setattr(obs, "_decide_interjection",
                            AsyncMock(return_value={"interject": True, "text": "Engagement stuck."}))
        idle = MagicMock()
        idle.content = {"event": "idle_detected", "engagement_id": "e1"}
        await obs._handle_event(idle)
        bus.notify.assert_awaited_once()          # FAILS on current code (dropped at rate limit)
        assert obs._interjection_counts.get("e1", 0) == 1  # only the posted one counted

    async def test_forget_clears_counts_and_silence(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        obs._count_interjection("e1")
        obs.silence("e1")
        assert obs._interjection_counts.get("e1") == 1
        assert obs.is_silenced("e1") is True

        obs.forget("e1")
        assert "e1" not in obs._interjection_counts
        assert obs.is_silenced("e1") is False


class TestConcurrentInterjectionCap:
    """#353: the bus dispatches each event as an independent task, so several
    trigger events can be in flight at once. The 3-interjection cap must hold
    under that concurrency: the budget slot is reserved BEFORE the LLM await,
    and released only when nothing was posted."""

    async def test_concurrent_triggers_cannot_exceed_cap(self, monkeypatch):
        import asyncio

        from observer import Observer

        bus = MagicMock(); bus.notify = AsyncMock()
        registry = MagicMock()
        registry.get.return_value = MagicMock(
            id="e1", task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "c1"},
            role_or_type="finance",
        )
        obs = Observer(bus=bus, engagement_registry=registry, model_name="haiku")

        release = asyncio.Event()

        async def _gated_decide(event_type, payload, rec):
            await release.wait()
            return {"interject": True, "text": "alert"}

        monkeypatch.setattr(obs, "_decide_interjection", _gated_decide)

        events = []
        for _ in range(5):
            m = MagicMock()
            m.content = {"event": "warn", "engagement_id": "e1"}
            events.append(m)
        tasks = [asyncio.ensure_future(obs._handle_event(m)) for m in events]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)

        assert bus.notify.await_count == 3, (
            "concurrent trigger events must never exceed the 3-interjection cap"
        )
        assert obs._interjection_counts.get("e1") == 3

    async def test_declined_concurrent_evaluation_releases_slot(self, monkeypatch):
        """A reserved slot whose evaluation declines must be handed back so a
        later genuine alert still fits in the budget (L68/L17 preserved)."""
        import asyncio

        from observer import Observer

        bus = MagicMock(); bus.notify = AsyncMock()
        registry = MagicMock()
        registry.get.return_value = MagicMock(
            id="e1", task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "c1"},
            role_or_type="finance",
        )
        obs = Observer(bus=bus, engagement_registry=registry, model_name="haiku")

        monkeypatch.setattr(
            obs, "_decide_interjection",
            AsyncMock(return_value={"interject": False, "text": ""}),
        )
        m = MagicMock()
        m.content = {"event": "warn", "engagement_id": "e1"}
        for _ in range(3):
            await obs._handle_event(m)
        # Declines must not have burned the budget.
        monkeypatch.setattr(
            obs, "_decide_interjection",
            AsyncMock(return_value={"interject": True, "text": "real alert"}),
        )
        await obs._handle_event(m)
        bus.notify.assert_awaited_once()
        assert obs._interjection_counts.get("e1", 0) == 1

    async def test_cancelled_evaluation_never_refunds_the_slot(self, monkeypatch):
        """An exception — cancellation included — is an ambiguous outcome: the
        notification may already be enqueued, so the reservation must NOT be
        refunded (the cap is the hard contract; budget loss is the safe
        direction)."""
        import asyncio

        from observer import Observer

        bus = MagicMock(); bus.notify = AsyncMock()
        registry = MagicMock()
        registry.get.return_value = MagicMock(
            id="e1", task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "c1"},
            role_or_type="finance",
        )
        obs = Observer(bus=bus, engagement_registry=registry, model_name="haiku")

        entered = asyncio.Event()

        async def _hanging_decide(event_type, payload, rec):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(obs, "_decide_interjection", _hanging_decide)
        m = MagicMock()
        m.content = {"event": "warn", "engagement_id": "e1"}
        task = asyncio.ensure_future(obs._handle_event(m))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert obs._interjection_counts.get("e1") == 1, (
            "an ambiguous (cancelled) evaluation must keep its reservation"
        )
