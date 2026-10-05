"""#1294: a session that was never granted the shell tool is never offered it.

A read-only ``Bash`` call skips the fail-closed ``can_use_tool`` on the pinned
CLI, and a ``run_in_background`` command's completion makes a live CLI run a
turn Casa never sent. The CLI removes a disallowed tool from the surface, so
the deny list is the only control that holds whatever the permission mode.
Every test here reads the options the real entry point hands the client.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugin_registry
import tools
from engagement_registry import EngagementRegistry
from test_plugin_job_launch import worker  # noqa: F401 — fixture
from test_start_job import Client, call, runtime  # noqa: F401 — fixture


def _specialist_cfg(allowed, disallowed):
    from config import HooksConfig

    return SimpleNamespace(
        role="finance", model="claude-haiku-4-5", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=list(allowed), disallowed=list(disallowed),
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=HooksConfig(), cwd="",
    )


def _specialist_options(monkeypatch, allowed, disallowed):
    from plugin_registry import ResolutionResult

    monkeypatch.setattr(tools, "_mcp_registry", None)
    return tools._build_specialist_options(
        _specialist_cfg(allowed, disallowed),
        resolution=ResolutionResult(registry_valid=True))


@pytest.mark.asyncio
async def test_a_resumed_plugin_job_worker_is_not_offered_bash(worker):
    """The resident-hosted worker rebuilt from its persisted record on resume."""
    result = await call()
    await tools.drain_launch_turns()
    loaded = EngagementRegistry(tombstone_path=worker.registry._tombstone_path, bus=None)
    await loaded.load()
    rec = loaded.get(result["engagement_id"])
    assert rec.kind == "plugin"
    plugin_registry.reload_snapshot(
        registry_path=Path(worker.registry._tombstone_path).parent / "absent.json")
    opts = tools.build_engagement_resume_options(rec, "resume-id")
    assert opts.resume == "resume-id"
    assert opts.disallowed_tools.count("Bash") == 1


def test_a_specialist_whose_role_does_not_list_bash_is_not_offered_it(monkeypatch):
    opts = _specialist_options(monkeypatch, ["Read"], [])
    assert "Bash" not in opts.allowed_tools
    assert opts.disallowed_tools.count("Bash") == 1


@pytest.mark.asyncio
async def test_a_specialist_hosted_job_is_not_offered_bash(runtime):
    """The launch entry point (start_job → specialist host), with a host role
    that lists neither ``Bash`` allowed nor denied."""
    assert "Bash" not in runtime.cfg.tools.allowed
    assert "Bash" not in runtime.cfg.tools.disallowed
    result = await call()
    assert result["status"] == "pending", result
    await tools.drain_launch_turns()
    opts = Client.instances[0].options
    assert opts.disallowed_tools.count("Bash") == 1


@pytest.mark.asyncio
async def test_a_resumed_specialist_hosted_job_is_not_offered_bash(runtime):
    result = await call()
    await tools.drain_launch_turns()
    rec = runtime.registry.get(result["engagement_id"])
    assert rec.kind == "specialist" and rec.plugin_artifacts is not None
    opts = tools.build_engagement_resume_options(rec, "resumed-session")
    assert opts.resume == "resumed-session"
    assert opts.disallowed_tools.count("Bash") == 1
