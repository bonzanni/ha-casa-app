"""§3.9 tier-aware verify_plugin_state: desired (registry) vs active (running)
agreement. Verification can never report active agreement while a running
consumer executes different code (FR3); dormant targets report configured
readiness; executors also need their plugin MCP namespaces authorized."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin_fixtures import entry, mk_artifact, mk_registry

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Point the system-requirements manifest + plugin-env.conf at absent tmp
    files so production /config state can't leak into a test."""
    import system_requirements.manifest as mani
    import plugin_env_conf as pec
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")
    monkeypatch.setattr(pec, "PLUGIN_ENV_CONF_PATH", tmp_path / "plugin-env.conf")


def _verify(tmp_path, name="probe", tools_bin=None):
    from tools import _tool_verify_plugin_state
    return _tool_verify_plugin_state(
        plugin_name=name,
        _registry_path=tmp_path / "registry.json",
        _store_root=tmp_path / "store",
        _tools_bin=tools_bin)


class _Snap:
    def __init__(self, binding, generation=1):
        self.binding = dict(binding)
        self.generation = generation


class _Agent:
    def __init__(self, binding, resolved=True, generation=1):
        # D2 (v0.74.0): verify reads the ONE snapshot; the legacy attrs stay
        # as plain attributes here for the absent-postcondition reader.
        self.plugin_binding_snapshot = (
            _Snap(binding, generation) if resolved else None)
        self.active_plugin_binding = dict(binding)
        self._plugin_resolution = object() if resolved else None


def _runtime(agents=None, executor_registry=None):
    return SimpleNamespace(agents=agents or {},
                           executor_registry=executor_registry)


def test_verify_dormant_ready(tmp_path):
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True
    assert r["targets"][0]["state"] == "dormant"
    assert r["targets"][0]["ready"] is True


def test_verify_not_registered(tmp_path):
    mk_registry(tmp_path, [])
    r = _verify(tmp_path)
    assert r["ready"] is False and r["reasons"] == ["not_registered"]


def test_verify_corrupt_artifact(tmp_path):
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    root = mk_artifact(store, "probe", e["artifact_id"])
    (root / "tampered.md").write_text("evil", encoding="utf-8")   # post-publish
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert "corrupt_artifact" in r["reasons"]


def test_verify_skill_only_ready(tmp_path):
    """R-1: a plugin with no .mcp.json is still ready (no required env)."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])          # no mcp_servers
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True and r["secrets"] == []


def test_verify_missing_verify_bin(tmp_path, monkeypatch):
    import system_requirements.manifest as mani
    import yaml
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    (tmp_path / "sysreq.yaml").write_text(yaml.safe_dump({"plugins": [
        {"name": "probe", "winning_strategy": "tarball", "verify_bin": "ffmpeg"}]}))
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")
    r = _verify(tmp_path, tools_bin=tmp_path / "empty-bin")
    assert r["ready"] is False
    assert r["tools"][0]["status"] == "missing"


def test_verify_missing_secret(tmp_path):
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"s": {"env": {"K": "${MY_API_KEY}"}}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert r["secrets"][0]["var"] == "MY_API_KEY"
    assert r["secrets"][0]["status"] == "unresolved"


def test_verify_unresolved_secret_carries_a_named_reason(tmp_path):
    """#533: a plainly-unresolved required secret used to append NO named
    reason, so the target row — and therefore the health report and the
    operator DM — degraded to the generic `not_ready`. The named
    `env_unresolved` code is what lets health carry the variable names."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"s": {"env": {"K": "${MY_API_KEY}"}}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert "env_unresolved" in r["reasons"]
    assert "env_unresolved" in r["targets"][0]["reasons"]


def test_env_unresolved_detail_is_bounded_and_names_only_unresolved():
    """#533: the health detail names the unresolved vars (bounded at 5,
    like the withhold WARNING) — never resolved or unprovisioned rows,
    and never a value."""
    from tools import _env_unresolved_detail
    verify = {"secrets": [
        {"var": "A_KEY", "status": "unresolved"},
        {"var": "B_KEY", "status": "resolved"},
        {"var": "C_KEY", "status": "unprovisioned"},
    ]}
    assert _env_unresolved_detail(verify) == "A_KEY"

    many = {"secrets": [
        {"var": f"V{i:02d}", "status": "unresolved"} for i in range(7)]}
    detail = _env_unresolved_detail(many)
    assert detail == "V00, V01, V02, V03, V04 +2 more"

    assert _env_unresolved_detail({"secrets": []}) is None
    assert _env_unresolved_detail({}) is None


