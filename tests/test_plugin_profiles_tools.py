"""S8 plugin access profiles — the configurator tools under widest-access-wins.

A profile is written only when ``plugin_assign`` CREATES a target; an existing
target is a no-op that reports the access it already holds; ``plugin_unassign``
drops the key in the same save; ``plugin_update`` refuses, before the repoint,
a manifest that no longer declares a held profile name. Requirements are
recommendations: ``requirement_candidates`` on add/assign, ``dependents`` on
unassign/remove, ``leftover_requirements`` on remove, and a ``requirement_unmet``
health WARNING — never a refusal, never an automatic write.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_plugin_tools import _State, _entry, _pr, _wire

FQ_READ = "mcp__plugin_gmail_gmail__search_emails"
FQ_SEND = "mcp__plugin_gmail_gmail__send_email"
GMAIL_CASA = {"provides_tools": [FQ_READ, FQ_SEND],
              "profiles": {"read": ["search_emails"], "sender": ["send_email"]}}


def _payload(r):
    return json.loads(r["content"][0]["text"])


def _wired(monkeypatch, tmp_path, st, **kw):
    """_wire, then publish the harness snapshot the way boot does — the S8
    helpers read the PUBLISHED snapshot's store root and never publish one."""
    tools_mod = _wire(monkeypatch, tmp_path, st, **kw)
    tools_mod.plugin_registry.reload_snapshot()
    st.log.clear()
    return tools_mod


def _store_manifest(tmp_path, name, artifact_id, casa, servers=("gmail",)):
    root = tmp_path / "store" / name / artifact_id
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "casa": casa}), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {s: {"command": "python3"} for s in servers}}), encoding="utf-8")
    return root


def _gmail_entry(targets, profiles=None):
    e = _entry("gmail")
    e["targets"] = list(targets)
    if profiles:
        e["profiles"] = dict(profiles)
    return e


# --- plugin_assign / plugin_unassign ---------------------------------------

async def test_assign_with_a_profile_writes_it_only_for_a_new_target(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["resident:assistant"]))
    _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    tools_mod = _wired(monkeypatch, tmp_path, st)
    p = _payload(await tools_mod.plugin_assign.handler(
        {"name": "gmail", "target": "specialist:finance", "profile": "read"}))
    assert p["ok"] is True and p["was_assigned"] is False
    assert p["profile"] == "read" and p["profile_tools"] == ["search_emails"]
    entry = st.raw["plugins"][0]
    assert entry["targets"] == ["resident:assistant", "specialist:finance"]
    assert entry["profiles"] == {"specialist:finance": "read"}


async def test_assign_on_an_existing_target_is_a_noop_that_reports_held_access(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "read"}))
    _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    tools_mod = _wired(monkeypatch, tmp_path, st)
    before = copy.deepcopy(st.raw)
    p = _payload(await tools_mod.plugin_assign.handler(
        {"name": "gmail", "target": "specialist:finance", "profile": "sender"}))
    assert p["ok"] is True and p["was_assigned"] is True
    assert p["profile"] == "read" and p["profile_tools"] == ["search_emails"]
    assert st.raw == before and "save" not in st.log


async def test_assign_without_a_profile_reports_full(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["resident:assistant"]))
    _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    tools_mod = _wired(monkeypatch, tmp_path, st)
    p = _payload(await tools_mod.plugin_assign.handler(
        {"name": "gmail", "target": "specialist:finance"}))
    assert p["profile"] == "full" and p["profile_tools"] is None
    assert "profiles" not in st.raw["plugins"][0]


async def test_assign_with_an_undeclared_profile_is_refused_before_any_write(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["resident:assistant"]))
    _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    tools_mod = _wired(monkeypatch, tmp_path, st)
    p = _payload(await tools_mod.plugin_assign.handler(
        {"name": "gmail", "target": "specialist:finance", "profile": "nope"}))
    assert p["ok"] is False and p["kind"] == "profile_missing_in_plugin"
    assert st.raw["plugins"][0]["targets"] == ["resident:assistant"]
    assert "save" not in st.log


