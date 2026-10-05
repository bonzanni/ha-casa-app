"""#1282 red case: a tap's pinned one-call turn is given Casa's instruction
only — no desk exchange, no recalled memory, no frame calling the task a
message from the operator — and its instruction names the plugin as the judge
of whether the tap still applies.

Driven through the REAL ``specialist_desk.handle_tap`` (the only setter of the
``stored_call`` marker and of ``tools._pinned_run``) over the REAL
``tools._run_delegated_agent`` and the real ``PinnedRun`` controller, with the
recording SDK client of ``test_pinned_wiring.py``. Each absence is its own
test, so a mutation that restores one input turns exactly its own case red.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

import agent as agent_mod
import pinned_run as pr
import specialist_desk as sd
import tools as tools_mod
from plugin_grants import ProfilePlan
from plugin_registry import ResolutionResult, ResolvedPlugin
from test_delegate_to_agent import _specialist_cfg
from test_delegated_transcript_delete import _Harness
from test_desk_tap import APPLY, ART, CANON, _map, _tap, _tool, env  # noqa: F401
from test_pinned_wiring import _install, bound  # noqa: F401

pytestmark = pytest.mark.asyncio

# captured at import, before any fixture replaces it with the tap tests' fake
REAL_RUNNER = tools_mod._run_delegated_agent

DESK_SENTINEL = "DESK-LOG-SENTINEL unpaired 3 payments"
MEMORY_SENTINEL = "MEMORY-DIGEST-SENTINEL"
OPERATOR_FRAME = "is a message from the operator"


async def _pinned_prompt(env, tmp_path, monkeypatch):
    """Run one committed tap end to end; return the prompts the pinned turn
    sent and the recall calls it made."""
    cfg = _specialist_cfg()
    cfg.kind = "specialist"
    cfg.cwd = str(tmp_path / "agent-home" / "finance")
    os.makedirs(cfg.cwd)
    cfg.memory.token_budget = 4000                      # recall is reachable
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setitem(tools_mod._agent_role_map, "finance", cfg)
    plugin_dir = tmp_path / "probe"
    plugin_dir.mkdir()
    (plugin_dir / ".mcp.json").write_text('{"mcpServers": {"probe": {"command": "serve"}}}')
    rp = ResolvedPlugin(name="probe", artifact_id=ART, path=str(plugin_dir), version="1",
                        manifest={"name": "probe"}, manifest_name="probe")
    env.build = pr.BuildInput(
        cfg=cfg, resolution=ResolutionResult(registry_valid=True, plugins=[rp]), withheld=(),
        protected={}, contract_map=_map([_tool("apply")]), plan=ProfilePlan(loaded=("probe",)),
        target="specialist:finance")
    # the desk already holds an applied exchange on this proposal family
    env.desk.append("operator", "[tapped: Apply]", now=990.0)
    env.desk.append("specialist", DESK_SENTINEL, now=990.0)

    recalls: list[dict] = []

    async def fake_recall(sem, **kw):
        recalls.append(kw)
        return MEMORY_SENTINEL
    monkeypatch.setattr(tools_mod, "delegated_recall", fake_recall)

    async def fake_retain(sem, **kw):
        return None
    monkeypatch.setattr(tools_mod, "retain_delegated", fake_retain)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", object(), raising=False)
    monkeypatch.setattr(tools_mod, "_run_delegated_agent", REAL_RUNNER)

    prompts: list[str] = []
    cli = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"])
    try:
        h = _Harness()
        _install(monkeypatch, h, cli_pid=cli.pid)
        base = tools_mod.ClaudeSDKClient

        class _Recording(base):
            async def query(self, prompt):
                prompts.append(prompt)
                return await super().query(prompt)
        monkeypatch.setattr(tools_mod, "ClaudeSDKClient", _Recording)
        await _tap(env)
    finally:
        try:
            os.kill(cli.pid, 9)
        except ProcessLookupError:
            pass
        cli.wait(timeout=5)
    assert len(prompts) == 1, (prompts, env.channel.marks, env.channel.notices)  # one query, no re-prompt
    return prompts[0], recalls


async def test_the_pinned_prompt_carries_no_desk_exchange(env, bound, tmp_path, monkeypatch):
    prompt, _ = await _pinned_prompt(env, tmp_path, monkeypatch)
    assert DESK_SENTINEL not in prompt, prompt
    assert "[tapped: Apply]" not in prompt, prompt
    assert "<desk>" not in prompt, prompt


async def test_the_pinned_prompt_carries_no_recalled_memory(env, bound, tmp_path, monkeypatch):
    prompt, _ = await _pinned_prompt(env, tmp_path, monkeypatch)
    assert "<memory_context" not in prompt, prompt
    assert MEMORY_SENTINEL not in prompt, prompt


async def test_the_pinned_prompt_carries_no_operator_message_frame(env, bound, tmp_path, monkeypatch):
    prompt, _ = await _pinned_prompt(env, tmp_path, monkeypatch)
    assert OPERATOR_FRAME not in prompt, prompt


async def test_the_pinned_turn_recalls_nothing(env, bound, tmp_path, monkeypatch):
    _, recalls = await _pinned_prompt(env, tmp_path, monkeypatch)
    assert recalls == []


async def test_the_pinned_prompt_names_the_call_and_the_plugin_as_judge(env, bound, tmp_path,
                                                                       monkeypatch):
    prompt, _ = await _pinned_prompt(env, tmp_path, monkeypatch)
    assert f"`{APPLY}`" in prompt and CANON in prompt
    assert prompt.count("[casa stored call]") == 1
    assert "the plugin decides whether the tap still applies" in prompt, prompt