def test_verify_reload_required_is_never_green(tmp_path, monkeypatch):
    """THE incident assertion: a constructed agent still bound to the OLD
    artifact after a registry update must report reload_required — verify can
    never green a stale binding (FR3)."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])           # NEW artifact_id
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    stale = _Agent({"probe": "old" * 21 + "o"})           # bound to OLD id
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(agents={"finance": stale}), raising=False)
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert r["stale_targets"] == ["specialist:finance"]
    row = r["targets"][0]
    assert row["state"] == "active" and row["reasons"] == ["reload_required"]

    # After reconstruction (binding == desired) → ready.
    fresh = _Agent({"probe": e["artifact_id"]})
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(agents={"finance": fresh}), raising=False)
    assert _verify(tmp_path)["ready"] is True


def test_verify_authorization_missing_vs_authorized(tmp_path, monkeypatch):
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["executor:plugin-developer"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"probe": {}})               # grant mcp__plugin_probe_probe
    mk_registry(tmp_path, [e])

    class _ExecReg:
        def __init__(self, allowed):
            self._allowed = allowed

        def is_disabled(self, t):
            return False

        def get(self, t):
            return SimpleNamespace(tools_allowed=self._allowed)

    # Missing the namespace → authorization_missing, not ready.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(executor_registry=_ExecReg(["Read"])),
                        raising=False)
    r = _verify(tmp_path)
    assert r["ready"] is False
    row = r["targets"][0]
    assert row["reasons"] == ["authorization_missing"]
    assert row["authorization"]["missing"] == ["mcp__plugin_probe_probe"]

    # Namespace granted → ready.
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        _runtime(executor_registry=_ExecReg(["mcp__plugin_probe_probe"])),
        raising=False)
    assert _verify(tmp_path)["ready"] is True


def test_verify_disabled_executor_target_not_authorization_missing(
        tmp_path, monkeypatch):
    """v0.71.1: a plugin assigned to a DISABLED executor must NOT report
    authorization_missing. plugin-developer ships enabled:false, so the registry
    excludes it from get() → verify read empty tools.allowed and flagged every
    derived grant missing → a false operator health DM on every boot, even though
    the executor's config carries the grant and works once enabled."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["executor:plugin-developer"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"probe": {}})               # grant mcp__plugin_probe_probe
    mk_registry(tmp_path, [e])

    class _DisabledExecReg:
        def __init__(self, allowed):
            self._defn = SimpleNamespace(tools_allowed=allowed)

        def is_disabled(self, t):
            return t == "plugin-developer"

        def get(self, t):
            return None                                  # disabled → absent

        def definition_any(self, t):
            return self._defn

    # Disabled executor whose config DOES carry the grant → dormant, ready, no alarm.
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        _runtime(executor_registry=_DisabledExecReg(["mcp__plugin_probe_probe"])),
        raising=False)
    r = _verify(tmp_path)
    row = r["targets"][0]
    assert row["state"] == "disabled"
    assert row["ready"] is True and row["reasons"] == []
    assert row["authorization"]["missing"] == []
    assert r["ready"] is True                            # top-level: no health issue

    # Even when the disabled executor's config LACKS the grant, a dormant target
    # is not flagged not-ready — it is re-checked when the operator enables it.
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        _runtime(executor_registry=_DisabledExecReg(["Read"])),
        raising=False)
    r2 = _verify(tmp_path)
    row2 = r2["targets"][0]
    assert row2["state"] == "disabled" and row2["ready"] is True
    assert row2["reasons"] == []
    assert row2["authorization"]["missing"] == ["mcp__plugin_probe_probe"]


def test_verify_running_engagement_on_previous_artifact_is_informational(
        tmp_path, monkeypatch):
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("probe", ["executor:plugin-developer"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    rec = SimpleNamespace(id="eng1", plugin_artifacts=[
        {"name": "probe", "artifact_id": "old" * 21 + "o"}])
    fake_reg = SimpleNamespace(active_and_idle=lambda: [rec])
    monkeypatch.setattr(tools_mod, "_engagement_registry", fake_reg)
    r = _verify(tmp_path)
    assert r["ready"] is True                     # informational, not blocking
    assert r["sessions_on_previous_artifact"] == [
        {"engagement_id": "eng1", "artifact_id": "old" * 21 + "o"}]


def test_verify_provenance_warning_on_legacy_content(tmp_path):
    store = tmp_path / "store"
    rev = "legacy-content:" + "c" * 64
    e = entry("probe", ["specialist:finance"], revision=rev)
    mk_artifact(store, "probe", e["artifact_id"], revision=rev)
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True                     # warning is not a blocker
    assert r["artifact"]["provenance_warning"] is True


def test_verify_plugin_secrets_shim(tmp_path, monkeypatch):
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    orig = tools_mod._tool_verify_plugin_state
    monkeypatch.setattr(
        tools_mod, "_tool_verify_plugin_state",
        lambda *, plugin_name: orig(
            plugin_name=plugin_name, _registry_path=tmp_path / "registry.json",
            _store_root=tmp_path / "store"))
    out = tools_mod._tool_verify_plugin_secrets(plugin_name="probe")
    assert "secrets" in out and isinstance(out["secrets"], list)


def test_bundled_registry_authorized_for_plugin_developer(tmp_path, monkeypatch):
    """Sol R2-5 (fast unit guard, NOT the proof): the checked-in default
    registry × the real plugin-developer definition — grants derived from
    fixture artifacts mirroring each bundled plugin's .mcp.json server keys
    must be fully authorized. (context7 is the only MCP-bearing default.)"""
    import json
    import agent as agent_mod
    import yaml

    root = Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa"
    default_reg_path = root / "defaults" / "plugin-registry.json"
    if not default_reg_path.is_file():
        pytest.skip("default plugin-registry.json lands in Task 18")
    default_reg = json.loads(default_reg_path.read_text(encoding="utf-8"))
    defn_raw = yaml.safe_load(
        (root / "defaults" / "agents" / "executors" / "plugin-developer"
         / "definition.yaml").read_text(encoding="utf-8"))
    allowed = list((defn_raw.get("tools") or {}).get("allowed") or [])

    store = tmp_path / "store"
    reg_entries = []
    for e in default_reg["plugins"]:
        servers = {"context7": {}} if e["name"] == "context7" else None
        src = e["source"]
        ent = entry(e["name"], e["targets"], revision=src["revision"],
                    subdir=src.get("subdir", ""))
        mk_artifact(store, e["name"], ent["artifact_id"],
                    revision=src["revision"], subdir=src.get("subdir", ""),
                    mcp_servers=servers)
        reg_entries.append(ent)
    mk_registry(tmp_path, reg_entries)

    class _ExecReg:
        def is_disabled(self, t):
            return False

        def get(self, t):
            return SimpleNamespace(tools_allowed=allowed)

    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(executor_registry=_ExecReg()), raising=False)
    for e in reg_entries:
        r = _verify(tmp_path, name=e["name"])
        row = r["targets"][0]
        assert row["authorization"]["missing"] == [], (
            f"{e['name']}: unauthorized {row['authorization']['missing']}")


# --- Sol review regressions -------------------------------------------------

def _mk_artifact_raw(store, name, artifact_id, *, plugin_json, mcp_text=None,
                     revision="git:" + "a" * 40):
    """Build an artifact writing raw plugin.json / .mcp.json BEFORE the checksum
    (so malformed content is captured as valid-checksum, not corrupt)."""
    import json as _json
    from plugin_store import content_checksum, write_metadata
    root = Path(store) / name / artifact_id
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        _json.dumps(plugin_json), encoding="utf-8")
    if mcp_text is not None:
        (root / ".mcp.json").write_text(mcp_text, encoding="utf-8")
    write_metadata(root, name=name, repo="o/r", ref="v1", revision=revision,
                   subdir="", artifact_id=artifact_id,
                   version=plugin_json.get("version", "1.0.0"),
                   checksum=content_checksum(root))
    return root