async def test_unassign_drops_the_profile_key_in_the_same_save(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(
        ["resident:assistant", "specialist:finance"],
        {"resident:assistant": "read", "specialist:finance": "read"}))
    tools_mod = _wired(monkeypatch, tmp_path, st)
    await tools_mod.plugin_unassign.handler({"name": "gmail", "target": "specialist:finance"})
    assert st.raw["plugins"][0]["profiles"] == {"resident:assistant": "read"}
    await tools_mod.plugin_unassign.handler({"name": "gmail", "target": "resident:assistant"})
    assert "profiles" not in st.raw["plugins"][0]
    assert st.log.count("save") == 2


# --- plugin_update ---------------------------------------------------------

async def test_update_refuses_a_manifest_that_drops_a_held_profile(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "read"}))
    pr = _pr("gmail", "1.3.0")          # new manifest: no casa.profiles at all
    tools_mod = _wired(monkeypatch, tmp_path, st, publish=pr)
    p = _payload(await tools_mod.plugin_update.handler({"name": "gmail", "new_ref": "v1.3.0"}))
    assert p["ok"] is False and p["kind"] == "profile_missing"
    assert p["profile"] == "read" and p["target"] == "specialist:finance"
    assert "install_requirements" not in st.log and "save" not in st.log
    assert st.raw["plugins"][0]["artifact_id"] == "e" * 64


async def test_update_keeping_the_profile_reports_its_new_tool_list(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "read"}))
    # the OLD (live) manifest grants read = search + send; the new one shrinks it
    _store_manifest(tmp_path, "gmail", "e" * 64,
                    {"provides_tools": [FQ_READ, FQ_SEND],
                     "profiles": {"read": ["search_emails", "send_email"]}})
    pr = _pr("gmail", "1.3.0")
    pr.manifest["casa"] = {"provides_tools": [FQ_READ, FQ_SEND],
                           "profiles": {"read": ["search_emails"]}}
    # a real publish leaves the new artifact on disk with its .mcp.json
    import dataclasses as _dc, json as _json
    new_root = tmp_path / "new-art"
    new_root.mkdir()
    (new_root / ".mcp.json").write_text(_json.dumps(
        {"mcpServers": {"gmail": {"command": "python3"}}}), encoding="utf-8")
    pr = _dc.replace(pr, path=str(new_root))
    tools_mod = _wired(monkeypatch, tmp_path, st, publish=pr)
    p = _payload(await tools_mod.plugin_update.handler({"name": "gmail", "new_ref": "v1.3.0"}))
    assert p["ok"] is True
    assert p["profile_tools"] == {"specialist:finance": ["search_emails"]}
    assert p["profile_tools_removed"] == {"specialist:finance": [FQ_SEND]}
    assert st.raw["plugins"][0]["profiles"] == {"specialist:finance": "read"}


# --- plugin_list -----------------------------------------------------------

async def test_plugin_list_reports_profiles_beside_targets(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "read"}))
    st.raw["plugins"].append(_entry("other"))
    tools_mod = _wired(monkeypatch, tmp_path, st)
    rows = {r["name"]: r for r in tools_mod._tool_plugin_list()["plugins"]}
    assert rows["gmail"]["profiles"] == {"specialist:finance": "read"}
    assert rows["other"]["profiles"] == {}


# --- requirements ----------------------------------------------------------

def _quarterly_pr(profile="read"):
    pr = _pr("quarterly", "2.0.0")
    row = {"plugin": "gmail", "why": "finds emailed invoices"}
    if profile is not None:
        row["profile"] = profile
    pr.manifest["casa"] = {"requires": [row]}
    return pr


