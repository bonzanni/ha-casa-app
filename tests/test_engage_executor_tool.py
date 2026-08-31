"""Tests for engage_executor tool (Plan 3 real implementation)."""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _bump_generation():
    """Simulate a concurrent reload_snapshot: republish the current frozen
    snapshot with generation+1 (the module-global _generation is gone —
    v0.74.0 D2 publishes generation inside the one frozen snapshot)."""
    import dataclasses
    import plugin_registry
    snap = plugin_registry._current()
    plugin_registry._snapshot = dataclasses.replace(
        snap, generation=snap.generation + 1)


def _mock_executor_def(**overrides):
    from config import ExecutorDefinition
    defaults = {
        "type": "configurator",
        "description": "Test configurator type for engage_executor tests.",
        "model": "claude-sonnet-4-6",
        "driver": "in_casa",
        "enabled": True,
        "tools_allowed": ["Read"],
        "tools_disallowed": [],
        "permission_mode": "acceptEdits",
        "mcp_server_names": ["casa-framework"],
        "idle_reminder_days": 7,
        "prompt_template_path": "/tmp/nonexistent.md",
        "hooks_path": None,
        "observer_policy_path": None,
        "doctrine_dir": "",   # v0.74.2: non-empty + missing now fails closed
    }
    defaults.update(overrides)
    return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT, **defaults)


async def _setup(
    executor_registry,
    channel_ok=True,
    prompt_template="You are {task}. Context: {context}. State: {world_state_summary}",
    tmp_path=None,
):
    from tools import init_tools
    if tmp_path is not None and executor_registry is not None:
        defn = executor_registry.get("configurator")
        if defn is not None:
            p = tmp_path / "prompt.md"
            p.write_text(prompt_template)
            defn.prompt_template_path = str(p)

    channel = MagicMock()
    channel.engagement_supergroup_id = -100123 if channel_ok else 0
    channel.engagement_permission_ok = channel_ok
    channel.open_engagement_topic = AsyncMock(return_value=42)
    channel.bot = MagicMock()
    channel.bot.edit_forum_topic = AsyncMock()
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)

    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        executor_registry=executor_registry,
    )
    return channel


class TestEngageExecutorReal:
    async def test_no_executor_types_when_registry_empty(self):
        from tools import engage_executor
        import agent as agent_mod

        reg = MagicMock()
        reg.get = MagicMock(return_value=None)
        reg.list_types = MagicMock(return_value=[])
        await _setup(reg)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "no_executor_types"

    async def test_unknown_type_error(self):
        from tools import engage_executor
        import agent as agent_mod

        reg = MagicMock()
        reg.get = MagicMock(return_value=None)
        reg.list_types = MagicMock(return_value=["other_type"])
        await _setup(reg)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "unknown_executor_type"
        # F-7 (v0.32.0): registry-rejected calls (e.g. disabled executor
        # types) must surface as MCP is_error so sdk_logging emits ok=False
        # — the tool didn't actually spawn an engagement, even though
        # Ellen's user-facing narration is graceful. Key is snake_case
        # because claude_agent_sdk reads ``result.get("is_error", False)``
        # at the MCP-server boundary.
        assert r.get("is_error") is True, (
            f"engage_executor must set is_error=True for unknown/disabled "
            f"executor types. envelope keys: {sorted(r.keys())}"
        )

    async def test_disabled_executor_type_returns_is_error(self):
        """F-7 (v0.32.0): the registry strips disabled executor entries
        from ``_defs``, so ``get(disabled_type)`` returns None and falls
        through the same ``unknown_executor_type`` path as truly-unknown
        names. The contract under test is the MCP envelope-level
        ``isError`` flag, exercised here through the disabled-executor
        live shape (P5 cid ``20a903c3`` from the 2026-05-02 exploration:
        plugin-developer was bundled but disabled, ``ok=True ms=7284``
        in the tool_result log even though no engagement spawned).
        """
        from tools import engage_executor
        import agent as agent_mod

        reg = MagicMock()
        # Real ExecutorRegistry behavior: disabled types are excluded
        # from _defs, so .get() returns None and .list_types() shows only
        # the enabled set (which may include other types).
        reg.get = MagicMock(return_value=None)
        reg.list_types = MagicMock(return_value=["configurator"])
        await _setup(reg)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "plugin-developer",
                "task": "build a thing", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown_executor_type"
        assert r.get("is_error") is True

    async def test_engagement_not_configured(self):
        from tools import engage_executor
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        await _setup(reg, channel_ok=False)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "engagement_not_configured"

    async def test_non_telegram_origin_gets_accurate_error(self):
        """R-2 (v0.69.7): when the engagement machinery is unavailable for a
        non-Telegram origin, the error must accurately say engagements
        originate from Telegram — NOT the misleading 'set
        telegram_engagement_supergroup_id' message (which telegram-origin
        callers still get, see test_engagement_not_configured)."""
        from tools import engage_executor
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        await _setup(reg, channel_ok=False)  # supergroup unavailable on this origin

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "voice",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "engagement_wrong_origin"
        assert "Telegram" in payload["message"]
        assert "supergroup" not in payload["message"].lower()

    async def test_ef_inline_retry_recovers_from_first_boot_race(
        self, tmp_path, monkeypatch,
    ):
        """E-F (v0.30.0) defensive: when supergroup IS configured but
        engagement_permission_ok is still False (first-boot race lost
        before _rebuild's tail setup ran), engage_executor must call
        setup_engagement_features() once in-line. If that retry flips
        the flag, the engagement proceeds normally — no manual restart
        required.
        """
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        # Channel with supergroup CONFIGURED but permission flag stuck
        # False. The _setup() helper supports `channel_ok=True` (both
        # set) and `channel_ok=False` (both cleared); we need the third
        # state, so build the channel by hand.
        channel = MagicMock()
        channel.engagement_supergroup_id = -100123
        channel.engagement_permission_ok = False
        channel.open_engagement_topic = AsyncMock(return_value=42)
        channel.bot = MagicMock()
        channel.bot.edit_forum_topic = AsyncMock()

        async def _flip_flag():
            channel.engagement_permission_ok = True

        channel.setup_engagement_features = AsyncMock(side_effect=_flip_flag)

        # Wire the prompt template so build_prompt does not blow up.
        p = tmp_path / "prompt.md"
        p.write_text("You are {task}. Context: {context}.")
        defn.prompt_template_path = str(p)

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)

        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver",
            MagicMock(start=AsyncMock()), raising=False,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        # In-line retry was attempted — exactly once.
        assert channel.setup_engagement_features.await_count == 1
        # Retry succeeded (flag flipped) → engagement opened normally.
        assert payload["status"] == "pending", payload
        assert payload["topic_id"] == 42

    async def test_ef_inline_retry_does_not_fire_when_supergroup_unset(self):
        """E-F retry is gated on supergroup-configured-but-flag-False.
        When supergroup is unset, no retry should fire — the operator
        hasn't opted into engagements at all.
        """
        from tools import engage_executor
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        # Build a channel with supergroup explicitly UNSET. Don't use
        # _setup(channel_ok=False) because that doesn't expose the
        # AsyncMock we want to assert on.
        channel = MagicMock()
        channel.engagement_supergroup_id = 0   # unset
        channel.engagement_permission_ok = False
        channel.setup_engagement_features = AsyncMock()
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        from tools import init_tools
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
            executor_registry=reg,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "engagement_not_configured"
        # No retry attempted — supergroup is unset, no point.
        assert channel.setup_engagement_features.await_count == 0

    async def test_happy_path_returns_pending(self, tmp_path, monkeypatch):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)

        channel = await _setup(reg, tmp_path=tmp_path)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )

        monkeypatch.setattr(agent_mod, "active_engagement_driver",
                            MagicMock(start=AsyncMock()), raising=False)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "make a thing",
                "context": "none",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["executor_type"] == "configurator"
        assert payload["topic_id"] == 42

    async def test_in_casa_factory_sees_final_interactive_grants(
        self, tmp_path, monkeypatch,
    ):
        from mcp_registry import McpServerRegistry
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def(tools_allowed=[
            "Read",
            "mcp__casa-framework__config_git_commit",
        ])
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        er = MagicMock()
        rec = MagicMock(id="abcd1234" + "0" * 24, topic_id=42)
        er.create = AsyncMock(return_value=rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)
        er.set_channel_state = AsyncMock()
        er.set_initial_state_emoji = AsyncMock()
        channel = await _setup(reg, tmp_path=tmp_path)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)

        mcp = McpServerRegistry()
        mcp.register_sdk_factory(
            "casa-framework",
            lambda role, grants: {
                "type": "sdk",
                "instance": object(),
                "resolved_role": role,
                "resolved_grants": grants,
            },
        )
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=mcp,
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        driver = MagicMock(start=AsyncMock())
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver", driver, raising=False,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            result = await engage_executor.handler({
                "executor_type": "configurator",
                "task": "make a thing",
                "context": "none",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "pending"
        options = driver.start.await_args.kwargs["options"]
        server = options.mcp_servers["casa-framework"]
        assert server["resolved_role"] == "configurator"
        assert server["resolved_grants"] == frozenset({
            "Read",
            "mcp__casa-framework__config_git_commit",
            "mcp__casa-framework__query_engager",
            "mcp__casa-framework__emit_completion",
        })

    async def test_requires_origin(self):
        from tools import engage_executor, init_tools
        reg = MagicMock()
        reg.list_types = MagicMock(return_value=[])
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
            executor_registry=reg,
        )
        r = await engage_executor.handler({"executor_type": "configurator", "task": "t"})
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "no_origin"

    async def test_does_not_leak_engagement_var_to_caller(
        self, tmp_path, monkeypatch,
    ):
        """engage_executor must not bind engagement_var in the engager's scope.

        The tool dispatches to driver.start, which (post-Phase-1) sets
        engagement_var only inside _deliver_turn. The engager's task must
        observe engagement_var == None both before and after the call.
        """
        from tools import engage_executor, engagement_var, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)

        channel = await _setup(reg, tmp_path=tmp_path)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver",
            MagicMock(start=AsyncMock()), raising=False,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            assert engagement_var.get(None) is None  # pre-state
            r = await engage_executor.handler({
                "executor_type": "configurator",
                "task": "t",
                "context": "",
            })
            assert engagement_var.get(None) is None  # post-state
        finally:
            agent_mod.origin_var.reset(token)

        # Sanity: handler still completed with the expected envelope shape
        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending"


