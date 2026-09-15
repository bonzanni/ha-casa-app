"""#535: the configurator vault tools inherit the configured default vault.

The plugin/secrets recipe doctrine promises "``vault`` defaults to the
operator's configured ``onepassword_default_vault``"; ``svc-casa/run`` exports
that option as ``ONEPASSWORD_DEFAULT_VAULT`` into the service environment.
These tests pin the fallback in the two vault-facing tool helpers: an omitted
``vault`` argument resolves to the env default, an explicit argument wins, and
an unset/empty default keeps today's account-wide behavior (no ``--vault``).

They also pin what the helpers may say: a failure is a fixed classification,
never ``op``'s own text; and no operator-typed string (title, label, section)
is ever returned: an item is named by the query term it matched and its id, a
field by its id, a role derived from the label, and its type
(design 2026-09-15 §2.D and its batch-1 build record §0e).
"""
from __future__ import annotations

import json
import subprocess as _sp
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
        tools._tool_list_vault_items(query="gmail", vault="")
    assert "--vault" in captured[0]
    assert captured[0][captured[0].index("--vault") + 1] == "Casa"


def test_get_item_fields_falls_back_to_default_vault(monkeypatch):
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    captured: list[list[str]] = []
    with patch.object(tools.subprocess, "run", _run_capturing(captured)):
        tools._tool_get_item_fields(item="Gmail", vault="")
    assert "--vault" in captured[0]
    assert captured[0][captured[0].index("--vault") + 1] == "Casa"


def test_explicit_vault_wins_over_default(monkeypatch):
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    captured: list[list[str]] = []
    with patch.object(tools.subprocess, "run", _run_capturing(captured)):
        tools._tool_list_vault_items(query="gmail", vault="Casa Test")
    assert "Casa Test" in captured[0]
    assert "Casa" not in captured[0]


def test_no_default_keeps_account_wide_listing(monkeypatch):
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT", raising=False)
    captured: list[list[str]] = []
    with patch.object(tools.subprocess, "run", _run_capturing(captured)):
        tools._tool_list_vault_items(query="gmail", vault="")
    assert "--vault" not in captured[0]


def test_empty_default_keeps_account_wide_listing(monkeypatch):
    # svc-casa/run normalises a null option to "" (#291); "" must not become
    # `--vault ""`.
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "")
    captured: list[list[str]] = []
    with patch.object(tools.subprocess, "run", _run_capturing(captured)):
        tools._tool_list_vault_items(query="gmail", vault="")
    assert "--vault" not in captured[0]


# ---------------------------------------------------------------------------
# Classified failures: no subprocess output ever enters a tool result.
#
# Both helpers returned ``op``'s raw stderr on a non-zero exit. Truncation is
# not redaction: whatever ``op`` prints on failure (an item title, a field
# value echoed in an error, a token fragment) would have reached the
# configurator's transcript and, from there, Telegram. The result now carries a
# fixed classification (and the exit code for a non-zero exit), nothing else.
# ---------------------------------------------------------------------------

_CANARY = "sk-live-CANARY-9f8e7d6c5b4a"


class _FailingResult:
    returncode = 1
    stderr = f"[ERROR] 2026/09/15 op read failed: value {_CANARY} rejected"
    stdout = ""


def _run_failing(cmd, **_kw):
    return _FailingResult()


def test_list_vault_items_failure_is_classified_not_quoted(monkeypatch):
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    with patch.object(tools.subprocess, "run", _run_failing):
        out = tools._tool_list_vault_items(query="gmail", vault="")
    assert out == {"error": "op_failed", "exit_code": 1}
    assert _CANARY not in json.dumps(out)


def test_get_item_fields_failure_is_classified_not_quoted(monkeypatch):
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    with patch.object(tools.subprocess, "run", _run_failing):
        out = tools._tool_get_item_fields(item="Gmail", vault="")
    assert out == {"error": "op_failed", "exit_code": 1}
    assert _CANARY not in json.dumps(out)


def test_list_vault_items_unreadable_output_is_classified():
    class _Garbage:
        returncode = 0
        stderr = ""
        stdout = f"not json {_CANARY}"

    with patch.object(tools.subprocess, "run", lambda *a, **k: _Garbage()):
        out = tools._tool_list_vault_items(query="gmail", vault="Casa")
    assert out == {"error": "op_unreadable"}


def test_get_item_fields_timeout_is_classified():
    def _slow(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, kw.get("timeout", 30))

    with patch.object(tools.subprocess, "run", _slow):
        out = tools._tool_get_item_fields(item="Gmail", vault="Casa")
    assert out == {"error": "op_timeout"}


# ---------------------------------------------------------------------------
# No operator-typed string is returned: ids, roles and types only.
# ---------------------------------------------------------------------------

def test_get_item_fields_projects_roles_and_ids_only():
    """One projection, shared with plugin_add's ``secret_candidates``: op's
    field id, a role derived from the label, and the type; never the label,
    the section, ``value``, ``reference`` or ``notes``."""
    item = {"fields": [
        {"id": "u", "label": "client id", "type": "STRING",
         "value": _CANARY, "reference": "op://Casa/x/client id"},
        {"id": "p", "label": "client secret", "type": "CONCEALED",
         "section": {"id": "s", "label": "OAuth"}, "value": _CANARY},
        {"id": "n", "label": f"client id {_CANARY}", "type": "STRING", "value": "-"},
    ]}
    fields = tools._project_item_fields(item)
    assert fields == [
        {"id": "u", "role": "client_id", "type": "STRING"},
        {"id": "p", "role": "client_secret", "type": "CONCEALED"},
        {"id": "n", "role": None, "type": "STRING"},
    ]
    out = json.dumps(fields)
    for forbidden in (_CANARY, "OAuth", "client id", "op://", "label", "section"):
        assert forbidden not in out, forbidden


def test_get_item_fields_handler_returns_the_projection(monkeypatch):
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")

    class _Doc:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"id": "abc", "title": "Gmail", "fields": [
            {"id": "u", "label": "email", "type": "EMAIL", "value": "casa-user"}]})

    with patch.object(tools.subprocess, "run", lambda *a, **k: _Doc()):
        out = tools._tool_get_item_fields(item="abc", vault="")
    assert out == {"fields": [{"id": "u", "role": "email", "type": "EMAIL"}]}


def test_list_vault_items_never_returns_the_title(monkeypatch):
    """The manual listing returns the matched query term in place of the
    title, the op id and a validated category, nothing operator-typed."""
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_CANARY_TOKEN")

    class _Listing:
        returncode = 0
        stderr = ""
        stdout = json.dumps([{"id": "x", "title": "Gmail ops_CANARY_TOKEN",
                              "category": "API_CREDENTIAL", "updated_at": "2026-01-01"},
                             {"id": "y", "title": "Gmail sk-live-ABCDEFGHIJKLMNOP01",
                              "category": "not a category"},
                             {"id": "z", "title": "Gmail"},
                             {"id": "w", "title": "Unrelated"}])

    with patch.object(tools.subprocess, "run", lambda *a, **k: _Listing()):
        out = tools._tool_list_vault_items(query="gmail", vault="Casa")
    assert out == {"items": [
        {"name": "gmail", "id": "x", "category": "API_CREDENTIAL"},
        {"name": "gmail", "id": "y", "category": None},
        {"name": "gmail", "id": "z", "category": None},
    ]}
    dumped = json.dumps(out)
    for forbidden in ("ops_CANARY_TOKEN", "sk-live", "Gmail", "Unrelated", "2026-01-01"):
        assert forbidden not in dumped, forbidden
