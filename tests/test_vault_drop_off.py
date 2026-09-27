"""#1047 — the vault drop-off: a sign-in link reaches a plugin's tool through
a vault item the plugin declared, never through a delegation brief.

Pins: the ``casa.dropOffs`` grammar; the write sequence (tagged create from a
stdin template, value never in argv; only Casa-tagged items deleted; an
untagged item with the title refuses the drop and nothing is written); the
tool's plugin resolution (registry OR runtime name, title always from the
runtime name — the owned finance entry is ``finance.bank-feed`` in the
registry and ``bank-feed`` to itself); and that no result, error or log line
carries the value.
"""
from __future__ import annotations

import json
import logging
import subprocess
import types

import pytest

import plugin_registry
import tools
import vault_drop_off as vdo
from plugin_store import StoreError, manifest_drop_offs, validate_manifest

SECRET = "https://signin.example.test/magic?code=abc==&x=[y]\\z"
TITLE = "Casa drop-off bank-feed signin_link"


# ---------------------------------------------------------------------------
# manifest grammar
# ---------------------------------------------------------------------------

def test_absent_declaration_is_no_drop_offs():
    assert manifest_drop_offs({}) == []
    assert manifest_drop_offs({"casa": {}}) == []


def test_a_valid_declaration_is_returned_in_order():
    assert manifest_drop_offs(
        {"casa": {"dropOffs": ["signin_link", "otp"]}}) == ["signin_link", "otp"]


@pytest.mark.parametrize("raw", [
    "signin_link",                      # not a list
    ["Signin"],                         # not lowercase
    ["signin link"],                    # space
    ["a:b"],                            # colon (an op:// segment rejects it)
    [3],                                # not a string
    ["x", "x"],                         # duplicate
    [f"d{i}" for i in range(9)],        # over the cap
], ids=["not-list", "upper", "space", "colon", "int", "dup", "cap"])
def test_a_malformed_declaration_is_refused(raw):
    with pytest.raises(StoreError) as ei:
        manifest_drop_offs({"casa": {"dropOffs": raw}})
    assert ei.value.reason_code == "drop_offs_invalid"


def test_install_validation_refuses_a_malformed_declaration(tmp_path):
    root = tmp_path / "p"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "p", "version": "1.0.0", "casa": {"dropOffs": "nope"}}))
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "p")
    assert ei.value.reason_code == "drop_offs_invalid"


# ---------------------------------------------------------------------------
# the write sequence
# ---------------------------------------------------------------------------

class FakeOp:
    """Scripted ``op``: records every call (argv + stdin) and answers the
    list with *rows*."""

    def __init__(self, rows=None, fail=None):
        self.rows = rows or []
        self.fail = fail or {}
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, cmd, stdin=None):
        self.calls.append((cmd, stdin))
        verb = cmd[2]
        rc = self.fail.get(verb, 0)
        out = json.dumps(self.rows) if verb == "list" else "{}"
        return types.SimpleNamespace(returncode=rc, stdout=out,
                                     stderr=f"op said {SECRET}" if rc else "")

    def verbs(self):
        return [c[0][2] for c in self.calls]


@pytest.fixture
def op_env(monkeypatch):
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")

    def install(fake):
        monkeypatch.setattr(vdo, "_run", fake)
        return fake
    return install


def test_a_drop_creates_one_tagged_item_and_the_value_is_only_on_stdin(op_env):
    fake = op_env(FakeOp())
    assert vdo.store("bank-feed", "signin_link", SECRET) == {"status": "ok"}
    assert fake.verbs() == ["list", "create"]
    for argv, _stdin in fake.calls:
        assert SECRET not in " ".join(argv)
        assert "--vault" in argv and argv[argv.index("--vault") + 1] == "Casa"
    tmpl = json.loads(fake.calls[-1][1])
    assert tmpl["title"] == TITLE and tmpl["tags"] == [vdo.TAG]
    assert [f["value"] for f in tmpl["fields"]] == [SECRET]


def test_an_earlier_casa_drop_is_replaced(op_env):
    fake = op_env(FakeOp(rows=[
        {"id": "old1", "title": TITLE, "tags": [vdo.TAG]},
        {"id": "other", "title": "EnableBanking"},
    ]))
    assert vdo.store("bank-feed", "signin_link", SECRET) == {"status": "ok"}
    assert fake.verbs() == ["list", "delete", "create"]
    assert fake.calls[1][0][3] == "old1"


def test_an_untagged_item_with_the_title_refuses_and_nothing_changes(op_env):
    fake = op_env(FakeOp(rows=[
        {"id": "mine", "title": TITLE, "tags": [vdo.TAG]},
        {"id": "hand", "title": TITLE},
    ]))
    assert vdo.store("bank-feed", "signin_link", SECRET) == {
        "error": "drop_off_collision"}
    assert fake.verbs() == ["list"]


@pytest.mark.parametrize("verb", ["list", "delete", "create"])
def test_an_op_failure_is_classified_and_never_quoted(op_env, verb):
    rows = [{"id": "old1", "title": TITLE, "tags": [vdo.TAG]}]
    op_env(FakeOp(rows=rows, fail={verb: 6}))
    out = vdo.store("bank-feed", "signin_link", SECRET)
    assert out == {"error": "op_failed", "exit_code": 6}


