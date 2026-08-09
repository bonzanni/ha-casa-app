"""§3.8: an engagement's plugin_artifacts binding is immutable and survives
every serialization layer (tombstone write + reload), and the run script
renders --plugin-dir flags from the RECORDED paths."""
from __future__ import annotations

import time

import pytest

from engagement_registry import EngagementRegistry

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_ARTIFACTS = [
    {"name": "superpowers", "artifact_id": "a" * 64,
     "path": "/config/plugins/store/superpowers/" + "a" * 64},
    {"name": "context7", "artifact_id": "b" * 64,
     "path": "/config/plugins/store/context7/" + "b" * 64},
]


async def test_create_persists_plugin_artifacts_roundtrip(tmp_path):
    path = str(tmp_path / "e.json")
    reg = EngagementRegistry(tombstone_path=path, bus=None)
    rec = await reg.create(
        "executor", "plugin-developer", "claude_code", "task", {}, 1,
        plugin_artifacts=_ARTIFACTS)
    assert rec.plugin_artifacts == tuple(_ARTIFACTS)
    # Reload from disk — the field survives serialization.
    reg2 = EngagementRegistry(tombstone_path=path, bus=None)
    await reg2.load()
    reloaded = reg2.get(rec.id)
    assert reloaded is not None
    assert list(reloaded.plugin_artifacts) == _ARTIFACTS


async def test_every_mutator_preserves_plugin_artifacts(tmp_path):
    path = str(tmp_path / "e.json")
    reg = EngagementRegistry(tombstone_path=path, bus=None)
    rec = await reg.create(
        "executor", "plugin-developer", "claude_code", "task", {}, 1,
        plugin_artifacts=_ARTIFACTS)
    await reg.mark_idle(rec.id)
    await reg.persist_session_id(rec.id, "sess-xyz")
    await reg.mark_completed(rec.id, completed_at=time.time())
    reg2 = EngagementRegistry(tombstone_path=path, bus=None)
    await reg2.load()
    reloaded = reg2.get(rec.id)
    assert list(reloaded.plugin_artifacts) == _ARTIFACTS


async def test_run_script_contains_plugin_dir_flags_from_record(tmp_path):
    from types import SimpleNamespace
    from drivers.workspace import render_run_script
    eng = SimpleNamespace(id="e" * 32, plugin_artifacts=_ARTIFACTS)
    out = render_run_script(
        engagement_id=eng.id, permission_mode="acceptEdits", extra_dirs=[],
        plugin_dirs=[pa["path"] for pa in eng.plugin_artifacts], uid=200005, gid=200005)
    for pa in _ARTIFACTS:
        assert f"--plugin-dir {pa['path']}" in out


# ---------------------------------------------------------------------------
# #429: the run script is a plugin-attach path of its own — it hands the
# recorded artifacts to a SUPERVISED CLI via --plugin-dir, outside the
# in-process option builders. Round 1 fixed the driver's start path; round 2
# found boot reconciliation re-rendering the same service pair without it. The
# overlay is therefore derived inside render_run_script, from the plugin_dirs
# being attached, so no caller can forget it.
# ---------------------------------------------------------------------------

def _declaring_artifact(tmp_path, *, casa, refs=True):
    import json as _json
    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        _json.dumps({"name": "bank-feed", "version": "1.0.0", "casa": casa}),
        encoding="utf-8")
    env = ({"KEY": "${CASA_PLUGIN_BANKFEED_PRIVATE_KEY}",
            "CP": "${CASA_PLUGIN_BANKFEED_CP_TOKEN:-}"} if refs else {})
    (root / ".mcp.json").write_text(_json.dumps({"mcpServers": {"bank-feed": {
        "command": "node", "env": env}}}), encoding="utf-8")
    return root


_BANKFEED_CASA = {
    "setupTool": "setup_bank_feed",
    "setupProvides": ["CASA_PLUGIN_BANKFEED_PRIVATE_KEY"],
}


