"""S8 plugin access profiles — the manifest side.

``casa.profiles: {<name>: [<bare tool>, …]}`` and
``casa.requires: [{plugin, profile?, why}]`` are validated at install/update
like ``casa.jobs``: present-but-malformed refuses the artifact with a typed
``StoreError``. Profiles are authored as BARE tool names; ``casa.provides_tools``
stays FULLY QUALIFIED and verbatim, is required when ``profiles`` is present,
and must contain every expansion of every bare name over the artifact's own
servers. ``full`` is reserved.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import plugin_store
from plugin_store import StoreError, expand_tool_names, manifest_profiles, manifest_requires


FQ_READ = "mcp__plugin_gmail_gmail__search_emails"
FQ_SEND = "mcp__plugin_gmail_gmail__send_email"


def _root(tmp_path, manifest: dict, servers=("gmail",)) -> Path:
    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {s: {"command": "python3"} for s in servers}}),
        encoding="utf-8")
    return root


def _manifest(**casa) -> dict:
    return {"name": "gmail", "version": "1.0.0", "casa": casa}


def test_expand_tool_names_namespaces_a_bare_name_per_server():
    assert expand_tool_names("gmail", ["gmail"], "search_emails") == [FQ_READ]
    assert expand_tool_names("my plugin", ["a", "b-c"], "x.y") == [
        "mcp__plugin_my_plugin_a__x_y", "mcp__plugin_my_plugin_b-c__x_y"]


def test_profiles_absent_is_empty_and_requires_nothing(tmp_path):
    root = _root(tmp_path, _manifest())
    assert manifest_profiles(_manifest(), "gmail", root) == {}
    assert manifest_requires(_manifest()) == []


def test_valid_profile_returns_bare_lists(tmp_path):
    m = _manifest(profiles={"read": ["search_emails"]},
                  provides_tools=[FQ_READ, FQ_SEND])
    root = _root(tmp_path, m)
    assert manifest_profiles(m, "gmail", root) == {"read": ["search_emails"]}


@pytest.mark.parametrize("profiles", [
    ["read"],                                   # not a mapping
    {"full": ["search_emails"]},                # reserved name
    {"Read": ["search_emails"]},                # bad name
    {"read": []},                               # empty list
    {"read": ["search_emails", "search_emails"]},  # duplicate
    {"read": [FQ_READ]},                        # fully qualified, not bare
    {"read": ["a__b"]},                         # contains the separator
    {"read": [7]},                              # not a string
])
def test_malformed_profiles_refuse_with_profiles_invalid(tmp_path, profiles):
    m = _manifest(profiles=profiles, provides_tools=[FQ_READ, FQ_SEND])
    root = _root(tmp_path, m)
    with pytest.raises(StoreError) as exc:
        manifest_profiles(m, "gmail", root)
    assert exc.value.reason_code == "profiles_invalid"


def test_profiles_require_provides_tools(tmp_path):
    m = _manifest(profiles={"read": ["search_emails"]})
    root = _root(tmp_path, m)
    with pytest.raises(StoreError) as exc:
        manifest_profiles(m, "gmail", root)
    assert exc.value.reason_code == "profiles_invalid"


def test_profile_tool_not_declared_in_provides_tools_is_refused(tmp_path):
    m = _manifest(profiles={"read": ["search_emails"]}, provides_tools=[FQ_SEND])
    root = _root(tmp_path, m)
    with pytest.raises(StoreError) as exc:
        manifest_profiles(m, "gmail", root)
    assert exc.value.reason_code == "profiles_invalid"
    assert "search_emails" in str(exc.value)


def test_every_server_expansion_must_be_declared(tmp_path):
    m = _manifest(profiles={"read": ["search_emails"]}, provides_tools=[FQ_READ])
    root = _root(tmp_path, m, servers=("gmail", "other"))
    with pytest.raises(StoreError) as exc:
        manifest_profiles(m, "gmail", root)
    assert exc.value.reason_code == "profiles_invalid"


def test_valid_requires_returns_rows_with_optional_profile():
    m = _manifest(requires=[{"plugin": "gmail", "profile": "read", "why": "finds invoices"},
                            {"plugin": "ledger", "why": "books them"}])
    assert manifest_requires(m) == [
        {"plugin": "gmail", "profile": "read", "why": "finds invoices"},
        {"plugin": "ledger", "profile": None, "why": "books them"},
    ]


@pytest.mark.parametrize("requires", [
    {"plugin": "gmail"},                                     # not a list
    [{"plugin": "Gmail", "why": "x"}],                       # bad plugin name
    [{"plugin": "gmail"}],                                   # missing why
    [{"plugin": "gmail", "why": ""}],                        # empty why
    [{"plugin": "gmail", "why": "a\nb"}],                    # newline in why
    [{"plugin": "gmail", "why": "x" * 201}],                 # too long
    [{"plugin": "gmail", "profile": "full", "why": "x"}],    # reserved
    [{"plugin": "gmail", "profile": "Read", "why": "x"}],    # bad profile name
    [{"plugin": "gmail", "why": "x", "extra": 1}],           # unknown field
    [{"plugin": "gmail", "why": "x"}, {"plugin": "gmail", "why": "y"}],  # dup plugin
])
def test_malformed_requires_refuse_with_requires_invalid(requires):
    with pytest.raises(StoreError) as exc:
        manifest_requires(_manifest(requires=requires))
    assert exc.value.reason_code == "requires_invalid"


def test_validate_manifest_runs_both_checks(tmp_path):
    bad = _manifest(profiles={"full": ["search_emails"]}, provides_tools=[FQ_READ])
    root = _root(tmp_path, bad)
    with pytest.raises(StoreError) as exc:
        plugin_store.validate_manifest(root, "gmail")
    assert exc.value.reason_code == "profiles_invalid"
    bad2 = _manifest(requires=[{"plugin": "gmail"}])
    root2 = _root(tmp_path / "two", bad2)
    with pytest.raises(StoreError) as exc2:
        plugin_store.validate_manifest(root2, "gmail")
    assert exc2.value.reason_code == "requires_invalid"


@pytest.mark.parametrize("casa", [
    {"profiles": {"read\n": ["search_emails"]}, "provides_tools": [FQ_READ]},
    {"profiles": {"read": ["search_emails\n"]}, "provides_tools": [FQ_READ]},
])
def test_identifiers_ending_in_a_newline_are_refused_in_profiles(tmp_path, casa):
    m = _manifest(**casa)
    root = _root(tmp_path, m)
    with pytest.raises(StoreError) as exc:
        manifest_profiles(m, "gmail", root)
    assert exc.value.reason_code == "profiles_invalid"


@pytest.mark.parametrize("row", [
    {"plugin": "gmail\n", "why": "x"},
    {"plugin": "gmail", "profile": "read\n", "why": "x"},
])
def test_identifiers_ending_in_a_newline_are_refused_in_requires(row):
    with pytest.raises(StoreError) as exc:
        manifest_requires(_manifest(requires=[row]))
    assert exc.value.reason_code == "requires_invalid"


def test_a_bare_tool_with_a_trailing_newline_is_refused_even_when_its_sanitised_form_is_declared(tmp_path):
    """Sanitisation maps the newline to `_`; a declaration of that sanitised
    name must not launder the malformed bare name (diff round 3, Terra)."""
    m = _manifest(profiles={"read": ["search_emails\n"]},
                  provides_tools=[FQ_READ + "_"])
    root = _root(tmp_path, m)
    with pytest.raises(StoreError) as exc:
        manifest_profiles(m, "gmail", root)
    assert exc.value.reason_code == "profiles_invalid"