class TestDuplicateTaskGuard:
    """P32 (v0.37.10): tool-level guard against engage_executor
    cumulative-context bleed. When a back-to-back assistant turn fires
    a duplicate engage_executor call (re-emitting the prior turn's
    task), the second call must be refused with kind=duplicate_task.

    Live evidence: docs/bug-review-2026-05-14-exploration6.md::O-6 —
    Ellen's O-6.2 turn fired two engage_executor calls in a single
    assistant message; the first carried the O-6.1 rename task.

    The guard uses Jaccard word similarity against the most-recent
    engagement for the same (channel, chat_id) within a 60s window.
    Computed in tools.py over the real engagement_registry, so this
    test uses a real registry (not MagicMock).
    """

    async def _real_registry(self, tmp_path):
        from engagement_registry import EngagementRegistry
        return EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"), bus=None,
        )

    async def _setup_with_real_registry(
        self, tmp_path, monkeypatch, *, prompt="t",
    ):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        registry = await self._real_registry(tmp_path)

        defn = _mock_executor_def()
        # Real prompt file so build path doesn't crash.
        p = tmp_path / "prompt.md"
        p.write_text(prompt)
        defn.prompt_template_path = str(p)

        exec_reg = MagicMock()
        exec_reg.get = MagicMock(return_value=defn)
        exec_reg.list_types = MagicMock(return_value=["configurator"])

        channel = MagicMock()
        channel.engagement_supergroup_id = -100123
        channel.engagement_permission_ok = True
        channel.open_engagement_topic = AsyncMock(return_value=42)
        channel.bot = MagicMock()
        channel.bot.edit_forum_topic = AsyncMock()
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)

        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=registry,
            executor_registry=exec_reg,
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver",
            MagicMock(start=AsyncMock()), raising=False,
        )
        return engage_executor, registry, channel

    async def test_distinct_tasks_both_succeed(self, tmp_path, monkeypatch):
        """Sanity: two engage_executor calls with non-overlapping tasks
        in the same channel/session must both succeed."""
        import agent as agent_mod
        engage_executor, registry, _ = await self._setup_with_real_registry(
            tmp_path, monkeypatch,
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r1 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": "rename the agent name from its current value to Ellen-A and back",
                "context": "",
            })
            r2 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": "build a brand new repo for the casa probe artifact bundle",
                "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        p1 = json.loads(r1["content"][0]["text"])
        p2 = json.loads(r2["content"][0]["text"])
        assert p1["status"] == "pending"
        assert p2["status"] == "pending"
        # Both engagements landed in the registry.
        assert len(registry._records) == 2

    async def test_duplicate_task_blocked(self, tmp_path, monkeypatch):
        """The load-bearing case: a back-to-back duplicate task is
        refused with kind=duplicate_task. Two configurator spawns with
        near-identical task text must result in exactly one engagement
        in the registry; the second returns is_error=True."""
        import agent as agent_mod
        engage_executor, registry, _ = await self._setup_with_real_registry(
            tmp_path, monkeypatch,
        )
        task1 = (
            "rename the agent name from its current value to Ellen-A and "
            "then back to the default"
        )
        # Near-duplicate (identical lead, slight phrasing variation) —
        # the bleed pattern observed live in exploration6.
        task2 = (
            "rename the agent name from its current value to Ellen-A and "
            "then back to the default value"
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r1 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": task1, "context": "",
            })
            r2 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": task2, "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        p1 = json.loads(r1["content"][0]["text"])
        p2 = json.loads(r2["content"][0]["text"])
        assert p1["status"] == "pending"
        assert p2["status"] == "error", (
            f"second engage_executor with duplicate task must be refused, "
            f"got {p2!r}"
        )
        assert p2["kind"] == "duplicate_task"
        # MCP envelope must carry is_error=True so sdk_logging emits ok=False.
        assert r2.get("is_error") is True
        # Exactly one engagement landed.
        assert len(registry._records) == 1

    async def test_concurrent_duplicates_one_wins(self, tmp_path, monkeypatch):
        """#320: the fast-path duplicate check is an unlocked read followed
        by awaits (topic creation) before create(). Two CONCURRENT identical
        calls — a single model turn can legally emit parallel tool calls —
        both pass the empty-history check; the re-check inside the creation
        critical section must refuse the loser with kind=duplicate_task."""
        import asyncio
        import agent as agent_mod
        import tools as tools_mod
        engage_executor, registry, channel = (
            await self._setup_with_real_registry(tmp_path, monkeypatch)
        )
        # Fresh lock: contended acquire loop-binds an asyncio.Lock, and the
        # module global must not stay bound to this test's loop.
        monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
        # Barrier inside open_engagement_topic: both calls must have passed
        # the fast-path duplicate check (which precedes topic creation)
        # before either proceeds toward create().
        arrived = 0
        gate = asyncio.Event()

        async def _open_topic(*, name, role):
            nonlocal arrived
            arrived += 1
            if arrived >= 2:
                gate.set()
            await gate.wait()
            return 40 + arrived

        channel.open_engagement_topic = AsyncMock(side_effect=_open_topic)

        task = "rotate the credentials for the household media services"

        async def _call():
            token = agent_mod.origin_var.set({
                "role": "assistant", "channel": "telegram",
                "chat_id": "c1", "cid": "x", "user_text": "hi",
            })
            try:
                return await engage_executor.handler({
                    "executor_type": "configurator",
                    "task": task, "context": "",
                })
            finally:
                agent_mod.origin_var.reset(token)

        r1, r2 = await asyncio.gather(_call(), _call())
        payloads = [
            json.loads(r["content"][0]["text"]) for r in (r1, r2)
        ]
        statuses = sorted(p["status"] for p in payloads)
        assert statuses == ["error", "pending"], (
            f"exactly one of two concurrent duplicates must win, "
            f"got {payloads!r}"
        )
        loser = next(p for p in payloads if p["status"] == "error")
        assert loser["kind"] == "duplicate_task"
        # Exactly one engagement landed — no twin executors doing the
        # same mutating work in two topics.
        assert len(registry._records) == 1

    async def test_other_channel_does_not_block(self, tmp_path, monkeypatch):
        """A duplicate task in a DIFFERENT channel must not block the
        new spawn. Cross-channel isolation."""
        import agent as agent_mod
        engage_executor, registry, _ = await self._setup_with_real_registry(
            tmp_path, monkeypatch,
        )
        task = (
            "rename the agent name from its current value to Ellen-A and "
            "then back to the default"
        )
        token1 = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r1 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": task, "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token1)

        # Different channel — guard must not block.
        token2 = agent_mod.origin_var.set({
            "role": "assistant", "channel": "discord",
            "chat_id": "c1", "cid": "y", "user_text": "hi",
        })
        try:
            r2 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": task, "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token2)

        p1 = json.loads(r1["content"][0]["text"])
        p2 = json.loads(r2["content"][0]["text"])
        assert p1["status"] == "pending"
        assert p2["status"] == "pending"
        assert len(registry._records) == 2


