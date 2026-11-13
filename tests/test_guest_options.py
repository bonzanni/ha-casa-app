"""The locked-down options of a guest run: the allow-list is exactly the
specialist's guest-flagged, non-protected plugin tools; nothing of Casa's,
no built-ins, no agent-to-agent, no session on disk; the pin is the first
PreToolUse matcher and the result broker carries the same list."""
from __future__ import annotations

import dataclasses
import json
import logging

import plugin_grants
import tools as tools_mod
from config import (
    AgentConfig, CharacterConfig, DelegateEntry, HooksConfig, ToolsConfig,
)
from guest_run import GUEST_MAX_TURNS
from plugin_registry import ResolutionResult, ResolvedPlugin

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

LOOKUP = "mcp__plugin_mtg_mtg__lookup_rule"
SEARCH = "mcp__plugin_mtg_mtg__search_rules"
EXPORT = "mcp__plugin_mtg_mtg__export_deck"       # declared safe, not for guests
SETUP = "mcp__plugin_mtg_mtg__setup_corpus"        # the setup tool, exempt elsewhere
CTX: dict = {"signal": None}


def _cfg(max_turns: int = 40) -> AgentConfig:
    """A specialist with a full grant set of its own — built-ins, a Casa tool,
    a Casa MCP server, a hook policy and a delegate — none of which a guest
    run may carry: the assertions below fail if any is unioned in."""
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="mtg", model="x",
                       character=CharacterConfig(name="Judge"), system_prompt="x",
                       kind="specialist",
                       tools=ToolsConfig(max_turns=max_turns, allowed=[
                           "Read", "Bash", "mcp__casa-framework__send_message"]),
                       mcp_server_names=["casa-framework"],
                       hooks=HooksConfig(pre_tool_use=[{"policy": "block_dangerous_bash"}]),
                       delegates=[DelegateEntry(agent="assistant", purpose="p", when="w")])


def _plugin(tmp_path, *, protected: list[str] | None = None) -> ResolutionResult:
    plug = tmp_path / "plug"
    plug.mkdir(exist_ok=True)
    (plug / ".claude-plugin").mkdir(exist_ok=True)
    (plug / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"mtg": {"command": "serve"}}}))
    casa = {
        "setupTool": "setup_corpus",
        "resultContract": {"version": 1, "tools": {
            "lookup_rule": {"result": "safe", "guest": True},
            "search_rules": {"result": "safe", "guest": True},
            "setup_corpus": {"result": "safe"},
            "export_deck": {"result": "safe"},
        }},
    }
    if protected:
        casa["protectedTools"] = list(protected)
    manifest = {"name": "mtg", "version": "1.0.0", "casa": casa}
    (plug / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    return ResolutionResult(registry_valid=True, plugins=[ResolvedPlugin(
        name="mtg.mtg", artifact_id="art-1", path=str(plug), version="1.0.0",
        manifest=manifest, manifest_name="mtg")])


def build(tmp_path, *, max_turns: int = 40, resolution=None):
    res = _plugin(tmp_path) if resolution is None else resolution
    return tools_mod._build_guest_options(_cfg(max_turns), resolution=res,
                                          system_prompt="GUEST PROMPT")


def _pre(tool: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}


def _reason(out: dict) -> str:
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def _argv(opts) -> list[str]:
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )
    return SubprocessCLITransport(prompt="x", options=opts)._build_command()


def test_the_allow_list_is_exactly_the_flagged_non_protected_tools(tmp_path):
    opts = build(tmp_path)
    assert opts.allowed_tools == [LOOKUP, SEARCH]
    assert opts.system_prompt == "GUEST PROMPT"


def test_the_plugin_tools_not_offered_are_out_of_the_models_view(tmp_path):
    """An unflagged plugin tool and the setup tool are denied outright, so
    the model never sees them; the flagged ones are not."""
    opts = build(tmp_path)
    assert EXPORT in opts.disallowed_tools
    assert SETUP in opts.disallowed_tools
    assert LOOKUP not in opts.disallowed_tools
    assert SEARCH not in opts.disallowed_tools
    assert len(opts.disallowed_tools) == len(set(opts.disallowed_tools))


def test_no_casa_tools_no_handoff_no_delegates_no_builtins(tmp_path):
    opts = build(tmp_path)
    assert opts.mcp_servers == {}
    assert not [t for t in opts.allowed_tools if t.startswith("mcp__casa-framework__")]
    assert opts.tools == []
    assert opts.setting_sources == []
    assert opts.skills == []
    assert opts.plugins == [{"type": "local", "path": str(tmp_path / "plug")}]
    for t in ("Bash", "Task", "Agent", "ToolSearch", "Skill"):
        assert t in opts.disallowed_tools, t
    from claude_runtime import (
        CROSS_SESSION_TOOLS, OUTBOUND_SHARE_TOOLS, SELF_SCHEDULING_TOOLS,
    )
    denied = set(opts.disallowed_tools)
    assert set(CROSS_SESSION_TOOLS) <= denied
    assert set(SELF_SCHEDULING_TOOLS) <= denied
    assert set(OUTBOUND_SHARE_TOOLS) <= denied
    assert opts.permission_mode == "dontAsk"
    assert opts.resume is None
    assert opts.strict_mcp_config is False
    assert json.loads(opts.settings)["disableAllHooks"] is True
    assert json.loads(opts.settings)["crossSessionInbound"] == "refuse"
    assert opts.include_partial_messages is False
    assert opts.can_use_tool is not None
    assert opts.cwd == "/config/agent-home/mtg"
    import result_broker as rb
    assert rb.ENV_CLIENT in opts.env and rb.ENV_SOCKET in opts.env