def test_verify_duplicate_name_not_green(tmp_path):
    """Sol #8: a duplicate-name entry that resolve_for() drops must not verify
    ready off the raw registry list."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e, dict(e)])           # same name twice
    r = _verify(tmp_path)
    assert r["ready"] is False and r["reasons"] == ["duplicate_name"]


def test_verify_entry_invalid_not_green(tmp_path):
    """Sol #8: a per-entry-invalid registry entry (artifact_id mismatch) that
    the resolver skips must not verify ready."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    e["artifact_id"] = "b" * 64                   # mismatch → entry_invalid
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False and r["reasons"] == ["entry_invalid"]


def test_verify_malformed_mcp_json_not_green(tmp_path):
    """Sol #16: a present-but-malformed .mcp.json (broken MCP server) must not
    verify ready even though grants/secrets silently degrade to []."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    _mk_artifact_raw(store, "probe", e["artifact_id"],
                     plugin_json={"name": "probe", "version": "1.0.0"},
                     mcp_text="{ this is : not valid json")
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False and "mcp_invalid" in r["reasons"]


def test_verify_declared_sysreq_not_installed_not_green(tmp_path):
    """Sol #11: a plugin declaring a systemRequirement with no installed
    manifest row must not verify ready on all([])."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    _mk_artifact_raw(store, "probe", e["artifact_id"], plugin_json={
        "name": "probe", "version": "1.0.0",
        "casa": {"systemRequirements": [{"type": "tarball", "verify_bin": "ffmpeg"}]}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert any(t["status"] == "missing" and t["verify_bin"] == "ffmpeg"
               for t in r["tools"])


def test_postcondition_present_requires_top_ready():
    """Sol #9: zero target rows must not vacuously pass all([]) — the postcondition
    now gates on the top-level readiness (which folds in artifact/tools/secrets
    AND every row)."""
    from tools import _postcondition_holds
    # Zero rows + not ready (e.g. unresolved secret on an unassigned plugin) →
    # must be False (was vacuously True).
    assert _postcondition_holds({"ready": False, "targets": []}, [],
                                expect="present") is False
    # Zero rows but fully configured-ready → True.
    assert _postcondition_holds({"ready": True, "targets": []}, [],
                                expect="present") is True
    # A ready plugin with rows → True.
    v = {"ready": True,
         "targets": [{"target": "specialist:finance", "ready": True}]}
    assert _postcondition_holds(v, ["specialist:finance"],
                                expect="present") is True
    # Top-level not ready even though the listed row is ready → False.
    v2 = {"ready": False,
          "targets": [{"target": "specialist:finance", "ready": True}]}
    assert _postcondition_holds(v2, ["specialist:finance"],
                                expect="present") is False


def test_regenerate_health_preserves_other_plugins_runtime_issue(
        monkeypatch, event_routing_ok):
    """Sol #13: a successful mutation of B must not erase A's still-active
    runtime issue (reload_required) from the health report.

    ``event_routing_ok`` (Minor-2): this reads every issue's
    ``.reason_code`` ATTRIBUTE, which chokes (AttributeError) on the
    plain-dict ``event_routing_unavailable`` row the conftest's actual
    PRODUCTION-default sentinel would otherwise contribute — opts into an
    authoritative empty routing map instead."""
    import tools
    import plugin_health
    import plugin_registry
    captured = {}
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace(
                            valid=True, entries=[{"name": "A"}, {"name": "B"}]))

    def _verify_stub(*, plugin_name):
        if plugin_name == "A":
            return {"ready": False, "targets": [
                {"target": "specialist:x", "ready": False,
                 "reasons": ["reload_required"]}]}
        return {"ready": True, "targets": [
            {"target": "executor:y", "ready": True}]}

    monkeypatch.setattr(tools, "_tool_verify_plugin_state", _verify_stub)
    monkeypatch.setattr(plugin_health, "write_report",
                        lambda **k: captured.update(k))
    tools._regenerate_plugin_health([])           # mutating B, no extras
    reasons = [i.reason_code for i in captured["issues"]]
    assert "reload_required" in reasons


