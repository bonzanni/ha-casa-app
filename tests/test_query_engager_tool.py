"""Tests for query_engager — retrieval + bounded LLM synthesis."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _remembers(tmp_path, monkeypatch, *texts):
    """A real memory store holding *texts*, wired as the process's store."""
    from memory import capture
    from memory_tools_support import new_store, store_event
    store = new_store(tmp_path / "mem")
    for text in texts:
        store_event(store, text)
    monkeypatch.setitem(capture._wiring, "store", store)
    return store


class TestQueryEngager:
    async def test_returns_ok_when_memory_has_context(self, tmp_path, monkeypatch):
        from engagement_registry import EngagementRegistry
        from tools import query_engager, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram",
                              "chat_id": "c1"},
            topic_id=42,
        )
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=MagicMock(), bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        _remembers(tmp_path, monkeypatch, "Lantern paid in March.")

        # Monkey-patch the constrained LLM helper
        async def _fake_synth(question, context, max_tokens):
            return "Yes, Lantern paid in March."
        monkeypatch.setattr("tools._synthesize_answer", _fake_synth)

        token = engagement_var.set(rec)
        try:
            res = await query_engager.handler({"question": "Did Lantern pay?",
                                                "max_tokens": 200})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "ok"
        assert "Lantern" in payload["text"]

    async def test_returns_unknown_when_synth_returns_unknown(self, tmp_path, monkeypatch):
        from engagement_registry import EngagementRegistry
        from tools import query_engager, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram",
                              "chat_id": "c1"},
            topic_id=42,
        )
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=MagicMock(), bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        # Nothing on the question in memory → unknown.
        _remembers(tmp_path, monkeypatch, "the bins go out on tuesday")
        monkeypatch.setattr("tools._synthesize_answer", AsyncMock(return_value="UNKNOWN"))

        token = engagement_var.set(rec)
        try:
            res = await query_engager.handler({"question": "x"})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "unknown"

    async def test_returns_not_in_engagement_outside(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import query_engager, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        res = await query_engager.handler({"question": "x"})
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "not_in_engagement"


class TestSynthesizeAnswerMaxTokens:
    """L25: _synthesize_answer must actually honor max_tokens instead of
    silently ignoring it."""

    async def test_synthesize_answer_propagates_max_tokens(self, monkeypatch):
        import tools

        captured = {}

        class FakeClient:
            def __init__(self, options):
                captured["options"] = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def query(self, prompt):
                captured["prompt"] = prompt

            async def receive_response(self):
                if False:
                    yield None  # empty async generator

        monkeypatch.setattr(tools, "ClaudeSDKClient", FakeClient)
        monkeypatch.setattr(
            tools.sdk_logging, "with_stderr_callback",
            lambda options, engagement_id=None: options,
        )
        await tools._synthesize_answer("q?", "some ctx", max_tokens=123)
        assert captured["options"].env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "123"
        assert "123" in captured["prompt"]  # prompt carries the budget instruction

    async def test_synthesize_answer_uses_verified_cli_path(self, monkeypatch):
        import tools
        from claude_runtime import CLAUDE_CLI_PATH

        captured = {}

        class FakeClient:
            def __init__(self, options):
                captured["options"] = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def query(self, prompt):
                pass

            async def receive_response(self):
                if False:
                    yield None

        monkeypatch.setattr(tools, "ClaudeSDKClient", FakeClient)
        monkeypatch.setattr(
            tools.sdk_logging,
            "with_stderr_callback",
            lambda options, engagement_id=None: options,
        )

        await tools._synthesize_answer("q?", "some ctx", max_tokens=123)

        assert captured["options"].cli_path == CLAUDE_CLI_PATH

    async def test_synthesize_answer_hard_truncates_overshoot(self, monkeypatch):
        import tools

        class FakeClient:
            def __init__(self, options):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def query(self, prompt):
                pass

            async def receive_response(self):
                from claude_agent_sdk import AssistantMessage, TextBlock
                yield AssistantMessage(
                    content=[TextBlock(text="x" * 1000)], model="haiku",
                )

        monkeypatch.setattr(tools, "ClaudeSDKClient", FakeClient)
        monkeypatch.setattr(
            tools.sdk_logging, "with_stderr_callback",
            lambda options, engagement_id=None: options,
        )
        out = await tools._synthesize_answer("q?", "ctx", max_tokens=10)
        # estimate_tokens ~= len(text)//4, so the hard cap truncates to ~40 chars.
        assert len(out) <= 40

    async def test_query_engager_clamps_max_tokens_arg(self, tmp_path, monkeypatch):
        """A hostile/buggy caller passing 0, negative, or huge max_tokens
        must be clamped to a sane [1, 4000] range before reaching
        _synthesize_answer."""
        from engagement_registry import EngagementRegistry
        from tools import query_engager, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram",
                              "chat_id": "c1"},
            topic_id=42,
        )
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=MagicMock(), bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        _remembers(tmp_path, monkeypatch, "some context on q")

        captured = {}

        async def _fake_synth(question, context, max_tokens):
            captured["max_tokens"] = max_tokens
            return "answer"
        monkeypatch.setattr("tools._synthesize_answer", _fake_synth)

        token = engagement_var.set(rec)
        try:
            # 0 is falsy, so `args.get(...) or 500` falls back to the
            # default 500 (matches the existing default-substitution
            # convention used throughout this tool's arg parsing).
            await query_engager.handler({"question": "q", "max_tokens": 0})
            assert captured["max_tokens"] == 500
            await query_engager.handler({"question": "q", "max_tokens": -5})
            assert captured["max_tokens"] == 1
            await query_engager.handler({"question": "q", "max_tokens": 999999})
            assert captured["max_tokens"] == 4000
        finally:
            engagement_var.reset(token)
