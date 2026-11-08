"""H-1 (v0.37.8): the claude-home-propagation block in setup-configs.sh.

casa-main boots with HOME=/root unless setup-configs.sh
writes HOME=/config/cc-home to
/run/s6/container_environment/HOME. Without this, the runtime claude
binary (called by install_casa_plugin / uninstall_casa_plugin / the
three marketplace_* MCP tools) reads /root/.claude/plugins/ and fails
to find the casa-plugins marketplace registration.

bug-review-2026-05-13-exploration4.md::H-1 has the full evidence.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SETUP_CONFIGS = Path("casa/rootfs/etc/s6-overlay/scripts/setup-configs.sh")
CC_HOME = "/config/cc-home"


def _extract_home_block() -> str:
    src = SETUP_CONFIGS.read_text(encoding="utf-8")
    start = src.find("# === claude-home-propagation: begin")
    end = src.find("# === claude-home-propagation: end")
    assert start >= 0 and end > start, (
        "claude-home-propagation block markers missing in setup-configs.sh — "
        "see bug-review-2026-05-13-exploration4.md::H-1"
    )
    return src[start:end]


def _run_block(*, container_env_dir: Path) -> tuple[int, str, str]:
    """Run the home-propagation block under POSIX sh.

    Stubs the `bashio::log.*` calls (bashio isn't available in unit
    test env) and rewrites the hardcoded `/run/s6/container_environment/HOME`
    path to a tmp_path-rooted file we can introspect.
    """
    block = _extract_home_block()

    # `bashio::log.info` uses `::` and `.` which POSIX sh does not allow
    # in function names. Rewrite to portable stubs before running. The
    # production script runs under bashio (bash) where these identifiers
    # are legal; the rewrite is test-only.
    block = block.replace("bashio::log.info", "_bx_log_info")

    bashio_stub = '_bx_log_info() { printf "[INFO] %s\\n" "$*"; }\n'

    # Override the hardcoded /run/s6/container_environment/HOME path
    # to the test container_env_dir.
    target = (container_env_dir / "HOME").as_posix()
    block_test = block.replace(
        "/run/s6/container_environment/HOME", target
    )

    full_script = bashio_stub + block_test

    proc = subprocess.run(
        ["sh", "-c", full_script],
        env={"PATH": "/usr/bin:/bin"},
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


class TestClaudeHomePropagationBlock:
    def test_writes_cc_home_to_container_environment(self, container_env: Path) -> None:
        """The block writes HOME=cc-home to /run/s6/container_environment/HOME."""
        rc, out, err = _run_block(container_env_dir=container_env)
        assert rc == 0, f"block failed: stdout={out!r} stderr={err!r}"
        target = container_env / "HOME"
        assert target.exists(), "HOME file should exist in container_environment"
        assert target.read_text() == CC_HOME
        assert "HOME propagated to s6 services" in out

    def test_idempotent_when_target_already_exists(self, container_env: Path) -> None:
        """Re-running the block overwrites cleanly — no append, no permission flip."""
        target = container_env / "HOME"
        target.write_text("/some/stale/path")
        rc, out, err = _run_block(container_env_dir=container_env)
        assert rc == 0
        assert target.read_text() == CC_HOME, "should overwrite, not append"


def test_setup_configs_has_home_block() -> None:
    """Sanity: the markers are present and bracket the block in the right spot
    (after claude-oauth-token end). The seed-copy block that used to follow it
    was removed in v0.71.0 — plugin materialization is now the init-plugin-store
    oneshot (plugin_boot.py).
    """
    src = SETUP_CONFIGS.read_text(encoding="utf-8")
    oauth_end = src.find("# === claude-oauth-token: end")
    home_begin = src.find("# === claude-home-propagation: begin")
    home_end = src.find("# === claude-home-propagation: end")
    assert "seed-copy: begin" not in src, "the seed-copy block must be gone (§3.6)"
    assert oauth_end < home_begin < home_end, (
        "claude-home-propagation block must sit after claude-oauth-token end"
    )


def test_setup_configs_writes_home_to_container_environment_markers_present() -> None:
    """Cheap regression guard: the printf line lives in its own marked
    block so a partial revert can't silently drop just the write.
    """
    script = SETUP_CONFIGS.read_text(encoding="utf-8")
    assert "/run/s6/container_environment/HOME" in script
    assert "/config/cc-home" in script
    assert "claude-home-propagation: begin" in script
    assert "claude-home-propagation: end" in script
