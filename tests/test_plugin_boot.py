"""init-plugin-store oneshot (plugin_boot): bundled import → seed → resolve-all →
health report. ALWAYS returns 0 (§3.6). The pre-v0.71.0 migration was removed in
v0.72.0; seeding is now unconditional (no sentinel) and idempotent."""
from __future__ import annotations

import pytest

import plugin_boot
import plugin_health
import plugin_registry
import plugin_store
import specialist_bundle_journal
from plugin_registry import PluginIssue, RegistryData, ResolutionResult

pytestmark = pytest.mark.unit


def _wire(monkeypatch, tmp_path, *, valid=True, import_issues=None,
          seeded=False, resolve_issues=None):
    reports = {"saved": 0}
    monkeypatch.setattr(plugin_boot, "BUNDLE_ROOT", tmp_path / "bundle")
    monkeypatch.setattr(plugin_registry, "STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(plugin_store, "import_bundle",
                        lambda root: list(import_issues or []))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: RegistryData(
                            raw={"schema_version": 1, "seeded_defaults": [],
                                 "plugins": []}, valid=valid))
    monkeypatch.setattr(plugin_registry, "seed_defaults", lambda *a, **k: seeded)

    def _save(*a, **k):
        reports["saved"] += 1
    monkeypatch.setattr(plugin_registry, "save_registry", _save)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: ResolutionResult(registry_valid=valid,
                                                 issues=list(resolve_issues or [])))

    def _write(*, issues, warnings, path=None):
        reports["issues"] = list(issues)
        reports["warnings"] = list(warnings)
    monkeypatch.setattr(plugin_health, "write_report", _write)
    return reports


def test_boot_happy_returns_zero(monkeypatch, tmp_path):
    reports = _wire(monkeypatch, tmp_path)
    assert plugin_boot.main() == 0
    assert "issues" in reports                       # health report written


def test_boot_unreadable_registry_reports_invalid(monkeypatch, tmp_path):
    reports = _wire(monkeypatch, tmp_path, valid=False)
    assert plugin_boot.main() == 0
    assert any(i.reason_code == "registry_invalid" for i in reports["issues"])


def test_boot_seeds_unconditionally_and_saves(monkeypatch, tmp_path):
    """v0.72.0: migration + its sentinel are gone — seed_defaults runs on EVERY
    boot (no sentinel gate) and a mutating seed is persisted. On a fresh install
    the absent registry loads valid-empty and this is the write that creates it."""
    reports = _wire(monkeypatch, tmp_path, seeded=True)    # seed reports a mutation
    assert plugin_boot.main() == 0
    assert reports["saved"] == 1


def test_boot_seed_noop_not_saved(monkeypatch, tmp_path):
    """A no-op seed (nothing new to add) does not rewrite the registry."""
    reports = _wire(monkeypatch, tmp_path, seeded=False)
    assert plugin_boot.main() == 0
    assert reports["saved"] == 0


def test_boot_invalid_registry_not_seeded_not_overwritten(monkeypatch, tmp_path):
    """A corrupt/zero-byte registry must NOT be treated as fresh: no seed, no
    save (never overwrite evidence / reseed removed defaults), flag invalid."""
    seen = {"seeded": False}
    reports = _wire(monkeypatch, tmp_path, valid=False)
    monkeypatch.setattr(
        plugin_registry, "seed_defaults",
        lambda *a, **k: seen.__setitem__("seeded", True) or True)
    assert plugin_boot.main() == 0
    assert seen["seeded"] is False and reports["saved"] == 0
    assert any(i.reason_code == "registry_invalid" for i in reports["issues"])


def test_boot_resolve_issues_reach_health(monkeypatch, tmp_path):
    issue = PluginIssue(name="lesina", target=None, stage="resolve",
                        reason_code="artifact_invalid")
    reports = _wire(monkeypatch, tmp_path, resolve_issues=[issue])
    assert plugin_boot.main() == 0
    assert any(i.reason_code == "artifact_invalid" for i in reports["issues"])


def test_boot_calls_bundle_reconcile_before_reload_snapshot(monkeypatch, tmp_path):
    """Task 9: crash-safe bundle-op journal reconciliation must run BEFORE
    the plugin snapshot loads."""
    _wire(monkeypatch, tmp_path)
    order = []
    monkeypatch.setattr(specialist_bundle_journal, "reconcile_boot",
                        lambda *a, **k: order.append("reconcile") or [])
    monkeypatch.setattr(plugin_registry, "reload_snapshot",
                        lambda *a, **k: order.append("reload"))
    assert plugin_boot.main() == 0
    assert order == ["reconcile", "reload"]


