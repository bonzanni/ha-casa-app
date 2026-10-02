"""S8 plugin access profiles — the registry's sibling ``profiles`` map.

``profiles`` is OPTIONAL and sits beside ``targets``: ``{"<target>": "<name>"}``.
``targets`` keeps its type and meaning (consent identities read it alone), the
schema version stays 1, and an absent key means full access — today's
behaviour. A malformed map is ``bad_profiles`` for THAT entry, the posture
``bad_targets`` already has; an owned (bundle) entry never carries one.
"""
from __future__ import annotations

import pytest

import plugin_registry
from plugin_registry import load_registry, reload_snapshot, resolve_all, resolve_for
from plugin_fixtures import entry, mk_artifact, mk_registry, owned_entry


def _loaded(tmp_path, e, *, art=True):
    store = tmp_path / "store"
    if art:
        mk_artifact(store, e["name"], e["artifact_id"],
                    manifest_name=e.get("manifest_name"),
                    mcp_servers={e["name"].split(".")[-1]: {}})
    path = mk_registry(tmp_path, [e])
    reload_snapshot(registry_path=path, store_root=store)
    return load_registry(path)


def test_profile_rides_the_resolved_plugin_for_its_target_only(tmp_path):
    e = entry("gmail", ["resident:assistant", "specialist:finance"])
    e["profiles"] = {"specialist:finance": "read"}
    data = _loaded(tmp_path, e)
    assert data.valid and data.entry_issues == []
    assert resolve_for("specialist:finance").plugins[0].profile == "read"
    assert resolve_for("resident:assistant").plugins[0].profile is None
    assert resolve_all().plugins[0].profile is None


def test_absent_profiles_key_is_full_access_as_today(tmp_path):
    data = _loaded(tmp_path, entry("gmail", ["specialist:finance"]))
    assert data.entry_issues == []
    assert resolve_for("specialist:finance").plugins[0].profile is None


def test_profiles_key_outside_targets_is_bad_profiles(tmp_path):
    e = entry("gmail", ["specialist:finance"])
    e["profiles"] = {"resident:assistant": "read"}
    assert plugin_registry._entry_error(e) == "bad_profiles"
    data = _loaded(tmp_path, e)
    assert [i.reason_code for i in data.entry_issues] == ["entry_invalid"]
    assert data.entries == []


@pytest.mark.parametrize("value", ["full", "Read", "", "r" * 25, 7, None, "read\n"])
def test_profiles_value_must_be_a_profile_name(tmp_path, value):
    e = entry("gmail", ["specialist:finance"])
    e["profiles"] = {"specialist:finance": value}
    assert plugin_registry._entry_error(e) == "bad_profiles"
    data = _loaded(tmp_path, e)
    assert [i.reason_code for i in data.entry_issues] == ["entry_invalid"]


@pytest.mark.parametrize("value", [["read"], "read", 1])
def test_profiles_not_a_mapping_is_bad_profiles(tmp_path, value):
    e = entry("gmail", ["specialist:finance"])
    e["profiles"] = value
    assert plugin_registry._entry_error(e) == "bad_profiles"
    data = _loaded(tmp_path, e)
    assert [i.reason_code for i in data.entry_issues] == ["entry_invalid"]


def test_owned_entry_with_profiles_is_owned_invariant(tmp_path):
    e = owned_entry()
    e["profiles"] = {"specialist:mtg": "read"}
    assert plugin_registry._entry_error(e) == "owned_invariant"
    data = _loaded(tmp_path, e, art=False)
    assert [i.reason_code for i in data.entry_issues] == ["entry_invalid"]


def test_profiles_survive_a_save_round_trip(tmp_path):
    e = entry("gmail", ["specialist:finance"])
    e["profiles"] = {"specialist:finance": "read"}
    path = mk_registry(tmp_path, [e])
    data = load_registry(path)
    plugin_registry.save_registry(data, path)
    again = load_registry(path)
    assert again.raw["plugins"][0]["profiles"] == {"specialist:finance": "read"}
