"""Tests for CasaRuntime dataclass."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_casa_runtime_holds_all_required_fields():
    from runtime import CasaRuntime

    rt = CasaRuntime(
        agents={},
        role_configs={},
        specialist_registry=MagicMock(),
        executor_registry=MagicMock(),
        engagement_registry=MagicMock(),
        agent_registry=MagicMock(),
        trigger_registry=MagicMock(),
        mcp_registry=MagicMock(),
        session_registry=MagicMock(),
        channel_manager=MagicMock(),
        bus=MagicMock(),
        engagement_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir="/addon_configs/casa",
        agents_dir="/addon_configs/casa/agents",
        home_root="/addon_configs/casa/agent-home",
        defaults_root="/opt/casa",
    )
    assert rt.config_dir == "/addon_configs/casa"
    assert rt.agents == {}


def test_casa_runtime_agents_is_mutable():
    from runtime import CasaRuntime

    rt = CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(), trigger_registry=MagicMock(),
        mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir="/x", agents_dir="/x/agents",
        home_root="/x/home", defaults_root="/opt/casa",
    )
    rt.agents["ellen"] = MagicMock()
    assert "ellen" in rt.agents


def test_init_tools_accepts_runtime():
    from runtime import CasaRuntime
    from tools import init_tools
    rt = CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(), trigger_registry=MagicMock(),
        mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir="/x", agents_dir="/x/agents",
        home_root="/x/home", defaults_root="/opt/casa",
    )
    init_tools(
        rt.channel_manager, rt.bus, rt.specialist_registry, rt.mcp_registry,
        agent_role_map={}, agent_registry=rt.agent_registry,
        trigger_registry=rt.trigger_registry,
        engagement_registry=rt.engagement_registry,
        executor_registry=rt.executor_registry,
        runtime=rt,
    )
    import tools
    assert tools._runtime is rt