def test_max_turns_is_capped(tmp_path):
    assert GUEST_MAX_TURNS == 12
    assert build(tmp_path, max_turns=40).max_turns == 12
    assert build(tmp_path, max_turns=8).max_turns == 8


def test_the_session_is_not_persisted(tmp_path):
    opts = build(tmp_path)
    assert opts.extra_args == {"no-session-persistence": None}
    assert _argv(opts).count("--no-session-persistence") == 1
    # the flag comes from extra_args and nowhere else
    without = dataclasses.replace(opts, extra_args={})
    assert _argv(without).count("--no-session-persistence") == 0


async def test_the_pin_is_first_and_the_broker_carries_the_guest_tools(tmp_path):
    opts = build(tmp_path)
    pre = opts.hooks["PreToolUse"]
    pin = pre[0]
    assert pin.matcher is None and len(pin.hooks) == 1
    assert _reason(await pin.hooks[0](_pre("Read"), "t", CTX)) == "Read is not available here"
    assert await pin.hooks[0](_pre(LOOKUP), "t", CTX) == {}
    admission = [h for m in pre for h in m.hooks
                 if getattr(h, "_casa_result_broker", None) == "admission"]
    assert len(admission) == 1
    out = await admission[0](_pre(EXPORT), "t", CTX)
    assert _reason(out) == f"not executed: {EXPORT} is not offered to guests"
    assert _reason(await admission[0](_pre(SETUP), "t", CTX)) == (
        f"not executed: {SETUP} is not offered to guests")
    assert set(opts.hooks) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
    assert len(opts.hooks["PostToolUse"]) == 1
    assert len(opts.hooks["PostToolUseFailure"]) == 1


def test_a_profile_withheld_tool_stays_out_even_when_flagged(tmp_path, monkeypatch):
    """A profile that drops a flagged tool keeps it out; one that names an
    unflagged tool does not let it in. The intersection is computed last."""
    prefix = "mcp__plugin_mtg_mtg__"
    plan = plugin_grants.ProfilePlan(entries=(plugin_grants.ProfiledPlugin(
        name="mtg.mtg", profile="narrow", prefixes=(prefix,),
        allowed_names=frozenset({LOOKUP, EXPORT}),
        declared_excluded=frozenset()),), loaded=("mtg.mtg",))
    monkeypatch.setattr(tools_mod, "profile_plan", lambda *a, **k: plan)
    opts = build(tmp_path)
    assert opts.allowed_tools == [LOOKUP]
    assert SEARCH not in opts.allowed_tools
    assert EXPORT not in opts.allowed_tools
    # the profile guard rides second, after the pin
    pre = opts.hooks["PreToolUse"]
    assert pre[0].matcher is None
    assert pre[1].matcher == "mcp__plugin_mtg_mtg__.*"


def test_a_protected_tool_never_reaches_a_guest_run(tmp_path, monkeypatch):
    """The manifest parser refuses ``guest`` on a protected tool; should a map
    nonetheless carry one (a hand-built or a drifted map), the builder keeps
    it out of the allow-list."""
    res = _plugin(tmp_path, protected=["export_deck"])
    real = plugin_grants.result_contract_map(res)
    flagged = dict(real.tools)
    flagged[EXPORT] = dataclasses.replace(real.tools[EXPORT], guest=True)
    forged = dataclasses.replace(real, tools=flagged)
    monkeypatch.setattr(tools_mod, "result_contract_map", lambda _r: forged)
    assert EXPORT in plugin_grants.protected_map(res)
    opts = build(tmp_path, resolution=res)
    assert opts.allowed_tools == [LOOKUP, SEARCH]


def test_a_build_without_plugins_has_an_empty_allow_list_and_still_the_pin(caplog):
    with caplog.at_level(logging.INFO, logger="tools"):
        opts = tools_mod._build_guest_options(
            _cfg(), resolution=ResolutionResult(registry_valid=True),
            system_prompt="GUEST PROMPT")
    assert opts.allowed_tools == []
    assert opts.plugins == []
    assert list(opts.hooks) == ["PreToolUse"]
    assert len(opts.hooks["PreToolUse"]) == 1
    assert opts.hooks["PreToolUse"][0].matcher is None
    lines = [r.getMessage() for r in caplog.records
             if "agent_capabilities_effective" in r.getMessage()]
    assert len(lines) == 1
    assert "role=mtg" in lines[0] and "surface=guest" in lines[0]
    assert "tool_count=0" in lines[0]
