"""#792: ``casa.resultContract`` extraction (strict, per-plugin degradation)
and the result-contract map derived from a RESOLVED resolution.

Counts and values, never names: every malformed shape yields exactly one
``result_contract_invalid``; the map expands exactly one entry per declared
tool per server; a malformed declaration excludes exactly that plugin's tools
(and represents it as NOT adopting, which under the broker means refused).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import plugin_store
from plugin_store import StoreError, manifest_result_contract
from plugin_registry import reload_snapshot, resolve_for
from plugin_fixtures import entry, mk_artifact, mk_registry


def _rc(tools, version=1, **extra):
    return {"casa": {"resultContract": {"version": version, "tools": tools, **extra}}}


# --- extraction --------------------------------------------------------------

def test_absent_is_not_adopting():
    assert manifest_result_contract({"name": "p"}) is None
    assert manifest_result_contract({"name": "p", "casa": {}}) is None
    assert manifest_result_contract({"name": "p", "casa": "nope"}) is None


def test_valid_declaration_is_normalized():
    out = manifest_result_contract({"name": "p", **_rc({
        "list": {"result": "safe"},
        "fetch": {"result": "capability", "provides": ["link"]},
        "done": {"result": "safe", "consumes": {"l": "link"}},
    })})
    assert out == {"version": 1, "tools": {
        "list": {"result": "safe", "provides": [], "consumes": {}, "delivers": {}},
        "fetch": {"result": "capability", "provides": ["link"], "consumes": {},
                  "delivers": {}},
        "done": {"result": "safe", "provides": [], "consumes": {"l": "link"},
                 "delivers": {}},
    }}


# --- #1015: `delivers` ---------------------------------------------------------

def _setup_rc(tools):
    return {"name": "p", "casa": {"setupTool": "setup_p",
                                  "resultContract": {"version": 1, "tools": tools}}}


@pytest.mark.parametrize("manifest", [
    _rc({"a": {"result": "safe", "delivers": {"x": "operator_link"}}}),      # on a safe tool
    _rc({"a": {"result": "safe", "delivers": {}}}),                          # on a safe tool, empty
    _rc({"a": {"result": "safe", "delivers": None}}),                        # explicit null (base: unknown key)
    _rc({"a": {"result": "capability", "provides": ["x"], "delivers": None}}),
    _rc({"a": {"result": "capability", "provides": ["x"],
               "delivers": {"y": "operator_link"}}}),                        # slot not provided
    _rc({"a": {"result": "capability", "provides": ["x"],
               "delivers": {"x": "operator_email"}}}),                       # unknown kind
    _rc({"a": {"result": "capability", "provides": ["x", "y"],
               "delivers": {"x": "operator_link", "y": "operator_link"}}}),  # two delivered
    _rc({"a": {"result": "capability", "provides": ["x"],
               "delivers": ["x"]}}),                                         # not an object
    _rc({"a": {"result": "capability", "provides": ["x"],
               "delivers": {"x": "operator_link"}},
         "b": {"result": "safe", "consumes": {"p": "x"}}}),                  # delivered AND consumed
    _rc({"a": {"result": "capability", "provides": ["x"],
               "delivers": {"x": "operator_link"}, "consumes": {"p": "x"}}}),  # by the same tool
    _setup_rc({"setup_p": {"result": "capability", "provides": ["x", "y"],
                           "delivers": {"x": "operator_link"}}}),            # setup: undelivered slot
    _setup_rc({"setup_p": {"result": "capability", "provides": ["x"]}}),     # setup: nothing delivered
    _setup_rc({"setup_p": {"result": "capability", "provides": ["x"],
                           "delivers": {"x": "operator_link"},
                           "consumes": {"p": "x"}}}),                        # setup: consumes
], ids=["safe", "safe-empty", "safe-null", "capability-null", "unprovided", "kind", "two",
        "shape", "consumed", "self-consumed", "setup-undelivered", "setup-none",
        "setup-consumes"])
def test_malformed_delivers_declarations_raise_result_contract_invalid(manifest):
    with pytest.raises(StoreError) as ei:
        manifest_result_contract({"name": "p", **manifest})
    assert ei.value.reason_code == "result_contract_invalid"


def test_delivers_is_normalized_and_a_delivering_setup_tool_is_accepted():
    out = manifest_result_contract({"name": "p", **_rc({
        "link": {"result": "capability", "provides": ["approval_link"],
                 "delivers": {"approval_link": "operator_link"}},
        "plain": {"result": "capability", "provides": ["token"]},
    })})
    assert out["tools"]["link"]["delivers"] == {"approval_link": "operator_link"}
    assert out["tools"]["plain"]["delivers"] == {}
    out = manifest_result_contract(_setup_rc({
        "setup_p": {"result": "capability", "provides": ["auth_url"],
                    "delivers": {"auth_url": "operator_link"}}}))
    assert out["tools"]["setup_p"] == {
        "result": "capability", "provides": ["auth_url"], "consumes": {},
        "delivers": {"auth_url": "operator_link"}}
    # the exempt form of the setup tool is still accepted beside it
    assert manifest_result_contract(_setup_rc({"setup_p": {"result": "safe"}}))[
        "tools"]["setup_p"]["delivers"] == {}


def test_setup_tool_may_be_listed_only_as_safe_without_consumes():
    base = {"name": "p", "casa": {"setupTool": "setup_p"}}
    ok = dict(base); ok["casa"] = {**base["casa"], "resultContract": {
        "version": 1, "tools": {"setup_p": {"result": "safe"},
                                "fetch": {"result": "capability", "provides": ["x"]}}}}
    assert "setup_p" in manifest_result_contract(ok)["tools"]
    bad_cap = dict(base); bad_cap["casa"] = {**base["casa"], "resultContract": {
        "version": 1, "tools": {"setup_p": {"result": "capability", "provides": ["x"]}}}}
    with pytest.raises(StoreError) as ei:
        manifest_result_contract(bad_cap)
    assert ei.value.reason_code == "result_contract_invalid"
    bad_consume = dict(base); bad_consume["casa"] = {**base["casa"], "resultContract": {
        "version": 1, "tools": {"fetch": {"result": "capability", "provides": ["x"]},
                                "setup_p": {"result": "safe", "consumes": {"a": "x"}}}}}
    with pytest.raises(StoreError):
        manifest_result_contract(bad_consume)


@pytest.mark.parametrize("manifest", [
    {"casa": {"resultContract": []}},                                   # not an object
    {"casa": {"resultContract": {"version": 2, "tools": {}}}},          # wrong version
    {"casa": {"resultContract": {"version": 1}}},                       # no tools
    {"casa": {"resultContract": {"version": 1, "tools": []}}},          # tools not an object
    {"casa": {"resultContract": {"version": 1, "tools": {}, "extra": 1}}},   # unknown top key
    _rc({"a b": {"result": "safe"}}),                                   # bad tool name
    _rc({"": {"result": "safe"}}),                                      # empty name
    _rc({"a.b": {"result": "safe"}, "a_b": {"result": "safe"}}),        # sanitization collision
    _rc({"a": "safe"}),                                                 # entry not an object
    _rc({"a": {"result": "safe", "bogus": 1}}),                         # unknown entry key
    _rc({"a": {"result": "maybe"}}),                                    # bad kind
    _rc({"a": {}}),                                                     # missing kind
    _rc({"a": {"result": "capability"}}),                               # capability without provides
    _rc({"a": {"result": "capability", "provides": []}}),               # empty provides
    _rc({"a": {"result": "safe", "provides": ["x"]}}),                  # safe with provides
    _rc({"a": {"result": "capability", "provides": ["Bad-Slot"]}}),     # bad slot grammar
    _rc({"a": {"result": "capability", "provides": ["x", "x"]}}),       # duplicate slot
    _rc({"a": {"result": "safe", "consumes": ["x"]}}),                  # consumes not an object
    _rc({"a": {"result": "safe", "consumes": {"1p": "x"}},
         "b": {"result": "capability", "provides": ["x"]}}),            # bad param name
    _rc({"a": {"result": "safe", "consumes": {"p": "nope"}}}),          # unprovided slot
    _rc({f"t{i}": {"result": "safe"} for i in range(65)}),              # too many tools
    _rc({"a": {"result": "capability", "provides": [f"s{i}" for i in range(17)]}}),  # too many slots
])
def test_malformed_declarations_raise_result_contract_invalid(manifest):
    with pytest.raises(StoreError) as ei:
        manifest_result_contract({"name": "p", **manifest})
    assert ei.value.reason_code == "result_contract_invalid"


def test_validate_manifest_refuses_a_malformed_declaration(tmp_path):
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "p", "version": "1.0.0", **_rc({"a": {"result": "maybe"}})}),
        encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        plugin_store.validate_manifest(root, "p")
    assert ei.value.reason_code == "result_contract_invalid"


def test_artifact_verdict_rechecks_result_contract(tmp_path):
    """A stored artifact carrying a malformed declaration is excluded from
    resolution (the setupTool/protectedTools upgrade-path posture)."""
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")
    res = plugin_store.publish_from_tree(
        name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", src_root=root, store_root=tmp_path / "store",
        staging_root=tmp_path / "staging")
    art = Path(res.path)
    pj = art / ".claude-plugin" / "plugin.json"
    os.chmod(art, 0o755); os.chmod(art / ".claude-plugin", 0o755); os.chmod(pj, 0o644)
    pj.write_text(json.dumps({"name": "p", "version": "1.0.0",
                              **_rc({"a": {"result": "maybe"}})}), encoding="utf-8")
    meta_path = art / plugin_store.METADATA_FILENAME
    os.chmod(meta_path, 0o644)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["content_checksum"] = plugin_store.content_checksum(art)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True),
                         encoding="utf-8")
    assert plugin_store.artifact_verdict(
        art, name="p", repo="o/r", revision="git:" + "a" * 40, subdir="",
        artifact_id=res.artifact_id) == "result_contract_invalid"


# --- the map -----------------------------------------------------------------

def _install(tmp_path, entries):
    store = tmp_path / "store"
    reload_snapshot(registry_path=mk_registry(tmp_path, entries), store_root=store)
    return store


def test_map_expands_each_declared_tool_across_every_server(tmp_path):
    from plugin_grants import result_contract_map
    store = tmp_path / "store"
    e = entry("probe", ["resident:assistant"])
    mk_artifact(store, "probe", e["artifact_id"], mcp_servers={"api": {}, "aux": {}},
                extra_manifest={"casa": {"setupTool": "setup_probe", "resultContract": {
                    "version": 1, "tools": {
                        "fetch": {"result": "capability", "provides": ["link"]},
                        "done": {"result": "safe", "consumes": {"l": "link"}},
                        "setup_probe": {"result": "safe"}}}}})
    _install(tmp_path, [e])
    m = result_contract_map(resolve_for("resident:assistant"))
    assert sorted(m.tools) == [
        "mcp__plugin_probe_api__done", "mcp__plugin_probe_api__fetch",
        "mcp__plugin_probe_aux__done", "mcp__plugin_probe_aux__fetch"]
    t = m.tools["mcp__plugin_probe_api__fetch"]
    assert (t.artifact_id, t.plugin_seg, t.kind, t.provides, t.consumes) == (
        e["artifact_id"], "probe", "capability", ("link",), {})
    assert m.tools["mcp__plugin_probe_aux__done"].consumes == {"l": "link"}
    p = m.plugins["probe"]
    assert p.adopted is True and p.artifact_id == e["artifact_id"]
    assert sorted(p.setup_tools) == [
        "mcp__plugin_probe_api__setup_probe", "mcp__plugin_probe_aux__setup_probe"]
    assert m.plugin_seg_of("mcp__plugin_probe_api__fetch") == "probe"
    assert m.plugin_seg_of("mcp__plugin_nope_x__y") is None
    assert m.plugin_seg_of("Bash") is None


def test_map_carries_delivers_and_keeps_a_capability_setup_entry(tmp_path):
    """#1015, through the REAL mapper: the delivered slot rides on the
    entry, and a setup tool declared as a capability is MAPPED (the exempt
    skip is narrowed to the safe/absent declaration) — or no setup entry
    could ever reach either hook. Its name is still listed among the
    plugin's setup tools; the hooks read the entry to decide."""
    from plugin_grants import result_contract_map
    store = tmp_path / "store"
    e = entry("probe", ["resident:assistant"])
    mk_artifact(store, "probe", e["artifact_id"], mcp_servers={"api": {}},
                extra_manifest={"casa": {"setupTool": "setup_probe", "resultContract": {
                    "version": 1, "tools": {
                        "link": {"result": "capability", "provides": ["approval_link"],
                                 "delivers": {"approval_link": "operator_link"}},
                        "fetch": {"result": "capability", "provides": ["token"]},
                        "setup_probe": {"result": "capability", "provides": ["auth_url"],
                                        "delivers": {"auth_url": "operator_link"}}}}}})
    _install(tmp_path, [e])
    m = result_contract_map(resolve_for("resident:assistant"))
    assert sorted(m.tools) == [
        "mcp__plugin_probe_api__fetch", "mcp__plugin_probe_api__link",
        "mcp__plugin_probe_api__setup_probe"]
    assert m.tools["mcp__plugin_probe_api__link"].delivers == {"approval_link": "operator_link"}
    assert m.tools["mcp__plugin_probe_api__fetch"].delivers == {}
    setup = m.tools["mcp__plugin_probe_api__setup_probe"]
    assert (setup.kind, setup.provides, setup.delivers) == (
        "capability", ("auth_url",), {"auth_url": "operator_link"})
    assert m.plugins["probe"].setup_tools == frozenset({"mcp__plugin_probe_api__setup_probe"})
    # a safe setup declaration stays unmapped (exempt), as before
    e2 = entry("plain", ["resident:assistant"])
    mk_artifact(store, "plain", e2["artifact_id"], mcp_servers={"api": {}},
                extra_manifest={"casa": {"setupTool": "setup_plain", "resultContract": {
                    "version": 1, "tools": {"setup_plain": {"result": "safe"}}}}})
    _install(tmp_path, [e, e2])
    m = result_contract_map(resolve_for("resident:assistant"))
    assert "mcp__plugin_plain_api__setup_plain" not in m.tools
    assert m.plugins["plain"].setup_tools == frozenset({"mcp__plugin_plain_api__setup_plain"})