class TestCancelledLaunchAbortsTopic:
    """#363: the launch path opens the Telegram topic FIRST; a cancellation
    in the window before create() (lock acquire, plugin resolve) used to
    unwind the tool call through no handler at all — `_abort_engagement_topic`
    was reached only via ``except Exception``, which cancellation bypasses —
    leaving an open topic with no engagement record behind it."""

    async def test_cancel_while_waiting_for_plugin_lock_aborts_topic(
            self, tmp_path, monkeypatch):
        import asyncio
        import agent as agent_mod
        import tools as tools_mod
        from tools import engage_executor, init_tools

        defn = _mock_executor_def()
        p = tmp_path / "prompt.md"
        p.write_text("t")
        defn.prompt_template_path = str(p)
        exec_reg = MagicMock()
        exec_reg.get = MagicMock(return_value=defn)
        exec_reg.list_types = MagicMock(return_value=["configurator"])

        channel = MagicMock()
        channel.engagement_supergroup_id = -100123
        channel.engagement_permission_ok = True
        opened = asyncio.Event()

        async def _open_topic(*, name, role):
            opened.set()
            return 77

        channel.open_engagement_topic = AsyncMock(side_effect=_open_topic)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
            executor_registry=exec_reg,
        )
        abort = AsyncMock()
        monkeypatch.setattr(tools_mod, "_abort_engagement_topic", abort)
        # Fresh lock: contended acquire loop-binds an asyncio.Lock, and the
        # module global must not stay bound to this test's loop.
        monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())

        async def _call():
            token = agent_mod.origin_var.set({
                "role": "assistant", "channel": "telegram",
                "chat_id": "c1", "cid": "x", "user_text": "hi",
            })
            try:
                return await engage_executor.handler({
                    "executor_type": "configurator",
                    "task": "do the thing", "context": "",
                })
            finally:
                agent_mod.origin_var.reset(token)

        # Hold the plugin-tools lock so the handler parks in the
        # topic-created-but-no-record window, then cancel it there.
        await tools_mod._PLUGIN_TOOLS_LOCK.acquire()
        try:
            task = asyncio.ensure_future(_call())
            await asyncio.wait_for(opened.wait(), timeout=5)
            for _ in range(10):  # let it advance to the lock acquire
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            tools_mod._PLUGIN_TOOLS_LOCK.release()

        # The abort is fire-and-forget (a cancelled task cannot await
        # network RTs) — drain the strong-ref'd background tasks.
        pending = list(tools_mod._ABORT_BG_TASKS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        abort.assert_awaited_once()
        assert abort.await_args.args[2] == 77  # the just-created topic


class TestCancelledLaunchAfterRecordCompensates:
    """Sol r1 (#363 family): a cancellation AFTER create() but BEFORE the
    driver started used to leave a durably-active record and an open topic
    with nothing driving them — the record must be marked errored and the
    topic aborted, in the background."""

    async def test_cancel_during_driver_start_marks_error_and_aborts_topic(
            self, tmp_path, monkeypatch):
        import asyncio
        import agent as agent_mod
        import tools as tools_mod
        from tools import engage_executor, init_tools
        from engagement_registry import EngagementRegistry

        registry = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"), bus=None)
        defn = _mock_executor_def()
        p = tmp_path / "prompt.md"
        p.write_text("t")
        defn.prompt_template_path = str(p)
        exec_reg = MagicMock()
        exec_reg.get = MagicMock(return_value=defn)
        exec_reg.list_types = MagicMock(return_value=["configurator"])

        channel = MagicMock()
        channel.engagement_supergroup_id = -100123
        channel.engagement_permission_ok = True
        channel.open_engagement_topic = AsyncMock(return_value=88)
        channel.bot = MagicMock()
        channel.bot.edit_forum_topic = AsyncMock()
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=registry,
            executor_registry=exec_reg,
        )
        abort = AsyncMock()
        monkeypatch.setattr(tools_mod, "_abort_engagement_topic", abort)

        started = asyncio.Event()

        async def _hanging_start(rec, *, prompt, options,
                                 expected_generation=None):
            started.set()
            await asyncio.Event().wait()

        fake_driver = MagicMock(
            start=AsyncMock(side_effect=_hanging_start),
            cancel=AsyncMock(),
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver", fake_driver,
            raising=False,
        )

        async def _call():
            token = agent_mod.origin_var.set({
                "role": "assistant", "channel": "telegram",
                "chat_id": "c1", "cid": "x", "user_text": "hi",
            })
            try:
                return await engage_executor.handler({
                    "executor_type": "configurator",
                    "task": "do the thing", "context": "",
                })
            finally:
                agent_mod.origin_var.reset(token)

        task = asyncio.ensure_future(_call())
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        pending = list(tools_mod._ABORT_BG_TASKS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # The record is terminally errored — not left active with no driver.
        recs = list(registry._records.values())
        assert len(recs) == 1
        assert recs[0].status == "error"
        abort.assert_awaited_once()
        assert abort.await_args.args[2] == 88
        # Terra/Sol r2: the driver may have gone LIVE before the cancellation
        # — the compensation runs its idempotent terminal teardown.
        fake_driver.cancel.assert_awaited_once()
        assert fake_driver.cancel.await_args.args[0].id == recs[0].id


class TestEngageExecutorClaudeCode:
    @pytest.mark.skip(reason="TODO(Phase G): Full wiring test — covered by D-block E2E")
    async def test_dispatches_to_claude_code_driver(self, monkeypatch, tmp_path):
        """When executor.driver == 'claude_code', engage_executor calls the
        claude_code driver with the ExecutorDefinition as options."""
        # See TestEngageExecutorConfigurator for the setup pattern. The real
        # coverage lands in the E2E D-block against the mock CLI.
        pass


class TestExecutorArchiveFetchedOncePerLaunch:
    """#583: a memory-enabled ``claude_code`` executor used to receive the
    prior-engagement archive TWICE, from two independent recalls that can
    return different snapshots — ``engage_executor``'s (interpolated into the
    initial FIFO prompt) and ``ClaudeCodeDriver.start``'s (written into the
    workspace ``CLAUDE.md``). The driver's is the copy that survives context
    compaction and is filtered at the record's own origin markers, so the
    engager's fetch is the one that goes.

    These assert the CALL COUNT, not the rendered status: the defect was two
    successful fetches, which no status distinguishes from one.
    """

    async def _launch(self, monkeypatch, tmp_path, *, driver_name):
        """Drive one engage_executor launch; return (fetch_calls, driver)."""
        from config import ExecutorMemoryConfig
        from tools import engage_executor, init_tools
        import agent as agent_mod
        import tools as tools_mod

        defn = _mock_executor_def(
            type="plugin-developer", driver=driver_name,
            memory=ExecutorMemoryConfig(enabled=True, token_budget=500),
        )
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["plugin-developer"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)
        er.set_channel_state = AsyncMock()
        er.set_initial_state_emoji = AsyncMock()
        er.set_procedural_epoch = AsyncMock()

        channel = await _setup(
            reg, tmp_path=tmp_path,
            prompt_template="task={task} mem={executor_memory}",
        )
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )

        calls: list = []

        async def _counting_fetch(**kwargs):
            calls.append(kwargs)
            return "ARCHIVE-BLOCK-MARKER"

        monkeypatch.setattr(
            tools_mod, "_fetch_executor_archive", _counting_fetch)

        driver = MagicMock(start=AsyncMock())
        monkeypatch.setattr(
            agent_mod, "active_claude_code_driver", driver, raising=False)
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver", driver, raising=False)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            await engage_executor.handler({
                "executor_type": "plugin-developer",
                "task": "Add a Skill for the casa-probe-foo plugin",
                "context": "none",
            })
        finally:
            agent_mod.origin_var.reset(token)
        return calls, driver

    async def test_a_claude_code_launch_does_not_fetch_the_archive(
        self, monkeypatch, tmp_path,
    ):
        """The engager performs ZERO archive recalls for claude_code — the
        driver's own fetch is the launch's single recall."""
        calls, driver = await self._launch(
            monkeypatch, tmp_path, driver_name="claude_code")
        assert calls == []
        driver.start.assert_awaited_once()
        prompt = driver.start.await_args.kwargs["prompt"]
        # The slot is still substituted — a literal placeholder must never
        # reach the FIFO.
        assert "{executor_memory}" not in prompt
        assert "ARCHIVE-BLOCK-MARKER" not in prompt

    async def test_an_in_casa_launch_still_fetches_the_archive_exactly_once(
        self, monkeypatch, tmp_path,
    ):
        """in_casa has no second fetch anywhere, so its ONE recall stays —
        this is the half the change must not take with it."""
        calls, driver = await self._launch(
            monkeypatch, tmp_path, driver_name="in_casa")
        assert len(calls) == 1
        driver.start.assert_awaited_once()
        assert "ARCHIVE-BLOCK-MARKER" in driver.start.await_args.kwargs["prompt"]


