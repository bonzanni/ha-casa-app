"""Task 5: every RUNTIME identity surface derives from the plugin's
manifest name (spec §2.1), never the scoped registry name (``mtg.mtg``).

Covers the three sites found reading plugin_grants.py whole, tools.py
(the A5 requires.plugins gate), and the RESUME path (recorded
plugin_artifacts round-tripping the same runtime name)."""
from __future__ import annotations

import json

import pytest

from plugin_registry import ResolvedPlugin, ResolutionResult, runtime_name

pytestmark = pytest.mark.unit


def _rp(name="mtg.mtg", manifest_name="mtg", path="/tmp/x",
        artifact_id="a" * 64, manifest=None):
    return ResolvedPlugin(name=name, artifact_id=artifact_id, path=path,
                          version="1.0.0", manifest=manifest or {"name": manifest_name},
                          manifest_name=manifest_name)


# --- plugin_grants.py: grant + protected-tool namespacing -------------------


def test_grants_for_resolved_uses_manifest_name(tmp_path):
    """The real derivation is plugin_grants.grants_for_resolved (server-level
    grant strings), reading `.mcp.json` off the resolved artifact's path."""
    import plugin_grants

    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "mtg", "version": "1.0.0"}))
    (root / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"rules": {"command": "python3", "args": ["srv.py"]}}}))
    rp = _rp(path=str(root))

    grants = plugin_grants.grants_for_resolved(rp)
    assert grants == ["mcp__plugin_mtg_rules"]
    assert not any("mtg.mtg" in g for g in grants)


def test_grants_for_resolution_uses_manifest_name(tmp_path):
    import plugin_grants

    root = tmp_path / "art"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"rules": {"command": "x"}}}))
    rp = _rp(path=str(root))
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    assert plugin_grants.grants_for_resolution(res) == ["mcp__plugin_mtg_rules"]


def test_protected_map_uses_manifest_name(tmp_path):
    import plugin_grants

    root = tmp_path / "art"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"rules": {"command": "x"}}}))
    rp = _rp(path=str(root),
              manifest={"casa": {"protectedTools": ["reset"]}})
    res = ResolutionResult(registry_valid=True, plugins=[rp])
    out = plugin_grants.protected_map(res)
    assert out == {
        "mcp__plugin_mtg_rules__reset": {"artifact_id": "a" * 64, "summary": None,
                                         "plugin": "mtg"},
    }


def test_unowned_plugin_grants_unaffected(tmp_path):
    """An unowned entry (manifest_name="") is unchanged — runtime_name falls
    back to the registry name, same behavior as before Task 5."""
    import plugin_grants

    root = tmp_path / "art"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"srv": {"command": "x"}}}))
    rp = ResolvedPlugin(name="gmail", artifact_id="b" * 64, path=str(root),
                        version="1.0.0", manifest={})
    assert plugin_grants.grants_for_resolved(rp) == ["mcp__plugin_gmail_srv"]


# --- tools.py: A5 requires.plugins gate -------------------------------------


def test_missing_required_plugins_satisfied_by_owned_entry():
    """The real seam is tools._missing_required_plugins (extracted from the
    A5 requires gate in _prelaunch, tools.py) — a requires.plugins: ["mtg"]
    entry is satisfied by an owned mtg.mtg registry entry (runtime name
    "mtg"), never by comparing against the scoped registry name."""
    import tools

    assert tools._missing_required_plugins(["mtg"], [_rp()]) == []


def test_missing_required_plugins_reports_absent_ones():
    import tools

    assert tools._missing_required_plugins(["mtg", "other"], [_rp()]) == ["other"]


def test_missing_required_plugins_unowned_matches_registry_name():
    import tools

    rp = ResolvedPlugin(name="gmail", artifact_id="c" * 64, path="/p",
                        version="1.0.0", manifest={})
    assert tools._missing_required_plugins(["gmail"], [rp]) == []


# --- resume path: recorded manifest_name round-trips the runtime name ------


def test_recorded_owned_artifact_round_trips_runtime_name(tmp_path):
    """Terra plan-r1 follow-up (Task 3 review): a resumed session rebuilds
    ResolvedPlugin from the RECORDED plugin_artifacts dict
    (tools._resolution_from_recorded). If manifest_name isn't threaded
    through the recording, a resumed grant/namespace string would disagree
    with the original launch's. Round-trip: build the recorded dict the
    SAME way the write sites do (name/artifact_id/path/manifest_name), feed
    it back through the read side, and assert the SAME runtime name comes
    out."""
    import tools
    import plugin_store

    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "mtg", "version": "1.0.0"}))

    original = _rp(path=str(root))
    recorded = {
        "name": original.name, "artifact_id": original.artifact_id,
        "path": original.path, "manifest_name": original.manifest_name,
    }

    def _fake_validate(_path):
        return True

    def _fake_read_metadata(_path):
        return {"artifact_id": original.artifact_id}

    orig_validate = plugin_store.validate_artifact
    orig_read = plugin_store.read_metadata
    plugin_store.validate_artifact = _fake_validate
    plugin_store.read_metadata = _fake_read_metadata
    try:
        rebuilt = tools._resolution_from_recorded([recorded])
    finally:
        plugin_store.validate_artifact = orig_validate
        plugin_store.read_metadata = orig_read

    assert len(rebuilt.plugins) == 1
    assert runtime_name(rebuilt.plugins[0]) == runtime_name(original) == "mtg"


def test_recorded_artifact_missing_manifest_name_key_tolerated(tmp_path):
    """An engagement recorded BEFORE this field existed has no
    "manifest_name" key at all — the read side must tolerate that (falls
    back to the registry name via runtime_name(), not raise KeyError)."""
    import tools
    import plugin_store

    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "old", "version": "1.0.0"}))
    recorded = {"name": "old", "artifact_id": "d" * 64, "path": str(root)}

    orig_validate = plugin_store.validate_artifact
    orig_read = plugin_store.read_metadata
    plugin_store.validate_artifact = lambda _p: True
    plugin_store.read_metadata = lambda _p: {"artifact_id": "d" * 64}
    try:
        rebuilt = tools._resolution_from_recorded([recorded])
    finally:
        plugin_store.validate_artifact = orig_validate
        plugin_store.read_metadata = orig_read

    assert len(rebuilt.plugins) == 1
    assert rebuilt.plugins[0].manifest_name == ""
    assert runtime_name(rebuilt.plugins[0]) == "old"
