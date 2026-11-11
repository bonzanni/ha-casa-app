"""A specialist build that grants a casa-framework tool also mounts the
casa-framework server, whatever the role's own server list says.

A specialist declaring ``mcp_servers: []`` used to receive its engagement or
job launch grants (``query_engager``, ``emit_completion``, and for a job
``report_job_progress`` and ``ask_operator``) in ``allowed_tools`` with no
server exposing them, so the model was told it had tools it could not call.
These tests drive the real ``_build_specialist_options`` with a real
``McpServerRegistry`` and read the tools the built server actually lists.
"""
from __future__ import annotations

import pytest

import background_jobs
import tools
from config import AgentConfig, CharacterConfig

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:  # pragma: no cover
    from role_artifact_stub import STUB_ROLE_ARTIFACT

_ENGAGEMENT = tools.SPECIALIST_CASA_GRANTS
_JOB = tools.SPECIALIST_CASA_GRANTS + background_jobs.JOB_CASA_GRANTS


@pytest.fixture
def bound(monkeypatch):
    from mcp_registry import McpServerRegistry
    import agent_inbox
    monkeypatch.setattr(agent_inbox, "_inboxes", {})
    for name in ("_agent_registry", "_specialist_telemetry", "_channel_manager"):
        monkeypatch.setattr(tools, name, None, raising=False)
    reg = McpServerRegistry()
    reg.register_sdk_factory("casa-framework",
                             lambda _role, grants: tools.create_casa_tools(grants))
    monkeypatch.setattr(tools, "_mcp_registry", reg)


@pytest.mark.parametrize("grants", [_ENGAGEMENT, _JOB], ids=["engagement", "job"])
def test_launch_grants_come_with_the_server_that_exposes_them(bound, grants):
    from plugin_registry import ResolutionResult
    from test_file_handoff_grants import _listed_tool_names
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="mtg", model="x",
                      character=CharacterConfig(name="Judge"), system_prompt="x",
                      kind="specialist")
    cfg.mcp_server_names = []
    opts = tools._build_specialist_options(
        cfg, resolution=ResolutionResult(registry_valid=True, plugins=[]),
        extra_casa_tools=grants)
    assert set(grants) <= set(opts.allowed_tools)
    assert "casa-framework" in opts.mcp_servers
    listed = _listed_tool_names(opts.mcp_servers["casa-framework"])
    assert {g.rsplit("__", 1)[1] for g in grants} <= listed
    assert cfg.mcp_server_names == []          # the config itself is untouched