def test_verify_discloses_draining_agent_on_previous_artifact(tmp_path,
                                                              monkeypatch):
    """Sol #4: a resident/specialist whose in-flight turn is still draining the
    PREVIOUS artifact after a reload is disclosed (informational) — ready stays
    true (the active binding is the new artifact; the old turn is transient)."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    runtime = _runtime(agents={})
    runtime.draining = [{"role": "finance", "binding": {"probe": "b" * 64}}]
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    r = _verify(tmp_path)
    assert any(s.get("draining_role") == "finance"
               for s in r["sessions_on_previous_artifact"])
    assert r["ready"] is True


def test_reload_track_and_drop_draining():
    """Sol #4: the draining tracker records a swapped-out agent's binding and
    removes it on close; a binding-less agent is not tracked."""
    from reload import _track_draining, _drop_draining
    runtime = SimpleNamespace()
    agent = SimpleNamespace(active_plugin_binding={"p": "aid1"})
    ent = _track_draining(runtime, "finance", agent)
    assert runtime.draining == [{"role": "finance", "binding": {"p": "aid1"}}]
    _drop_draining(runtime, ent)
    assert runtime.draining == []
    assert _track_draining(
        runtime, "x", SimpleNamespace(active_plugin_binding={})) is None


def test_verify_prefers_valid_entry_over_same_name_invalid(tmp_path):
    """Sol round-3 M-shadow: a valid entry must not be shadowed by an unrelated
    same-name invalid entry — verify serves the resolved (valid) one."""
    store = tmp_path / "store"
    valid = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", valid["artifact_id"])
    invalid = dict(valid)
    invalid["artifact_id"] = "z" * 64            # → entry_invalid (id mismatch)
    mk_registry(tmp_path, [invalid, valid])      # invalid first
    r = _verify(tmp_path)
    assert r["ready"] is True                     # valid entry served, not shadowed


def test_regenerate_health_surfaces_verify_exception(monkeypatch):
    """Sol round-3 H13: a verifier crash on one plugin is surfaced as an issue,
    not silently dropped."""
    import tools
    import plugin_health
    import plugin_registry
    captured = {}
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace(
                            valid=True, entries=[{"name": "boom"}]))

    def _verify_boom(*, plugin_name):
        raise RuntimeError("verify crashed")

    monkeypatch.setattr(tools, "_tool_verify_plugin_state", _verify_boom)
    monkeypatch.setattr(plugin_health, "write_report",
                        lambda **k: captured.update(k))
    tools._regenerate_plugin_health([])
    assert any(i.reason_code == "verify_exception" for i in captured["issues"])


def test_verify_secret_configured_but_not_in_effective_env_unresolved(
        tmp_path, monkeypatch):
    """Sol round-4: a secret whose op:// ref failed at boot (config present but
    os.environ unset) must verify UNRESOLVED, not falsely green off config presence."""
    import plugin_env_conf as pec
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"s": {"env": {"K": "${MY_API_KEY}"}}})
    mk_registry(tmp_path, [e])
    (tmp_path / "plugin-env.conf").write_text(
        "MY_API_KEY=op://vault/item/field\n", encoding="utf-8")
    monkeypatch.setattr(pec, "PLUGIN_ENV_CONF_PATH", tmp_path / "plugin-env.conf")
    monkeypatch.delenv("MY_API_KEY", raising=False)          # not resolved at boot
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert r["secrets"][0]["status"] == "unresolved"

    # Now present in the effective env → resolved.
    monkeypatch.setenv("MY_API_KEY", "sk-real-value")
    r2 = _verify(tmp_path)
    assert r2["secrets"][0]["status"] == "resolved"


def test_verify_tolerates_manifest_row_without_winning_strategy(tmp_path, monkeypatch):
    """Sol CI-review: a hand-corrupted sysreq manifest row (name but no
    winning_strategy) must not crash verify (defensive access)."""
    import system_requirements.manifest as mani
    import yaml
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    (tmp_path / "sysreq.yaml").write_text(yaml.safe_dump({"plugins": [
        {"name": "probe", "verify_bin": "ffmpeg"}]}))   # no winning_strategy
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")
    r = _verify(tmp_path, tools_bin=tmp_path / "empty")   # must not raise
    assert r["tools"][0]["requirement"] == "unknown"


# --- D2 (v0.74.0): snapshot read, swap-race, generation disclosure -----------


def test_verify_reads_the_snapshot_not_legacy_attrs(tmp_path, monkeypatch):
    """D2: grading reads plugin_binding_snapshot alone; a disagreeing legacy
    attribute (impossible on a real Agent, possible on a stand-in) is
    ignored."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    a = _Agent({"probe": e["artifact_id"]})
    a.active_plugin_binding = {}                 # stale legacy view — ignored
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(agents={"finance": a}), raising=False)
    r = _verify(tmp_path)
    row = r["targets"][0]
    assert row["ready"] is True
    assert row["active_artifact_id"] == e["artifact_id"]
    assert row["resolution_generation"] == 1