class TestU3TopicTitle:
    """E-12 (v0.37.0) Task 22: U3 state-encoded topic title at engagement open.

    Spec §6.3 (revised v0.37.1 D-1): ``<state-emoji> <concise task>`` — no
    engagement-id suffix. The role icon now lives on the bubble (numeric
    custom_emoji_id via channels.topic_icons), not in the title text.
    """

    async def test_engage_executor_opens_topic_with_state_encoded_title(
        self, tmp_path, monkeypatch,
    ):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def(type="plugin-developer")
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["plugin-developer"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)
        er.set_channel_state = AsyncMock()
        er.set_initial_state_emoji = AsyncMock()

        channel = await _setup(reg, tmp_path=tmp_path)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver",
            MagicMock(start=AsyncMock()), raising=False,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            await engage_executor.handler({
                "executor_type": "plugin-developer",
                "task": "Please add a Skill for the casa-probe-foo plugin",
                "context": "none",
            })
        finally:
            agent_mod.origin_var.reset(token)

        # 1. open_engagement_topic called with the U3-shaped title.
        channel.open_engagement_topic.assert_awaited_once()
        kwargs = channel.open_engagement_topic.await_args.kwargs
        name = kwargs["name"]
        # v0.37.1 D-1: title is "<state> <task>" — role icon is on the
        # bubble (kwargs["role"]), not in the title.
        assert name.startswith("🟢 "), f"got {name!r}"
        assert "Skill" in name
        # No engagement-id suffix.
        assert " | " not in name
        # Role is passed as a kwarg (resolves to numeric custom_emoji_id
        # inside open_engagement_topic).
        assert kwargs["role"] == "plugin-developer"
        # 2. the initial 🟢 persisted via the conditional launch-time
        # initializer (#529 — must not clobber a terminal/sentinel state).
        er.set_initial_state_emoji.assert_awaited()
        emoji_calls = [
            args[1] for args, _ in er.set_initial_state_emoji.await_args_list
        ]
        assert "🟢" in emoji_calls

    async def test_engage_executor_unknown_role_falls_back_to_robot_emoji(
        self, tmp_path, monkeypatch,
    ):
        """v0.37.1 D-1: unknown executor_type → bubble falls back to
        DEFAULT_ROLE_ID (🤖). Title format no longer encodes the role."""
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def(type="exotic-future-type")
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["exotic-future-type"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "x" * 32
        mock_rec.topic_id = 7
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)
        er.set_channel_state = AsyncMock()
        er.set_initial_state_emoji = AsyncMock()

        channel = await _setup(reg, tmp_path=tmp_path)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver",
            MagicMock(start=AsyncMock()), raising=False,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            await engage_executor.handler({
                "executor_type": "exotic-future-type",
                "task": "do the new thing",
                "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        kwargs = channel.open_engagement_topic.await_args.kwargs
        # Title is just "<state> <task>" — role no longer in title.
        assert kwargs["name"].startswith("🟢 ")
        # Role passes through verbatim; open_engagement_topic resolves
        # it to DEFAULT_ROLE_ID via icon_id_for_role.
        assert kwargs["role"] == "exotic-future-type"


class TestOriginContextPropagation:
    """L61/L10: engage_executor's context= argument (and the world-state
    summary) must be threaded onto the EngagementRecord's origin so the
    claude_code driver can later render them into the workspace CLAUDE.md.
    Before the fix, origin=dict(origin_var) never carried a 'context' key,
    so the driver's engagement.origin.get('context', '') was always empty."""

    async def test_context_and_world_state_land_in_created_origin(
        self, tmp_path, monkeypatch,
    ):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)
        er.recent_for_origin = MagicMock(return_value=None)

        channel = await _setup(reg, tmp_path=tmp_path)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        monkeypatch.setattr(agent_mod, "active_engagement_driver",
                             MagicMock(start=AsyncMock()), raising=False)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "do it",
                "context": "repo is x/y, branch dev",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending"

        created_origin = er.create.await_args.kwargs["origin"]
        assert created_origin["context"] == "repo is x/y, branch dev"
        assert "world_state_summary" in created_origin
        # Original origin_var fields must still be present.
        assert created_origin["role"] == "assistant"
        assert created_origin["channel"] == "telegram"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_claude_code_driver_receives_context_and_world_state(
        self, monkeypatch, tmp_path,
    ):
        """Driver-side regression: ClaudeCodeDriver.start must read the
        origin's 'context'/'world_state_summary' back out and pass them
        into provision_workspace. Follows the mocking pattern of
        tests/test_claude_code_driver.py::TestStart."""
        from drivers import claude_code_driver as ccd
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc
        from engagement_registry import EngagementRecord

        # Task 7 (containment stage 2): stub the uid-drop preflight — its
        # real preconditions (workspace chown, passwd entry) are landed by
        # Task 8; this test exercises context/world_state plumbing, not the
        # preflight itself.
        monkeypatch.setattr(ccd, "_preflight_uid_drop", lambda rec, ws: None)
        # Task 8: provision_workspace now really calls these for a record
        # with a real allocated_uid (200005, below) — stub them so this
        # test doesn't need root to chown an arbitrary uid.
        from drivers import workspace as ws_mod
        monkeypatch.setattr(ws_mod, "chown_workspace", lambda ws, uid, gid: None)
        monkeypatch.setattr(ws_mod, "ensure_identity", lambda uid, home: None)

        async def fake_cau():
            pass

        async def fake_start_kw(*, engagement_id):
            pass

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_kw)
        monkeypatch.setattr(
            s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc-root"),
        )
        (tmp_path / "svc-root").mkdir()
        monkeypatch.setattr(
            ClaudeCodeDriver, "_spawn_background_tasks",
            lambda self, engagement: None,
        )

        async def _noop_write(self, engagement, text):
            return None
        monkeypatch.setattr(ClaudeCodeDriver, "_write_to_fifo", _noop_write)

        defn = _mock_executor_def(driver="claude_code")
        exec_dir = tmp_path / "defaults-executors" / "hello-driver"
        exec_dir.mkdir(parents=True)
        prompt_path = exec_dir / "prompt.md"
        prompt_path.write_text(
            "T:{task} C:{context} W:{world_state_summary}",
        )
        defn.prompt_template_path = str(prompt_path)

        rec = EngagementRecord(
            id="abc12345def67890", kind="executor", role_or_type="hello-driver",
            driver="claude_code", status="active", topic_id=999,
            started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None,
            origin={
                "channel": "telegram", "chat_id": "42",
                "context": "repo is x/y, branch dev",
                "world_state_summary": "ws-summary",
            },
            task="do it",
            # Task 6 (containment stage 2): start() feeds allocated_uid into
            # render_run_script's setpriv wrapper — a real allocated uid
            # keeps this the fixture for a NORMAL (post-allocation) launch.
            allocated_uid=200005,
        )

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()

        await drv.start(rec, prompt="system prompt body", options=defn)

        claude_md = (
            tmp_path / "engagements" / rec.id / "CLAUDE.md"
        ).read_text(encoding="utf-8")
        assert "C:repo is x/y, branch dev" in claude_md


