"""Tests for the get_schedule framework tool."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _make_summary(name, type_, schedule_desc, next_fire):
    from trigger_registry import TriggerSummary
    return TriggerSummary(
        name=name, type=type_, schedule_desc=schedule_desc, next_fire=next_fire,
    )


class TestGetSchedule:
    async def test_returns_markdown_list(self, monkeypatch):
        import agent as agent_mod
        import tools

        reg = MagicMock()
        next_fire = datetime(2026, 4, 22, 8, 0, tzinfo=timezone.utc)
        reg.list_jobs_for = MagicMock(return_value=[
            _make_summary("morning-briefing", "cron", "0 8 * * 1-5", next_fire),
        ])
        tools.init_tools(
            channel_manager=MagicMock(),
            bus=MagicMock(),
            specialist_registry=MagicMock(),
            mcp_registry=MagicMock(),
            trigger_registry=reg,
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            result = await tools.get_schedule.handler({"within_hours": 24})
        finally:
            agent_mod.origin_var.reset(token)

        text = result["content"][0]["text"]
        assert "morning-briefing" in text
        assert "cron" in text
        assert "0 8 * * 1-5" in text
        assert "2026-04-22" in text

    async def test_no_turn_context_returns_error(self):
        import tools
        tools.init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(),
        )
        result = await tools.get_schedule.handler({"within_hours": 24})
        assert "Error" in result["content"][0]["text"]
        assert "turn" in result["content"][0]["text"].lower()
        assert result.get("is_error") is True

    async def test_registry_missing_is_an_error(self):
        import tools
        tools.init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=None,
        )
        result = await tools.get_schedule.handler({"within_hours": 24})
        assert "registry" in result["content"][0]["text"]
        assert result.get("is_error") is True

    async def test_origin_without_role_is_an_error(self):
        import agent as agent_mod
        import tools
        reg = MagicMock()
        tools.init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=reg,
        )
        token = agent_mod.origin_var.set({
            "role": "", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            result = await tools.get_schedule.handler({"within_hours": 24})
        finally:
            agent_mod.origin_var.reset(token)
        assert "no role" in result["content"][0]["text"]
        assert result.get("is_error") is True
        reg.list_jobs_for.assert_not_called()

    async def test_empty_schedule_message(self, monkeypatch):
        import agent as agent_mod
        import tools

        reg = MagicMock()
        reg.list_jobs_for = MagicMock(return_value=[])
        tools.init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=reg,
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            result = await tools.get_schedule.handler({"within_hours": 6})
        finally:
            agent_mod.origin_var.reset(token)

        text = result["content"][0]["text"]
        assert "no scheduled" in text.lower()
        assert "6" in text

    async def test_within_hours_default_24(self, monkeypatch):
        import agent as agent_mod
        import tools

        reg = MagicMock()
        reg.list_jobs_for = MagicMock(return_value=[])
        tools.init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=reg,
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            await tools.get_schedule.handler({})
        finally:
            agent_mod.origin_var.reset(token)
        reg.list_jobs_for.assert_called_once()
        assert reg.list_jobs_for.call_args.kwargs["within_hours"] == 24
