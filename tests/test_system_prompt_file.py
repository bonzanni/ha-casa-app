"""#1570: a system prompt reaches the CLI as a file, never as one argv entry.

The SDK passes a string system prompt as a single ``--system-prompt`` argument,
and Linux refuses an exec whose single argument reaches 128 KiB
(MAX_ARG_STRLEN). The assistant's prompt, memory block included, crossed it and
every start of its CLI failed with E2BIG. These tests exec the real argv the
pinned SDK transport builds, so the kernel itself decides.
"""
from __future__ import annotations

import ast
import errno
import os
import subprocess
import time
from pathlib import Path

import pytest

import claude_runtime
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import (
    SubprocessCLITransport,
)

REPO = Path(__file__).resolve().parents[1]
APP_ROOT = REPO / "casa" / "rootfs" / "opt" / "casa"

# Over MAX_ARG_STRLEN (131,072 bytes including the terminating NUL).
LARGE_PROMPT = "memory line\n" * 20_000


@pytest.fixture(autouse=True)
def prompt_dir(tmp_path, monkeypatch):
    d = tmp_path / "prompts"
    monkeypatch.setattr(claude_runtime, "SYSTEM_PROMPT_DIR", d)
    return d


def _argv(opts) -> list[str]:
    from dataclasses import replace
    opts = replace(opts, cli_path="/bin/true")
    return SubprocessCLITransport(prompt="x", options=opts)._build_command()


def _exec_true(argv: list[str]) -> None:
    """Exec /bin/true with *argv*'s arguments: the kernel's argv limits apply,
    the CLI itself is not needed."""
    subprocess.run(["/bin/true", *argv[1:]], check=True)


def test_raw_large_prompt_is_refused_by_the_kernel():
    """The failure, reproduced: the string form cannot be exec'd."""
    argv = _argv(ClaudeAgentOptions(system_prompt=LARGE_PROMPT))
    with pytest.raises(OSError) as exc:
        _exec_true(argv)
    assert exc.value.errno == errno.E2BIG


def test_large_prompt_execs_through_a_file(prompt_dir):
    opts = claude_runtime.with_system_prompt_file(
        ClaudeAgentOptions(system_prompt=LARGE_PROMPT))
    argv = _argv(opts)
    _exec_true(argv)
    assert "--system-prompt" not in argv
    path = Path(argv[argv.index("--system-prompt-file") + 1])
    assert path.parent == prompt_dir
    assert path.read_text(encoding="utf-8") == LARGE_PROMPT
    assert (prompt_dir.stat().st_mode & 0o777) == 0o700
    assert (path.stat().st_mode & 0o777) == 0o600


def test_same_prompt_reuses_one_file(prompt_dir):
    a = claude_runtime.with_system_prompt_file(ClaudeAgentOptions(system_prompt="p"))
    b = claude_runtime.with_system_prompt_file(ClaudeAgentOptions(system_prompt="p"))
    assert a.system_prompt == b.system_prompt
    assert [p.name for p in prompt_dir.iterdir()] == [
        Path(a.system_prompt["path"]).name]


def test_stale_files_are_pruned_and_a_reused_one_is_kept(prompt_dir):
    old = claude_runtime.with_system_prompt_file(
        ClaudeAgentOptions(system_prompt="old")).system_prompt["path"]
    kept = claude_runtime.with_system_prompt_file(
        ClaudeAgentOptions(system_prompt="kept")).system_prompt["path"]
    stale = time.time() - claude_runtime.SYSTEM_PROMPT_FILE_TTL_S - 60
    os.utime(old, (stale, stale))
    os.utime(kept, (stale, stale))
    claude_runtime.with_system_prompt_file(ClaudeAgentOptions(system_prompt="kept"))
    assert not Path(old).exists()
    assert Path(kept).exists()


@pytest.mark.parametrize("prompt", [
    None, "", {"type": "preset", "preset": "claude_code"},
    {"type": "file", "path": "/x.md"},
])
def test_non_string_and_empty_prompts_pass_through(prompt, prompt_dir):
    opts = ClaudeAgentOptions(system_prompt=prompt)
    assert claude_runtime.with_system_prompt_file(opts) is opts
    assert not prompt_dir.exists()


def test_pool_default_client_gets_the_file_form(monkeypatch):
    import claude_agent_sdk
    import sdk_client_pool
    seen = []
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", seen.append)
    sdk_client_pool._default_make_client(
        ClaudeAgentOptions(system_prompt=LARGE_PROMPT))
    assert seen[0].system_prompt["type"] == "file"


def _options_arg(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "options":
            return kw.value
    return call.args[0] if call.args else None


def _wrapped(expr: ast.expr | None) -> bool:
    return (isinstance(expr, ast.Call)
            and getattr(expr.func, "id", getattr(expr.func, "attr", None))
            == "with_system_prompt_file")


def _converted_names(tree: ast.AST) -> set[str]:
    """Names assigned straight from ``with_system_prompt_file(...)``, and
    never assigned anything else, in the same module."""
    wrapped, other = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    (wrapped if _wrapped(node.value) else other).add(target.id)
    return wrapped - other


def test_every_sdk_exec_site_passes_the_file_form():
    """Every ``ClaudeSDKClient(...)`` and SDK ``query(prompt=..., options=...)``
    in the app hands the SDK options converted at the site, or a variable
    assigned only from the conversion."""
    offenders = []
    for path in APP_ROOT.rglob("*.py"):
        rel = path.relative_to(APP_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        converted = _converted_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            is_client = name == "ClaudeSDKClient"
            is_query = (name == "query" and isinstance(node.func, ast.Attribute)
                        and any(k.arg == "prompt" for k in node.keywords)
                        and any(k.arg == "options" for k in node.keywords))
            if not (is_client or is_query):
                continue
            arg = _options_arg(node)
            if _wrapped(arg) or (isinstance(arg, ast.Name) and arg.id in converted):
                continue
            offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, offenders