class TestFailedStartClosesTopic:
    """L23 leak guard: a failed engagement start must not leave a
    permanently-open 'active' forum topic — it must be flipped to 'failed'
    and closed."""

    async def test_driver_start_failure_flips_and_closes_topic(
        self, tmp_path, monkeypatch,
    ):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()  # driver="in_casa"
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)
        er.set_channel_state = AsyncMock()
        er.set_initial_state_emoji = AsyncMock()
        er.recent_for_origin = MagicMock(return_value=None)

        channel = await _setup(reg, tmp_path=tmp_path)
        channel.update_topic_state = AsyncMock()
        channel.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver",
            MagicMock(start=AsyncMock(side_effect=RuntimeError("boom"))),
            raising=False,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            result = await engage_executor.handler(
                {"executor_type": "configurator", "task": "do a thing"},
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "driver_start_failed"
        # #757: the record is terminalized by the STRICT transition now, and
        # what matters is the OUTCOME — a durable `error` carrying this arm's
        # own named kind, which is what authorizes the close asserted above.
        er.try_transition_terminal.assert_awaited_once()
        _call = er.try_transition_terminal.await_args
        assert _call.args[1] == "error", _call
        assert _call.kwargs["strict"] is True, _call
        assert _call.kwargs["error_kind"] == "driver_start_failed", _call
        er.mark_error.assert_not_awaited()
        # The leak fix: the just-created topic must be flipped to failed and closed.
        channel.update_topic_state.assert_awaited_once_with(
            engagement_id=mock_rec.id, new_state="failed",
        )
        channel.close_topic.assert_awaited_once_with(thread_id=42)

    async def test_prompt_template_missing_flips_and_closes_topic(
        self, tmp_path, monkeypatch,
    ):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        defn.prompt_template_path = "/nonexistent/prompt.md"
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)
        er.set_channel_state = AsyncMock()
        er.set_initial_state_emoji = AsyncMock()
        er.recent_for_origin = MagicMock(return_value=None)

        channel = await _setup(reg, tmp_path=None)
        channel.update_topic_state = AsyncMock()
        channel.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            result = await engage_executor.handler(
                {"executor_type": "configurator", "task": "do a thing"},
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "prompt_template_missing"
        channel.update_topic_state.assert_awaited_once_with(
            engagement_id=mock_rec.id, new_state="failed",
        )
        channel.close_topic.assert_awaited_once_with(thread_id=42)


class TestEngageExecutorPluginGate:
    """§3.5/§3.8: executor launches gate on the plugin resolution (before any
    topic is created), and record the resolved binding."""

    async def _run(self, monkeypatch, resolution):
        from tools import engage_executor
        import agent as agent_mod
        import plugin_registry

        reg = MagicMock()
        reg.get = MagicMock(return_value=_mock_executor_def(driver="claude_code"))
        reg.list_types = MagicMock(return_value=["configurator"])
        channel = await _setup(reg)
        monkeypatch.setattr(plugin_registry, "resolve_for", lambda t: resolution)
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        return json.loads(r["content"][0]["text"]), channel

    async def test_engage_executor_blocks_on_registry_invalid(self, monkeypatch):
        from plugin_registry import ResolutionResult
        payload, channel = await self._run(
            monkeypatch, ResolutionResult(registry_valid=False))
        assert payload["kind"] == "plugin_registry_invalid"
        channel.open_engagement_topic.assert_not_called()   # gate is pre-topic

    async def test_engage_executor_blocks_on_plugin_issue(self, monkeypatch):
        from plugin_registry import PluginIssue, ResolutionResult
        issue = PluginIssue(name="lesina-invoice",
                            target="executor:configurator", stage="resolve",
                            reason_code="artifact_missing")
        payload, channel = await self._run(
            monkeypatch,
            ResolutionResult(registry_valid=True, issues=[issue]))
        assert payload["kind"] == "plugin_unavailable"
        assert "lesina-invoice" in payload["message"]
        channel.open_engagement_topic.assert_not_called()

    async def test_blocks_on_not_ready_plugin(self, monkeypatch):
        """Sol round-3 B4: a resolvable-but-not-ready plugin (unresolved secret /
        authorization_missing / missing sysreq / mcp_invalid) must NOT launch."""
        from tools import engage_executor
        import agent as agent_mod
        import plugin_registry
        import tools as tools_mod
        from plugin_registry import ResolutionResult, ResolvedPlugin

        rp = ResolvedPlugin(name="p", artifact_id="a" * 64, path="/store/p",
                            version="1", manifest={})
        reg = MagicMock()
        reg.get = MagicMock(
            return_value=_mock_executor_def(driver="claude_code"))
        reg.list_types = MagicMock(return_value=["configurator"])
        channel = await _setup(reg)
        monkeypatch.setattr(plugin_registry, "resolve_for",
                            lambda t: ResolutionResult(registry_valid=True,
                                                       plugins=[rp]))
        monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                            lambda *, plugin_name: {"ready": False, "targets": [
                                {"target": "executor:configurator",
                                 "ready": False,
                                 "reasons": ["authorization_missing"]}]})
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "plugin_not_ready"
        channel.open_engagement_topic.assert_not_called()   # gate is pre-topic

    async def test_aborts_when_snapshot_changes_during_create(
        self, monkeypatch, tmp_path,
    ):
        """Sol round-4: a lock-less reload_full() bumping the generation DURING
        create()'s await is caught by the post-create recheck → the engagement is
        aborted (plugin_superseded) before the driver starts."""
        from tools import engage_executor, init_tools
        import agent as agent_mod
        import plugin_registry
        import tools as tools_mod
        from plugin_registry import ResolutionResult, ResolvedPlugin

        rp = ResolvedPlugin(name="p", artifact_id="a" * 64, path="/store/p",
                            version="1", manifest={})
        reg = MagicMock()
        reg.get = MagicMock(
            return_value=_mock_executor_def(driver="claude_code"))
        reg.list_types = MagicMock(return_value=["configurator"])
        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)
        er.recent_for_origin = MagicMock(return_value=None)

        async def _create(**kw):
            _bump_generation()   # reload_full during create's await
            return mock_rec
        er.create = AsyncMock(side_effect=_create)

        channel = await _setup(reg)
        channel.update_topic_state = AsyncMock()
        channel.close_topic = AsyncMock()
        monkeypatch.setattr(plugin_registry, "resolve_for",
                            lambda t: ResolutionResult(registry_valid=True,
                                                       plugins=[rp]))
        monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                            lambda *, plugin_name: {"ready": True, "targets": [
                                {"target": "executor:configurator",
                                 "ready": True}]})
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(channel_manager=cm, bus=MagicMock(),
                   specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                   trigger_registry=MagicMock(), engagement_registry=er,
                   executor_registry=reg)
        monkeypatch.setattr(agent_mod, "active_engagement_driver",
                            MagicMock(start=AsyncMock()), raising=False)
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi"})
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": ""})
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "plugin_superseded"
        # #757: as above — the strict transition, carrying this arm's kind.
        er.try_transition_terminal.assert_awaited_once()
        _call = er.try_transition_terminal.await_args
        assert _call.args[1] == "error", _call
        assert _call.kwargs["strict"] is True, _call
        assert _call.kwargs["error_kind"] == "plugin_superseded", _call
        er.mark_error.assert_not_awaited()

    async def test_reresolves_on_concurrent_update_during_launch(
        self, monkeypatch, tmp_path,
    ):
        """Sol #6 TOCTOU: a plugin_update during the topic-creation await bumps
        the snapshot generation → engage_executor re-resolves so the record pins
        the CURRENT artifact, not the one resolved before the await."""
        from tools import engage_executor, init_tools
        import agent as agent_mod
        import plugin_registry
        from plugin_registry import ResolutionResult, ResolvedPlugin

        reg = MagicMock()
        reg.get = MagicMock(
            return_value=_mock_executor_def(driver="claude_code"))
        reg.list_types = MagicMock(return_value=["configurator"])
        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        # #757: the launch-failure arms ask the STRICT transition now, so
        # the double needs the method the code actually calls. A bare
        # MagicMock returns a non-awaitable, which the abort owner catches
        # as a rolled-back persist and correctly declines to close on.
        er.try_transition_terminal = AsyncMock(return_value=True)
        er.set_channel_state = AsyncMock()
        er.set_initial_state_emoji = AsyncMock()
        er.recent_for_origin = MagicMock(return_value=None)

        old = ResolvedPlugin(name="p", artifact_id="a" * 64,
                             path="/store/p/old", version="1", manifest={})
        new = ResolvedPlugin(name="p", artifact_id="b" * 64,
                             path="/store/p/new", version="2", manifest={})
        state = {"res": ResolutionResult(registry_valid=True, plugins=[old])}
        monkeypatch.setattr(plugin_registry, "resolve_for",
                            lambda t: state["res"])
        # B4 gate runs verify before the topic — stub it ready so the re-resolve
        # path (not readiness) is what this test exercises.
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                            lambda *, plugin_name: {"ready": True, "targets": [
                                {"target": "executor:configurator",
                                 "ready": True}]})

        channel = await _setup(reg, tmp_path=tmp_path)

        async def _open(**kw):
            # Simulate a concurrent plugin_update landing during topic creation.
            _bump_generation()
            state["res"] = ResolutionResult(registry_valid=True, plugins=[new])
            return 42
        channel.open_engagement_topic = AsyncMock(side_effect=_open)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        monkeypatch.setattr(agent_mod, "active_engagement_driver",
                            MagicMock(start=AsyncMock()), raising=False)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        arts = er.create.await_args.kwargs["plugin_artifacts"]
        assert [a["artifact_id"] for a in arts] == ["b" * 64], (
            f"record must pin the FRESH artifact after a mid-launch update: {arts}")


