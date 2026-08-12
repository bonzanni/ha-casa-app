"""§3.10 plugin-health report: structured fingerprints, carry-forward dedup,
first-contact notice.

Task 11: the report additionally surfaces the specialist-bundle registry's
`quarantined_bundles` ledger (Task 9) and boot-reconciliation actions (Task 9's
`specialist_bundle_journal.last_boot_reconcile_actions`), and annotates an
owned entry's issue/warning row with its `owner` — additive, no fingerprint
impact (§3.10 hashes only the first five PluginIssue fields)."""
from __future__ import annotations

import json

import pytest

import plugin_health
import specialist_bundle_journal
from plugin_registry import PluginIssue

pytestmark = pytest.mark.unit


def _issue(name="p", target="specialist:finance", stage="resolve",
           reason_code="corrupt_artifact", artifact_id="a" * 64):
    return PluginIssue(name=name, target=target, stage=stage,
                       reason_code=reason_code, artifact_id=artifact_id)


def _registry_doc(*, quarantined_bundles=None, plugins=None) -> dict:
    return {
        "schema_version": 1,
        "seeded_defaults": [],
        "plugins": plugins or [],
        "quarantined_bundles": quarantined_bundles or [],
    }


def test_fingerprint_stable_and_field_sensitive():
    a = _issue()
    b = _issue()
    assert plugin_health.fingerprint(a) == plugin_health.fingerprint(b)
    # different reason_code → different fingerprint
    c = _issue(reason_code="artifact_missing")
    assert plugin_health.fingerprint(c) != plugin_health.fingerprint(a)
    # different target → different fingerprint
    d = _issue(target="resident:assistant")
    assert plugin_health.fingerprint(d) != plugin_health.fingerprint(a)


def test_write_load_roundtrip(tmp_path):
    p = tmp_path / "health.json"
    rep = plugin_health.write_report(issues=[_issue()], warnings=[], path=p)
    assert rep["schema_version"] == 1
    assert rep["issues"][0]["reason_code"] == "corrupt_artifact"
    loaded = plugin_health.load_report(p)
    assert loaded == rep


def test_carry_forward_dedup_and_reappearance(tmp_path):
    p = tmp_path / "health.json"
    x = _issue()
    fp_x = plugin_health.fingerprint(x)

    rA = plugin_health.write_report(issues=[x], warnings=[], path=p)
    assert plugin_health.new_fingerprints(rA) == [fp_x]
    plugin_health.mark_notified([fp_x], path=p)

    rB = plugin_health.write_report(issues=[x], warnings=[], path=p)
    assert plugin_health.new_fingerprints(rB) == []          # stays notified

    plugin_health.write_report(issues=[], warnings=[], path=p)  # X resolved
    rD = plugin_health.write_report(issues=[x], warnings=[], path=p)
    assert plugin_health.new_fingerprints(rD) == [fp_x]       # NEW again


def test_first_contact_notice_matches_role_and_registry_wide(tmp_path):
    p = tmp_path / "health.json"
    plugin_health.write_report(
        issues=[_issue(name="lesina-invoice", target="specialist:finance")],
        warnings=[], path=p)
    assert "lesina-invoice" in plugin_health.first_contact_notice("finance", p)
    assert "operator has been notified" in \
        plugin_health.first_contact_notice("finance", p)
    assert plugin_health.first_contact_notice("assistant", p) is None

    # registry-wide (target=None) matches ANY role
    plugin_health.write_report(
        issues=[_issue(name="*", target=None, stage="registry",
                       reason_code="registry_invalid", artifact_id=None)],
        warnings=[], path=p)
    assert plugin_health.first_contact_notice("assistant", p) is not None


def test_first_contact_notice_absent_or_empty(tmp_path):
    assert plugin_health.first_contact_notice("finance",
                                              tmp_path / "nope.json") is None
    p = tmp_path / "health.json"
    plugin_health.write_report(issues=[], warnings=[], path=p)
    assert plugin_health.first_contact_notice("finance", p) is None


def test_first_contact_notice_caps_at_two_plus_more(tmp_path):
    p = tmp_path / "health.json"
    plugin_health.write_report(issues=[
        _issue(name="a", reason_code="corrupt_artifact"),
        _issue(name="b", reason_code="artifact_missing"),
        _issue(name="c", reason_code="reload_required"),
    ], warnings=[], path=p)
    notice = plugin_health.first_contact_notice("finance", p)
    assert "a (corrupt_artifact)" in notice and "b (artifact_missing)" in notice
    assert "+1 more" in notice and "c (" not in notice


