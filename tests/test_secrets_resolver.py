"""secrets_resolver — op:// reference resolution at boot."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from secrets_resolver import resolve

pytestmark = pytest.mark.unit


def test_plaintext_passthrough() -> None:
    assert resolve("plaintext-value") == "plaintext-value"
    assert resolve("") == ""


def test_op_not_a_prefix() -> None:
    # Strings that contain 'op' but don't start with 'op://' pass through.
    assert resolve("options") == "options"


@patch("secrets_resolver.subprocess.run")
def test_op_resolves(mock_run) -> None:
    resolve.cache_clear()
    mock_run.return_value.stdout = "real-secret\n"
    mock_run.return_value.returncode = 0
    assert resolve("op://Casa/GitHub/token") == "real-secret"


@patch("secrets_resolver.subprocess.run")
def test_op_failure_raises(mock_run) -> None:
    resolve.cache_clear()
    import subprocess as sp
    mock_run.side_effect = sp.CalledProcessError(1, "op", stderr="auth error")
    with pytest.raises(RuntimeError, match="auth error"):
        resolve("op://Casa/X/y")


@patch("secrets_resolver.subprocess.run")
def test_op_timeout_raises(mock_run) -> None:
    resolve.cache_clear()
    import subprocess as sp
    mock_run.side_effect = sp.TimeoutExpired("op", 10)
    with pytest.raises(RuntimeError, match="Timeout"):
        resolve("op://Casa/X/y")


@patch("secrets_resolver.subprocess.run")
def test_cached(mock_run) -> None:
    resolve.cache_clear()
    mock_run.return_value.stdout = "v"
    mock_run.return_value.returncode = 0
    resolve("op://a/b/c")
    resolve("op://a/b/c")
    assert mock_run.call_count == 1


@patch("secrets_resolver.subprocess.run")
def test_missing_op_binary_raises_the_runtime_error_contract(mock_run) -> None:
    """#345: an absent `op` binary surfaces as FileNotFoundError from
    subprocess.run. Callers (casa_core boot, reload, discovery) handle only
    RuntimeError — pre-fix the OSError escaped and aborted secret-consuming
    startup instead of degrading with a warning."""
    resolve.cache_clear()
    mock_run.side_effect = FileNotFoundError("[Errno 2] No such file or directory: 'op'")
    with pytest.raises(RuntimeError, match="op"):
        resolve("op://Casa/X/y")


@patch("secrets_resolver.subprocess.run")
def test_invalidate_cache_forces_a_fresh_resolution(mock_run) -> None:
    """#345: rotation support — after invalidate_cache() the next resolve()
    must hit `op read` again instead of returning the cached (revoked) value."""
    import secrets_resolver
    resolve.cache_clear()
    mock_run.return_value.stdout = "old-secret\n"
    mock_run.return_value.returncode = 0
    assert resolve("op://Casa/Rotating/cred") == "old-secret"
    mock_run.return_value.stdout = "new-secret\n"
    assert resolve("op://Casa/Rotating/cred") == "old-secret"  # cached
    secrets_resolver.invalidate_cache()
    assert resolve("op://Casa/Rotating/cred") == "new-secret"


@patch("secrets_resolver.subprocess.run")
def test_op_resolves_github_credential(mock_run) -> None:
    """v0.14.9: setup-configs.sh resolves op://VAULT/GitHub/credential
    at boot. Verify the resolver shells `op read` with the canonical
    GitHub credential reference shape."""
    resolve.cache_clear()
    mock_run.return_value.stdout = "github_pat_TESTTOKEN\n"
    mock_run.return_value.returncode = 0
    token = resolve("op://CasaTest/GitHub/credential")
    assert token == "github_pat_TESTTOKEN"
    assert mock_run.call_args[0][0] == ["op", "read", "op://CasaTest/GitHub/credential"]


def test_every_password_typed_option_is_in_the_boot_resolution_list() -> None:
    """#277: casa_core's boot pass resolves op:// for "password-typed addon
    options" — the list must cover ALL of them. context7_api_key (password? in
    config.yaml, exported as CONTEXT7_API_KEY by svc-casa/run) was missing, so
    an op:// value reached the context7 MCP server as the literal reference."""
    import casa_core

    assert set(casa_core._PASSWORD_ENV_VARS) == {
        "CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN",
        "WEBHOOK_SECRET", "CONTEXT7_API_KEY"}


def test_a_plugin_facing_option_is_unset_when_its_reference_fails(
        monkeypatch) -> None:
    """#580: `CONTEXT7_API_KEY` is in `CASA_OWNED_ENV_OPTIONS` — the map of env
    names a PLUGIN may reference and Casa supplies — so its consumer is an MCP
    server, and the CLI hands whatever the variable holds straight to it. An
    unresolvable reference must therefore leave the variable ABSENT: a bare
    `${VAR}` reference then withholds the plugin, and a `${VAR:-default}` one
    takes its default instead of expanding to the literal."""
    import casa_core

    def _boom(value):
        raise RuntimeError("op read failed")

    monkeypatch.setattr("secrets_resolver.resolve", _boom)
    env = {"CONTEXT7_API_KEY": "op://v/i/f"}
    failed = casa_core._resolve_password_options(env)
    assert failed == ["CONTEXT7_API_KEY"]
    assert "CONTEXT7_API_KEY" not in env


def test_a_casa_consumed_option_keeps_the_literal_when_its_reference_fails(
        monkeypatch) -> None:
    """The counterpart rule, and the reason it is not "pop every secret":
    `TELEGRAM_BOT_TOKEN` is read as `if telegram_token:` (casa_core.py), so an
    ABSENT token builds no Telegram channel at all — silently removing the
    operator's only notification surface. The literal instead constructs the
    channel and fails loudly on connect. No plugin server receives it."""
    import casa_core

    def _boom(value):
        raise RuntimeError("op read failed")

    monkeypatch.setattr("secrets_resolver.resolve", _boom)
    env = {"TELEGRAM_BOT_TOKEN": "op://v/i/f"}
    failed = casa_core._resolve_password_options(env)
    assert failed == ["TELEGRAM_BOT_TOKEN"]
    assert env["TELEGRAM_BOT_TOKEN"] == "op://v/i/f"


def test_a_resolved_password_option_is_replaced_with_its_value(
        monkeypatch) -> None:
    import casa_core

    monkeypatch.setattr("secrets_resolver.resolve",
                        lambda v: "plaintext" if v.startswith("op://") else v)
    env = {"CONTEXT7_API_KEY": "op://v/i/f", "WEBHOOK_SECRET": "literal"}
    assert casa_core._resolve_password_options(env) == []
    assert env["CONTEXT7_API_KEY"] == "plaintext"
    assert env["WEBHOOK_SECRET"] == "literal"
