"""§3.3 resolver: one ResolutionResult feeds everything. plugins XOR issues;
warnings accompany loaded plugins; registry-wide invalid vs per-entry."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import plugin_registry
from plugin_registry import reload_snapshot, resolve_for
from plugin_fixtures import (
    entry as _entry,
    mk_artifact as _mk_artifact,
    mk_registry as _mk_registry,
)

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


def test_resolve_happy(tmp_path):
    store = tmp_path / "store"
    e = _entry("probe", ["specialist:finance"])
    _mk_artifact(store, "probe", e["artifact_id"])
    reload_snapshot(registry_path=_mk_registry(tmp_path, [e]),
                    store_root=store)
    res = resolve_for("specialist:finance")
    assert res.registry_valid is True
    assert [rp.name for rp in res.plugins] == ["probe"]
    assert res.plugins[0].artifact_id == e["artifact_id"]
    assert res.plugins[0].manifest["version"] == "1.0.0"
    assert res.issues == [] and res.warnings == []
    # Target not assigned → empty, no issue.
    other = resolve_for("resident:assistant")
    assert other.plugins == [] and other.issues == []


def test_missing_artifact_is_issue_not_plugin(tmp_path):
    e = _entry("probe", ["specialist:finance"])
    reload_snapshot(registry_path=_mk_registry(tmp_path, [e]),
                    store_root=tmp_path / "store")
    res = resolve_for("specialist:finance")
    assert res.plugins == []
    assert [i.reason_code for i in res.issues] == ["artifact_missing"]
    assert res.issues[0].target == "specialist:finance"


def test_manifest_name_mismatch_is_artifact_invalid(tmp_path):
    store = tmp_path / "store"
    e = _entry("probe", ["specialist:finance"])
    _mk_artifact(store, "probe", e["artifact_id"], manifest_name="other")
    reload_snapshot(registry_path=_mk_registry(tmp_path, [e]),
                    store_root=store)
    res = resolve_for("specialist:finance")
    assert res.plugins == []
    assert [i.reason_code for i in res.issues] == ["artifact_invalid"]


def test_legacy_content_loads_with_warning(tmp_path):
    store = tmp_path / "store"
    rev = "legacy-content:" + "c" * 64
    e = _entry("probe", ["specialist:finance"], revision=rev)
    _mk_artifact(store, "probe", e["artifact_id"], revision=rev)
    reload_snapshot(registry_path=_mk_registry(tmp_path, [e]),
                    store_root=store)
    res = resolve_for("specialist:finance")
    assert [rp.name for rp in res.plugins] == ["probe"]
    assert [w.reason_code for w in res.warnings] == ["legacy_provenance"]


def test_tampered_user_artifact_detected_at_snapshot_reload(tmp_path):
    """Sol F1: deep validation at snapshot load — a tampered NON-bundled
    artifact must yield corrupt_artifact, never a loaded plugin."""
    store = tmp_path / "store"
    e = _entry("probe", ["specialist:finance"])
    root = _mk_artifact(store, "probe", e["artifact_id"])
    (root / "evil.md").write_text("tampered", encoding="utf-8")  # post-publish tamper
    reload_snapshot(registry_path=_mk_registry(tmp_path, [e]),
                    store_root=store)
    res = resolve_for("specialist:finance")
    assert res.plugins == []
    assert [i.reason_code for i in res.issues] == ["corrupt_artifact"]


def test_wrong_identity_metadata_is_artifact_invalid_at_resolve(tmp_path):
    """Sol R2-1: checksum-valid artifact whose metadata names a different
    revision/repo/subdir must be artifact_invalid at ORDINARY resolution."""
    import plugin_store
    store = tmp_path / "store"
    e = _entry("probe", ["specialist:finance"])
    root = _mk_artifact(store, "probe", e["artifact_id"])
    meta_path = root / ".casa-artifact.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["revision"] = "git:" + "b" * 40          # altered identity
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    meta["content_checksum"] = plugin_store.content_checksum(root)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")  # checksum OK
    reload_snapshot(registry_path=_mk_registry(tmp_path, [e]),
                    store_root=store)
    res = resolve_for("specialist:finance")
    assert res.plugins == []
    assert [i.reason_code for i in res.issues] == ["artifact_invalid"]


def test_unrelated_invalid_entry_does_not_pollute_other_targets(tmp_path):
    """Sol F2: a malformed RESIDENT entry must not appear in an EXECUTOR
    resolve (per-entry isolation, spec 3.1/3.5) — but health sees it."""
    store = tmp_path / "store"
    # #923: a plugin ATTACHED to a worker is one Casa ships — bundled.
    good = _entry("good", ["executor:plugin-developer"], source_type="bundled")
    _mk_artifact(store, "good", good["artifact_id"])
    bad = dict(_entry("bad", ["resident:assistant"]), artifact_id="0" * 64)
    reload_snapshot(registry_path=_mk_registry(tmp_path, [bad, good]),
                    store_root=store)
    res = resolve_for("executor:plugin-developer")
    assert [rp.name for rp in res.plugins] == ["good"]
    assert res.issues == []                       # executor launch NOT blocked
    health = plugin_registry.resolve_all()
    assert "bad" in {i.name for i in health.issues}


def test_same_name_collision_does_not_lend_targets(tmp_path):
    """Sol R2-2: an invalid, UNSCOPABLE entry named x must not inherit the
    targets of a valid entry also named x. (Both are name-colliding raw
    entries; the valid one's duplicate issue scopes to its own targets,
    the invalid one's stays health-only.)"""
    store = tmp_path / "store"
    valid_x = _entry("x", ["executor:plugin-developer"],
                     source_type="bundled")     # #923: attached ⇒ bundled
    _mk_artifact(store, "x", valid_x["artifact_id"])
    invalid_x = dict(_entry("x", ["resident:assistant"]), targets="oops")
    reload_snapshot(registry_path=_mk_registry(tmp_path, [invalid_x, valid_x]),
                    store_root=store)
    res = resolve_for("executor:plugin-developer")
    # invalid_x is entry_invalid (bad targets) -> health only. valid_x has no
    # valid-entry duplicate (the invalid one never reached the PK check), so
    # it resolves normally.
    assert [rp.name for rp in res.plugins] == ["x"]
    assert res.issues == []
    assert any(i.name == "x" and i.reason_code == "entry_invalid"
               for i in plugin_registry.resolve_all().issues)


def test_unscopable_invalid_entry_only_in_resolve_all(tmp_path):
    store = tmp_path / "store"
    good = _entry("good", ["executor:plugin-developer"])
    _mk_artifact(store, "good", good["artifact_id"])
    bad = dict(_entry("bad", ["resident:assistant"]), targets="oops")
    reload_snapshot(registry_path=_mk_registry(tmp_path, [bad, good]),
                    store_root=store)
    assert resolve_for("executor:plugin-developer").issues == []
    assert resolve_for("resident:assistant").issues == []      # unscopable
    assert "bad" in {i.name for i in plugin_registry.resolve_all().issues}


def test_malformed_protected_tools_degrades_only_that_plugin(tmp_path):
    """A:§3.7 (r2-B6/r3-4): a stored artifact with a malformed
    casa.protectedTools is EXCLUDED (protected_tools_invalid) while a
    healthy sibling assigned to the SAME target still loads — per-plugin
    degradation, never a whole-role failure."""
    store = tmp_path / "store"
    good = _entry("good", ["specialist:finance"])
    _mk_artifact(store, "good", good["artifact_id"])
    bad = _entry("bad", ["specialist:finance"])
    _mk_artifact(store, "bad", bad["artifact_id"],
                 extra_manifest={"casa": {"protectedTools": ["x", 1]}})
    reload_snapshot(registry_path=_mk_registry(tmp_path, [bad, good]),
                    store_root=store)
    res = resolve_for("specialist:finance")
    assert [rp.name for rp in res.plugins] == ["good"]
    codes = {i.name: i.reason_code for i in res.issues}
    assert codes == {"bad": "protected_tools_invalid"}


def test_one_bad_entry_never_defeats_the_rest(tmp_path):
    store = tmp_path / "store"
    # #923: `good` and `missing` must ATTACH, so they are bundled. `bad` stays
    # github-sourced deliberately — it never survives validation, so it never
    # reaches the effective-target projection, and its issue still scopes to
    # the executor target through its own raw `scoped_targets` (F2). That is
    # what keeps this test about per-entry isolation rather than about #923.
    good = _entry("good", ["executor:plugin-developer"], source_type="bundled")
    _mk_artifact(store, "good", good["artifact_id"])
    bad = dict(_entry("bad", ["executor:plugin-developer"]),
               artifact_id="0" * 64)          # per-entry invalid, SAME target
    missing = _entry("missing", ["executor:plugin-developer"],
                     source_type="bundled")
    reload_snapshot(
        registry_path=_mk_registry(tmp_path, [bad, good, missing]),
        store_root=store)
    res = resolve_for("executor:plugin-developer")
    assert [rp.name for rp in res.plugins] == ["good"]
    codes = {i.name: i.reason_code for i in res.issues}
    # bad names THIS target in its parseable targets list -> scoped in (F2).
    assert codes == {"bad": "entry_invalid", "missing": "artifact_missing"}


def test_registry_wide_invalid(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text("{broken", encoding="utf-8")
    reload_snapshot(registry_path=p, store_root=tmp_path / "store")
    res = resolve_for("resident:assistant")
    assert res.registry_valid is False and res.plugins == []


def test_snapshot_is_stale_until_reloaded(tmp_path):
    """§3.9: resolve_for reads the SNAPSHOT, not the disk."""
    store = tmp_path / "store"
    e = _entry("probe", ["specialist:finance"])
    _mk_artifact(store, "probe", e["artifact_id"])
    reg = _mk_registry(tmp_path, [e])
    reload_snapshot(registry_path=reg, store_root=store)
    assert len(resolve_for("specialist:finance").plugins) == 1
    reg.write_text(json.dumps({"schema_version": 1, "plugins": []}),
                   encoding="utf-8")
    assert len(resolve_for("specialist:finance").plugins) == 1   # stale by design
    reload_snapshot(registry_path=reg, store_root=store)
    assert resolve_for("specialist:finance").plugins == []


def test_tier_for_role():
    from agent_registry import AgentRegistry
    from config import AgentConfig
    reg = AgentRegistry.build(
        residents={"assistant": AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="assistant")},
        specialists={"finance": AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="finance")},
    )
    assert reg.tier_for_role("assistant") == "resident"
    assert reg.tier_for_role("finance") == "specialist"
    assert reg.tier_for_role("ghost") is None


