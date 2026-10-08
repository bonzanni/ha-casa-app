"""Every Casa CLI session hard-denies the CLI's cross-session tools and refuses
inbound cross-session messages.

The CLI (measured on 2.1.273, still offered on the pinned 2.1.293) offers
``SendMessage``, ``ListAgents`` and ``PushNotification`` by default, and none
of them asks for permission, so the
fail-closed ``can_use_tool`` never sees a call. ``SendMessage`` reaches the
other Claude Code sessions running as the same OS user on the machine, which
in the Casa container is every agent. Only ``disallowed_tools`` (the CLI's
``--disallowedTools``) removes them, and only ``crossSessionInbound: "refuse"``
(passed through ``--settings``) stops a session accepting a message.

Each test builds options through a real production builder and asserts the
result. The sweep at the end pins that no ``ClaudeAgentOptions`` construction
exists outside the builders exercised here without passing both keywords, so
a new builder cannot silently start a session with the defaults.
"""
from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
APP_ROOT = REPO / "casa" / "rootfs" / "opt" / "casa"

# Literal on purpose: the test must not read the names from the constant it
# is pinning, or emptying the constant would pass.
NAMES = {"SendMessage", "ListAgents", "PushNotification"}
# #1258: the pinned CLI's self-scheduling built-ins, typed from the survey's
# measurement of the 2.1.273 bundle, never copied from the constant: the CLI
# accepts a misspelt name silently, so a copied typo would pass here and leave
# the real tool on every surface.
SELF_SCHEDULING = {"ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
                   "Monitor", "RemoteTrigger"}
# #1353: CLI 2.1.293's permission-free upload of ./ONBOARDING.md to a claude.ai
# share link, typed from its measured tool surface, not from the constant.
OUTBOUND_SHARE = {"ShareOnboardingGuide"}


def _assert_locked(opts) -> None:
    missing = NAMES - set(opts.disallowed_tools or ())
    assert not missing, f"cross-session tools not denied: {sorted(missing)}"
    missing = SELF_SCHEDULING - set(opts.disallowed_tools or ())
    assert not missing, f"self-scheduling tools not denied: {sorted(missing)}"
    missing = OUTBOUND_SHARE - set(opts.disallowed_tools or ())
    assert not missing, f"outbound share tools not denied: {sorted(missing)}"
    assert opts.settings is not None, "no --settings: inbound not refused"
    assert json.loads(opts.settings).get("crossSessionInbound") == "refuse"


def _argv(opts) -> list[str]:
    """The argv the pinned SDK transport hands the CLI for *opts*."""
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )
    return SubprocessCLITransport(prompt="x", options=opts)._build_command()


def _assert_loads_no_settings_source(opts) -> None:
    """#1181: a utility one-shot loads no settings source at all and passes no
    ``cleanupPeriodDays``. The pinned CLI runs its own age sweep over the
    whole projects root only when user settings are loaded or a loaded source
    sets ``cleanupPeriodDays`` — and that sweep would delete a resident
    transcript INV-MEM-017 holds. A project source is no better: its file
    could carry the key."""
    assert opts.setting_sources == [], opts.setting_sources
    assert sum(k == "cleanupPeriodDays" for k in json.loads(opts.settings)) == 0
    argv = _argv(opts)
    assert [a for a in argv if a.startswith("--setting-sources=")] == [
        "--setting-sources="]
    assert argv.count("--settings") == 1
    flag = json.loads(argv[argv.index("--settings") + 1])
    assert sum(k == "cleanupPeriodDays" for k in flag) == 0


def _assert_no_user_source(opts) -> None:
    """INV-MEM-021 on the project-sourced launches: their sources are pinned,
    never the user source, and their ``--settings`` flag carries no
    ``cleanupPeriodDays``."""
    assert opts.setting_sources is not None
    assert "user" not in opts.setting_sources
    assert sum(k == "cleanupPeriodDays" for k in json.loads(opts.settings)) == 0


def _assert_persists_no_session(opts) -> None:
    """#1168: a utility one-shot is never resumed and nothing names its
    session afterwards, so it must not write a transcript at all."""
    assert _argv(opts).count("--no-session-persistence") == 1


def test_shared_constants_name_the_three_tools_and_refuse():
    from claude_runtime import (
        CROSS_SESSION_INBOUND,
        CROSS_SESSION_TOOLS,
        cli_session_settings,
        with_cross_session_tools_denied,
    )

    assert set(CROSS_SESSION_TOOLS) == NAMES
    assert CROSS_SESSION_INBOUND == "refuse"
    # A caller's own value never relaxes the refusal.
    assert json.loads(cli_session_settings(
        {"crossSessionInbound": "accept", "x": 1})) == {
            "crossSessionInbound": "refuse", "x": 1}
    assert with_cross_session_tools_denied(["Bash", "SendMessage"]) == [
        "Bash", "SendMessage", "ListAgents", "PushNotification",
        "ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "Monitor",
        "RemoteTrigger", "ShareOnboardingGuide"]


