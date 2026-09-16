"""Guard: the global claude CLI install must be version-pinned and the
claude-agent-sdk pin must be exact + match the intended version (spec §6).
Pure-unit static parse — no docker, no network."""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
APP_ROOT = REPO / "casa" / "rootfs" / "opt" / "casa"
DOCKERFILE = REPO / "casa" / "Dockerfile"
TEST_DOCKERFILE = REPO / "test-local" / "Dockerfile.test"
REQUIREMENTS = REPO / "casa" / "requirements.txt"

SDK_VERSION = "0.2.153"


def test_global_claude_cli_is_pinned() -> None:
    from claude_runtime import CLAUDE_CLI_VERSION

    text = DOCKERFILE.read_text(encoding="utf-8")
    assert not re.search(
        r"@anthropic-ai/claude-code(?:\s|\\|$)(?!@)", text
    ), "global claude CLI install is unpinned"
    assert f"@anthropic-ai/claude-code@{CLAUDE_CLI_VERSION}" in text, (
        "Dockerfile must pin "
        f"@anthropic-ai/claude-code@{CLAUDE_CLI_VERSION}"
    )


def test_e2e_image_claude_cli_pin_matches_production() -> None:
    from claude_runtime import CLAUDE_CLI_VERSION

    text = TEST_DOCKERFILE.read_text(encoding="utf-8")
    assert f"@anthropic-ai/claude-code@{CLAUDE_CLI_VERSION}" in text, (
        "Dockerfile.test must use the production Claude CLI pin"
    )


def test_sdk_pin_is_exact_and_current() -> None:
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    sdk_lines = [line for line in lines if line.strip().startswith("claude-agent-sdk")]
    assert sdk_lines, "claude-agent-sdk not pinned in requirements.txt"
    assert sdk_lines[0].strip() == f"claude-agent-sdk=={SDK_VERSION}", (
        f"expected claude-agent-sdk=={SDK_VERSION}, got {sdk_lines[0].strip()!r}"
    )


def test_every_production_claude_options_uses_shared_cli_path() -> None:
    """Every in-process SDK client must launch the boot-verified CLI."""
    observed: dict[str, str | None] = {}
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            constructor = node.func
            is_options = (
                isinstance(constructor, ast.Name)
                and constructor.id == "ClaudeAgentOptions"
            ) or (
                isinstance(constructor, ast.Attribute)
                and constructor.attr == "ClaudeAgentOptions"
            )
            if not is_options:
                continue
            cli_kwarg = next(
                (kw for kw in node.keywords if kw.arg == "cli_path"),
                None,
            )
            location = f"{path.relative_to(APP_ROOT)}:{node.lineno}"
            observed[location] = (
                ast.unparse(cli_kwarg.value) if cli_kwarg is not None else None
            )

    assert observed, "no production ClaudeAgentOptions constructions found"
    unpinned = {
        location: value
        for location, value in observed.items()
        if value != "CLAUDE_CLI_PATH"
    }
    assert not unpinned, (
        "every production ClaudeAgentOptions construction must pass the "
        f"shared CLAUDE_CLI_PATH; unpinned={unpinned}"
    )


def test_effective_cli_probe_accepts_exact_version(monkeypatch) -> None:
    import claude_runtime

    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args[0], 0, stdout="2.1.273 (Claude Code)\n", stderr="",
        )

    monkeypatch.setattr(claude_runtime.subprocess, "run", fake_run)

    assert claude_runtime.verify_effective_cli() == "2.1.273 (Claude Code)"
    assert calls == [(([
        "/usr/local/bin/claude", "--version",
    ],), {
        "capture_output": True,
        "text": True,
        "timeout": 10,
        "check": False,
    })]


@pytest.mark.parametrize("rendered", [
    "2.1.219 (Claude Code)\n",
    "2.1.2730 (Claude Code)\n",
])
def test_effective_cli_probe_rejects_mismatch(monkeypatch, rendered) -> None:
    import claude_runtime

    monkeypatch.setattr(
        claude_runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=rendered, stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="expected 2.1.273"):
        claude_runtime.verify_effective_cli()


