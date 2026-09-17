"""``plugin_add`` / ``plugin_update`` explore the default vault for the
plugin's required secrets and return what they found: ids, roles and types,
never an operator-typed string.

Design 2026-09-15 §2.D (and its batch-1 build record §0e): whether the
configurator searched the vault used to depend on the brief's wording (the
0.177.0 playbook pass said "credentials are in 1Password"; the N150 message on
2026-09-15 did not, and the configurator gave up). A result field that is
always there turns "did it think of searching?" into "did it read its own tool
result?". The tool never wires anything: ``set_plugin_env_reference`` stays the
deciding step.

Three diff rounds found a secret riding into the result inside an
operator-typed vault string (a title, a label, a section) past every detector
tried. The mechanism is cut: no operator-typed string is returned at all. An
item is named by the query term it matched and its op id; a field by its op
id, a role from a closed set derived from the label, and its type.
"""
from __future__ import annotations

import json
import logging
import subprocess as _sp
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
_GMAIL_FIELDS_PROJECTED = [
    {"id": "a", "role": "client_id", "type": "STRING"},
    {"id": "b", "role": "client_secret", "type": "CONCEALED"},
    {"id": "c", "role": "email", "type": "STRING"},
]


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


def _doc_with(extra: dict, fields=None) -> dict:
    doc = dict(_GMAIL_DOC)
    doc.update(extra)
    if fields is not None:
        doc["fields"] = fields
    return doc


def _observability(tools_mod, monkeypatch, required):
    monkeypatch.setattr(
        tools_mod, "_resolved_observability",
        lambda name, *, manifest=None: {
            "granted_tools": [], "required_env_vars": list(required),
            "setup_tool": "setup_gmail"})


def _gmail_env(monkeypatch, tools_mod, required=_REQUIRED, *, token="ops_CANARY_TOKEN"):
    _observability(tools_mod, monkeypatch, required)
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", token)
    for v in _REQUIRED:
        monkeypatch.delenv(v, raising=False)


async def _add(tools_mod):
    r = await tools_mod.plugin_add.handler({
        "name": "gmail", "repo": "bonzanni/casa-plugin-gmail", "ref": "v0.7.0",
        "targets": ["resident:assistant"]})
    return json.loads(r["content"][0]["text"])


def _gmail_tools(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    return st, tools_mod


# --- the happy path ---------------------------------------------------------

async def test_one_matching_item_is_returned_as_ids_and_roles(monkeypatch, tmp_path):
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)
    calls: list[list[str]] = []
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC}, calls=calls)):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    sc = payload["secret_candidates"]
    assert sc["vault"] == "Casa"
    assert sc["queries"] == ["gmail"]
    assert sc["unresolved"] == _REQUIRED
    assert sc["items"] == [{"matched_query": "gmail", "id": "abc123",
                            "fields": _GMAIL_FIELDS_PROJECTED}]
    serialised = json.dumps(payload)
    for forbidden in (_CANARY, "ops_CANARY_TOKEN", "op://", "Gmail", "client id",
                      "Account", "x.apps", "casa-user"):
        assert forbidden not in serialised, forbidden
    # Every op call was scoped to the default vault.
    assert all("--vault" in c and c[c.index("--vault") + 1] == "Casa" for c in calls)


