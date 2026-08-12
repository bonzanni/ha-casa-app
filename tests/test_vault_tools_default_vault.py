"""#535: the configurator vault tools inherit the configured default vault.

The plugin/secrets recipe doctrine promises "``vault`` defaults to the
operator's configured ``onepassword_default_vault``"; ``svc-casa/run`` exports
that option as ``ONEPASSWORD_DEFAULT_VAULT`` into the service environment.
These tests pin the fallback in the two vault-facing tool helpers: an omitted
``vault`` argument resolves to the env default, an explicit argument wins, and
an unset/empty default keeps today's account-wide behavior (no ``--vault``).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import tools


class _Result:
    returncode = 0
    stderr = ""
    stdout = json.dumps([])


def _run_capturing(captured):
    def _fake_run(cmd, **_kw):
        captured.append(list(cmd))
        res = _Result()
        # get_item_fields json.loads an OBJECT, list_vault_items a LIST.
        res.stdout = (
            json.dumps({"fields": []}) if "get" in cmd else json.dumps([])
        )
        return res
    return _fake_run


def test_list_vault_items_falls_back_to_default_vault(monkeypatch):
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    captured: list[list[str]] = []
    with patch.object(tools.subprocess, "run", _run_capturing(captured)):
        tools._tool_list_vault_items(query="", vault="")
    assert ["--vault", "Casa"] == [
        x for x in captured[0] if x in ("--vault", "Casa")
    ]


def test_get_item_fields_falls_back_to_default_vault(monkeypatch):
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    captured: list[list[str]] = []
    with patch.object(tools.subprocess, "run", _run_capturing(captured)):
        tools._tool_get_item_fields(item="Gmail", vault="")
    assert ["--vault", "Casa"] == [
        x for x in captured[0] if x in ("--vault", "Casa")
    ]


def test_explicit_vault_wins_over_default(monkeypatch):
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    captured: list[list[str]] = []
    with patch.object(tools.subprocess, "run", _run_capturing(captured)):
        tools._tool_list_vault_items(query="", vault="Casa Test")
    assert "Casa Test" in captured[0]
    assert "Casa" not in captured[0]


def test_no_default_keeps_account_wide_listing(monkeypatch):
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT", raising=False)
    captured: list[list[str]] = []
    with patch.object(tools.subprocess, "run", _run_capturing(captured)):
        tools._tool_list_vault_items(query="", vault="")
    assert "--vault" not in captured[0]


def test_empty_default_keeps_account_wide_listing(monkeypatch):
    # svc-casa/run normalizes a null option to the EMPTY string — an empty
    # env value must behave exactly like an unset one.
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "")
    captured: list[list[str]] = []
    with patch.object(tools.subprocess, "run", _run_capturing(captured)):
        tools._tool_list_vault_items(query="", vault="")
    assert "--vault" not in captured[0]
