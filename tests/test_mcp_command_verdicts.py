"""P1 (plugin-mcp-selfcontainment plan): static resolvability check of a
plugin's ``.mcp.json`` launch references.

``mcp_command_verdicts`` detects *resolvable command and artifact-file
references* — NOT "the MCP server can spawn" (missing imports, bad shebang,
bad cwd, dead external services stay invisible; a handshake probe is a
non-goal). Blocking wiring: a ``missing`` verdict surfaces as the
``mcp_command_missing`` reason in ``verify_plugin_state``; opaque shapes
(shell-form commands, env-dependent references) are ``unchecked`` and never
block.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plugin_fixtures import entry, mk_artifact, mk_registry

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# helper: mcp_command_verdicts
# ---------------------------------------------------------------------------


def _write_mcp(root: Path, servers: dict) -> Path:
    p = root / ".mcp.json"
    p.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return p


def _verdicts(root: Path, servers: dict, **kw):
    from plugin_store import mcp_command_verdicts
    return mcp_command_verdicts(_write_mcp(root, servers), root, **kw)


def _by_server(rows, name):
    return [r for r in rows if r["server"] == name]


def test_absolute_root_ref_missing(tmp_path):
    rows = _verdicts(tmp_path, {"gmail": {
        "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
    (row,) = _by_server(rows, "gmail")
    assert row["status"] == "missing"
    assert ".venv/bin/python" in row["reason"]


def test_absolute_root_ref_present_executable(tmp_path):
    exe = tmp_path / "bin" / "run"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    rows = _verdicts(tmp_path, {"s": {
        "command": "${CLAUDE_PLUGIN_ROOT}/bin/run"}})
    (row,) = _by_server(rows, "s")
    assert row["status"] == "ok"


def test_absolute_root_ref_present_not_executable(tmp_path):
    exe = tmp_path / "bin" / "run"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o644)
    rows = _verdicts(tmp_path, {"s": {
        "command": "${CLAUDE_PLUGIN_ROOT}/bin/run"}})
    (row,) = _by_server(rows, "s")
    assert row["status"] == "missing"
    assert "not executable" in row["reason"]


def test_bare_command_found_via_injected_resolver(tmp_path):
    rows = _verdicts(tmp_path, {"s": {"command": "node"}},
                     _which=lambda c: "/usr/bin/node" if c == "node" else None)
    (row,) = _by_server(rows, "s")
    assert row["status"] == "ok"


def test_bare_command_missing_via_injected_resolver(tmp_path):
    rows = _verdicts(tmp_path, {"s": {"command": "nodezilla"}},
                     _which=lambda c: None)
    (row,) = _by_server(rows, "s")
    assert row["status"] == "missing"
    assert "PATH" in row["reason"]


def test_foreign_env_var_unchecked(tmp_path):
    rows = _verdicts(tmp_path, {"s": {"command": "${GMAIL_HOME}/bin/serve"}})
    (row,) = _by_server(rows, "s")
    assert row["status"] == "unchecked"


def test_plugin_data_var_unchecked(tmp_path):
    """${CLAUDE_PLUGIN_DATA} is runtime-populated — its own unchecked case,
    distinct from unknown foreign vars."""
    rows = _verdicts(tmp_path, {"s": {"command": "${CLAUDE_PLUGIN_DATA}/x"}})
    (row,) = _by_server(rows, "s")
    assert row["status"] == "unchecked"
    assert "CLAUDE_PLUGIN_DATA" in row["reason"]


def test_shell_form_command_unchecked(tmp_path):
    """Whitespace/shell syntax: single-executable-token support only."""
    rows = _verdicts(tmp_path, {"s": {"command": "python3 -m server"}})
    (row,) = _by_server(rows, "s")
    assert row["status"] == "unchecked"


def test_relative_path_command_unchecked(tmp_path):
    """A relative path with a separator resolves against the CLI's spawn cwd,
    which verify cannot know — conservative unchecked, never a false block."""
    rows = _verdicts(tmp_path, {"s": {"command": "server/run.sh"}})
    (row,) = _by_server(rows, "s")
    assert row["status"] == "unchecked"


def test_url_server_skipped(tmp_path):
    rows = _verdicts(tmp_path, {"remote": {"url": "https://example.com/sse"}})
    assert _by_server(rows, "remote") == []


def test_args_root_ref_missing_blocks(tmp_path):
    rows = _verdicts(
        tmp_path,
        {"s": {"command": "python3",
               "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"]}},
        _which=lambda c: "/usr/bin/python3")
    assert any(r["status"] == "missing" and "server.py" in r["reason"]
               for r in _by_server(rows, "s"))


def test_args_root_ref_present_needs_no_exec_bit(tmp_path):
    """python3 ${CLAUDE_PLUGIN_ROOT}/server/server.py is legitimate with a
    non-executable source file (existence only, no X_OK)."""
    src = tmp_path / "server" / "server.py"
    src.parent.mkdir()
    src.write_text("print('hi')\n", encoding="utf-8")
    src.chmod(0o644)
    rows = _verdicts(
        tmp_path,
        {"s": {"command": "python3",
               "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"]}},
        _which=lambda c: "/usr/bin/python3")
    assert all(r["status"] == "ok" for r in _by_server(rows, "s"))


def test_args_foreign_var_skipped(tmp_path):
    rows = _verdicts(
        tmp_path,
        {"s": {"command": "python3", "args": ["${SOME_DIR}/x.py"]}},
        _which=lambda c: "/usr/bin/python3")
    assert all(r["status"] != "missing" for r in _by_server(rows, "s"))


def test_absent_mcp_json_no_rows(tmp_path):
    from plugin_store import mcp_command_verdicts
    assert mcp_command_verdicts(tmp_path / ".mcp.json", tmp_path) == []


# --- Sol r4 hardening -------------------------------------------------------


def test_malformed_args_is_malformed_not_crash(tmp_path):
    """Sol r4-1: `"args": 1` beside a valid command must mark the file
    malformed (mcp_invalid path) and must NOT raise from the verdicts
    helper mid-§3.9."""
    from plugin_store import parse_mcp_servers, mcp_command_verdicts
    p = _write_mcp(tmp_path, {"s": {"command": "python3", "args": 1}})
    _, malformed = parse_mcp_servers(p)
    assert malformed is True
    mcp_command_verdicts(p, tmp_path, _which=lambda c: "/x")  # no raise


def test_malformed_args_entry_is_malformed(tmp_path):
    from plugin_store import parse_mcp_servers
    p = _write_mcp(tmp_path, {"s": {"command": "python3", "args": [1, "ok"]}})
    assert parse_mcp_servers(p)[1] is True


def test_args_directory_ref_is_ok(tmp_path):
    """Sol r4-8: `--directory ${ROOT}/server` must not false-block — a
    directory is a legitimate path argument."""
    (tmp_path / "server").mkdir()
    rows = _verdicts(
        tmp_path,
        {"s": {"command": "python3",
               "args": ["--directory", "${CLAUDE_PLUGIN_ROOT}/server"]}},
        _which=lambda c: "/usr/bin/python3")
    assert all(r["status"] == "ok" for r in _by_server(rows, "s"))


def test_args_embedded_option_ref_checked(tmp_path):
    """Sol r4-8: `--config=${ROOT}/config.json` — the path after `=` is
    checked, missing blocks, present passes."""
    rows = _verdicts(
        tmp_path,
        {"s": {"command": "python3",
               "args": ["--config=${CLAUDE_PLUGIN_ROOT}/config.json"]}},
        _which=lambda c: "/usr/bin/python3")
    assert any(r["status"] == "missing" for r in _by_server(rows, "s"))
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    rows = _verdicts(
        tmp_path,
        {"s2": {"command": "python3",
                "args": ["--config=${CLAUDE_PLUGIN_ROOT}/config.json"]}},
        _which=lambda c: "/usr/bin/python3")
    assert all(r["status"] == "ok" for r in _by_server(rows, "s2"))


def test_command_traversal_escape_blocks_even_if_target_exists(tmp_path):
    """Sol r4-9: ${ROOT}/../x escaping the artifact must block even when the
    escaped target exists — verify must not bless mutable sibling content."""
    root = tmp_path / "artifact"
    root.mkdir()
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)
    from plugin_store import mcp_command_verdicts
    p = root / ".mcp.json"
    p.write_text(json.dumps({"mcpServers": {"s": {
        "command": "${CLAUDE_PLUGIN_ROOT}/../outside.sh"}}}), encoding="utf-8")
    rows = mcp_command_verdicts(p, root)
    (row,) = [r for r in rows if r["server"] == "s"]
    assert row["status"] == "missing"
    assert "escape" in row["reason"]


def test_symlink_escape_blocks(tmp_path):
    root = tmp_path / "artifact"
    (root / "bin").mkdir(parents=True)
    outside = tmp_path / "evil"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)
    (root / "bin" / "run").symlink_to(outside)
    from plugin_store import mcp_command_verdicts
    p = root / ".mcp.json"
    p.write_text(json.dumps({"mcpServers": {"s": {
        "command": "${CLAUDE_PLUGIN_ROOT}/bin/run"}}}), encoding="utf-8")
    rows = mcp_command_verdicts(p, root)
    (row,) = [r for r in rows if r["server"] == "s"]
    assert row["status"] == "missing"
    assert "escape" in row["reason"]


def test_env_root_ref_missing_blocks(tmp_path):
    """Sol r4-6: PYTHONPATH=${ROOT}/server/vendor is the vendored pattern's
    load-bearing path — a missing vendor dir must block."""
    rows = _verdicts(
        tmp_path,
        {"s": {"command": "python3",
               "env": {"PYTHONPATH": "${CLAUDE_PLUGIN_ROOT}/server/vendor"}}},
        _which=lambda c: "/usr/bin/python3")
    assert any(r["status"] == "missing" and "vendor" in r["reason"]
               for r in _by_server(rows, "s"))


def test_env_root_ref_dir_present_ok(tmp_path):
    (tmp_path / "server" / "vendor").mkdir(parents=True)
    rows = _verdicts(
        tmp_path,
        {"s": {"command": "python3",
               "env": {"PYTHONPATH": "${CLAUDE_PLUGIN_ROOT}/server/vendor"}}},
        _which=lambda c: "/usr/bin/python3")
    assert all(r["status"] == "ok" for r in _by_server(rows, "s"))


def test_env_colon_joined_segments_checked(tmp_path):
    (tmp_path / "a").mkdir()
    rows = _verdicts(
        tmp_path,
        {"s": {"command": "python3",
               "env": {"PYTHONPATH":
                       "${CLAUDE_PLUGIN_ROOT}/a:${CLAUDE_PLUGIN_ROOT}/b"}}},
        _which=lambda c: "/usr/bin/python3")
    assert any(r["status"] == "missing" for r in _by_server(rows, "s"))


def test_env_foreign_var_value_skipped(tmp_path):
    rows = _verdicts(
        tmp_path,
        {"s": {"command": "python3",
               "env": {"GMAIL_SA": "${GMAIL_SA}"}}},
        _which=lambda c: "/usr/bin/python3")
    assert all(r["status"] != "missing" for r in _by_server(rows, "s"))


def test_verify_stale_plain_secret_reports_unresolved(tmp_path):
    """Sol r4-10 (rotation stale-green): a PLAIN conf value that differs from
    the effective os.environ value means the reload hasn't run — must NOT
    report resolved."""
    import os as _os
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"], mcp_servers={
        "s": {"command": "python3", "env": {"MY_TOKEN": "${MY_TOKEN}"}}})
    mk_registry(tmp_path, [e])
    (tmp_path / "plugin-env.conf").write_text("MY_TOKEN=newvalue\n",
                                              encoding="utf-8")
    _os.environ["MY_TOKEN"] = "oldvalue"
    try:
        r = _verify(tmp_path)
    finally:
        _os.environ.pop("MY_TOKEN", None)
    (row,) = [s_ for s_ in r["secrets"] if s_["var"] == "MY_TOKEN"]
    assert row["status"] == "unresolved"
    assert "reload" in row["reason"]


# ---------------------------------------------------------------------------
# verify_plugin_state wiring
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    import system_requirements.manifest as mani
    import plugin_env_conf as pec
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")
    monkeypatch.setattr(pec, "PLUGIN_ENV_CONF_PATH", tmp_path / "plugin-env.conf")


def _verify(tmp_path, name="probe"):
    from tools import _tool_verify_plugin_state
    return _tool_verify_plugin_state(
        plugin_name=name,
        _registry_path=tmp_path / "registry.json",
        _store_root=tmp_path / "store")


def test_verify_gmail_shaped_missing_interpreter_blocks(tmp_path):
    """The live incident: a gitignored per-plugin venv interpreter is absent
    from the installed artifact → precise blocking reason."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"], mcp_servers={
        "gmail": {"command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python",
                  "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"]}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert "mcp_command_missing" in r["reasons"]
    assert any(row["status"] == "missing" for row in r["mcp_commands"])
    assert r["targets"][0]["ready"] is False