def test_verify_swap_race_grades_the_replacement(tmp_path, monkeypatch):
    """D2: when runtime.agents[role] is swapped between the read and the
    grade, verify re-reads and grades the REPLACEMENT, never the replaced
    object."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    stale = _Agent({"probe": "old" * 21 + "o"})
    fresh = _Agent({"probe": e["artifact_id"]})

    class _SwappingAgents(dict):
        """First .get() returns the pre-swap agent; later reads the fresh
        one — deterministic simulation of a reload swap racing verify."""
        def __init__(self):
            super().__init__({"finance": fresh})
            self._first = True

        def get(self, key, default=None):
            if self._first:
                self._first = False
                return stale
            return super().get(key, default)

    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(agents=_SwappingAgents()), raising=False)
    r = _verify(tmp_path)
    row = r["targets"][0]
    assert row["ready"] is True                          # graded the fresh agent
    assert row["active_artifact_id"] == e["artifact_id"]


def test_verify_stale_generation_with_equal_binding_stays_ready(
        tmp_path, monkeypatch):
    """FR3: binding==desired => ready — an unrelated mutation's generation
    bump must not degrade an untouched agent. Disclosed, not failed.
    (PROVISIONAL pending Nicola's B1 spec decision — Sol r3/r4.)"""
    import agent as agent_mod
    import plugin_registry as preg
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    a = _Agent({"probe": e["artifact_id"]}, generation=1)
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(agents={"finance": a}), raising=False)
    monkeypatch.setattr(preg, "snapshot_generation", lambda: 7)
    r = _verify(tmp_path)
    row = r["targets"][0]
    assert row["ready"] is True                  # FR3 grading unchanged
    assert row["generation_stale"] is True       # ...but disclosed


def test_verify_idle_stale_binding_stays_blocking(tmp_path, monkeypatch):
    """D1/FR3: a persistent Agent's stale binding remains reload_required
    while idle — it can reuse it on its next bus/trigger turn."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    a = _Agent({"probe": "old" * 21 + "o"})
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(agents={"finance": a}), raising=False)
    r = _verify(tmp_path)
    row = r["targets"][0]
    assert row["ready"] is False and row["reasons"] == ["reload_required"]
    assert r["stale_targets"] == ["specialist:finance"]


def test_fr3_rule_verbatim_in_docstring():
    from tools import _tool_verify_plugin_state
    doc = " ".join(_tool_verify_plugin_state.__doc__.split())   # unwrap lines
    assert ("Readiness describes the artifact a target can execute on its "
            "next new turn") in doc
    assert "remains `reload_required` while idle" in doc


# --- D2/B3 (v0.74.0): health regen derives from its own fresh pass -----------


def _regen_harness(monkeypatch, *, entries, verify_stub, extras):
    import tools
    import plugin_health
    import plugin_registry
    captured = {}
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace(valid=True,
                                                        entries=entries))
    monkeypatch.setattr(tools, "_tool_verify_plugin_state", verify_stub)
    monkeypatch.setattr(plugin_health, "write_report",
                        lambda **k: captured.update(k))
    tools._regenerate_plugin_health(extras)
    return captured


def test_regen_drops_transient_verify_extra_when_fresh_pass_clean(
        monkeypatch, event_routing_ok):
    """D2: a torn-read reload_required from the mutation's verify must not
    linger once the regen's OWN fresh pass of that (plugin, target) is
    clean.

    ``event_routing_ok`` (Minor-2): reads every issue's ``.reason_code``
    attribute — see test_regenerate_health_preserves_other_plugins_
    runtime_issue's docstring for why."""
    from plugin_registry import PluginIssue
    stale = PluginIssue(name="probe", target="specialist:finance",
                        stage="verify", reason_code="reload_required")
    captured = _regen_harness(
        monkeypatch,
        entries=[{"name": "probe", "targets": ["specialist:finance"]}],
        verify_stub=lambda *, plugin_name: {
            "ready": True,
            "targets": [{"target": "specialist:finance", "ready": True}]},
        extras=[stale])
    assert all(i.reason_code != "reload_required" for i in captured["issues"])


def test_regen_keeps_still_true_reload_required(monkeypatch):
    """A GENUINELY stale binding stays flagged: the extra is dropped but the
    fresh pass rediscovers the same row."""
    captured = _regen_harness(
        monkeypatch,
        entries=[{"name": "probe", "targets": ["specialist:finance"]}],
        verify_stub=lambda *, plugin_name: {
            "ready": False,
            "targets": [{"target": "specialist:finance", "ready": False,
                         "reasons": ["reload_required"]}]},
        extras=[])
    assert any(i.reason_code == "reload_required"
               and i.target == "specialist:finance"
               for i in captured["issues"])


def test_regen_carries_forward_reload_stage_rows(monkeypatch):
    """stage='reload' failures are NOT rediscoverable by verify — kept."""
    from plugin_registry import PluginIssue
    err = PluginIssue(name="probe", target="specialist:finance",
                      stage="reload", reason_code="reload_failed")
    captured = _regen_harness(
        monkeypatch,
        entries=[{"name": "probe", "targets": ["specialist:finance"]}],
        verify_stub=lambda *, plugin_name: {"ready": True, "targets": []},
        extras=[err])
    assert any(i.reason_code == "reload_failed" for i in captured["issues"])


def test_regen_keeps_unassigned_target_postcondition_row(monkeypatch):
    """r2-B3: after plugin_unassign, the stale target is no longer in the
    entry's targets — the fresh pass cannot rediscover it; keep the row."""
    from plugin_registry import PluginIssue
    row = PluginIssue(name="probe", target="specialist:finance",
                      stage="verify", reason_code="postcondition_failed")
    captured = _regen_harness(
        monkeypatch,
        entries=[{"name": "probe", "targets": []}],       # target unassigned
        verify_stub=lambda *, plugin_name: {"ready": True, "targets": []},
        extras=[row])
    assert any(i.reason_code == "postcondition_failed"
               for i in captured["issues"])