def test_effective_cli_probe_rejects_failed_command_without_stderr_leak(
    monkeypatch,
) -> None:
    import claude_runtime

    secret = "SECRET_CLI_STDERR"
    monkeypatch.setattr(
        claude_runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="", stderr=secret,
        ),
    )

    with pytest.raises(RuntimeError) as raised:
        claude_runtime.verify_effective_cli()
    assert secret not in str(raised.value)


@pytest.mark.asyncio
async def test_resident_specialist_and_executor_options_use_verified_cli(
    tmp_path, monkeypatch,
) -> None:
    from agent import Agent
    from channels import ChannelManager
    from config import (
        AgentConfig,
        CharacterConfig,
        HooksConfig,
        MemoryConfig,
        ToolsConfig,
    )
    from mcp_registry import McpServerRegistry
    from plugin_registry import ResolutionResult
    from session_registry import SessionRegistry
    import plugin_registry
    import tools as tools_mod

    monkeypatch.setattr(
        plugin_registry,
        "resolve_for",
        lambda _target: ResolutionResult(registry_valid=True),
    )
    monkeypatch.setattr(tools_mod, "_mcp_registry", None)

    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role="butler",
        model="claude-haiku-4-5",
        system_prompt="You are Tina.",
        character=CharacterConfig(name="Tina"),
        tools=ToolsConfig(allowed=["Read"]),
        memory=MemoryConfig(token_budget=800, read_strategy="cached"),
    )
    memory = AsyncMock()
    memory.profile.return_value = ""
    memory.recall.return_value = ""
    resident = Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        semantic_memory=memory,
    )
    resident_options = await resident._build_options(
        channel="voice",
        channel_key="voice-test",
        is_fresh=True,
        resume_sid=None,
        user_text="hello",
    )

    specialist_cfg = SimpleNamespace(
        role="finance",
        model="claude-haiku-4-5",
        system_prompt="You are Alex.",
        tools=SimpleNamespace(
            allowed=["Read"],
            disallowed=[],
            permission_mode="acceptEdits",
            max_turns=10,
        ),
        mcp_server_names=[],
        hooks=HooksConfig(),
        cwd="",
    )
    specialist_options = tools_mod._build_specialist_options(
        specialist_cfg,
        resolution=ResolutionResult(registry_valid=True),
    )
    executor_defn = SimpleNamespace(
        hooks_path=None,
        mcp_server_names=[],
        tools_allowed=["Read"],
        model="claude-sonnet-4-6",
        permission_mode="acceptEdits",
        tools_disallowed=[],
    )
    executor_options = tools_mod._build_executor_options(
        executor_defn,
        executor_type="configurator",
        resolution=ResolutionResult(registry_valid=True),
    )

    assert resident_options.cli_path == "/usr/local/bin/claude"
    assert specialist_options.cli_path == "/usr/local/bin/claude"
    assert executor_options.cli_path == "/usr/local/bin/claude"


def test_effective_cli_is_verified_before_any_ingress_listener() -> None:
    import inspect
    from casa_core import main

    source = inspect.getsource(main)
    probe = "await asyncio.to_thread(verify_effective_cli)"
    assert probe in source
    assert source.index(probe) < source.index("await site.start()")


@pytest.fixture(scope="session")
def _pin_image_tag() -> str:
    return "casa:local-clipin"


@pytest.fixture(scope="session")
def _build_pin_image(_pin_image_tag: str) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    subprocess.run(
        ["docker", "build",
         "--build-arg", "BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm",
         "-t", _pin_image_tag, "-f", "casa/Dockerfile", "casa/"],
        check=True,
    )


@pytest.mark.docker
def test_image_claude_cli_version(_pin_image_tag: str, _build_pin_image: None) -> None:
    from claude_runtime import CLAUDE_CLI_PATH, CLAUDE_CLI_VERSION

    path_result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "/bin/sh", _pin_image_tag,
         "-c", "command -v claude"],
        capture_output=True, text=True,
    )
    assert path_result.returncode == 0, (
        f"command -v claude failed: {path_result.stderr}"
    )
    assert path_result.stdout.strip() == CLAUDE_CLI_PATH

    r = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "/bin/sh", _pin_image_tag,
         "-c", f"{CLAUDE_CLI_PATH} --version"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"claude --version failed: {r.stderr}"
    assert CLAUDE_CLI_VERSION in r.stdout, (
        "image claude version "
        f"{r.stdout.strip()!r} != pinned {CLAUDE_CLI_VERSION}"
    )