def test_map_represents_a_non_adopting_plugin_and_skips_skill_only(tmp_path):
    from plugin_grants import result_contract_map
    store = tmp_path / "store"
    legacy = entry("legacy", ["resident:assistant"])
    mk_artifact(store, "legacy", legacy["artifact_id"], mcp_servers={"srv": {}},
                extra_manifest={"casa": {"setupTool": "setup_legacy"}})
    skill = entry("skillonly", ["resident:assistant"])
    mk_artifact(store, "skillonly", skill["artifact_id"])
    _install(tmp_path, [legacy, skill])
    m = result_contract_map(resolve_for("resident:assistant"))
    assert m.tools == {}
    assert sorted(m.plugins) == ["legacy"]
    assert m.plugins["legacy"].adopted is False
    assert m.plugins["legacy"].setup_tools == frozenset(
        {"mcp__plugin_legacy_srv__setup_legacy"})


def test_map_treats_a_malformed_declaration_as_not_adopting(tmp_path, caplog):
    """Per-plugin degradation at the map: a resolved plugin whose manifest
    carries a malformed declaration (reachable only through a resolution
    built from a stand-in, since the store verdict excludes it earlier)
    contributes zero tools and is represented as NOT adopting."""
    from types import SimpleNamespace
    from plugin_grants import result_contract_map
    root = tmp_path / "art"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": {"command": "python3"}}}))
    rp = SimpleNamespace(name="bad", artifact_id="1" * 64, path=str(root), version="1",
                         manifest={"name": "bad", **_rc({"a": {"result": "maybe"}})},
                         manifest_name="")
    good = SimpleNamespace(name="good", artifact_id="2" * 64, path=str(root), version="1",
                           manifest={"name": "good", **_rc({"a": {"result": "safe"}})},
                           manifest_name="")
    m = result_contract_map(SimpleNamespace(plugins=[rp, good]))
    assert m.plugins["bad"].adopted is False
    assert m.plugins["good"].adopted is True
    assert sorted(m.tools) == ["mcp__plugin_good_s__a"]


def test_map_without_artifact_id_is_not_adopting(tmp_path):
    from types import SimpleNamespace
    from plugin_grants import result_contract_map
    root = tmp_path / "art"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": {"command": "python3"}}}))
    rp = SimpleNamespace(name="p", artifact_id="", path=str(root), version="1",
                         manifest={"name": "p", **_rc({"a": {"result": "safe"}})},
                         manifest_name="")
    m = result_contract_map(SimpleNamespace(plugins=[rp]))
    assert m.plugins["p"].adopted is False and m.tools == {}
