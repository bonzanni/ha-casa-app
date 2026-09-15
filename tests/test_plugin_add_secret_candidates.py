"""``plugin_add`` / ``plugin_update`` explore the default vault for the
plugin's required secrets and return what they found — labels only.

Design 2026-09-15 §2.D: whether the configurator searched the vault used to
depend on the brief's wording (the 0.177.0 playbook pass said "credentials are
in 1Password"; the N150 message on 2026-09-15 did not, and the configurator
gave up). A result field that is always there turns "did it think of
searching?" into "did it read its own tool result?". The tool never wires
anything: ``set_plugin_env_reference`` stays the deciding step.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from test_plugin_tools import _State, _entry, _pr, _wire

_CANARY = "GOCSPX-CANARY-VALUE-do-not-leak"
_REQUIRED = ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_USER_EMAIL"]

_GMAIL_ITEM = {
    "id": "abc123", "title": "Gmail", "category": "API_CREDENTIAL",
    "updated_at": "2026-08-06T00:00:00Z",
}
_GMAIL_DOC = {
    "id": "abc123", "title": "Gmail",
    "fields": [
        {"id": "a", "label": "client id", "type": "STRING", "value": "x.apps"},
        {"id": "b", "label": "client secret", "type": "CONCEALED",
         "value": _CANARY, "reference": "op://Casa/abc123/client secret"},
        {"id": "c", "label": "email", "type": "STRING",
         "section": {"id": "s", "label": "Account"}, "value": "casa-user"},
    ],
}


class _Res:
    def __init__(self, code: int, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = code, stdout, stderr


def _fake_op(items, docs, *, fail: str | None = None, calls=None):
    """A fake ``op``: ``item list`` → ``items``; ``item get <id>`` → ``docs[id]``.
    ``fail`` = "list" | "get" makes that verb exit 1 with a canary on stderr."""
    def _run(cmd, **_kw):
        if calls is not None:
            calls.append(list(cmd))
        if cmd[:3] == ["op", "item", "list"]:
            if fail == "list":
                return _Res(1, stderr=f"[ERROR] list failed {_CANARY}")
            return _Res(0, json.dumps(items))
        if cmd[:3] == ["op", "item", "get"]:
            if fail == "get":
                return _Res(1, stderr=f"[ERROR] get failed {_CANARY}")
            return _Res(0, json.dumps(docs[cmd[3]]))
        raise AssertionError(f"unexpected op call {cmd}")
    return _run


def _observability(tools_mod, monkeypatch, required):
    monkeypatch.setattr(
        tools_mod, "_resolved_observability",
        lambda name, *, manifest=None: {
            "granted_tools": [], "required_env_vars": list(required),
            "setup_tool": "setup_gmail"})


async def _add(tools_mod):
    r = await tools_mod.plugin_add.handler({
        "name": "gmail", "repo": "bonzanni/casa-plugin-gmail", "ref": "v0.7.0",
        "targets": ["resident:assistant"]})
    return json.loads(r["content"][0]["text"])


async def test_one_matching_item_is_returned_with_labels_only(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_CANARY_TOKEN")
    for v in _REQUIRED:
        monkeypatch.delenv(v, raising=False)
    calls: list[list[str]] = []
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC}, calls=calls)):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    sc = payload["secret_candidates"]
    assert sc["vault"] == "Casa"
    assert sc["queries"] == ["gmail"]
    assert sc["unresolved"] == _REQUIRED
    assert sc["items"] == [{
        "name": "Gmail", "id": "abc123",
        "fields": [
            {"label": "client id", "section": "", "type": "STRING"},
            {"label": "client secret", "section": "", "type": "CONCEALED"},
            {"label": "email", "section": "Account", "type": "STRING"},
        ]}]
    serialised = json.dumps(payload)
    assert _CANARY not in serialised
    assert "ops_CANARY_TOKEN" not in serialised
    assert "op://" not in serialised
    # Every op call was scoped to the default vault.
    assert all("--vault" in c and c[c.index("--vault") + 1] == "Casa"
               for c in calls)


async def test_vendor_stem_query_is_tried_when_the_name_finds_nothing(
        monkeypatch, tmp_path):
    """Queries: the plugin name, then each distinct vendor stem of the
    unresolved vars, deduplicated, at most three."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="voicemail"))
    _observability(tools_mod, monkeypatch,
                   ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "OPENAI_KEY"])
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")
    item = {"id": "e1", "title": "ElevenLabs", "category": "API_CREDENTIAL"}
    calls: list[list[str]] = []
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([item], {"e1": {"fields": []}}, calls=calls)):
        r = await tools_mod.plugin_add.handler({
            "name": "voicemail", "repo": "o/r", "ref": "v1",
            "targets": ["resident:assistant"]})
    sc = json.loads(r["content"][0]["text"])["secret_candidates"]
    assert sc["queries"] == ["voicemail", "elevenlabs", "openai"]
    assert [i["id"] for i in sc["items"]] == ["e1"]