def test_verify_uninstalled_specialist_target_dormant_not_blocking(
        tmp_path, monkeypatch):
    """#211 symmetry: a registered plugin whose specialist target is NOT yet
    installed (no constructed agent) grades dormant + ready — the documented
    plugin-before-specialist install order must never surface as a blocking
    verify reason."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:mtg"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    monkeypatch.setattr(agent_mod, "active_runtime", _runtime(agents={}),
                        raising=False)
    r = _verify(tmp_path)
    assert r["ready"] is True
    row = r["targets"][0]
    assert row["state"] == "dormant"
    assert row["ready"] is True and row["reasons"] == []


def test_regen_emits_target_pending_warning_for_uninstalled_specialist(
        monkeypatch, tmp_path, event_routing_ok):
    """#211: the health regeneration recomputes a WARNING-class
    "target_pending" row (never a blocking issue) for a registered plugin
    targeting a specialist with no agent directory yet; it self-clears once
    the directory exists.

    ``event_routing_ok`` (Minor-2): reads every issue's ``.reason_code``
    attribute — see test_regenerate_health_preserves_other_plugins_
    runtime_issue's docstring for why."""
    import agent as agent_mod
    agents_dir = tmp_path / "agents"
    (agents_dir / "specialists").mkdir(parents=True)
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(agents={}, agents_dir=str(agents_dir)), raising=False)
    captured = _regen_harness(
        monkeypatch,
        entries=[{"name": "probe", "targets": ["specialist:mtg"]}],
        verify_stub=lambda *, plugin_name: {"ready": True, "targets": []},
        extras=[])
    assert any(w.reason_code == "target_pending"
               and w.target == "specialist:mtg"
               for w in captured["warnings"])
    assert all(i.reason_code != "target_pending" for i in captured["issues"])

    # Specialist installed (directory present) → the warning self-clears.
    (agents_dir / "specialists" / "mtg").mkdir()
    captured = _regen_harness(
        monkeypatch,
        entries=[{"name": "probe", "targets": ["specialist:mtg"]}],
        verify_stub=lambda *, plugin_name: {"ready": True, "targets": []},
        extras=[])
    assert all(w.reason_code != "target_pending" for w in captured["warnings"])


def test_regen_keeps_rows_for_unregistered_plugins(monkeypatch):
    """A plugin_remove postcondition row targets a plugin the fresh pass no
    longer covers — kept."""
    from plugin_registry import PluginIssue
    row = PluginIssue(name="ghost", target="specialist:finance",
                      stage="verify", reason_code="postcondition_failed")
    captured = _regen_harness(
        monkeypatch, entries=[], verify_stub=lambda *, plugin_name: {},
        extras=[row])
    assert any(i.name == "ghost" for i in captured["issues"])


# --- D3 (v0.74.0): postcondition de-dup --------------------------------------


def test_postcondition_row_suppressed_when_reload_required_explains():
    """D3: postcondition_failed(target=None) must not duplicate a concrete
    reload_required row — that duplication warned EVERY resident."""
    from tools import _issues_from_mutation
    verify = {"ready": False, "reasons": [],
              "targets": [{"target": "specialist:finance", "ready": False,
                           "reasons": ["reload_required"]}]}
    issues = _issues_from_mutation(
        "probe", reload_errors=[], verify=verify, expect="present",
        postcondition_ok=False)
    codes = [(i.reason_code, i.target) for i in issues]
    assert ("reload_required", "specialist:finance") in codes
    assert not any(c == "postcondition_failed" for c, _ in codes)


def test_postcondition_row_suppressed_when_top_level_reason_explains():
    from tools import _issues_from_mutation
    verify = {"ready": False, "reasons": ["mcp_invalid"], "targets": []}
    issues = _issues_from_mutation(
        "probe", reload_errors=[], verify=verify, expect="present",
        postcondition_ok=False)
    assert not any(i.reason_code == "postcondition_failed" for i in issues)


def test_postcondition_row_emitted_when_nothing_explains():
    from tools import _issues_from_mutation
    verify = {"ready": False, "reasons": [], "targets": []}
    issues = _issues_from_mutation(
        "probe", reload_errors=[], verify=verify, expect="present",
        postcondition_ok=False)
    assert [(i.reason_code, i.target) for i in issues] == \
        [("postcondition_failed", None)]


def test_absent_postcondition_targets_the_stale_role():
    """D3: absent-case failures name the CONCRETE stale target — no more
    registry-wide target=None amplification."""
    from tools import _issues_from_mutation
    issues = _issues_from_mutation(
        "probe", reload_errors=[],
        verify={"ready": False, "reasons": ["not_registered"], "targets": []},
        expect="absent", postcondition_ok=False,
        stale_absent_targets=["specialist:finance"])
    assert [(i.reason_code, i.target) for i in issues] == \
        [("postcondition_failed", "specialist:finance")]


def test_snapshot_raced_issue_replaces_postcondition_row():
    """r2-B3: snapshot_raced must not fall through to a registry-wide
    postcondition_failed(target=None)."""
    from tools import _issues_from_mutation
    issues = _issues_from_mutation(
        "probe", reload_errors=[],
        verify={"ready": False, "reasons": [], "targets": []},
        expect="present", postcondition_ok=False, snapshot_raced=True)
    assert [(i.reason_code, i.target) for i in issues] == \
        [("snapshot_raced", None)]