# --- D2 (v0.74.0): generation lives inside the frozen snapshot ---------------


def test_resolution_carries_snapshot_generation(tmp_path):
    """D2: resolve_for returns the generation it was computed against."""
    from plugin_registry import snapshot_generation
    store = tmp_path / "store"
    e = _entry("probe", ["specialist:finance"])
    _mk_artifact(store, "probe", e["artifact_id"])
    reg = _mk_registry(tmp_path, [e])
    reload_snapshot(registry_path=reg, store_root=store)
    g1 = snapshot_generation()
    assert resolve_for("specialist:finance").generation == g1
    reload_snapshot(registry_path=reg, store_root=store)
    assert snapshot_generation() == g1 + 1
    assert resolve_for("specialist:finance").generation == g1 + 1


def test_generation_lives_inside_the_frozen_snapshot(tmp_path):
    """D2: generation is a field OF the (frozen) snapshot object — one
    assignment publishes both; the torn module-global pair is gone."""
    import dataclasses
    reload_snapshot(registry_path=tmp_path / "absent.json",
                    store_root=tmp_path / "store")
    snap = plugin_registry._current()
    assert snap.generation == plugin_registry.snapshot_generation()
    assert not hasattr(plugin_registry, "_generation")
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.generation = 999


def test_registry_invalid_resolution_still_carries_generation(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text("{broken", encoding="utf-8")
    reload_snapshot(registry_path=p, store_root=tmp_path / "store")
    res = resolve_for("resident:assistant")
    assert res.registry_valid is False
    assert res.generation == plugin_registry.snapshot_generation()


def test_concurrent_reload_snapshot_is_serialized_and_monotonic(
        tmp_path, monkeypatch):
    """r2-B2 (r3-B2 hardened): reload_snapshot's whole load/validate/publish
    must be MUTUALLY EXCLUSIVE across worker threads (mutation + manual
    reload_full). The instrumented load_registry counts concurrently-active
    calls — max_active == 1 is the lock's direct signature (Sol verified the
    bare final-generation assertion alone passes 30/30 against UNLOCKED
    code, so mutual exclusion is the load-bearing assert); the final
    generation being EXACTLY start+N additionally pins monotonicity."""
    import threading
    import time as _time
    from plugin_registry import load_registry as real_load
    reload_snapshot(registry_path=tmp_path / "absent.json",
                    store_root=tmp_path / "store")
    start = plugin_registry.snapshot_generation()

    counter_lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def instrumented_load(path=None):
        with counter_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        _time.sleep(0.005)          # hold the section open so overlap SHOWS
        try:
            return real_load(tmp_path / "absent.json")
        finally:
            with counter_lock:
                state["active"] -= 1

    monkeypatch.setattr(plugin_registry, "load_registry", instrumented_load)
    n = 16
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        reload_snapshot(registry_path=tmp_path / "absent.json",
                        store_root=tmp_path / "store")

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max_active"] == 1                  # mutual exclusion held
    assert plugin_registry.snapshot_generation() == start + n


def test_operator_executor_target_is_ignored(tmp_path, monkeypatch):
    """#923 (operator's decision, 2026-09-10): an operator-installed plugin —
    one whose entry's ``source.type`` is not ``bundled`` — does not reach a
    worker. Its stored ``executor:*`` target is IGNORED when a fresh executor
    launch resolves, and reported as ignored; the entry keeps serving its other
    targets and the registry file on disk is not rewritten.

    Counts, not statuses: zero executor plugins, zero executor ISSUES (a row
    carried into that target's resolution would make the worker unlaunchable
    through the launch gate's refuse-on-any-issue rule), exactly one restriction
    row in ``resolve_all()``, one resident plugin, zero registry saves.
    """
    store = tmp_path / "store"
    e = _entry("redcase-probe", ["resident:ellen", "executor:plugin-developer"])
    _mk_artifact(store, "redcase-probe", e["artifact_id"])
    registry_path = _mk_registry(tmp_path, [e])
    before_bytes = registry_path.read_bytes()
    original_plugins = json.loads(before_bytes)["plugins"]

    saves = []
    monkeypatch.setattr(plugin_registry, "save_registry",
                        lambda *a, **k: saves.append(a))

    reload_snapshot(registry_path=registry_path, store_root=store)

    executor = resolve_for("executor:plugin-developer")
    assert len(executor.plugins) == 0
    assert len(executor.issues) == 0
    assert len(resolve_for("resident:ellen").plugins) == 1
    assert plugin_registry.resolve_all().issues == [
        plugin_registry.PluginIssue(
            name="redcase-probe",
            target="executor:plugin-developer",
            stage="registry",
            reason_code="operator_executor_target_ignored",
            artifact_id=None,
            scoped_targets=(),
            detail="executor:plugin-developer",
        )
    ]
    assert saves == []
    assert registry_path.read_bytes() == before_bytes
    assert plugin_registry.snapshot_registry().raw["plugins"] == original_plugins