async def _add_quarterly(monkeypatch, tmp_path, st, *, profile="read"):
    tools_mod = _wired(monkeypatch, tmp_path, st, publish=_quarterly_pr(profile))
    return _payload(await tools_mod.plugin_add.handler({
        "name": "quarterly", "repo": "o/q", "ref": "v2",
        "targets": ["specialist:finance"]}))


@pytest.mark.parametrize("held,expected", [
    (None, "not_installed"),
    ([], "offer"),
    ([("resident:assistant", None)], "registered_elsewhere"),
    ([("specialist:finance", None)], "satisfied"),
    ([("specialist:finance", "read")], "satisfied"),
    ([("specialist:finance", "sender")], "held_narrower"),
])
async def test_add_reports_requirement_candidates(monkeypatch, tmp_path, held, expected):
    st = _State()
    if held is not None:
        targets = [t for t, _ in held]
        profiles = {t: p for t, p in held if p}
        st.raw["plugins"].append(_gmail_entry(targets, profiles))
        _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    p = await _add_quarterly(monkeypatch, tmp_path, st)
    assert p["ok"] is True
    (c,) = p["requirement_candidates"]
    assert (c["plugin"], c["profile"], c["target"], c["state"]) == (
        "gmail", "read", "specialist:finance", expected)
    assert c["why"] == "finds emailed invoices"


async def test_add_reports_a_required_profile_the_plugin_does_not_declare(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "read"}))
    _store_manifest(tmp_path, "gmail", "e" * 64,
                    {"provides_tools": [FQ_READ], "profiles": {"read": ["search_emails"]}})
    tools_mod = _wired(monkeypatch, tmp_path, st, publish=_quarterly_pr("archive"))
    p = _payload(await tools_mod.plugin_add.handler({
        "name": "quarterly", "repo": "o/q", "ref": "v2", "targets": ["specialist:finance"]}))
    (c,) = p["requirement_candidates"]
    assert c["state"] == "profile_missing_in_plugin"


async def test_an_omitted_requirement_profile_means_full(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "read"}))
    _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    p = await _add_quarterly(monkeypatch, tmp_path, st, profile=None)
    (c,) = p["requirement_candidates"]
    assert c["profile"] is None and c["state"] == "held_narrower"


async def test_unassign_and_remove_name_dependents_without_blocking(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance", "resident:assistant"],
                                          {"specialist:finance": "read"}))
    q = _entry("quarterly")
    q["targets"] = ["specialist:finance"]
    q["artifact_id"] = "f" * 64
    st.raw["plugins"].append(q)
    _store_manifest(tmp_path, "quarterly", "f" * 64,
                    {"requires": [{"plugin": "gmail", "profile": "read", "why": "finds invoices"}]},
                    servers=("q",))
    tools_mod = _wired(monkeypatch, tmp_path, st)
    p = _payload(await tools_mod.plugin_unassign.handler(
        {"name": "gmail", "target": "specialist:finance"}))
    assert p["ok"] is True
    assert p["dependents"] == [{"plugin": "quarterly", "target": "specialist:finance",
                                "profile": "read", "why": "finds invoices"}]
    assert st.raw["plugins"][0]["targets"] == ["resident:assistant"]
    p2 = _payload(await tools_mod.plugin_unassign.handler(
        {"name": "gmail", "target": "resident:assistant"}))
    assert p2["dependents"] == []


async def test_remove_of_a_dependent_names_the_leftover_requirement(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "read"}))
    q = _entry("quarterly")
    q["targets"] = ["specialist:finance"]
    q["artifact_id"] = "f" * 64
    st.raw["plugins"].append(q)
    _store_manifest(tmp_path, "quarterly", "f" * 64,
                    {"requires": [{"plugin": "gmail", "profile": "read", "why": "finds invoices"}]},
                    servers=("q",))
    tools_mod = _wired(monkeypatch, tmp_path, st)
    p = _payload(await tools_mod.plugin_remove.handler({"name": "quarterly"}))
    assert p["ok"] is True
    assert p["leftover_requirements"] == [
        {"plugin": "gmail", "target": "specialist:finance", "profile": "read"}]
    assert [e["name"] for e in st.raw["plugins"]] == ["gmail"]
    assert st.raw["plugins"][0]["targets"] == ["specialist:finance"]


