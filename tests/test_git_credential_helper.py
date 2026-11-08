"""Tests for /opt/casa/scripts/git-credential-casa.sh.

The helper is invoked by git when an HTTPS clone needs credentials.
It reads $GITHUB_TOKEN from process env. If the var is unset/empty,
emit nothing → git treats as anonymous. If set, emit
'username=x-access-token\npassword=$GITHUB_TOKEN\n' on stdout.

The image-wide /etc/gitconfig routes GitHub access through that helper; the
token is set at app boot by setup-configs.sh (written to
/run/s6/container_environment/GITHUB_TOKEN) and is never embedded in the
gitconfig itself.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HELPER = Path(
    "casa/rootfs/opt/casa/scripts/git-credential-casa.sh"
)


def _run(action: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Invoke the helper with the given action ('get'/'store'/'erase')."""
    return subprocess.run(
        ["sh", str(HELPER), action],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        check=False, timeout=5,
    )


def test_get_with_token_emits_credentials() -> None:
    r = _run("get", env={"GITHUB_TOKEN": "ghp_testtoken123"})
    assert r.returncode == 0, r.stderr
    assert r.stdout == "username=x-access-token\npassword=ghp_testtoken123\n"


def test_get_without_token_emits_nothing() -> None:
    r = _run("get", env={})  # GITHUB_TOKEN absent
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_get_with_empty_token_emits_nothing() -> None:
    r = _run("get", env={"GITHUB_TOKEN": ""})
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_get_strips_trailing_newline_from_token() -> None:
    """`op read` can emit trailing newline on some versions; helper
    must strip it so git's credential protocol doesn't see a malformed
    extra blank line in the response."""
    r = _run("get", env={"GITHUB_TOKEN": "ghp_abc\n"})
    assert r.returncode == 0, r.stderr
    assert r.stdout == "username=x-access-token\npassword=ghp_abc\n"


def test_get_strips_crlf_from_token() -> None:
    """Same as above for CRLF-terminated tokens."""
    r = _run("get", env={"GITHUB_TOKEN": "ghp_abc\r\n"})
    assert r.returncode == 0, r.stderr
    assert r.stdout == "username=x-access-token\npassword=ghp_abc\n"


def test_store_action_is_noop() -> None:
    """git invokes the helper with 'store' after a successful auth.
    A stateless helper must not write anything to disk."""
    r = _run("store", env={"GITHUB_TOKEN": "ghp_x"})
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_erase_action_is_noop() -> None:
    """git invokes 'erase' on auth failure. Stateless helper must noop."""
    r = _run("erase", env={"GITHUB_TOKEN": "ghp_x"})
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_etc_gitconfig_credential_helper_present() -> None:
    """github-access goes through the credential helper, not URL embedding."""
    cfg = Path("casa/rootfs/etc/gitconfig").read_text()
    assert "git-credential-casa.sh" in cfg
    assert "x-access-token" not in cfg, (
        "Token must come from helper at request time, never be embedded in "
        "the gitconfig URL section"
    )
