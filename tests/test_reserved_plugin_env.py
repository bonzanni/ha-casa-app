"""G6 corrected (Sol v095 DISAGREE, empirically verified on the CLI):
``CLAUDE_PLUGIN_DATA`` is a CLI-RESERVED placeholder. The CLI provides a
native per-plugin data path in every plugin MCP server's environment; a
plugin that SELF-DECLARES the var in ``.mcp.json::env``
(``"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"``) shadows the native
value with the literal string — the exact gmail-v0.4.0 token-in-a-
literal-directory bug, which Casa's own doctrine used to instruct.

Enforcement: self-declaring a reserved CLI var is a blocking verify reason
(``mcp_reserved_env``).
"""
from __future__ import annotations

import json

import pytest

from plugin_fixtures import entry, mk_artifact, mk_registry

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# plugin_store.reserved_env_violations
# ---------------------------------------------------------------------------


def _violations(tmp_path, servers):
    from plugin_store import reserved_env_violations
    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return reserved_env_violations(p)


def test_self_declared_plugin_data_flagged(tmp_path):
    v = _violations(tmp_path, {"s": {
        "command": "python3",
        "env": {"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"}}})
    assert v and "CLAUDE_PLUGIN_DATA" in v[0] and "s" in v[0]


def test_self_declared_plugin_root_flagged(tmp_path):
    v = _violations(tmp_path, {"s": {
        "command": "python3",
        "env": {"CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}"}}})
    assert v and "CLAUDE_PLUGIN_ROOT" in v[0]


def test_reserved_key_flagged_regardless_of_value(tmp_path):
    """Any value under a reserved KEY shadows the CLI's native provision."""
    v = _violations(tmp_path, {"s": {
        "command": "python3",
        "env": {"CLAUDE_PLUGIN_DATA": "/data/custom"}}})
    assert v


def test_normal_env_not_flagged(tmp_path):
    v = _violations(tmp_path, {"s": {
        "command": "python3",
        "env": {"GMAIL_CLIENT_ID": "${GMAIL_CLIENT_ID}",
                "PYTHONPATH": "${CLAUDE_PLUGIN_ROOT}/server/vendor"}}})
    assert v == []


def test_absent_mcp_json_no_violations(tmp_path):
    from plugin_store import reserved_env_violations
    assert reserved_env_violations(tmp_path / ".mcp.json") == []


# ---------------------------------------------------------------------------
# verify wiring
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    import system_requirements.manifest as mani
    import plugin_env_conf as pec
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")
    monkeypatch.setattr(pec, "PLUGIN_ENV_CONF_PATH", tmp_path / "plugin-env.conf")


def test_verify_blocks_reserved_env(tmp_path):
    from tools import _tool_verify_plugin_state
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"], mcp_servers={
        "s": {"command": "python3",
              "env": {"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"}}})
    mk_registry(tmp_path, [e])
    r = _tool_verify_plugin_state(
        plugin_name="probe", _registry_path=tmp_path / "registry.json",
        _store_root=tmp_path / "store")
    assert r["ready"] is False
    assert "mcp_reserved_env" in r["reasons"]