async def test_health_report_carries_an_unmet_requirement_as_a_warning(monkeypatch, tmp_path):
    st = _State()
    # The regeneration reads the LIVE artifact's manifest from the store (a
    # real publish writes it there; the harness's fake publish does not).
    _store_manifest(tmp_path, "quarterly", "a" * 64,
                    {"requires": [{"plugin": "gmail", "profile": "read",
                                   "why": "finds emailed invoices"}]}, servers=("q",))
    p = await _add_quarterly(monkeypatch, tmp_path, st)
    assert p["ok"] is True
    report = json.loads(Path(tmp_path / "plugin-health.json").read_text())
    (w,) = [w for w in report["warnings"] if w["reason_code"] == "requirement_unmet"]
    assert w["name"] == "quarterly" and w["target"] == "specialist:finance"
    assert w["detail"]["plugin"] == "gmail" and w["detail"]["state"] == "not_installed"
    import plugin_health
    assert "gmail" in plugin_health.describe_issue(w)
    assert "finds emailed invoices" in plugin_health.describe_issue(w)


async def test_assign_discloses_operator_config_denies_on_profile_tools(monkeypatch, tmp_path):
    """An operator's runtime.yaml deny on a tool inside the profile keeps
    winning (never removed); the assignment envelope names it."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["resident:assistant"]))
    _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    tools_mod = _wired(monkeypatch, tmp_path, st)
    spec_reg = MagicMock()
    spec_reg.get = MagicMock(return_value=SimpleNamespace(
        tools=SimpleNamespace(disallowed=[FQ_READ, "Bash"])))
    monkeypatch.setattr(tools_mod, "_specialist_registry", spec_reg, raising=False)
    p = _payload(await tools_mod.plugin_assign.handler(
        {"name": "gmail", "target": "specialist:finance", "profile": "read"}))
    assert p["ok"] is True
    assert p["profile_tools_denied_by_config"] == ["search_emails"]


async def test_assign_without_config_denies_reports_an_empty_list(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["resident:assistant"]))
    _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    tools_mod = _wired(monkeypatch, tmp_path, st)
    monkeypatch.setattr(tools_mod, "_specialist_registry", None, raising=False)
    p = _payload(await tools_mod.plugin_assign.handler(
        {"name": "gmail", "target": "specialist:finance", "profile": "read"}))
    assert p["profile_tools_denied_by_config"] == []


async def test_a_full_holder_satisfies_a_requirement_naming_an_absent_profile(monkeypatch, tmp_path):
    """Full access satisfies every requirement, even one naming a profile the
    required plugin does not declare (diff round 1, Terra + Astra B)."""
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"]))
    _store_manifest(tmp_path, "gmail", "e" * 64,
                    {"provides_tools": [FQ_READ], "profiles": {"read": ["search_emails"]}})
    tools_mod = _wired(monkeypatch, tmp_path, st, publish=_quarterly_pr("archive"))
    p = _payload(await tools_mod.plugin_add.handler({
        "name": "quarterly", "repo": "o/q", "ref": "v2", "targets": ["specialist:finance"]}))
    (c,) = p["requirement_candidates"]
    assert c["state"] == "satisfied"


async def test_an_absent_required_profile_is_still_missing_for_a_non_holder(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["resident:assistant"]))
    _store_manifest(tmp_path, "gmail", "e" * 64,
                    {"provides_tools": [FQ_READ], "profiles": {"read": ["search_emails"]}})
    tools_mod = _wired(monkeypatch, tmp_path, st, publish=_quarterly_pr("archive"))
    p = _payload(await tools_mod.plugin_add.handler({
        "name": "quarterly", "repo": "o/q", "ref": "v2", "targets": ["specialist:finance"]}))
    (c,) = p["requirement_candidates"]
    assert c["state"] == "profile_missing_in_plugin"


async def test_an_invalid_profile_on_an_existing_target_is_still_the_noop(monkeypatch, tmp_path):
    """Whatever `profile` says, an existing assignment is a no-op that reports
    held access (diff round 2, Astra B)."""
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "read"}))
    _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    tools_mod = _wired(monkeypatch, tmp_path, st)
    p = _payload(await tools_mod.plugin_assign.handler(
        {"name": "gmail", "target": "specialist:finance", "profile": "Read"}))
    assert p["ok"] is True and p["was_assigned"] is True and p["profile"] == "read"
    assert "save" not in st.log


async def test_a_profile_ending_in_a_newline_is_refused_for_a_new_target(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["resident:assistant"]))
    _store_manifest(tmp_path, "gmail", "e" * 64, GMAIL_CASA)
    tools_mod = _wired(monkeypatch, tmp_path, st)
    p = _payload(await tools_mod.plugin_assign.handler(
        {"name": "gmail", "target": "specialist:finance", "profile": "read\n"}))
    assert p["ok"] is False and p["kind"] == "invalid_profile"
    assert "save" not in st.log


async def test_update_names_a_tool_dropped_by_a_removed_server(monkeypatch, tmp_path):
    """The removed set is old-expanded-over-OLD-servers minus new-expanded-
    over-NEW-servers (diff round 3, Astra B)."""
    import json as _json
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "read"}))
    _store_manifest(tmp_path, "gmail", "e" * 64,
                    {"provides_tools": [FQ_READ, "mcp__plugin_gmail_old__search_emails"],
                     "profiles": {"read": ["search_emails"]}}, servers=("gmail", "old"))
    pr = _pr("gmail", "1.3.0")
    pr.manifest["casa"] = {"provides_tools": [FQ_READ], "profiles": {"read": ["search_emails"]}}
    new_root = tmp_path / "new-art"
    new_root.mkdir()
    (new_root / ".mcp.json").write_text(_json.dumps(
        {"mcpServers": {"gmail": {"command": "python3"}}}), encoding="utf-8")
    import dataclasses as _dc
    pr = _dc.replace(pr, path=str(new_root))
    tools_mod = _wired(monkeypatch, tmp_path, st, publish=pr)
    p = _payload(await tools_mod.plugin_update.handler({"name": "gmail", "new_ref": "v1.3.0"}))
    assert p["ok"] is True
    assert p["profile_tools"] == {"specialist:finance": ["search_emails"]}
    assert p["profile_tools_removed"] == {"specialist:finance": ["mcp__plugin_gmail_old__search_emails"]}


async def test_a_wider_profile_covers_a_narrower_named_requirement(monkeypatch, tmp_path):
    """Containment, not equality: a target holding `wide` (search + send)
    satisfies a requirement naming `read` (search), nothing is written to the
    required plugin, and only the added plugin is saved (diff round 4, Astra B)."""
    st = _State()
    st.raw["plugins"].append(_gmail_entry(["specialist:finance"],
                                          {"specialist:finance": "wide"}))
    _store_manifest(tmp_path, "gmail", "e" * 64,
                    {"provides_tools": [FQ_READ, FQ_SEND],
                     "profiles": {"read": ["search_emails"],
                                  "wide": ["search_emails", "send_email"]}})
    p = await _add_quarterly(monkeypatch, tmp_path, st)
    assert p["ok"] is True
    (c,) = p["requirement_candidates"]
    assert c["state"] == "satisfied"
    assert st.log.count("save") == 1
    assert st.raw["plugins"][0]["profiles"] == {"specialist:finance": "wide"}
    assert st.raw["plugins"][0]["targets"] == ["specialist:finance"]