def test_boot_bundle_reconcile_exception_degrades_boot_not_blocks(monkeypatch, tmp_path):
    """§3.6 degrade-and-boot: a reconciliation failure (even one that
    escapes reconcile_boot's own internal quarantine handling) must never
    stop the rest of boot from proceeding."""
    reports = _wire(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("bundle reconcile boom")
    monkeypatch.setattr(specialist_bundle_journal, "reconcile_boot", _boom)
    reloaded = {"called": False}
    monkeypatch.setattr(plugin_registry, "reload_snapshot",
                        lambda *a, **k: reloaded.__setitem__("called", True))
    assert plugin_boot.main() == 0
    assert reloaded["called"] is True
    assert "issues" in reports


def test_boot_exception_returns_zero_with_boot_exception(monkeypatch, tmp_path):
    """§3.6: any boot exception becomes a boot_exception health issue and the
    process still exits 0 (never blocks svc-casa)."""
    reports = _wire(monkeypatch, tmp_path)

    def _boom(root):
        raise RuntimeError("boom")
    monkeypatch.setattr(plugin_store, "import_bundle", _boom)
    assert plugin_boot.main() == 0
    assert any(i.reason_code == "boot_exception" for i in reports["issues"])


# --- #669: a partial boot write must not clear a notification mark ----------
#
# Red case specified by the drive review round (redcase-specify-sol, run
# 2026-08-25 cluster P). It pins the INVARIANT, not the parameter that
# implements it: a fingerprint marked notified must SURVIVE boot's
# resolver-only write and be cleared only by an authoritative full
# regeneration that genuinely no longer carries the row.
#
# The health writer here is the REAL `plugin_health.write_report` — the
# monkeypatch is a forwarding adapter that only redirects the path, so every
# transition executes production code. It forwards `**kwargs` rather than a
# fixed signature so that on the pre-fix tree no argument that does not exist
# yet is ever synthesized: the test fails on a fingerprint COUNT, never on a
# TypeError.

@pytest.mark.parametrize("writer", ["normal", "exception"])
def test_boot_partial_health_write_preserves_standing_mark_and_exposes_new_issue(
        monkeypatch, tmp_path, writer):
    path = tmp_path / "health.json"
    registry_path = tmp_path / "registry.json"
    real_write_report = plugin_health.write_report

    # A runtime-class row boot can never observe: only a full regeneration
    # (which runs verify over every registered plugin) produces it.
    x = PluginIssue(name="plg-a", target="resident:assistant", stage="verify",
                    reason_code="secret_missing")
    fp_x = plugin_health.fingerprint(x)

    r0 = real_write_report(issues=[x], warnings=[], path=path,
                           registry_path=registry_path)
    plugin_health.mark_notified([fp_x], path=path,
                                generation=r0["generation"])
    assert len(plugin_health.load_report(path)["notified_fingerprints"]) == 1

    # A genuinely NEW problem, present at this boot and never announced.
    if writer == "normal":
        y = PluginIssue(name="plg-b", target=None, stage="resolve",
                        reason_code="artifact_missing")
        _wire(monkeypatch, tmp_path, resolve_issues=[y])
    else:
        # The degraded exception writer at plugin_boot.py:115 — its sole row
        # is the boot_exception one it appends itself.
        _wire(monkeypatch, tmp_path)
        monkeypatch.setattr(plugin_store, "import_bundle",
                            lambda root: (_ for _ in ()).throw(RuntimeError("boom")))

    def write_to_test_path(*, issues, warnings, **kwargs):
        kwargs.pop("path", None)
        return real_write_report(issues=issues, warnings=warnings, path=path,
                                 registry_path=registry_path, **kwargs)
    monkeypatch.setattr(plugin_health, "write_report", write_to_test_path)

    assert plugin_boot.main() == 0
    partial = plugin_health.load_report(path)
    # X's mark SURVIVED a write that could not have observed X resolving...
    assert len(partial["notified_fingerprints"]) == 1
    # ...and the genuinely new row is still announceable.
    assert len(plugin_health.new_fingerprints(partial)) == 1

    # The authoritative full regeneration carries X again: unchanged, so it is
    # announced no second time, while only the new row remains new.
    monkeypatch.setattr(plugin_health, "write_report", real_write_report)
    y_full = PluginIssue(name="plg-b", target=None, stage="resolve",
                         reason_code="artifact_missing")
    full = real_write_report(issues=[x, y_full], warnings=[], path=path,
                             registry_path=registry_path)
    assert len(full["notified_fingerprints"]) == 1
    assert len(plugin_health.new_fingerprints(full)) == 1

    # The converse, in the same sequence: an AUTHORITATIVE write that no longer
    # carries X must still clear its mark, and a recurrence is newly
    # announceable. Without this half, "never prune" would also pass.
    cleared = real_write_report(issues=[], warnings=[], path=path,
                                registry_path=registry_path)
    assert len(cleared["notified_fingerprints"]) == 0
    recurred = real_write_report(issues=[x], warnings=[], path=path,
                                 registry_path=registry_path)
    assert len(plugin_health.new_fingerprints(recurred)) == 1