async def test_resident_options(tmp_path, monkeypatch):
    """A resident whose runtime.yaml denies nothing still denies the three."""
    from agent import Agent
    from channels import ChannelManager
    from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
    from mcp_registry import McpServerRegistry
    from plugin_registry import ResolutionResult
    from session_registry import SessionRegistry
    import plugin_registry

    monkeypatch.setattr(plugin_registry, "resolve_for",
                        lambda _t: ResolutionResult(registry_valid=True))
    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT, role="butler",
        model="claude-haiku-4-5", system_prompt="You are Tina.",
        character=CharacterConfig(name="Tina"),
        tools=ToolsConfig(allowed=["Read"], disallowed=[]),
        memory=MemoryConfig(token_budget=800, read_strategy="cached"),
    )
    memory = AsyncMock()
    memory.profile.return_value = ""
    memory.recall.return_value = ""
    resident = Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(), channel_manager=ChannelManager(),
        semantic_memory=memory,
    )
    for channel in ("telegram", "voice"):
        opts = await resident._build_options(
            channel=channel, channel_key=f"{channel}-k", is_fresh=True,
            resume_sid=None, user_text="hello")
        _assert_locked(opts)
        _assert_no_user_source(opts)


@pytest.mark.parametrize("delivers_to_operator", [False, True])
def test_restricted_webhook_options(delivers_to_operator):
    from agent import build_restricted_webhook_options

    opts = build_restricted_webhook_options(
        model="m", role="assistant", system_prompt="p", max_turns=5,
        agent_home="/tmp", resume_sid=None,
        delivers_to_operator=delivers_to_operator)
    _assert_locked(opts)
    _assert_no_user_source(opts)
    # The restricted runtime's own settings survive the merge.
    assert json.loads(opts.settings)["disableAllHooks"] is True


def _specialist_cfg():
    from config import HooksConfig

    return SimpleNamespace(
        role="finance", model="claude-haiku-4-5", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=["Read"], disallowed=[],
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=HooksConfig(), cwd="",
    )


def _executor_defn():
    return SimpleNamespace(
        hooks_path=None, mcp_server_names=[], tools_allowed=["Read"],
        model="claude-sonnet-4-6", permission_mode="acceptEdits",
        max_turns=None, tools_disallowed=[], driver="in_casa",
    )


def test_specialist_and_executor_options(monkeypatch):
    from plugin_registry import ResolutionResult
    import tools as tools_mod

    monkeypatch.setattr(tools_mod, "_mcp_registry", None)
    for opts in (
        tools_mod._build_specialist_options(
            _specialist_cfg(), resolution=ResolutionResult(registry_valid=True)),
        tools_mod._build_executor_options(
            _executor_defn(), executor_type="configurator",
            resolution=ResolutionResult(registry_valid=True)),
    ):
        _assert_locked(opts)
        _assert_no_user_source(opts)
        # The per-delegation session id is chosen by the delegation runner,
        # never here: in_casa engagements launch and resume through these.
        assert opts.session_id is None


@pytest.mark.parametrize("kind", ["specialist", "executor"])
def test_engagement_resume_options(monkeypatch, kind):
    import plugin_registry
    import tools as tools_mod

    monkeypatch.setattr(
        tools_mod.plugin_registry, "resolve_for",
        lambda t: plugin_registry.ResolutionResult(registry_valid=True))
    monkeypatch.setattr("hooks.resolve_hooks", lambda *a, **kw: {})
    spec_reg, exec_reg = MagicMock(), MagicMock()
    spec_reg.get = MagicMock(return_value=_specialist_cfg())
    exec_reg.get = MagicMock(return_value=_executor_defn())
    tools_mod.init_tools(
        channel_manager=MagicMock(), bus=MagicMock(),
        specialist_registry=spec_reg, mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        executor_registry=exec_reg,
    )
    role = "finance" if kind == "specialist" else "configurator"
    opts = tools_mod.build_engagement_resume_options(
        SimpleNamespace(kind=kind, role_or_type=role), "sess-1")
    assert opts.resume == "sess-1"
    assert opts.session_id is None
    _assert_locked(opts)
    _assert_no_user_source(opts)


class _CapturingClient:
    captured: dict = {}

    def __init__(self, options):
        type(self).captured["options"] = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        pass

    async def receive_response(self):
        if False:
            yield None