# ---------------------------------------------------------------------------
# W3 (Task 8): structured brief envelope — schema, validation, both drivers.
# ---------------------------------------------------------------------------


def _real_registry(tmp_path):
    from engagement_registry import EngagementRegistry
    return EngagementRegistry(
        tombstone_path=str(tmp_path / "eng-tomb.json"), bus=None,
    )


async def _brief_setup(reg, defn, registry, driver_attr, monkeypatch, tmp_path):
    """Wire init_tools with a REAL engagement registry + a mock driver, and a
    prompt template that carries {task}. Returns the driver mock."""
    from tools import init_tools
    import agent as agent_mod

    p = tmp_path / "prompt.md"
    p.write_text("SYS-PROMPT task=<{task}> ctx=<{context}> ws=<{world_state_summary}>")
    defn.prompt_template_path = str(p)

    channel = MagicMock()
    channel.engagement_supergroup_id = -100123
    channel.engagement_permission_ok = True
    channel.open_engagement_topic = AsyncMock(return_value=42)
    channel.bot = MagicMock()
    channel.bot.edit_forum_topic = AsyncMock()
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)
    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=registry,
        executor_registry=reg,
    )
    driver_mock = MagicMock(start=AsyncMock())
    monkeypatch.setattr(agent_mod, driver_attr, driver_mock, raising=False)
    return driver_mock