def test_first_contact_reload_required_uses_incomplete_update_wording(tmp_path):
    """D4 (v0.74.0): never 'updating / will refresh next use' (false for a
    cached persistent Agent) — say the update is incomplete."""
    p = tmp_path / "h.json"
    plugin_health.write_report(
        issues=[_issue(name="lesina-invoice", target="specialist:finance",
                       stage="verify", reason_code="reload_required")],
        warnings=[], path=p)
    notice = plugin_health.first_contact_notice("finance", p)
    assert "Plugin update incomplete" in notice
    assert "remains bound to the previous artifact" in notice
    assert "reload_required" in notice
    assert "will refresh" not in notice and "updating" not in notice


def test_first_contact_targeted_issue_does_not_warn_other_roles(tmp_path):
    p = tmp_path / "h.json"
    plugin_health.write_report(
        issues=[_issue(name="lesina-invoice", target="specialist:finance",
                       stage="verify", reason_code="reload_required")],
        warnings=[], path=p)
    assert plugin_health.first_contact_notice("assistant", p) is None


def test_first_contact_mixed_issues_keep_degraded_header(tmp_path):
    p = tmp_path / "h.json"
    plugin_health.write_report(
        issues=[_issue(reason_code="reload_required"),
                _issue(name="q", reason_code="corrupt_artifact")],
        warnings=[], path=p)
    notice = plugin_health.first_contact_notice("finance", p)
    assert notice.startswith("⚠️ Plugin degraded:")


# ---------------------------------------------------------------------------
# Task 11: bundle ownership + quarantine surfacing
# ---------------------------------------------------------------------------

def test_report_surfaces_quarantined_bundles_from_registry_doc(tmp_path):
    hp = tmp_path / "health.json"
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps(_registry_doc(quarantined_bundles=["mtg"])))
    rep = plugin_health.write_report(issues=[], warnings=[], path=hp,
                                     registry_path=reg)
    assert rep["quarantined_bundles"] == ["mtg"]
    assert plugin_health.load_report(hp)["quarantined_bundles"] == ["mtg"]


def test_report_quarantined_bundles_empty_when_registry_absent(tmp_path):
    hp = tmp_path / "health.json"
    rep = plugin_health.write_report(
        issues=[], warnings=[], path=hp,
        registry_path=tmp_path / "no-such-registry.json")
    assert rep["quarantined_bundles"] == []


def test_owned_entry_issue_row_carries_owner_and_scoped_name(tmp_path):
    hp = tmp_path / "health.json"
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps(_registry_doc(plugins=[
        {"name": "mtg.mtg", "owner": "specialist:mtg",
         "manifest_name": "mtg", "targets": ["specialist:mtg"]},
    ])))
    issue = _issue(name="mtg.mtg", target="specialist:mtg",
                   reason_code="reload_required")
    rep = plugin_health.write_report(issues=[issue], warnings=[], path=hp,
                                     registry_path=reg)
    row = rep["issues"][0]
    assert row["name"] == "mtg.mtg"                       # already scoped
    assert row["owner"] == "specialist:mtg"


def test_unowned_entry_issue_row_carries_no_owner_key(tmp_path):
    hp = tmp_path / "health.json"
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps(_registry_doc(plugins=[
        {"name": "gmail", "targets": ["resident:assistant"]},
    ])))
    rep = plugin_health.write_report(
        issues=[_issue(name="gmail", target="resident:assistant")],
        warnings=[], path=hp, registry_path=reg)
    assert "owner" not in rep["issues"][0]


def test_boot_reconcile_actions_roundtrip(tmp_path, monkeypatch):
    hp = tmp_path / "health.json"
    actions = [{"slug": "mtg", "action": "quarantine"},
               {"slug": None, "action": "quarantine_all"}]
    monkeypatch.setattr(specialist_bundle_journal,
                        "last_boot_reconcile_actions", actions)
    rep = plugin_health.write_report(issues=[], warnings=[], path=hp,
                                     registry_path=tmp_path / "none.json")
    assert rep["boot_reconcile_actions"] == actions
    assert plugin_health.load_report(hp)["boot_reconcile_actions"] == actions


def test_boot_reconcile_actions_empty_by_default(tmp_path, monkeypatch):
    hp = tmp_path / "health.json"
    monkeypatch.setattr(specialist_bundle_journal,
                        "last_boot_reconcile_actions", [])
    rep = plugin_health.write_report(issues=[], warnings=[], path=hp,
                                     registry_path=tmp_path / "none.json")
    assert rep["boot_reconcile_actions"] == []