def test_verify_committed_entrypoint_ready(tmp_path):
    """Committed-bundle shape: baseline interpreter + committed entry file → ready."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    root = mk_artifact(store, "probe", e["artifact_id"], mcp_servers={
        "inv": {"command": "python3",
                "args": ["${CLAUDE_PLUGIN_ROOT}/server/dist/index.py"]}},
        extra_files={"server/dist/index.py": "print('serve')\n"})
    assert (root / "server" / "dist" / "index.py").is_file()
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True
    assert all(row["status"] == "ok" for row in r["mcp_commands"])


def test_verify_unchecked_shape_does_not_block(tmp_path, monkeypatch):
    # The command VERDICT stays "unchecked" and non-blocking for an
    # env-dependent command — but since #423 r2 (Sol 5) the ${VAR} inside it
    # is a required SECRET (the CLI expands command/args/url/headers too, and
    # an unset var spawns a literal-placeholder command). With the var
    # resolved, unchecked still never blocks.
    monkeypatch.setenv("SOME_TOOL_HOME", "/opt/some-tool")
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"], mcp_servers={
        "s": {"command": "${SOME_TOOL_HOME}/bin/serve"}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True
    assert "mcp_command_missing" not in r["reasons"]
    assert any(row["status"] == "unchecked" for row in r["mcp_commands"])


def test_verify_env_dependent_command_requires_the_var(tmp_path, monkeypatch):
    # #423 r2 (Sol 5): the same shape with the var UNRESOLVED blocks
    # readiness through the secrets channel, naming the variable.
    monkeypatch.delenv("SOME_TOOL_HOME", raising=False)
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"], mcp_servers={
        "s": {"command": "${SOME_TOOL_HOME}/bin/serve"}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert any(row["var"] == "SOME_TOOL_HOME"
               and row["status"] == "unresolved"
               for row in r["secrets"])


def test_verify_reason_order_keeps_artifact_reason_first(tmp_path):
    """mcp_command_missing must not mask artifact-integrity reasons in the
    single-reason health rollup (reason_code = reasons[0])."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    root = mk_artifact(store, "probe", e["artifact_id"], mcp_servers={
        "s": {"command": "${CLAUDE_PLUGIN_ROOT}/gone/bin/x"}})
    (root / "tampered.md").write_text("evil", encoding="utf-8")
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert r["reasons"][0] in ("artifact_invalid", "corrupt_artifact")