async def test_op_failure_is_classified_and_does_not_fail_the_add(
        monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC}, fail="list")):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    assert payload["secret_candidates"] == {"error": "op_failed", "exit_code": 1}
    assert _CANARY not in json.dumps(payload)


async def test_no_default_vault_means_no_exploration(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT", raising=False)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")

    def _no_op(cmd, **_kw):
        raise AssertionError(f"op must not be called: {cmd}")

    with patch.object(tools_mod.subprocess, "run", _no_op):
        payload = await _add(tools_mod)
    assert "secret_candidates" not in payload
    assert payload["required_env_vars"] == _REQUIRED


async def test_no_required_vars_means_no_exploration(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, [])
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")

    def _no_op(cmd, **_kw):
        raise AssertionError(f"op must not be called: {cmd}")

    with patch.object(tools_mod.subprocess, "run", _no_op):
        payload = await _add(tools_mod)
    assert "secret_candidates" not in payload


async def test_already_resolved_vars_are_not_listed_as_unresolved(
        monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")
    monkeypatch.setenv("GMAIL_USER_EMAIL", "casa-user")
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC})):
        payload = await _add(tools_mod)
    assert payload["secret_candidates"]["unresolved"] == [
        "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET"]


async def test_plugin_update_explores_too(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"] = [_entry(name="gmail", version="0.5.6")]
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC})):
        r = await tools_mod.plugin_update.handler({
            "name": "gmail", "new_ref": "v0.7.0"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True, payload
    assert payload["secret_candidates"]["items"][0]["id"] == "abc123"


# --- Terra diff r1 D2: unreadable and timed-out op calls are classified -----

async def test_malformed_op_output_is_classified_and_the_envelope_holds(
        monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")

    def _garbage(cmd, **_kw):
        return _Res(0, stdout=f"not json {_CANARY}")

    with patch.object(tools_mod.subprocess, "run", _garbage):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    assert payload["secret_candidates"] == {"error": "op_unreadable"}
    assert _CANARY not in json.dumps(payload)
    for key in ("artifact_id", "version", "revision", "activation_committed",
                "runtime_ready", "verify", "granted_tools",
                "required_env_vars", "setup_tool"):
        assert key in payload, key


async def test_op_timeout_is_classified_and_the_envelope_holds(
        monkeypatch, tmp_path):
    import subprocess as _sp
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")

    def _slow(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, kw.get("timeout", 30))

    with patch.object(tools_mod.subprocess, "run", _slow):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    assert payload["secret_candidates"] == {"error": "op_timeout"}


async def test_a_foreign_exception_in_exploration_is_classified(
        monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")

    def _boom(cmd, **_kw):
        raise OSError(f"op binary missing {_CANARY}")

    with patch.object(tools_mod.subprocess, "run", _boom):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    assert payload["secret_candidates"] == {"error": "op_failed", "exit_code": -1}
    assert _CANARY not in json.dumps(payload)


# --- Astra/Terra diff r1 D1: a title that repeats a secret is withheld -------

async def test_a_title_repeating_a_field_value_is_withheld(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_CANARY_TOKEN")
    leaky_item = dict(_GMAIL_ITEM, title=f"Gmail {_CANARY}")
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([leaky_item], {"abc123": _GMAIL_DOC})):
        payload = await _add(tools_mod)
    item = payload["secret_candidates"]["items"][0]
    assert item["id"] == "abc123"
    assert item["name"] == tools_mod._TITLE_WITHHELD
    assert _CANARY not in json.dumps(payload)


async def test_a_title_repeating_the_token_is_withheld(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_CANARY_TOKEN")
    leaky_item = dict(_GMAIL_ITEM, title="Gmail ops_CANARY_TOKEN")
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([leaky_item], {"abc123": _GMAIL_DOC})):
        payload = await _add(tools_mod)
    assert payload["secret_candidates"]["items"][0]["name"] == tools_mod._TITLE_WITHHELD
    assert "ops_CANARY_TOKEN" not in json.dumps(payload)


async def test_exploration_runs_after_the_reload(monkeypatch, tmp_path):
    """Astra diff r1 D2 test gap: every op call lands AFTER the reload and
    verify the sequencer performs — activation is never delayed by it."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _observability(tools_mod, monkeypatch, _REQUIRED)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")
    inner = _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC})

    def _logging_op(cmd, **kw):
        st.log.append("op")
        return inner(cmd, **kw)

    with patch.object(tools_mod.subprocess, "run", _logging_op):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    assert "op" in st.log
    assert st.log.index("dispatch:assistant") < st.log.index("op")
    assert st.log.index("reload_snapshot") < st.log.index("op")
