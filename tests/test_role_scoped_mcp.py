"""Role-scoped MCP registry and Casa-framework schema selection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_role_sdk_override_wins_only_for_target_role():
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://raw")
    registry.register_role_sdk(
        "homeassistant", "butler", {"type": "sdk", "instance": object()},
    )

    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"]["type"] == "sdk"
    assert registry.resolve(
        ["homeassistant"], role="assistant",
    )["homeassistant"]["url"] == "http://raw"


async def test_wire_ha_facade_publishes_before_invalidating_only_butler():
    from casa_core import wire_tina_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://raw")
    old_config = {"type": "sdk", "instance": object()}
    registry.register_role_sdk("homeassistant", "butler", old_config)
    new_config = {"type": "sdk", "instance": object()}

    class ObservingAgent:
        def __init__(self, role):
            self.role = role
            self.seen = []

        async def invalidate_tool_surface(self):
            self.seen.append(
                registry.resolve(["homeassistant"], role=self.role)[
                    "homeassistant"
                ]
            )

    butler = ObservingAgent("butler")
    assistant = ObservingAgent("assistant")

    await wire_tina_ha_facade(
        registry,
        type("Facade", (), {"server_config": new_config, "surface_digest": ""})(),
        {"butler": butler, "assistant": assistant},
        tina_role="butler",
    )

    assert butler.seen == [new_config]
    assert assistant.seen == []
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"] is new_config
    assert registry.resolve(
        ["homeassistant"], role="assistant",
    )["homeassistant"]["url"] == "http://raw"


def test_sdk_factory_receives_role_and_frozen_grants():
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_sdk_factory(
        "casa-framework",
        lambda role, grants: {
            "type": "sdk", "role": role, "grants": grants,
        },
    )

    cfg = registry.resolve(
        ["casa-framework"],
        role="butler",
        allowed_tools=["mcp__casa-framework__get_schedule"],
    )["casa-framework"]

    assert cfg["role"] == "butler"
    assert cfg["grants"] == frozenset({
        "mcp__casa-framework__get_schedule",
    })


def test_casa_framework_factory_exposes_only_granted_tools():
    """Pins INV-MCP-003 (SDK half; the HTTP half is test_svc_tools_list_uses_static_snapshot). Red case demonstrated: neutering select_casa_tools' allowed_tools filter fails this test."""
    from tools import create_casa_tools, select_casa_tools

    grants = frozenset({
        "mcp__casa-framework__get_schedule",
        "mcp__casa-framework__recall_memory",
    })

    cfg = create_casa_tools(grants)

    assert {tool.name for tool in select_casa_tools(grants)} == {
        "get_schedule", "recall_memory",
    }
    assert cfg["alwaysLoad"] is True


def test_server_level_framework_grant_keeps_full_surface():
    from tools import CASA_TOOLS, create_casa_tools, select_casa_tools

    grants = frozenset({"mcp__casa-framework"})

    cfg = create_casa_tools(grants)

    assert {tool.name for tool in select_casa_tools(grants)} == {
        tool.name for tool in CASA_TOOLS
    }
    assert cfg["alwaysLoad"] is True


def test_ha_grant_doctrine_forbids_duplicate_raw_and_facade_names():
    path = (
        Path(__file__).parents[1]
        / "casa/rootfs/opt/casa/defaults/agents/executors/configurator"
        / "doctrine/recipes/resident/grant_ha_tools.md"
    )
    text = path.read_text(encoding="utf-8")

    assert "role-specific eager facade" in text
    assert "same logical server name, `homeassistant`" in text
    assert "Never grant raw `homeassistant` alongside a second facade" in text
    assert "duplicate tools" in text
