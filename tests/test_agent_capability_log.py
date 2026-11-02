"""Layer 5 — per-agent capability boot log.

Every Agent construction (boot AND reload — reload._construct_agent builds a
fresh Agent) emits one INFO `agent_capabilities` line recording the resolved
capability surface: role, model, enabled, tool count + names, mcp servers. This
makes capability drift visible in prod `docker logs` and diffable across
deploys — a runtime backstop for anything that slips past the L1/L2 CI guards
(e.g. the search_memory grant vanishing after a config_sync reconcile).
"""
from __future__ import annotations

import logging

import pytest

from agent import Agent
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.unit]


def _construct(tmp_path, **cfg_kw) -> None:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=cfg_kw.get("role", "assistant"),
        model=cfg_kw.get("model", "claude-sonnet-4-6"),
        enabled=cfg_kw.get("enabled", True),
        tools=ToolsConfig(allowed=cfg_kw.get("allowed", ["Read"])),
        mcp_server_names=cfg_kw.get("mcp_server_names", []),
        character=CharacterConfig(name="X"),
        system_prompt="x",
    )
    Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "s.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


def test_construction_emits_one_capability_line(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="agent"):
        _construct(
            tmp_path, role="assistant", model="claude-opus-4-8",
            allowed=[
                "Read",
                "mcp__casa-framework__search_memory",
                "mcp__casa-framework__get_schedule",
            ],
            mcp_server_names=["casa-framework", "homeassistant"],
        )
    lines = [
        r.getMessage() for r in caplog.records
        if r.name == "agent" and "agent_capabilities" in r.getMessage()
    ]
    assert len(lines) == 1, lines
    m = lines[0]
    assert "role=assistant" in m
    assert "model=claude-opus-4-8" in m
    assert "enabled=True" in m
    assert "tool_count=3" in m
    assert "search_memory" in m           # the drift-prone grant is visible
    assert "casa-framework" in m and "homeassistant" in m


def test_capability_line_reflects_missing_search_tool(tmp_path, caplog):
    """The line is diffable: a config WITHOUT search_memory shows a smaller
    surface and no search_memory token — exactly what makes drift spottable."""
    with caplog.at_level(logging.INFO, logger="agent"):
        _construct(tmp_path, role="butler",
                   allowed=["Read", "mcp__casa-framework__get_schedule"])
    m = [r.getMessage() for r in caplog.records
         if "agent_capabilities" in r.getMessage()][0]
    assert "role=butler" in m
    assert "tool_count=2" in m
    assert "search_memory" not in m