def test_stale_absent_targets_reads_live_bindings():
    from tools import _stale_absent_targets
    runtime = SimpleNamespace(agents={
        "finance": SimpleNamespace(active_plugin_binding={"probe": "aid"}),
        "butler": SimpleNamespace(active_plugin_binding={})})
    assert _stale_absent_targets(
        ["specialist:finance", "resident:butler", "executor:x"],
        "probe", runtime) == ["specialist:finance"]


def test_verify_disabled_specialist_target_not_reload_required(
        tmp_path, monkeypatch):
    """v0.74.1 (live finding, proxy-drive 2026-07-13): a plugin targeting a
    DISABLED specialist must verify state='disabled', never reload_required —
    the specialist-tier analogue of the v0.71.1 disabled-executor rule. A
    disabled specialist is excluded from the AgentRegistry, so a
    reload-constructed instance tier-misses to resident:<role> and resolves
    an EMPTY binding; grading that binding produced the §1.4 'Plugin
    degraded' amplification (per FR3 it is dormant-by-config: no new turn
    can enter a disabled specialist)."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    # The reload-constructed disabled specialist: resolved with EMPTY binding
    # (the live signature: state would be active, active_artifact_id None).
    a = _Agent({}, resolved=True)

    class _SpecReg:
        def is_disabled(self, role):
            return role == "finance"

    runtime = _runtime(agents={"finance": a})
    runtime.specialist_registry = _SpecReg()
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    r = _verify(tmp_path)
    row = r["targets"][0]
    assert row["state"] == "disabled"
    assert row["ready"] is True and row["reasons"] == []
    assert r["stale_targets"] == []
    assert r["ready"] is True                    # no health issue, no DM


def test_verify_enabled_specialist_still_graded(tmp_path, monkeypatch):
    """The disabled-specialist rule must not weaken FR3 for ENABLED ones: a
    stale binding on an enabled specialist stays reload_required."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    a = _Agent({"probe": "old" * 21 + "o"})

    class _SpecReg:
        def is_disabled(self, role):
            return False

    runtime = _runtime(agents={"finance": a})
    runtime.specialist_registry = _SpecReg()
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    r = _verify(tmp_path)
    row = r["targets"][0]
    assert row["ready"] is False and row["reasons"] == ["reload_required"]


def test_verify_shows_protected_tools(tmp_path):
    """A:§3.7 (B7): verify_plugin_state discloses the declared protected
    tools list (eyeball-checkable) — legacy string entries, no summaries."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                extra_manifest={"casa": {"protectedTools": ["b_tool", "a_tool"]}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True
    assert r["protected_tools"] == ["a_tool", "b_tool"]      # sorted
    assert r["protected_tool_summaries"] == {}


def test_verify_absent_protected_tools_is_empty_list(tmp_path):
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["protected_tools"] == []
    assert r["protected_tool_summaries"] == {}


def test_verify_malformed_protected_tools_not_green(tmp_path):
    """A malformed casa.protectedTools is a blocking artifact_verdict
    reason (protected_tools_invalid), disclosed via 'protected_tools': []."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    _mk_artifact_raw(store, "probe", e["artifact_id"], plugin_json={
        "name": "probe", "version": "1.0.0",
        "casa": {"protectedTools": ["x", 1]}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert "protected_tools_invalid" in r["reasons"]
    assert r["protected_tools"] == []
    assert r["protected_tool_summaries"] == {}


def test_verify_shows_protected_tool_summaries_for_object_entries(tmp_path):
    """v0.78.0 W1: object-form protectedTools entries surface their exact
    template via the NEW protected_tool_summaries field (name -> template),
    keeping protected_tools names-only. A mixed legacy string entry
    contributes to protected_tools but not to protected_tool_summaries."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"], extra_manifest={"casa": {
        "protectedTools": [
            {"name": "invoice_reset",
             "summary": "Delete the invoice draft for {period}"},
            {"name": "no_summary_tool"},
            "legacy_tool",
        ],
    }})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True
    assert r["protected_tools"] == ["invoice_reset", "legacy_tool",
                                    "no_summary_tool"]
    assert r["protected_tool_summaries"] == {
        "invoice_reset": "Delete the invoice draft for {period}",
    }


def test_verify_disabled_specialist_does_not_mask_config_failure(
        tmp_path, monkeypatch):
    """Sol v0.74.1-B2: being disabled must not mask a BROKEN configuration —
    an unresolved secret still fails the row (like disabled executors)."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"s": {"env": {"K": "${MY_API_KEY}"}}})
    mk_registry(tmp_path, [e])

    class _SpecReg:
        def is_disabled(self, role):
            return role == "finance"

    runtime = _runtime(agents={})
    runtime.specialist_registry = _SpecReg()
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    r = _verify(tmp_path)
    row = r["targets"][0]
    assert row["state"] == "disabled"
    assert row["ready"] is False                 # config problem NOT masked
    assert r["ready"] is False
    assert any(s["status"] == "unresolved" for s in r["secrets"])


# ---------------------------------------------------------------------------
# #429: how the two env declarations grade on the verify surface.
#
# Both stop WITHHOLDING the plugin (plugin_grants), but they carry opposite
# READINESS meanings here — which is the whole reason they are two fields and
# The declaration exists ONLY for the readiness signal: a genuinely optional
# variable needs no declaration, because `${VAR:-}` in .mcp.json expands to
# empty and is invisible to the extractor (#431).
# ---------------------------------------------------------------------------

