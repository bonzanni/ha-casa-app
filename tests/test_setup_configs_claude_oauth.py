"""K-1 (v0.34.1): the claude-oauth-token block in setup-configs.sh.

Pre-fix the OAuth token was only exported into svc-casa's process env
(svc-casa/run:13). s6-rc child services launched via `with-contenv`
(engagement subprocesses, plugin-developer + hello-driver via
claude_code_driver) read /run/s6/container_environment/, NOT
svc-casa's process env. Result: every claude_code_driver subprocess
hit "Not logged in · Please run /login" and produced no useful
output. Latently broken since v0.13.0 (~8 days).

This test asserts the new propagation block:
- writes the token to a target file when configured
- removes the target file when not configured
- handles op:// resolution path the same way GITHUB_TOKEN does

bug-review-2026-05-01-exploration4.md::K-1 has the full evidence.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SETUP_CONFIGS = Path("casa/rootfs/etc/s6-overlay/scripts/setup-configs.sh")


def _extract_oauth_block() -> str:
    src = SETUP_CONFIGS.read_text(encoding="utf-8")
    start = src.find("# === claude-oauth-token: begin")
    end = src.find("# === claude-oauth-token: end")
    assert start >= 0 and end > start, (
        "claude-oauth-token block markers missing in setup-configs.sh — "
        "see bug-review-2026-05-01-exploration4.md::K-1"
    )
    return src[start:end]


def _run_block(
    *,
    oauth_value: str,
    op_token: str = "",
    container_env_dir: Path,
    fake_op_resolve_to: str | None = None,
) -> tuple[int, str, str]:
    """Run the oauth block under POSIX sh.

    Stubs the `bashio::config` calls (bashio isn't available in unit
    test env) and the `op` CLI (we don't want a real 1Password call)
    via env vars + a tiny shim PATH.
    """
    block = _extract_oauth_block()

    # `bashio::config` and `bashio::log.*` use `::` and `.` which POSIX
    # sh does not allow in function names. Rewrite to portable stubs
    # before running. The production script runs under bashio (bash)
    # where these identifiers are legal; the rewrite is test-only.
    block = (
        block
        .replace("bashio::config", "_bx_config")
        .replace("bashio::log.info", "_bx_log_info")
        .replace("bashio::log.warning", "_bx_log_warn")
    )

    bashio_stub = (
        f"_bx_config() {{\n"
        f"  case \"$1\" in\n"
        f"    claude_oauth_token) printf '%s' '{oauth_value}' ;;\n"
        f"    onepassword_service_account_token) printf '%s' '{op_token}' ;;\n"
        f"  esac\n"
        f"}}\n"
        f"_bx_log_info() {{ printf '[INFO] %s\\n' \"$*\"; }}\n"
        f"_bx_log_warn() {{ printf '[WARN] %s\\n' \"$*\"; }}\n"
    )

    # If a fake op resolution is requested, shim `op` so it echoes that.
    if fake_op_resolve_to is not None:
        op_stub_dir = container_env_dir.parent / "op_stub_bin"
        op_stub_dir.mkdir(parents=True, exist_ok=True)
        op_path = op_stub_dir / "op"
        op_path.write_text(
            f"#!/bin/sh\nprintf '%s' '{fake_op_resolve_to}'\n",
            encoding="utf-8",
        )
        op_path.chmod(0o755)
        path_env = f"{op_stub_dir.as_posix()}:/usr/bin:/bin"
    else:
        path_env = "/usr/bin:/bin"

    # Override the hardcoded /run/s6/container_environment/CLAUDE_CODE_OAUTH_TOKEN
    # path to the test container_env_dir.
    target = (container_env_dir / "CLAUDE_CODE_OAUTH_TOKEN").as_posix()
    block_test = block.replace(
        "/run/s6/container_environment/CLAUDE_CODE_OAUTH_TOKEN", target
    )

    full_script = bashio_stub + block_test

    proc = subprocess.run(
        ["sh", "-c", full_script],
        env={"PATH": path_env},
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture
def container_env(tmp_path: Path) -> Path:
    d = tmp_path / "container_environment"
    d.mkdir()
    return d


class TestClaudeOauthBlock:
    def test_writes_raw_token_when_configured(self, container_env: Path) -> None:
        """Raw OAuth token (non-op://) is written to container_environment."""
        rc, out, err = _run_block(
            oauth_value="sk-ant-oat01-raw-test-token",
            container_env_dir=container_env,
        )
        assert rc == 0, f"block failed: stdout={out!r} stderr={err!r}"
        target = container_env / "CLAUDE_CODE_OAUTH_TOKEN"
        assert target.exists(), "token file should exist"
        assert target.read_text() == "sk-ant-oat01-raw-test-token"
        assert "Claude OAuth: token propagated" in out

    def test_removes_target_when_oauth_unconfigured(self, container_env: Path) -> None:
        """Empty oauth value → target file removed + warning logged."""
        target = container_env / "CLAUDE_CODE_OAUTH_TOKEN"
        target.write_text("stale-leftover-token")  # pre-existing stale state
        rc, out, err = _run_block(
            oauth_value="",
            container_env_dir=container_env,
        )
        assert rc == 0
        assert not target.exists(), "stale token should be removed"
        assert "K-1" in out, f"warning should reference K-1; got: {out!r}"

    def test_removes_target_when_oauth_is_null_string(self, container_env: Path) -> None:
        """bashio returns literal 'null' for unset values; treat as empty."""
        target = container_env / "CLAUDE_CODE_OAUTH_TOKEN"
        target.write_text("stale-leftover-token")
        rc, out, err = _run_block(
            oauth_value="null",
            container_env_dir=container_env,
        )
        assert rc == 0
        assert not target.exists()
        assert "K-1" in out

    def test_op_reference_resolves_via_op_cli(self, container_env: Path) -> None:
        """op:// reference uses the op CLI to resolve, then writes resolved value."""
        rc, out, err = _run_block(
            oauth_value="op://Vault/Claude/credential",
            op_token="ops-test-token",
            container_env_dir=container_env,
            fake_op_resolve_to="sk-ant-oat01-resolved-via-op",
        )
        assert rc == 0, f"block failed: stdout={out!r} stderr={err!r}"
        target = container_env / "CLAUDE_CODE_OAUTH_TOKEN"
        assert target.exists()
        assert target.read_text() == "sk-ant-oat01-resolved-via-op"

    def test_op_reference_without_op_token_fails_safe(self, container_env: Path) -> None:
        """op:// reference but no OP_SERVICE_ACCOUNT_TOKEN → no write + warn."""
        target = container_env / "CLAUDE_CODE_OAUTH_TOKEN"
        target.write_text("stale-leftover-token")
        rc, out, err = _run_block(
            oauth_value="op://Vault/Claude/credential",
            op_token="",  # no OP token configured
            container_env_dir=container_env,
        )
        assert rc == 0
        assert not target.exists(), "should not have written; op resolve failed"
        assert "K-1" in out


def test_setup_configs_has_oauth_block() -> None:
    """Sanity: the markers are present and bracket the block in the right spot
    (after github-token). The seed-copy block that followed it was removed in
    v0.71.0 (plugin materialization → init-plugin-store oneshot)."""
    src = SETUP_CONFIGS.read_text(encoding="utf-8")
    gh_end = src.find("# === github-token: end")
    oauth_begin = src.find("# === claude-oauth-token: begin")
    oauth_end = src.find("# === claude-oauth-token: end")
    assert gh_end < oauth_begin < oauth_end, (
        "claude-oauth-token block must sit after github-token end"
    )