class TestEngageExecutorSchema:
    def test_input_schema_only_executor_type_required(self):
        from tools import engage_executor
        schema = engage_executor.input_schema
        assert schema["required"] == ["executor_type"]
        props = schema["properties"]
        for k in ("task", "brief", "context"):
            assert k in props, k
        # brief itself only requires objective.
        assert schema["properties"]["brief"]["required"] == ["objective"]


class TestEngageExecutorBriefValidation:
    async def _invalid(self, args):
        from tools import engage_executor
        import agent as agent_mod

        reg = MagicMock()
        reg.get = MagicMock(return_value=_mock_executor_def())
        reg.list_types = MagicMock(return_value=["configurator"])
        await _setup(reg)
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({"executor_type": "configurator", **args})
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "invalid_arguments", payload
        assert r.get("is_error") is True

    async def test_both_task_and_brief(self):
        await self._invalid({"task": "t", "brief": {"objective": "o"}})

    async def test_neither_task_nor_brief(self):
        await self._invalid({})

    async def test_brief_with_top_level_context(self):
        await self._invalid({"brief": {"objective": "o"}, "context": "c"})

    async def test_interaction_required_truthy_non_bool(self):
        await self._invalid({"brief": {"objective": "o", "interaction_required": "yes"}})

    async def test_acceptance_criteria_empty_entry(self):
        await self._invalid({"brief": {"objective": "o", "acceptance_criteria": ["", "x"]}})

    async def test_process_requirements_str_not_list(self):
        await self._invalid({"brief": {"objective": "o", "process_requirements": "x"}})

    async def test_brief_context_wrong_type(self):
        await self._invalid({"brief": {"objective": "o", "context": 5}})