# ---------------------------------------------------------------------------
# #431 r1: the var-reference scanner these verdicts use was, in v0.154.0,
# SHADOWED by a same-named declaration-grammar constant defined later in
# plugin_store. Every lookup resolved to the wrong pattern, so no `${VAR}`
# was ever detected — a var-dependent command was probed as a literal path
# and reported `mcp_command_missing`, which REFUSES a bundled install.
# ---------------------------------------------------------------------------

def test_ref_scanner_is_not_shadowed_by_the_declaration_grammar():
    """The regression itself, at the smallest possible scope."""
    import plugin_store
    assert plugin_store._MCP_REF_VAR_RE.search("${FOO}")
    assert plugin_store._MCP_REF_VAR_RE.search("${FOO:-bar}")
    # The declaration grammar is a DIFFERENT name and must not match a
    # reference (it grades bare declared names, not `${...}` syntax).
    assert not plugin_store._DECLARABLE_ENV_NAME_RE.fullmatch("${FOO}")
    assert plugin_store._DECLARABLE_ENV_NAME_RE.fullmatch("CASA_PLUGIN_FOO")


@pytest.mark.parametrize("command", ["${MCP_BIN}", "${MCP_BIN:-node}"])
def test_var_dependent_command_is_unchecked_never_missing(tmp_path, command):
    """Both documented expansion forms are unresolvable here by design — the
    value is only known at spawn time — so they report `unchecked` and never
    block. Reporting `missing` would refuse the plugin's bundled install."""
    import json as _json
    import plugin_store
    root = tmp_path / "art"
    root.mkdir()
    mcp = root / ".mcp.json"
    mcp.write_text(_json.dumps({"mcpServers": {"s": {"command": command}}}),
                   encoding="utf-8")
    rows = plugin_store.mcp_command_verdicts(mcp, root, _which=lambda _c: None)
    assert [r["status"] for r in rows] == ["unchecked"]