def test_fingerprint_unaffected_by_owner_and_top_level_keys(tmp_path):
    """An owned entry's fingerprint must be IDENTICAL whether or not the
    registry annotates it with an owner — the fingerprint (§3.10) hashes only
    name/target/stage/reason_code/artifact_id, computed before `owner` is
    ever attached to the serialized row."""
    hp_bare = tmp_path / "bare.json"
    hp_owned = tmp_path / "owned.json"
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps(_registry_doc(plugins=[
        {"name": "mtg.mtg", "owner": "specialist:mtg",
         "manifest_name": "mtg", "targets": ["specialist:mtg"]},
    ])))
    issue = _issue(name="mtg.mtg", target="specialist:mtg")

    rep_bare = plugin_health.write_report(
        issues=[issue], warnings=[], path=hp_bare,
        registry_path=tmp_path / "no-registry.json")
    rep_owned = plugin_health.write_report(
        issues=[issue], warnings=[], path=hp_owned, registry_path=reg)

    assert "owner" not in rep_bare["issues"][0]
    assert rep_owned["issues"][0]["owner"] == "specialist:mtg"
    assert (rep_bare["issues"][0]["fingerprint"]
            == rep_owned["issues"][0]["fingerprint"]
            == plugin_health.fingerprint(issue))


def test_concurrent_regeneration_keeps_just_marked_fingerprint(tmp_path, monkeypatch):
    """#353 (low): write_report runs in a thread (asyncio.to_thread) while
    mark_notified runs on the loop. A regeneration that read the previous
    report BEFORE a concurrent mark_notified landed must not overwrite the
    file without the just-delivered marker — that would re-DM the same issue.
    The read-merge-write must be serialized (and the previous report read
    inside the critical section)."""
    import threading

    p = tmp_path / "health.json"
    x = _issue()
    fp_x = plugin_health.fingerprint(x)
    plugin_health.write_report(issues=[x], warnings=[], path=p)

    in_registry_state = threading.Event()
    release = threading.Event()
    real_registry_state = plugin_health._registry_state

    def gated_registry_state(registry_path=None):
        in_registry_state.set()
        release.wait(timeout=5)
        return real_registry_state(registry_path)

    monkeypatch.setattr(plugin_health, "_registry_state", gated_registry_state)

    t = threading.Thread(
        target=plugin_health.write_report,
        kwargs={"issues": [x], "warnings": [], "path": p},
    )
    t.start()
    try:
        assert in_registry_state.wait(timeout=5)
        # The notification lands while the regeneration is mid-flight.
        plugin_health.mark_notified([fp_x], path=p)
    finally:
        release.set()
        t.join(timeout=5)
    assert not t.is_alive()

    final = plugin_health.load_report(p)
    assert fp_x in (final.get("notified_fingerprints") or []), (
        "a concurrent regeneration must not erase a delivered-notification marker"
    )


# ---------------------------------------------------------------------------
# #533 — issue detail: reported, DM'd, never fingerprinted
# ---------------------------------------------------------------------------


def test_issue_detail_reported_but_never_fingerprinted(tmp_path):
    """#533: `detail` carries the unresolved var NAMES into the report row.
    It is additive — §3.10's fingerprint hashes the five structured fields
    only, so a wording/detail change never re-alerts."""
    a = PluginIssue("probe", None, "verify", "env_unresolved",
                    detail="A_KEY, B_KEY")
    b = PluginIssue("probe", None, "verify", "env_unresolved")
    assert plugin_health.fingerprint(a) == plugin_health.fingerprint(b)

    rep = plugin_health.write_report(
        issues=[a], warnings=[], path=tmp_path / "h.json",
        registry_path=tmp_path / "registry.json")
    assert rep["issues"][0]["detail"] == "A_KEY, B_KEY"

    rep2 = plugin_health.write_report(
        issues=[b], warnings=[], path=tmp_path / "h2.json",
        registry_path=tmp_path / "registry.json")
    assert "detail" not in rep2["issues"][0]


def test_first_contact_notice_names_the_detail(tmp_path):
    p = tmp_path / "h.json"
    plugin_health.write_report(
        issues=[PluginIssue("probe", None, "verify", "env_unresolved",
                            detail="MY_API_KEY")],
        warnings=[], path=p, registry_path=tmp_path / "registry.json")
    notice = plugin_health.first_contact_notice("assistant", path=p)
    assert notice is not None and "MY_API_KEY" in notice
