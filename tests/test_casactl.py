"""Tests for casactl CLI script."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CASACTL = Path(__file__).resolve().parent.parent / "casa" / \
          "rootfs" / "usr" / "local" / "bin" / "casactl"


def test_casactl_no_args_shows_usage():
    result = subprocess.run(
        [sys.executable, str(CASACTL)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "usage" in (result.stderr + result.stdout).lower()


def test_casactl_reload_missing_scope_errors():
    result = subprocess.run(
        [sys.executable, str(CASACTL), "reload"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "scope" in (result.stderr + result.stdout).lower()


def test_casactl_help_smoke():
    """Smoke: --help works and lists subcommands."""
    result = subprocess.run(
        [sys.executable, str(CASACTL), "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "reload" in result.stdout
    assert "restart-supervised" in result.stdout


def test_casactl_scope_choices_cover_all_registered_handlers():
    """L29: every scope registered in reload._HANDLERS must be accepted by
    casactl. Guards against future scopes (e.g. v0.47.0's config_sync) being
    forgotten in the CLI's argparse choices, even though the server-side
    /admin/reload path would accept them."""
    import reload as reload_mod  # conftest.py adds the code root to sys.path

    result = subprocess.run(
        [sys.executable, str(CASACTL), "reload", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    for scope in reload_mod._HANDLERS:
        assert scope in result.stdout, (
            f"casactl --scope choices missing registered scope {scope!r}"
        )


def _load_casactl():
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("casactl_under_test", str(CASACTL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_casactl_reload_with_failures_reports_them_and_exits_nonzero(capsys):
    """#1650: an ok envelope that lists units which failed to load must not
    read as a clean success: each failure is printed and the exit code is 1."""
    import argparse

    mod = _load_casactl()
    envelope = {
        "status": "ok", "ms": 41,
        "actions": ["rebuild_agent_registry", "refresh_role_map"],
        "failures": [{"unit": "resident:copy", "detail": "role does not match directory"}],
    }
    mod._post_via_unix_socket = lambda path, body: (200, envelope)
    args = argparse.Namespace(scope="agents", role=None, include_env=False, json=False)

    rc = mod._cmd_reload(args)

    err = capsys.readouterr().err
    assert "resident:copy: role does not match directory" in err
    assert rc == 1