@pytest.mark.parametrize("arg,expect", [
    # r1 (Sol): the CLI ALWAYS sets these, so a default is meaningless — but
    # the defaulted spelling used to skip the exact-prefix containment check
    # and resolve OUTSIDE the checksummed artifact at runtime.
    ("${CLAUDE_PLUGIN_ROOT:-.}/../elsewhere/server.py", "missing"),
    ("${CLAUDE_PLUGIN_ROOT:-.}/nope.py", "missing"),
    ("${CLAUDE_PLUGIN_ROOT}/server/ok.py", "ok"),
    ("${CLAUDE_PLUGIN_ROOT:-.}/server/ok.py", "ok"),
])
def test_defaulted_plugin_root_is_held_to_containment(tmp_path, arg, expect):
    import json as _json
    import plugin_store
    root = tmp_path / "art"
    (root / "server").mkdir(parents=True)
    (root / "server" / "ok.py").write_text("x", encoding="utf-8")
    mcp = root / ".mcp.json"
    mcp.write_text(_json.dumps({"mcpServers": {"s": {
        "command": "python3", "args": [arg]}}}), encoding="utf-8")
    rows = plugin_store.mcp_command_verdicts(
        mcp, root, _which=lambda _c: "/usr/bin/python3")
    arg_rows = [r for r in rows if r["ref"] != "python3"]
    assert [r["status"] for r in arg_rows] == [expect]