class TestEngageExecutorBriefDelivery:
    async def test_objective_only_brief_persisted_verbatim(self, tmp_path, monkeypatch):
        from tools import engage_executor
        import agent as agent_mod

        defn = _mock_executor_def(driver="in_casa")
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        registry = _real_registry(tmp_path)
        await _brief_setup(reg, defn, registry, "active_engagement_driver",
                           monkeypatch, tmp_path)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator",
                "brief": {"objective": "Reset the invoice counter"},
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending", payload
        rec = list(registry._records.values())[0]
        # RAW brief persisted verbatim — no injected default keys.
        assert rec.origin["brief"] == {"objective": "Reset the invoice counter"}
        assert "interaction_required" not in rec.origin["brief"]
        assert "acceptance_criteria" not in rec.origin["brief"]
        # objective drives the canonical task (title/P32/engagement.task).
        assert rec.task == "Reset the invoice counter"
        # not interaction-required → no first_contact_required state.
        assert rec.interaction_state == ""

    async def test_configurator_in_casa_delivery(self, tmp_path, monkeypatch):
        from tools import engage_executor
        from drivers.brief import FIRST_CONTACT_PARAGRAPH, COMPLETION_ACCOUNTING_LINE
        import agent as agent_mod

        defn = _mock_executor_def(driver="in_casa")
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        registry = _real_registry(tmp_path)
        driver_mock = await _brief_setup(
            reg, defn, registry, "active_engagement_driver", monkeypatch, tmp_path)

        brief = {
            "objective": "Reset invoice counter to 1000",
            "acceptance_criteria": ["counter reads 1000", "audit log updated"],
            "process_requirements": ["Confirm with operator before writing",
                                     "NEVER touch prod without a backup"],
            "context": "quarterly rollover",
            "interaction_required": True,   # ignored for in_casa (W2 claude_code-only)
        }
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "brief": brief,
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending", payload
        sys_prompt = driver_mock.start.await_args.kwargs["options"].system_prompt
        # W3 content reaches the in_casa configurator's system_prompt.
        assert "Reset invoice counter to 1000" in sys_prompt
        assert "counter reads 1000" in sys_prompt
        assert "audit log updated" in sys_prompt
        assert "Confirm with operator before writing" in sys_prompt
        assert "NEVER touch prod without a backup" in sys_prompt
        assert COMPLETION_ACCOUNTING_LINE in sys_prompt
        # W2 is claude_code-only: no first-contact paragraph, no stuck state.
        assert FIRST_CONTACT_PARAGRAPH not in sys_prompt
        rec = list(registry._records.values())[0]
        assert rec.interaction_state == ""

    async def test_claude_code_delivery_two_phase(self, tmp_path, monkeypatch):
        from tools import engage_executor
        from drivers.brief import FIRST_CONTACT_PARAGRAPH, COMPLETION_ACCOUNTING_LINE
        import agent as agent_mod

        defn = _mock_executor_def(driver="claude_code")
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        registry = _real_registry(tmp_path)
        driver_mock = await _brief_setup(
            reg, defn, registry, "active_claude_code_driver", monkeypatch, tmp_path)

        brief = {
            "objective": "Migrate the widgets table",
            "process_requirements": ["Run migrations in a transaction"],
            "interaction_required": True,
        }
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "brief": brief,
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending", payload
        prompt = driver_mock.start.await_args.kwargs["prompt"]
        assert "Migrate the widgets table" in prompt
        assert "Run migrations in a transaction" in prompt
        assert COMPLETION_ACCOUNTING_LINE in prompt
        # claude_code + interaction_required → two-phase paragraph + state.
        assert FIRST_CONTACT_PARAGRAPH in prompt
        rec = list(registry._records.values())[0]
        assert rec.interaction_state == "first_contact_required"

    async def test_claude_code_no_interaction_no_two_phase(self, tmp_path, monkeypatch):
        from tools import engage_executor
        from drivers.brief import FIRST_CONTACT_PARAGRAPH
        import agent as agent_mod

        defn = _mock_executor_def(driver="claude_code")
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        registry = _real_registry(tmp_path)
        driver_mock = await _brief_setup(
            reg, defn, registry, "active_claude_code_driver", monkeypatch, tmp_path)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator",
                "brief": {"objective": "o", "acceptance_criteria": [],
                          "process_requirements": []},
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending", payload   # empty lists are valid
        prompt = driver_mock.start.await_args.kwargs["prompt"]
        assert FIRST_CONTACT_PARAGRAPH not in prompt
        rec = list(registry._records.values())[0]
        assert rec.interaction_state == ""
