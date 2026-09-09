"""Red case for the plugin result contract (#792, INV-PLUG-017).

Declared invariant (D34): in every SDK-callback session that carries Casa's
authorization seam — a resident session, a delegated specialist session, a
specialist engagement — and that loads at least one plugin, a call to a
NON-SETUP MCP tool of a plugin that has NOT adopted the result contract
(``casa.resultContract`` absent) is refused BEFORE it runs by a code-registered
PreToolUse hook whose deny quotes none of the call's arguments, and — as a
second boundary — a result arriving for such a tool is replaced BEFORE the model
sees it by a code-registered PostToolUse hook whose ``updatedToolOutput``
quotes none of the result's bytes. The plugin's exact ``casa.setupTool``
(expanded to its full name) is exempt from both.

Red at the base (8a75a7bc): no session registers any PostToolUse matcher
(``hooks.py:3165-3216`` returns only ``PreToolUse``; ``agent.py:2300-2345`` and
``tools.py:1795-1822`` append only PreToolUse guards and, conditionally, the
authz matcher), so the "exactly one admission callback" and "exactly one
result callback" assertions count ZERO matchers — the intended reason, never an
import failure.

Specified by **astra** in the drive red-case round. The builders and callbacks
are driven directly: no CLI, no listening socket, no Telegram channel.

Callback discovery is by the ``_casa_result_broker`` marker attribute the
production hooks carry (``"admission"`` / ``"result"``), the way the authz hook
carries ``_casa_authz_role`` — never by closure name.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import plugin_registry
from plugin_registry import reload_snapshot
from agent import Agent
from channels import ChannelManager
from config import (
    AgentConfig, CharacterConfig, HooksConfig, MemoryConfig, ToolsConfig,
)
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry
from plugin_fixtures import entry, mk_artifact, mk_registry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


# --- the specified inputs ----------------------------------------------------

TOOL = "mcp__plugin_probe_api__fetch_login_link"
# An exemption mistakenly implemented as a PREFIX match on the setup tool's
# name would exempt this; the exact-name exemption must not.
SETUP_LOOKALIKE = "mcp__plugin_probe_api__setup_probe_extra"
SETUP_EXACT = "mcp__plugin_probe_api__setup_probe"
ARG_NAME = "HR_ARG_NAME_7d19"
ARG_VALUE = "HR_ARG_VALUE_b84e"
TOOL_INPUT = {ARG_NAME: ARG_VALUE}
RESULT_FIELD = "HR_RESULT_FIELD_19f0"
SENTINEL = "HR_RESULT_SENTINEL_936f"
TOOL_RESPONSE = json.dumps({RESULT_FIELD: SENTINEL})
TOOL_USE_ID = "hr-call"

HOOK_CONFIGS = {
    "empty": HooksConfig(),
    "declared": HooksConfig(pre_tool_use=[{"policy": "block_dangerous_bash"}]),
}


# --- fixture: a NON-ADOPTING plugin with one MCP server -----------------------

def _install_probe(tmp_path, protected):
    store = tmp_path / "store"
    e = entry("probe", ["resident:assistant", "specialist:finance"])
    art = mk_artifact(
        store, "probe", e["artifact_id"],
        mcp_servers={"api": {"command": "python3", "args": ["-c", "pass"]}},
        extra_manifest={"casa": {
            "setupTool": "setup_probe",
            "protectedTools": list(protected),
        }},
    )
    manifest = json.loads(
        (art / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "resultContract" not in manifest["casa"]       # non-adopting
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    return e, art


def _resident(tmp_path, hooks_cfg):
    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT, role="assistant",
        model="claude-sonnet-4-6", system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        hooks=hooks_cfg,
    )
    sm = AsyncMock()
    sm.profile.return_value = ""
    sm.recall.return_value = ""
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        agent_registry=None,
        semantic_memory=sm,
    )


def _spec_cfg(hooks_cfg, role="finance"):
    return SimpleNamespace(
        role=role, model="claude-sonnet-4-6", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=["Read", "Skill"], disallowed=["Bash"],
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=hooks_cfg, cwd="",
    )


async def _build(kind, tmp_path, hooks_cfg, monkeypatch):
    """Build the options for one of the three in-scope session kinds."""
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    if kind == "resident":
        return await _resident(tmp_path, hooks_cfg)._build_options(
            channel="telegram", channel_key="k", is_fresh=True,
            resume_sid=None, user_text="hi")
    if kind == "delegated":
        return tools_mod._build_specialist_options(_spec_cfg(hooks_cfg))
    if kind == "engagement":
        resolution = plugin_registry.resolve_for("specialist:finance")
        return tools_mod._build_specialist_options(
            _spec_cfg(hooks_cfg), resolution=resolution,
            extra_casa_tools=tools_mod.SPECIALIST_CASA_GRANTS)
    raise AssertionError(kind)


def _assert_plugin_loaded(opts, art):
    assert opts.plugins == [{"type": "local", "path": str(art)}]
    assert opts.allowed_tools.count("mcp__plugin_probe_api") == 1


def _broker_callbacks(opts, event, marker, tool_name):
    """Every broker callback of ``marker`` registered under ``event`` whose
    matcher accepts ``tool_name``. Routing through the matcher is part of the
    contract: a callback the CLI would never invoke for this tool is not a
    boundary."""
    out = []
    for m in (opts.hooks or {}).get(event, []) or []:
        matcher = getattr(m, "matcher", None)
        if matcher is not None and not re.fullmatch(matcher, tool_name):
            continue
        for h in getattr(m, "hooks", []) or []:
            if getattr(h, "_casa_result_broker", None) == marker:
                out.append(h)
    return out


def _pre_input(tool_name, tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": tool_name,
            "tool_input": dict(tool_input)}


def _post_input(tool_name, tool_input, tool_response):
    return {"hook_event_name": "PostToolUse", "tool_name": tool_name,
            "tool_input": dict(tool_input), "tool_response": tool_response}


KINDS = ("resident", "delegated", "engagement")
PROTECTED = ((), ("invoice_reset",))          # never the tested tool
NON_SETUP_TOOLS = (TOOL, SETUP_LOOKALIKE)


# --- the red case ------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("hooks_name", sorted(HOOK_CONFIGS))
@pytest.mark.parametrize("protected", PROTECTED, ids=["unprotected", "protected"])
@pytest.mark.parametrize("tool_name", NON_SETUP_TOOLS, ids=["tool", "setup-lookalike"])
async def test_nonadopter_denied_before_execution(
        tmp_path, monkeypatch, kind, hooks_name, protected, tool_name):
    """First boundary: exactly one admission callback matches the tool, and it
    denies with a reason that quotes neither argument marker."""
    _e, art = _install_probe(tmp_path, protected)
    opts = await _build(kind, tmp_path, HOOK_CONFIGS[hooks_name], monkeypatch)
    _assert_plugin_loaded(opts, art)

    cbs = _broker_callbacks(opts, "PreToolUse", "admission", tool_name)
    assert len(cbs) == 1, (kind, hooks_name, len(cbs))

    out = await cbs[0](_pre_input(tool_name, TOOL_INPUT), TOOL_USE_ID, {})
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert isinstance(reason, str) and reason
    assert reason.count(ARG_NAME) == 0
    assert reason.count(ARG_VALUE) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("hooks_name", sorted(HOOK_CONFIGS))
@pytest.mark.parametrize("protected", PROTECTED, ids=["unprotected", "protected"])
@pytest.mark.parametrize("tool_name", NON_SETUP_TOOLS, ids=["tool", "setup-lookalike"])
async def test_nonadopter_result_replaced(
        tmp_path, monkeypatch, kind, hooks_name, protected, tool_name):
    """Second boundary, driven independently of the first: exactly one result
    callback matches the tool, and it replaces the result with a withheld
    notice carrying zero bytes of the original."""
    _e, art = _install_probe(tmp_path, protected)
    opts = await _build(kind, tmp_path, HOOK_CONFIGS[hooks_name], monkeypatch)
    _assert_plugin_loaded(opts, art)

    cbs = _broker_callbacks(opts, "PostToolUse", "result", tool_name)
    assert len(cbs) == 1, (kind, hooks_name, len(cbs))

    out = await cbs[0](
        _post_input(tool_name, TOOL_INPUT, TOOL_RESPONSE), TOOL_USE_ID, {})
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    replaced = hso["updatedToolOutput"]
    assert isinstance(replaced, str)
    assert json.loads(replaced)["casa_result_withheld"] is True
    assert replaced.count(SENTINEL) == 0
    assert replaced.count(RESULT_FIELD) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("hooks_name", sorted(HOOK_CONFIGS))
@pytest.mark.parametrize("protected", PROTECTED, ids=["unprotected", "protected"])
async def test_exact_setup_tool_exempt(
        tmp_path, monkeypatch, kind, hooks_name, protected):
    """The plugin's exact ``casa.setupTool`` passes BOTH boundaries with ``{}``
    — and both callbacks must EXIST, so an absent matcher cannot vacuously
    satisfy the exemption."""
    _e, art = _install_probe(tmp_path, protected)
    opts = await _build(kind, tmp_path, HOOK_CONFIGS[hooks_name], monkeypatch)
    _assert_plugin_loaded(opts, art)

    pre = _broker_callbacks(opts, "PreToolUse", "admission", SETUP_EXACT)
    post = _broker_callbacks(opts, "PostToolUse", "result", SETUP_EXACT)
    assert len(pre) == 1, (kind, hooks_name, len(pre))
    assert len(post) == 1, (kind, hooks_name, len(post))

    assert await pre[0](_pre_input(SETUP_EXACT, {}), TOOL_USE_ID, {}) == {}
    assert await post[0](
        _post_input(SETUP_EXACT, {}, TOOL_RESPONSE), TOOL_USE_ID, {}) == {}
