"""Restricted runtime for untrusted webhook turns (Release A, Layer 1).

The primary containment boundary: an untrusted webhook turn must build agent
options that load no plugins, no external hooks, no built-in tools, and expose
exactly two casa-framework tools — so third-party content cannot reach Bash,
the filesystem, the network, or unauthenticated Hindsight.
"""
from __future__ import annotations

import json

from agent import build_restricted_webhook_options

RESTRICTED_TOOLS = {
    "mcp__casa-framework__recall_memory",
    "mcp__casa-framework__send_message",
}


def _opts():
    return build_restricted_webhook_options(
        model="claude-x", role="assistant", system_prompt="you are Ellen",
        max_turns=12, agent_home="/config/agent-home/assistant", resume_sid=None,
    )


def test_no_plugins_settings_or_skills():
    o = _opts()
    assert o.plugins == []
    assert o.setting_sources == []
    assert o.skills == []


def test_all_hooks_disabled_via_settings():
    o = _opts()
    assert json.loads(o.settings) == {"disableAllHooks": True,
                                      "crossSessionInbound": "refuse"}
    assert not o.hooks  # no in-process hooks either


def test_no_builtin_tools_and_strict_mcp():
    o = _opts()
    assert o.tools == []                 # strips Bash/Read/Write/… base tools
    assert o.strict_mcp_config is True   # no ambient .mcp.json
    assert o.permission_mode == "dontAsk"


def test_agent_and_task_disallowed():
    o = _opts()
    assert "Agent" in o.disallowed_tools
    assert "Task" in o.disallowed_tools
    assert "Bash" in o.disallowed_tools


def test_exact_two_tool_allowlist_and_server():
    o = _opts()
    assert set(o.allowed_tools) == RESTRICTED_TOOLS
    assert set(o.mcp_servers.keys()) == {"casa-framework"}


def test_identity_and_resume_preserved():
    o = build_restricted_webhook_options(
        model="claude-x", role="butler", system_prompt="SP",
        max_turns=5, agent_home="/h", resume_sid="sid-123",
    )
    assert o.model == "claude-x"
    assert o.system_prompt == "SP"
    assert o.resume == "sid-123"
    assert o.cwd == "/h"