_SETUP_ENV_SERVERS = {"s": {"env": {
    "KEY": "${CASA_PLUGIN_BANKFEED_PRIVATE_KEY}",
    "CP": "${CASA_PLUGIN_BANKFEED_CP_TOKEN}",
}}}


def _setup_env_plugin(tmp_path, **casa):
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers=_SETUP_ENV_SERVERS,
                extra_manifest={"casa": casa})
    mk_registry(tmp_path, [e])
    return _verify(tmp_path)


def test_verify_setup_provided_var_is_unprovisioned_and_not_ready(tmp_path):
    """A credential the plugin's own setup tool must create is NOT graded as
    a plain missing secret — but it is still not ready. A plugin sitting on
    empty credentials because setup never ran has to stay loud."""
    r = _setup_env_plugin(
        tmp_path, setupTool="setup_bank_feed",
        setupProvides=["CASA_PLUGIN_BANKFEED_PRIVATE_KEY"])
    row = next(s for s in r["secrets"]
               if s["var"] == "CASA_PLUGIN_BANKFEED_PRIVATE_KEY")
    assert row["status"] == "unprovisioned"
    assert "setupProvides" in row["reason"]
    assert r["ready"] is False
    assert "setup_env_unprovisioned" in r["reasons"]


def test_verify_ready_once_the_setup_credential_lands(tmp_path, monkeypatch):
    """The self-clearing half: once the setup run lands its credential the
    unprovisioned row disappears and the plugin grades ready."""
    monkeypatch.setenv("CASA_PLUGIN_BANKFEED_PRIVATE_KEY", "-----BEGIN KEY-----")
    monkeypatch.setenv("CASA_PLUGIN_BANKFEED_CP_TOKEN", "cp-tok")
    r = _setup_env_plugin(
        tmp_path, setupTool="setup_bank_feed",
        setupProvides=["CASA_PLUGIN_BANKFEED_PRIVATE_KEY"])
    assert r["ready"] is True
    assert {s["var"]: s["status"] for s in r["secrets"]} == {
        "CASA_PLUGIN_BANKFEED_PRIVATE_KEY": "resolved",
        "CASA_PLUGIN_BANKFEED_CP_TOKEN": "resolved",
    }


def test_verify_undeclared_var_still_grades_as_a_missing_secret(tmp_path):
    """Nothing about the declarations may soften an undeclared secret."""
    r = _setup_env_plugin(tmp_path, setupTool="setup_bank_feed",
                          setupProvides=["CASA_PLUGIN_BANKFEED_PRIVATE_KEY"])
    cp = next(s for s in r["secrets"]
              if s["var"] == "CASA_PLUGIN_BANKFEED_CP_TOKEN")
    assert cp["status"] == "unresolved"
    assert cp["source"] == "missing"


def test_verify_malformed_declaration_is_visible_not_silent(tmp_path):
    """A declaration that does not parse RELAXES a gate if read loosely, so
    it must surface here rather than degrade to 'no declaration'."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers=_SETUP_ENV_SERVERS,
                extra_manifest={"casa": {"setupTool": "setup_x",
                                         "setupProvides": "not-a-list"}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert "setup_provides_invalid" in r["reasons"]


def test_verify_casa_owned_var_names_the_app_option(tmp_path, monkeypatch):
    """OP_SERVICE_ACCOUNT_TOKEN is exported by svc-casa/run from an app
    option — a plugin-env entry cannot substitute for the unset option, so
    the row must not send the operator to set_plugin_env_reference."""
    # A developer shell may export the token; grade the UNSET case.
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"s": {"env": {"OP": "${OP_SERVICE_ACCOUNT_TOKEN}"}}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    row = next(s for s in r["secrets"]
               if s["var"] == "OP_SERVICE_ACCOUNT_TOKEN")
    assert "onepassword_service_account_token" in row["reason"]
    assert "plugin-env.conf" not in row["reason"]


def test_verify_grades_a_setup_provided_var_not_named_in_mcp_json(
        tmp_path, monkeypatch):
    """Sol r1: the secrets rows come from `.mcp.json` ${VAR} references only.
    A plugin whose server reads its provisioned credential from the INHERITED
    environment produced no row — and graded READY with the credential
    absent, which is the exact state setup_env_unprovisioned exists to make
    loud."""
    monkeypatch.delenv("CASA_PLUGIN_BANKFEED_APP_ID", raising=False)
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"s": {}},          # references NOTHING
                extra_manifest={"casa": {
                    "setupTool": "setup_bank_feed",
                    "setupProvides": ["CASA_PLUGIN_BANKFEED_APP_ID"]}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    row = next(s for s in r["secrets"]
               if s["var"] == "CASA_PLUGIN_BANKFEED_APP_ID")
    assert row["status"] == "unprovisioned"
    assert r["ready"] is False
    assert "setup_env_unprovisioned" in r["reasons"]


def test_verify_unreferenced_setup_var_clears_once_provisioned(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CASA_PLUGIN_BANKFEED_APP_ID", "app-123")
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"s": {}},
                extra_manifest={"casa": {
                    "setupTool": "setup_bank_feed",
                    "setupProvides": ["CASA_PLUGIN_BANKFEED_APP_ID"]}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True
    assert r["secrets"] == []
