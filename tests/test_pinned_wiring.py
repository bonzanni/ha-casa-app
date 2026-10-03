"""S5 §1.7 (plan): the tools wiring of a pinned one-call turn — the options
builder consumes the build input the tap CAPTURED under the desk lock (no
second resolve, no filter, no rebuild, no plan) and puts the pin matcher
FIRST; the capture withholds exactly what the builder withholds; the
delegated runner obtains its client from the controller and never exits it,
retains nothing and leaves the transcript to the controller
(INV-PROP-001, INV-PROP-002).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import types

import claude_agent_sdk
import pytest

import agent as agent_mod
import pinned_run as pr
import plugin_grants
import tools as tools_mod
from plugin_grants import ProfilePlan, protected_map, result_contract_map
from plugin_registry import ResolutionResult, ResolvedPlugin
from test_delegate_to_agent import _specialist_cfg, _with_origin
from test_delegated_transcript_delete import _Harness, _project, _write_session

MISSING = "S5_WIRING_MISSING_VAR"


def _plugin(tmp_path, name, env=None):
    d = tmp_path / name
    d.mkdir()
    server = {"command": "serve"}
    if env:
        server["env"] = env
    (d / ".mcp.json").write_text(json.dumps({"mcpServers": {name: server}}))
    return ResolvedPlugin(name=name, artifact_id=f"art-{name}", path=str(d), version="1",
                          manifest={"name": name}, manifest_name=name)


def _raise(*a, **k):
    raise AssertionError("derived again on a pinned turn")


@pytest.fixture
def bound(monkeypatch):
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_specialist_telemetry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_channel_manager", None, raising=False)


def _build_input(cfg, res):
    filtered, withheld = plugin_grants.withhold_env_unresolved(res, context="t")
    return pr.BuildInput(cfg=cfg, resolution=filtered, withheld=tuple(withheld),
                         protected=protected_map(filtered), contract_map=result_contract_map(filtered),
                         plan=ProfilePlan(loaded=tuple(rp.name for rp in filtered.plugins)),
                         target="specialist:finance")


def _owner(build, **over):
    kw = dict(run_id="run-w", runtime_name="mcp__plugin_plain_plain__x", canonical="{}", label="L",
              build_input=build)
    kw.update(over)
    return pr.PinnedRun(**kw)


def _pinned(owner, fn, *a, **k):
    tok = tools_mod._pinned_run.set(owner)
    try:
        return fn(*a, **k)
    finally:
        tools_mod._pinned_run.reset(tok)


# --- the builder ------------------------------------------------------------------------

def test_the_pinned_builder_consumes_the_captured_input_and_derives_nothing_again(tmp_path, bound, monkeypatch):
    cfg = _specialist_cfg()
    res = ResolutionResult(registry_valid=True, plugins=[_plugin(tmp_path, "plain")])
    build = _build_input(cfg, res)
    owner = _owner(build)
    for name in ("withhold_env_unresolved", "protected_map", "result_contract_map", "profile_plan"):
        monkeypatch.setattr(plugin_grants, name, _raise)
    monkeypatch.setattr(tools_mod, "_delegated_resolution", _raise)
    # the unpinned positive control: the builder performs these derivations itself
    with pytest.raises(AssertionError):
        tools_mod._build_specialist_options(cfg, resolution=res)
    opts = _pinned(owner, tools_mod._build_specialist_options, cfg)
    assert opts.plugins == [{"type": "local", "path": str(tmp_path / "plain")}]
    first = opts.hooks["PreToolUse"][0]
    assert first.matcher is None and first.hooks == [owner.pin_hook]
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        bound_hooks = [h for m in opts.hooks[event] for h in m.hooks
                       if getattr(h, "_casa_pinned", None) is owner]
        assert len(bound_hooks) == 1, event


def test_the_pin_matcher_is_first_even_for_a_session_without_plugins(bound):
    cfg = _specialist_cfg()
    build = _build_input(cfg, ResolutionResult(registry_valid=True, plugins=[]))
    owner = _owner(build)
    opts = _pinned(owner, tools_mod._build_specialist_options, cfg)
    assert opts.hooks["PreToolUse"][0].hooks == [owner.pin_hook]
    plain = tools_mod._build_specialist_options(cfg, resolution=build.resolution)
    assert all(owner.pin_hook not in m.hooks for m in plain.hooks.get("PreToolUse", []))


# --- the capture ---------------------------------------------------------------------------

def test_the_capture_withholds_exactly_what_the_builder_withholds(tmp_path, bound, monkeypatch):
    monkeypatch.delenv(MISSING, raising=False)
    cfg = _specialist_cfg()
    res = ResolutionResult(registry_valid=True, plugins=[
        _plugin(tmp_path, "needy", env={"KEY": "${" + MISSING + "}"}), _plugin(tmp_path, "plain")])
    monkeypatch.setattr(tools_mod, "_delegated_resolution", lambda c: res)
    build = tools_mod._capture_build_input(cfg)
    assert [rp.name for rp in build.resolution.plugins] == ["plain"]
    assert [(rp.name, vars_) for rp, vars_ in build.withheld] == [("needy", [MISSING])]
    assert set(build.contract_map.plugins) == {"plain"}
    assert build.target == "specialist:finance" and build.plan.loaded == ("plain",)
    assert build.cfg is cfg and build.protected == {}
    # parity: the ordinary builder on the raw resolution and the pinned builder on the
    # capture launch the same plugins (the unfiltered resolution would load both)
    plain = tools_mod._build_specialist_options(cfg, resolution=res)
    pinned = _pinned(_owner(build), tools_mod._build_specialist_options, cfg)
    assert plain.plugins == pinned.plugins == [{"type": "local", "path": str(tmp_path / "plain")}]


# --- the runner ------------------------------------------------------------------------------

def _origin(**over):
    base = {"role": "assistant", "execution_role": "finance", "channel": "telegram",
            "chat_id": "42", "cid": "c1", "user_text": "[tapped: Yes]", "_operator_turn": True}
    base.update(over)
    return base


@pytest.fixture
def runner_env(tmp_path, monkeypatch, bound):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    cwd = tmp_path / "agent-home" / "finance"
    cwd.mkdir(parents=True)
    cfg = _specialist_cfg()
    cfg.cwd = str(cwd)
    cfg.memory.token_budget = 4000                       # retention is reachable
    retains = []

    async def fake_retain(sem, **kw):
        retains.append(kw)
    monkeypatch.setattr(tools_mod, "retain_delegated", fake_retain)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", object(), raising=False)
    return types.SimpleNamespace(cfg=cfg, retains=retains, monkeypatch=monkeypatch)


def _install(monkeypatch, h, *, cli_pid=None):
    base = h.client_cls()

    class _Process:                                      # hashable, as the SDK's is
        pid, returncode = cli_pid, None

    class _Client(base):
        def __init__(self, options):
            super().__init__(options)
            self._transport = types.SimpleNamespace(_process=_Process())

        async def __aexit__(self, *exc):
            if cli_pid is not None:
                try:
                    os.kill(cli_pid, 9)                  # the SDK's disconnect ends the CLI
                except ProcessLookupError:
                    pass
            return await super().__aexit__(*exc)

    monkeypatch.setattr(tools_mod, "ClaudeSDKClient", _Client)
    monkeypatch.setattr(claude_agent_sdk, "delete_session", h.recording_delete())


async def test_the_pinned_runner_hands_its_client_to_the_controller_and_retains_nothing(runner_env):
    cfg, monkeypatch = runner_env.cfg, runner_env.monkeypatch
    cli = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])
    try:
        h = _Harness()
        _install(monkeypatch, h, cli_pid=cli.pid)
        build = _build_input(cfg, ResolutionResult(registry_valid=True, plugins=[]))
        owner = _owner(build)
        tok = tools_mod._pinned_run.set(owner)
        try:
            out = await _with_origin(
                tools_mod._run_delegated_agent(cfg, "task", "", resolution=build.resolution),
                _origin(stored_call={"run_id": "run-w", "runtime_name": "x", "canonical": "{}",
                                     "label": "Yes"}))
        finally:
            tools_mod._pinned_run.reset(tok)
        assert out.text == "answer"
        assert len(h.launches) == 1 and h.exits_done == 0        # entered by the controller, never exited here
        assert h.launches[0].hooks["PreToolUse"][0].hooks == [owner.pin_hook]
        assert owner.transcript == (h.launches[0].session_id, cfg.cwd)
        assert h.deletes == []                                   # the controller deletes after termination
        assert owner.pinned_pids() == {cli.pid} and owner.alive()
        for _ in range(3):
            await asyncio.sleep(0)
        assert runner_env.retains == []                          # a stored-call turn retains nothing
        # normal completion: the controller's orderly close, confirmed by pidfd, no SDK await
        assert await owner.finish() is True
        assert h.exits_done == 1 and owner.alive() is False and owner.sealed
        assert len(h.written) == 1                               # the CLI flushed its transcript on exit…
        assert h.deletes == []                                   # …and the desk deletes it, not the runner
    finally:
        try:
            os.kill(cli.pid, 9)
        except ProcessLookupError:
            pass
        cli.wait(timeout=5)


async def test_the_unpinned_runner_still_exits_its_client_retains_and_deletes(runner_env):
    cfg, monkeypatch = runner_env.cfg, runner_env.monkeypatch
    h = _Harness()
    _install(monkeypatch, h)
    out = await _with_origin(tools_mod._run_delegated_agent(cfg, "task", "", resolution=None),
                             _origin())
    assert out.text == "answer"
    assert h.exits_done == 1 and len(h.deletes) == 1
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(runner_env.retains) == 1                          # the positive control retains


# --- #1220: the stored tool is in the pinned turn's first request -----------------------------

def test_pinned_builder_disallows_toolsearch_once_without_changing_ordinary_build(bound):
    """#1220: with ToolSearch on the surface the pinned CLI defers every MCP tool,
    and the pin denies the one ToolSearch that would load the stored tool, so
    the turn ends ``no_call``. The pinned build takes ToolSearch off the
    surface; an ordinary build of the same config keeps it (tools.py's
    ``_SUBAGENT_SPAWN_TOOLS`` ruling)."""
    cfg = _specialist_cfg()
    build = _build_input(cfg, ResolutionResult(registry_valid=True, plugins=[]))
    pinned = _pinned(_owner(build), tools_mod._build_specialist_options, cfg)
    ordinary = tools_mod._build_specialist_options(cfg, resolution=build.resolution)
    assert pinned.disallowed_tools.count("ToolSearch") == 1
    assert ordinary.disallowed_tools.count("ToolSearch") == 0
    # a config that already denies it: still exactly once on both
    cfg.tools.disallowed = [*cfg.tools.disallowed, "ToolSearch"]
    pinned = _pinned(_owner(build), tools_mod._build_specialist_options, cfg)
    ordinary = tools_mod._build_specialist_options(cfg, resolution=build.resolution)
    assert pinned.disallowed_tools.count("ToolSearch") == 1
    assert ordinary.disallowed_tools.count("ToolSearch") == 1


def _install_status(monkeypatch, h, statuses, order, *, cli_pid=None):
    """``_install`` plus an MCP status sequence and an order log of the
    client calls the runner makes."""
    _install(monkeypatch, h, cli_pid=cli_pid)
    base = tools_mod.ClaudeSDKClient
    seq = list(statuses)

    class _Client(base):
        async def __aenter__(self):
            order.append("enter")
            return await super().__aenter__()

        async def get_mcp_status(self):
            order.append("status")
            return seq.pop(0) if len(seq) > 1 else seq[0]

        async def query(self, prompt):
            order.append("query")
            return await super().query(prompt)

        async def __aexit__(self, *exc):
            order.append("exit")
            return await super().__aexit__(*exc)

    monkeypatch.setattr(tools_mod, "ClaudeSDKClient", _Client)


_PENDING = {"mcpServers": [{"name": "a", "status": "connected"},
                           {"name": "b", "status": "pending"}]}
_SETTLED = {"mcpServers": [{"name": "a", "status": "connected"},
                           {"name": "b", "status": "connected"}]}


async def test_pinned_runner_queries_once_after_all_servers_settle(runner_env):
    """#1220: plugin MCP servers connect in the background, so a server still
    starting contributes no tool to the pinned turn's first and only request.
    The pinned runner asks for the MCP status and sends its one query only
    when no server is still pending — the second server here, so a check of
    the first entry alone would query early. An ordinary run never asks."""
    cfg, monkeypatch = runner_env.cfg, runner_env.monkeypatch
    cli = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])
    try:
        h = _Harness()
        order: list[str] = []
        _install_status(monkeypatch, h, [_PENDING, _PENDING, _SETTLED], order, cli_pid=cli.pid)
        build = _build_input(cfg, ResolutionResult(registry_valid=True, plugins=[]))
        owner = _owner(build)
        tok = tools_mod._pinned_run.set(owner)
        try:
            out = await _with_origin(
                tools_mod._run_delegated_agent(cfg, "task", "", resolution=build.resolution),
                _origin(stored_call={"run_id": "run-w", "runtime_name": "x", "canonical": "{}",
                                     "label": "Yes"}))
        finally:
            tools_mod._pinned_run.reset(tok)
        assert out.text == "answer"
        assert len(h.launches) == 1
        assert order == ["enter", "status", "status", "status", "query"]
        assert await owner.finish() is True
    finally:
        try:
            os.kill(cli.pid, 9)
        except ProcessLookupError:
            pass
        cli.wait(timeout=5)

    # the ordinary control: no status request, one query
    h = _Harness()
    order = []
    _install_status(monkeypatch, h, [_PENDING], order)
    out = await _with_origin(tools_mod._run_delegated_agent(cfg, "task", "", resolution=None),
                             _origin())
    assert out.text == "answer"
    assert len(h.launches) == 1
    assert order == ["enter", "query", "exit"]