def _clear(monkeypatch):
    for var in ("CASA_PLUGIN_BANKFEED_PRIVATE_KEY",
                "CASA_PLUGIN_BANKFEED_CP_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_run_script_pins_declared_vars_as_empty_exports(tmp_path, monkeypatch):
    """Without this a declared-but-unresolved ${VAR} reaches the MCP server as
    the literal placeholder."""
    from drivers.workspace import render_run_script
    _clear(monkeypatch)
    root = _declaring_artifact(tmp_path, casa=_BANKFEED_CASA)
    out = render_run_script(
        engagement_id="e" * 32, permission_mode="acceptEdits", extra_dirs=[],
        plugin_dirs=[str(root)], uid=200005, gid=200005)
    assert "export CASA_PLUGIN_BANKFEED_PRIVATE_KEY=\'\'" in out
    assert "CASA_PLUGIN_BANKFEED_CP_TOKEN" not in out


def test_run_script_pins_a_declared_var_the_launch_config_never_names(
        tmp_path, monkeypatch):
    """Sol r2: driven by the DECLARATION, not the ${VAR} reference set. A
    server that reads its provisioned credential from the inherited
    environment would otherwise get no binding — and would see a leftover
    op:// reference, which an idempotent setup tool can read as 'already
    provisioned' and skip the creation over."""
    from drivers.workspace import render_run_script
    monkeypatch.setenv("CASA_PLUGIN_BANKFEED_PRIVATE_KEY", "op://V/i/f")
    monkeypatch.delenv("CASA_PLUGIN_BANKFEED_CP_TOKEN", raising=False)
    root = _declaring_artifact(tmp_path, casa=_BANKFEED_CASA, refs=False)
    out = render_run_script(
        engagement_id="e" * 32, permission_mode="acceptEdits", extra_dirs=[],
        plugin_dirs=[str(root)], uid=200005, gid=200005)
    assert "export CASA_PLUGIN_BANKFEED_PRIVATE_KEY=\'\'" in out


def test_run_script_leaves_a_wired_value_alone(tmp_path, monkeypatch):
    from drivers.workspace import render_run_script
    monkeypatch.setenv("CASA_PLUGIN_BANKFEED_PRIVATE_KEY", "-----BEGIN KEY----")
    monkeypatch.delenv("CASA_PLUGIN_BANKFEED_CP_TOKEN", raising=False)
    root = _declaring_artifact(tmp_path, casa=_BANKFEED_CASA)
    out = render_run_script(
        engagement_id="e" * 32, permission_mode="acceptEdits", extra_dirs=[],
        plugin_dirs=[str(root)], uid=200005, gid=200005)
    assert "CASA_PLUGIN_BANKFEED_PRIVATE_KEY" not in out
    assert "CASA_PLUGIN_BANKFEED_CP_TOKEN" not in out


def test_run_script_no_overlay_without_declarations(tmp_path, monkeypatch):
    from drivers.workspace import render_run_script
    _clear(monkeypatch)
    root = _declaring_artifact(tmp_path, casa={})
    out = render_run_script(
        engagement_id="e" * 32, permission_mode="acceptEdits", extra_dirs=[],
        plugin_dirs=[str(root)], uid=200005, gid=200005)
    assert "CASA_PLUGIN_BANKFEED" not in out


def test_run_script_never_fails_over_a_broken_artifact(tmp_path):
    """An engagement start must not die because a manifest is unreadable."""
    from drivers.workspace import render_run_script
    out = render_run_script(
        engagement_id="e" * 32, permission_mode="acceptEdits", extra_dirs=[],
        plugin_dirs=[str(tmp_path / "nonexistent")], uid=200005, gid=200005)
    assert "--plugin-dir" in out


def test_boot_reconciliation_render_gets_the_same_overlay(tmp_path,
                                                          monkeypatch):
    """Sol r2 P1: boot healing re-renders the service pair through the SAME
    entry point the driver uses. Pinning it here is what makes 'no caller can
    forget' true rather than aspirational — this call mirrors casa_core's
    boot-replay re-render exactly."""
    from drivers.workspace import render_run_script
    _clear(monkeypatch)
    root = _declaring_artifact(tmp_path, casa=_BANKFEED_CASA)
    rec_artifacts = [{"name": "bank-feed", "artifact_id": "a" * 64,
                      "path": str(root)}]
    out = render_run_script(
        engagement_id="e" * 32, permission_mode="acceptEdits",
        extra_dirs=[], plugin_dirs=[pa["path"] for pa in rec_artifacts], uid=200005, gid=200005)
    assert "export CASA_PLUGIN_BANKFEED_PRIVATE_KEY=\'\'" in out
