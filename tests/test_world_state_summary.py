"""The world-state block every executor engagement reads renders the facts the
deployment already holds.

On the N150 (v0.311.0, 2026-09-15) the configurator's prompt said
``Addon version: unknown`` — the builder read ``/opt/casa/VERSION`` and
``/config/VERSION``, neither of which has ever existed; the version is in
``CASA_VERSION`` (``svc-casa/run``). And nothing rendered the configured default
vault at all, so the configurator could not name it and its one vault call was
rejected for lack of it. Defaults are the configurator's to know: they are
rendered here, not remembered.
"""
from __future__ import annotations

import tools


def _lines(monkeypatch, **env) -> dict[str, str]:
    for k in ("CASA_VERSION", "ONEPASSWORD_DEFAULT_VAULT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(tools, "_specialist_registry", None, raising=False)
    out = tools._build_world_state_summary()
    rows = {}
    for line in out.splitlines():
        key, _, value = line.partition(":")
        rows[key.strip()] = value.strip()
    return rows


def test_version_comes_from_casa_version(monkeypatch):
    rows = _lines(monkeypatch, CASA_VERSION="0.311.0")
    assert rows["Addon version"] == "0.311.0"


def test_version_unknown_when_unset(monkeypatch):
    rows = _lines(monkeypatch)
    assert rows["Addon version"] == "unknown"


def test_default_vault_is_rendered(monkeypatch):
    rows = _lines(monkeypatch, ONEPASSWORD_DEFAULT_VAULT="Casa")
    assert rows["Default vault"] == "Casa"


def test_default_vault_absent_is_said_so(monkeypatch):
    rows = _lines(monkeypatch)
    assert rows["Default vault"] == "(none configured)"


def test_empty_default_vault_is_none_configured(monkeypatch):
    """svc-casa/run normalises a null option to ``""`` (#291); the block must
    not render an empty name."""
    rows = _lines(monkeypatch, ONEPASSWORD_DEFAULT_VAULT="")
    assert rows["Default vault"] == "(none configured)"