def test_no_vault_or_token_makes_no_call(monkeypatch):
    fake = FakeOp()
    monkeypatch.setattr(vdo, "_run", fake)
    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT", raising=False)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "t")
    assert vdo.store("p", "d", SECRET) == {"error": "no_vault"}
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN")
    assert vdo.store("p", "d", SECRET) == {"error": "no_token"}
    assert fake.calls == []


def test_a_timeout_is_classified(op_env):
    def slow(cmd, stdin=None):
        raise subprocess.TimeoutExpired(cmd, 30)
    op_env(slow)
    assert vdo.store("p", "d", SECRET) == {"error": "op_timeout"}


def test_real_run_keeps_the_value_out_of_argv(monkeypatch):
    """The one real seam: ``_run`` hands the template to ``op`` as stdin."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
    monkeypatch.setattr(vdo.subprocess, "run", fake_run)
    vdo._run(["op", "item", "create", "-"], stdin="TEMPLATE")
    assert seen["kw"]["input"] == "TEMPLATE" and "stdin" not in seen["kw"]
    vdo._run(["op", "item", "list"])
    assert seen["kw"]["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize("value,ok", [
    ("https://x.test/a?b=c", True), ("", False), ("a\nb", False),
    ("x" * 4096, True), ("x" * 4097, False), (None, False), (7, False),
])
def test_value_grammar(value, ok):
    assert vdo.valid_value(value) is ok


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------

def _rp(name, manifest_name="", drop_offs=("signin_link",)):
    return plugin_registry.ResolvedPlugin(
        name=name, artifact_id="a" * 64, path="/x", version="1",
        manifest={"name": manifest_name or name,
                  "casa": {"dropOffs": list(drop_offs)}},
        manifest_name=manifest_name)


@pytest.fixture
def finance(monkeypatch):
    res = plugin_registry.ResolutionResult(registry_valid=True, plugins=[
        _rp("finance.bank-feed", "bank-feed"),
        _rp("gmail", drop_offs=()),
    ])
    monkeypatch.setattr(plugin_registry, "resolve_all", lambda: res)
    stored = []

    def store(plugin, drop_off, value):
        stored.append((plugin, drop_off, value))
        return {"status": "ok"}
    monkeypatch.setattr(vdo, "store", store)
    return stored


@pytest.mark.parametrize("spelling", ["bank-feed", "finance.bank-feed"])
def test_either_name_writes_the_runtime_title(finance, spelling, caplog):
    caplog.set_level(logging.DEBUG)
    out = tools._tool_vault_drop_off(plugin=spelling, drop_off="signin_link",
                                     value=SECRET)
    assert out["status"] == "ok" and out["plugin"] == "bank-feed"
    assert finance == [("bank-feed", "signin_link", SECRET)]
    assert vdo.item_title("bank-feed", "signin_link") == TITLE
    assert SECRET not in json.dumps(out) and SECRET not in caplog.text


def test_an_undeclared_drop_off_names_what_is_declared(finance):
    out = tools._tool_vault_drop_off(plugin="bank-feed", drop_off="otp",
                                     value=SECRET)
    assert out == {"error": "unknown_drop_off", "declared": ["signin_link"]}
    out = tools._tool_vault_drop_off(plugin="gmail", drop_off="signin_link",
                                     value=SECRET)
    assert out == {"error": "unknown_drop_off", "declared": []}
    assert finance == []


def test_an_unknown_or_ambiguous_plugin_writes_nothing(finance, monkeypatch):
    assert tools._tool_vault_drop_off(
        plugin="nope", drop_off="signin_link", value=SECRET) == {
            "error": "unknown_plugin"}
    res = plugin_registry.ResolutionResult(registry_valid=True, plugins=[
        _rp("finance.bank-feed", "bank-feed"),
        _rp("other.bank-feed", "bank-feed")])
    monkeypatch.setattr(plugin_registry, "resolve_all", lambda: res)
    assert tools._tool_vault_drop_off(
        plugin="bank-feed", drop_off="signin_link", value=SECRET) == {
            "error": "ambiguous_plugin"}
    assert finance == []


def test_an_invalid_value_writes_nothing(finance):
    out = tools._tool_vault_drop_off(plugin="bank-feed", drop_off="signin_link",
                                     value="a\nb")
    assert out == {"error": "invalid_value"} and finance == []


def test_a_failed_store_is_returned_without_the_value(finance, monkeypatch,
                                                      caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(vdo, "store",
                        lambda *a: {"error": "op_failed", "exit_code": 1})
    out = tools._tool_vault_drop_off(plugin="bank-feed", drop_off="signin_link",
                                     value=SECRET)
    assert out == {"error": "op_failed", "exit_code": 1}
    assert SECRET not in caplog.text


def test_the_tool_is_the_assistants_alone():
    """Allowed on the assistant resident (role ceiling and runtime list) and
    on no other shipped agent."""
    from pathlib import Path
    root = Path(tools.__file__).parent / "defaults"
    holders = sorted(str(p.relative_to(root)) for p in root.rglob("*.yaml")
                     if "mcp__casa-framework__vault_drop_off" in p.read_text())
    assert holders == ["agents/assistant/runtime.yaml",
                       "roles/resident/assistant/role.yaml"]