async def _synthesis_launches(monkeypatch) -> list:
    import tools

    launches = []

    class Client(_CapturingClient):
        def __init__(self, options):
            launches.append(options)

    monkeypatch.setattr(tools, "ClaudeSDKClient", Client)
    monkeypatch.setattr(tools.sdk_logging, "with_stderr_callback",
                        lambda options, engagement_id=None: options)
    await tools._synthesize_answer("q?", "ctx", max_tokens=50)
    return launches


async def _observer_launches(monkeypatch) -> list:
    import claude_agent_sdk
    import sdk_logging
    from observer import Observer

    launches = []
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = claude_agent_sdk.ClaudeAgentOptions
    fake.TextBlock = claude_agent_sdk.TextBlock
    fake.AssistantMessage = claude_agent_sdk.AssistantMessage

    class Client(_CapturingClient):
        def __init__(self, options):
            launches.append(options)

    fake.ClaudeSDKClient = Client
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    monkeypatch.setattr(sdk_logging, "with_stderr_callback",
                        lambda options, engagement_id=None: options)
    obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                   model_name="haiku")
    await obs._decide_interjection(
        "warn", {}, MagicMock(id="e-1", task="t", role_or_type="x"))
    return launches


async def _classifier_launches(monkeypatch) -> list:
    import claude_agent_sdk
    import tier_classifier

    launches = []
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = claude_agent_sdk.ClaudeAgentOptions
    fake.AssistantMessage = claude_agent_sdk.AssistantMessage

    async def query(*, prompt, options):
        launches.append(options)
        if False:
            yield None

    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    await tier_classifier.classify_tier("some fact")
    return launches


# Every launch each one-shot makes for one call: the classifier re-asks an
# empty reply once (#508), so it launches twice and both launches count.
_ONE_SHOTS = {
    "synthesis": (_synthesis_launches, 1),
    "observer": (_observer_launches, 1),
    "classifier": (_classifier_launches, 2),
}


async def test_query_engager_synthesis_options(monkeypatch):
    launches = await _synthesis_launches(monkeypatch)
    assert len(launches) == 1
    _assert_locked(launches[0])
    _assert_loads_no_settings_source(launches[0])


async def test_observer_decider_options(monkeypatch):
    launches = await _observer_launches(monkeypatch)
    assert len(launches) == 1
    _assert_locked(launches[0])
    _assert_loads_no_settings_source(launches[0])


async def test_tier_classifier_options(monkeypatch):
    launches = await _classifier_launches(monkeypatch)
    assert len(launches) == 2
    for opts in launches:
        _assert_locked(opts)
        _assert_loads_no_settings_source(opts)


@pytest.mark.parametrize("one_shot", sorted(_ONE_SHOTS))
async def test_one_shot_launches_persist_no_session(monkeypatch, one_shot):
    capture, expected = _ONE_SHOTS[one_shot]
    launches = await capture(monkeypatch)
    assert len(launches) == expected
    for opts in launches:
        _assert_persists_no_session(opts)


def test_claude_code_driver_settings(tmp_path):
    """The claude_code driver starts its own CLI from a rendered
    .claude/settings.json: deny rules plus a project-level refuse (the
    strictest value, so no other settings source relaxes it)."""
    from config import ExecutorDefinition
    from drivers.workspace import _build_cc_permissions, render_workspace_template

    defn = ExecutorDefinition(
        role_artifact=STUB_ROLE_ARTIFACT, type="test-fixture",
        description="test fixture twenty-character description here",
        model="sonnet", driver="claude_code", tools_allowed=["Read", "Bash"],
        permission_mode="acceptEdits",
    )
    assert NAMES | SELF_SCHEDULING <= set(_build_cc_permissions(defn)["deny"])

    tmpl = tmp_path / "tmpl"
    (tmpl / ".claude").mkdir(parents=True)
    (tmpl / "CLAUDE.md.tmpl").write_text("{task}", encoding="utf-8")
    dest = tmp_path / "eng"
    render_workspace_template(
        template_root=tmpl, dest=dest, defn=defn, executor_type="test-fixture",
        task="t", context="c", world_state_summary="", hooks_yaml_data={})
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert NAMES | SELF_SCHEDULING <= set(settings["permissions"]["deny"])
    assert settings["crossSessionInbound"] == "refuse"


def test_pinned_sdk_forwards_the_denial_and_the_refusal_to_the_cli():
    """The real pinned SDK transport turns the options into the CLI flags
    the CLI enforces: ``--disallowedTools`` and ``--settings``."""
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )
    from agent import build_restricted_webhook_options

    opts = build_restricted_webhook_options(
        model="m", role="assistant", system_prompt="p", max_turns=5,
        agent_home="/tmp", resume_sid=None)
    cmd = SubprocessCLITransport(prompt="x", options=opts)._build_command()
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert NAMES | SELF_SCHEDULING <= set(denied)
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert settings["crossSessionInbound"] == "refuse"



