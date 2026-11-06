"""S-1 (block-S live finding 2026-07-15): eager schemas for casa-framework.

Claude Code ≥2.1.7 defers MCP tool schemas behind ToolSearch; since ~2.1.69
deferral is on by default regardless of tool count, and an explicitly
allowed tool is still deferred. On cold voice sessions the resulting
ToolSearch round-trips (each a full model turn) ate the 27s voice budget
before `delegate_to_agent` could even be called (cids ef8c68bb/8fff87ef/
93f501bb, production 2026-07-15).

Fix under test: the casa-framework SDK MCP server config carries
``alwaysLoad: True`` — the CLI's per-server eager-load knob (shipped
v2.1.121, present in every pin since, incl. 2.1.220. The Python SDK
transport forwards every non-``instance`` key of an sdk-type server config
to ``--mcp-config`` verbatim — same code path in 0.2.114 and 0.2.128,
`subprocess_cli.py`). The framework toolset is the small always-needed
core surface; plugin/HA/n8n servers deliberately keep deferral.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_casa_framework_server_config_carries_alwaysload():
    from tools import CASA_TOOLS, create_casa_tools, select_casa_tools

    cfg = create_casa_tools()
    assert cfg.get("type") == "sdk"
    assert "instance" in cfg  # the in-process server object must survive
    assert cfg.get("alwaysLoad") is True, (
        "casa-framework must opt out of ToolSearch deferral (S-1): "
        f"config keys = {sorted(cfg.keys())!r}"
    )
    assert select_casa_tools() == CASA_TOOLS