async def test_vendor_stem_query_is_tried_when_the_name_finds_nothing(monkeypatch, tmp_path):
    """Queries: the plugin name, then each distinct vendor stem of the
    unresolved vars, deduplicated, at most three."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="voicemail"))
    _gmail_env(monkeypatch, tools_mod,
               ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "OPENAI_KEY"])
    item = {"id": "e1", "title": "ElevenLabs", "category": "API_CREDENTIAL"}
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([item], {"e1": {"fields": []}})):
        r = await tools_mod.plugin_add.handler({
            "name": "voicemail", "repo": "o/r", "ref": "v1",
            "targets": ["resident:assistant"]})
    sc = json.loads(r["content"][0]["text"])["secret_candidates"]
    assert sc["queries"] == ["voicemail", "elevenlabs", "openai"]
    assert sc["items"] == [{"matched_query": "elevenlabs", "id": "e1", "fields": []}]


async def test_already_resolved_vars_are_not_listed_as_unresolved(monkeypatch, tmp_path):
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)
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
    _gmail_env(monkeypatch, tools_mod)
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC})):
        r = await tools_mod.plugin_update.handler({"name": "gmail", "new_ref": "v0.7.0"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True, payload
    assert payload["secret_candidates"]["items"][0]["id"] == "abc123"


# --- absence: nothing to explore for -----------------------------------------

async def test_no_default_vault_means_no_exploration(monkeypatch, tmp_path):
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT", raising=False)

    def _no_op(cmd, **_kw):
        raise AssertionError(f"op must not be called: {cmd}")

    with patch.object(tools_mod.subprocess, "run", _no_op):
        payload = await _add(tools_mod)
    assert "secret_candidates" not in payload
    assert payload["required_env_vars"] == _REQUIRED


async def test_no_required_vars_means_no_exploration(monkeypatch, tmp_path):
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod, [])

    def _no_op(cmd, **_kw):
        raise AssertionError(f"op must not be called: {cmd}")

    with patch.object(tools_mod.subprocess, "run", _no_op):
        payload = await _add(tools_mod)
    assert "secret_candidates" not in payload


# --- failures are classified and never fail the mutation ----------------------

_ENVELOPE_KEYS = ("artifact_id", "version", "revision", "activation_committed",
                  "runtime_ready", "verify", "granted_tools", "required_env_vars",
                  "setup_tool")


async def test_op_failure_is_classified_and_does_not_fail_the_add(monkeypatch, tmp_path):
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC}, fail="list")):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    assert payload["secret_candidates"] == {"error": "op_failed", "exit_code": 1}
    assert _CANARY not in json.dumps(payload)


async def test_malformed_op_output_is_classified_and_the_envelope_holds(monkeypatch, tmp_path):
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)
    with patch.object(tools_mod.subprocess, "run",
                      lambda cmd, **_kw: _Res(0, stdout=f"not json {_CANARY}")):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    assert payload["secret_candidates"] == {"error": "op_unreadable"}
    assert _CANARY not in json.dumps(payload)
    for key in _ENVELOPE_KEYS:
        assert key in payload, key


async def test_op_timeout_is_classified_and_the_envelope_holds(monkeypatch, tmp_path):
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)

    def _slow(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, kw.get("timeout", 30))

    with patch.object(tools_mod.subprocess, "run", _slow):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    assert payload["secret_candidates"] == {"error": "op_timeout"}
    for key in _ENVELOPE_KEYS:
        assert key in payload, key


async def test_a_foreign_exception_is_classified_and_never_reaches_the_log(
        monkeypatch, tmp_path, caplog):
    """The catch-all logs the exception's class, never its message or a
    traceback; a message can carry what op printed."""
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)

    def _boom(cmd, **_kw):
        raise OSError(f"op binary missing {_CANARY}")

    with caplog.at_level(logging.DEBUG), patch.object(tools_mod.subprocess, "run", _boom):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    assert payload["secret_candidates"] == {"error": "op_failed", "exit_code": -1}
    rendered = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert _CANARY not in rendered
    assert "OSError" in rendered
    assert _CANARY not in json.dumps(payload)


# --- ordering: exploration never delays activation ----------------------------

async def test_add_explores_after_the_reload(monkeypatch, tmp_path):
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)
    inner = _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC})

    def _logging_op(cmd, **kw):
        st.log.append("op")
        return inner(cmd, **kw)

    with patch.object(tools_mod.subprocess, "run", _logging_op):
        payload = await _add(tools_mod)
    assert payload["ok"] is True
    first_op = st.log.index("op")
    assert st.log.index("dispatch:assistant") < first_op
    assert st.log.index("reload_snapshot") < first_op


async def test_update_explores_after_the_reload_too(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"] = [_entry(name="gmail", version="0.5.6")]
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(name="gmail", version="0.7.0"))
    _gmail_env(monkeypatch, tools_mod)
    inner = _fake_op([_GMAIL_ITEM], {"abc123": _GMAIL_DOC})

    def _logging_op(cmd, **kw):
        st.log.append("op")
        return inner(cmd, **kw)

    with patch.object(tools_mod.subprocess, "run", _logging_op):
        r = await tools_mod.plugin_update.handler({"name": "gmail", "new_ref": "v0.7.0"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True, payload
    first_op = st.log.index("op")
    assert st.log.index("dispatch:assistant") < first_op
    assert st.log.index("reload_snapshot") < first_op


# --- no operator-typed string is ever returned --------------------------------
#
# Diff rounds 1–3 each found a secret riding in a title, a label or a section
# (duplicated, split across labels, or typed as the label itself) past every
# comparison the code tried, and round 3 also found ordinary labels withheld
# because the notes mentioned them. None of that can happen when nothing
# operator-typed is returned and nothing is compared against the document.

@pytest.mark.parametrize("doc_extra, title", [
    ({}, f"Gmail {_CANARY}"),                               # value in the title
    ({"notesPlain": f"note {_CANARY}"}, f"Gmail {_CANARY}"),
    ({"urls": [{"href": f"https://x/{_CANARY}"}]}, f"Gmail https://x/{_CANARY}"),
    ({"tags": [_CANARY]}, f"Gmail {_CANARY}"),
    ({}, "Gmail ops_CANARY_TOKEN"),                         # the token in the title
    ({}, "Gmail sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"),
    ({}, "Gmail key=value"),
    ({}, "Gmail op://Casa/x/y"),
], ids=["value", "notesPlain", "url", "tag", "token", "long-run", "equals", "colon"])
async def test_the_title_is_never_returned(monkeypatch, tmp_path, doc_extra, title):
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([dict(_GMAIL_ITEM, title=title)],
                               {"abc123": _doc_with(doc_extra)})):
        payload = await _add(tools_mod)
    item = payload["secret_candidates"]["items"][0]
    assert item["matched_query"] == "gmail"   # the matched query term
    assert "name" not in item            # #1019: never a key a reader takes for the title
    assert item["id"] == "abc123"
    serialised = json.dumps(payload)
    for forbidden in (_CANARY, "ops_CANARY_TOKEN", "sk-live", "key=value", "op://"):
        assert forbidden not in serialised, forbidden


async def test_labels_and_sections_are_never_returned(monkeypatch, tmp_path):
    """A value duplicated into a label or a section label; a secret typed AS
    the label; fragments of any length across labels: all of it is a label,
    and labels are not returned. Only ids, roles and types are."""
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)
    fragmented_value = "CanaryAb-Qx123-Zy789"
    fields = [
        {"id": "a", "label": f"client secret {_CANARY}", "type": "CONCEALED", "value": _CANARY},
        {"id": "b", "label": "email", "type": "STRING", "value": "casa-user",
         "section": {"id": "s", "label": f"Account {_CANARY}"}},
        {"id": "c", "label": "hunter2 correct horse", "type": "STRING", "value": "-",
         "section": {"id": "s2", "label": "battery staple"}},
    ] + [{"id": f"f{i}", "label": f"part {i} {fragmented_value[i*7:(i+1)*7]}", "type": "STRING",
          "value": "-"} for i in range(3)]
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([_GMAIL_ITEM], {"abc123": _doc_with({}, fields)})):
        payload = await _add(tools_mod)
    got = payload["secret_candidates"]["items"][0]["fields"]
    assert got == [
        {"id": "a", "role": None, "type": "CONCEALED"},   # label is not a known role
        {"id": "b", "role": "email", "type": "STRING"},
        {"id": "c", "role": None, "type": "STRING"},
        {"id": "f0", "role": None, "type": "STRING"},
        {"id": "f1", "role": None, "type": "STRING"},
        {"id": "f2", "role": None, "type": "STRING"},
    ]
    serialised = json.dumps(payload)
    for forbidden in (_CANARY, "Account", "hunter2", "staple", "part", "CanaryA",
                      "b-Qx123", "-Zy789", "label", "section"):
        assert forbidden not in serialised, forbidden


async def test_notes_mentioning_the_labels_do_not_hide_the_roles(monkeypatch, tmp_path):
    """Astra r3 F2.1: comparing labels against the document withheld five of
    six ordinary labels once the notes mentioned them. Roles are derived from
    the label alone; the document is never consulted."""
    st, tools_mod = _gmail_tools(monkeypatch, tmp_path)
    _gmail_env(monkeypatch, tools_mod)
    fields = [
        {"id": "a", "label": "credential", "type": "CONCEALED", "value": "-"},
        {"id": "b", "label": "username", "type": "STRING", "value": "-"},
        {"id": "c", "label": "hostname", "type": "STRING", "value": "-"},
        {"id": "d", "label": "client id", "type": "STRING", "value": "-"},
        {"id": "e", "label": "client secret", "type": "CONCEALED", "value": "-"},
        {"id": "f", "label": "email", "type": "EMAIL", "value": "-"},
    ]
    notes = ("API credential for Casa. The username is the email address. Use this "
             "hostname for API calls. Store the client id and client secret together.")
    with patch.object(tools_mod.subprocess, "run",
                      _fake_op([_GMAIL_ITEM], {"abc123": _doc_with({"notesPlain": notes}, fields)})):
        payload = await _add(tools_mod)
    roles = [f["role"] for f in payload["secret_candidates"]["items"][0]["fields"]]
    assert roles == ["credential", "username", "hostname", "client_id", "client_secret", "email"]


@pytest.mark.parametrize("label, role", [
    ("client id", "client_id"), ("Client_ID", "client_id"), ("OAuth client secret", "client_secret"),
    ("email", "email"), ("E-mail address", "email"), ("API key", "api_key"), ("apikey", "api_key"),
    ("refresh token", "refresh_token"), ("access token", "access_token"), ("token", "token"),
    ("username", "username"), ("password", "password"), ("hostname", "hostname"),
    ("Account ID", "account"), ("credential", "credential"), ("bot token", "other_known"),
    ("Vault", "other_known"), ("database", "other_known"), ("port", "other_known"),
    ("type", "other_known"), ("one-time password", "other_known"), ("notes", "other_known"),
    ("hunter2 password", None), ("key GOCSPXa", None), ("id2", None), ("", None), (None, None),
])
def test_field_roles(label, role):
    import tools as tools_mod
    assert tools_mod._field_role(label) == role


def test_untrusted_ids_and_types_are_dropped():
    import tools as tools_mod
    doc = {"fields": [
        {"id": "ok_1-A", "label": "email", "type": "EMAIL"},
        {"id": "bad id with spaces GOCSPX", "label": "email", "type": "NOT_A_TYPE"},
        {"id": 7, "label": "email", "type": None},
        "not a field",
    ]}
    assert tools_mod._project_item_fields(doc) == [
        {"id": "ok_1-A", "role": "email", "type": "EMAIL"},
        {"id": None, "role": "email", "type": None},
        {"id": None, "role": "email", "type": None},
    ]