# Every production ClaudeAgentOptions construction, by file and enclosing
# function, and the real-builder test above that exercises it. A new site, a
# moved one or a removed one fails the sweep until it is listed here — and
# listing it means naming the test that covers it.
EXPECTED_SITES = {
    ("agent.py", "build_restricted_webhook_options"):
        "test_restricted_webhook_options",
    ("agent.py", "Agent._build_options"): "test_resident_options",
    ("tools.py", "_build_specialist_options"):
        "test_specialist_and_executor_options",
    ("tools.py", "_build_plugin_job_options"):
        "test_plugin_job_launch.py::test_worker_options_record_and_topic",
    ("tools.py", "_build_executor_options"):
        "test_specialist_and_executor_options",
    ("tools.py", "_synthesize_answer"): "test_query_engager_synthesis_options",
    ("observer.py", "Observer._decide_interjection"):
        "test_observer_decider_options",
    ("tier_classifier.py", "classify_tier"): "test_tier_classifier_options",
}

# The calls whose result carries the denial: the helper itself, and the
# sub-agent clamp, which the sweep separately requires to return the helper.
_DENY_CALLS = {"with_cross_session_tools_denied", "_with_subagent_spawn_disallowed"}


def _call_name(node) -> str | None:
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
    return None


def _is_options_ctor(node) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) == "ClaudeAgentOptions"


def _sites(tree):
    """Yield (qualified enclosing function, function node, ctor call)."""
    def walk(node, scope, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, scope + [child.name], child)
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, scope + [child.name], fn)
            else:
                if _is_options_ctor(child):
                    yield ".".join(scope), fn, child
                yield from walk(child, scope, fn)
    yield from walk(tree, [], None)


def _denial_problem(fn, expr) -> str | None:
    """None when *expr* (a site's disallowed_tools value) is built by a deny
    call, directly or through a local name every binding of which is one."""
    if _call_name(expr) in _DENY_CALLS:
        return None
    if isinstance(expr, ast.Name) and fn is not None:
        bindings = []
        for node in ast.walk(fn):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == expr.id:
                    bindings.append(
                        node.value if not isinstance(node, ast.AugAssign)
                        else None)
        if bindings and all(_call_name(b) in _DENY_CALLS for b in bindings):
            return None
        return f"name {expr.id!r} has a binding that is not a deny call"
    return f"expression {ast.unparse(expr)!r} is not a deny call"


def test_every_production_claude_options_passes_denial_and_settings():
    """The exact set of ``ClaudeAgentOptions`` construction sites is pinned,
    and at each one ``disallowed_tools`` is built by the deny helper (or the
    sub-agent clamp that returns it) and ``settings`` by
    ``cli_session_settings``. Presence of the keywords is not enough:
    ``ClaudeAgentOptions(disallowed_tools=[], settings=...)`` fails here."""
    observed: dict[tuple[str, str], int] = {}
    problems: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = str(path.relative_to(APP_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope, fn, call in _sites(tree):
            key = (rel, scope)
            observed[key] = observed.get(key, 0) + 1
            kws = {k.arg: k.value for k in call.keywords}
            where = f"{rel}::{scope} (line {call.lineno})"
            if "disallowed_tools" not in kws:
                problems.append(f"{where}: no disallowed_tools")
            else:
                why = _denial_problem(fn, kws["disallowed_tools"])
                if why:
                    problems.append(f"{where}: {why}")
            if _call_name(kws.get("settings")) != "cli_session_settings":
                problems.append(f"{where}: settings is not cli_session_settings(...)")

    assert observed == {k: 1 for k in EXPECTED_SITES}, (
        "ClaudeAgentOptions construction sites changed — new, moved, "
        f"duplicated or removed: {sorted(set(observed) ^ set(EXPECTED_SITES))}"
        f" counts={observed}")
    assert not problems, problems

    # The sub-agent clamp counts as a deny call only because every return of
    # it passes through the helper.
    tools_tree = ast.parse((APP_ROOT / "tools.py").read_text(encoding="utf-8"))
    clamp = next(n for n in ast.walk(tools_tree)
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "_with_subagent_spawn_disallowed")
    returns = [n for n in ast.walk(clamp) if isinstance(n, ast.Return)]
    assert returns and all(
        _call_name(r.value) == "with_cross_session_tools_denied"
        for r in returns), "the sub-agent clamp no longer returns the helper"
