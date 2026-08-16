"""§3.10 plugin-health report: structured fingerprints, carry-forward dedup,
first-contact notice.

Task 11: the report additionally surfaces the specialist-bundle registry's
`quarantined_bundles` ledger (Task 9) and boot-reconciliation actions (Task 9's
`specialist_bundle_journal.last_boot_reconcile_actions`), and annotates an
owned entry's issue/warning row with its `owner` — additive, no fingerprint
impact (§3.10 hashes only the first five PluginIssue fields)."""
from __future__ import annotations

import json
import re
from pathlib import Path

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
    plugin_health.mark_notified([fp_x], path=p,
                                generation=rA["generation"])

    rB = plugin_health.write_report(issues=[x], warnings=[], path=p)
    assert plugin_health.new_fingerprints(rB) == []          # stays notified

    plugin_health.write_report(issues=[], warnings=[], path=p)  # X resolved
    rD = plugin_health.write_report(issues=[x], warnings=[], path=p)
    assert plugin_health.new_fingerprints(rD) == [fp_x]       # NEW again


def test_notice_matches_role_and_registry_wide(tmp_path):
    p = tmp_path / "health.json"
    plugin_health.write_report(
        issues=[_issue(name="lesina-invoice", target="specialist:finance")],
        warnings=[], path=p)
    assert "lesina-invoice" in plugin_health.render_notice("finance", p)
    # #551: the reader IS the notified party — never tell them someone else was.
    assert "notified" not in plugin_health.render_notice("finance", p)
    assert plugin_health.render_notice("assistant", p) is None

    # registry-wide (target=None) matches ANY role
    plugin_health.write_report(
        issues=[_issue(name="*", target=None, stage="registry",
                       reason_code="registry_invalid", artifact_id=None)],
        warnings=[], path=p)
    assert plugin_health.render_notice("assistant", p) is not None


def test_notice_absent_or_empty(tmp_path):
    assert plugin_health.render_notice("finance",
                                       tmp_path / "nope.json") is None
    p = tmp_path / "health.json"
    plugin_health.write_report(issues=[], warnings=[], path=p)
    assert plugin_health.render_notice("finance", p) is None


def test_notice_caps_at_two_plus_more(tmp_path):
    p = tmp_path / "health.json"
    plugin_health.write_report(issues=[
        _issue(name="a", reason_code="corrupt_artifact"),
        _issue(name="b", reason_code="artifact_missing"),
        _issue(name="c", reason_code="reload_required"),
    ], warnings=[], path=p)
    notice = plugin_health.render_notice("finance", p)
    assert "a could not be loaded" in notice
    assert "b could not be found" in notice
    assert "and 1 more" in notice and "c " not in notice


def test_notice_reload_required_uses_incomplete_update_wording(tmp_path):
    """D4 (v0.74.0): never 'updating / will refresh next use' (false for a
    cached persistent Agent) — say the update did not finish."""
    p = tmp_path / "h.json"
    plugin_health.write_report(
        issues=[_issue(name="lesina-invoice", target="specialist:finance",
                       stage="verify", reason_code="reload_required")],
        warnings=[], path=p)
    notice = plugin_health.render_notice("finance", p)
    assert "did not finish" in notice
    assert "still running its previous version" in notice
    assert "will refresh" not in notice and "updating" not in notice


def test_notice_targeted_issue_does_not_warn_other_roles(tmp_path):
    p = tmp_path / "h.json"
    plugin_health.write_report(
        issues=[_issue(name="lesina-invoice", target="specialist:finance",
                       stage="verify", reason_code="reload_required")],
        warnings=[], path=p)
    assert plugin_health.render_notice("assistant", p) is None


def test_notice_mixed_issues_keep_degraded_header(tmp_path):
    p = tmp_path / "h.json"
    plugin_health.write_report(
        issues=[_issue(reason_code="reload_required"),
                _issue(name="q", reason_code="corrupt_artifact")],
        warnings=[], path=p)
    notice = plugin_health.render_notice("finance", p)
    assert notice.startswith("⚠️ Something needs attention:")


# --- #551: no reason code ever reaches the operator ------------------------

def _live_reason_codes() -> set[str]:
    """Every reason code the tree can put in a report, derived from the SOURCE
    so a code minted tomorrow is swept without anyone remembering this test.

    Sol/Terra diff r1: the first version globbed only the top level and matched
    only two shapes, so it missed `reload_required`, `authorization_missing`,
    the event family and every code pushed through a list literal or a
    `reasons =` assignment — a leak in any of those passed. It now walks the
    tree recursively and covers the shapes that actually produce a row, and
    asserts a floor below, because a regex that silently stops matching turns
    this whole sweep green and vacuous."""
    root = Path(__file__).resolve().parents[1] / "casa/rootfs/opt/casa"
    codes: set[str] = set()
    patterns = (
        r'reason_code=["\']([a-z_]+)["\']',
        r'reason_code["\']?\]?\s*[:=]\s*["\']([a-z_]+)["\']',
        r'reasons\.(?:append|extend)\(\s*\[?\s*["\']([a-z_]+)["\']',
        r'reasons\s*=\s*\[\s*["\']([a-z_]+)["\']',
        r'["\']kind["\']\s*:\s*f?["\']([a-z_]+)(?:\{|["\'])',
        # event_reconcile builds rows through a local `_issue(name, code, …)`
        # helper, so the code is a bare positional string, not a keyword.
        r'_issue\([^,)]+,\s*["\']([a-z_]+)["\']',
        # …including through a call whose first argument is itself a call,
        # e.g. `_issue(si.get("emitter"), "event_spool_issue")` (Sol/Terra
        # diff r2 — a producer the previous pattern still walked past).
        r'_issue\([^,]*\([^)]*\),\s*["\']([a-z_]+)["\']',
        r'["\']reason["\']\s*:\s*["\']([a-z_]+)["\']',
    )
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            codes.update(re.findall(pattern, text))
    # Interpolated families: `setup_episode_{status}` and the reasons a verify
    # row carries through `reasons[0]` from its own vocabulary.
    codes.update(f"setup_episode_{s}"
                 for s in ("pending", "failed", "stale", "refused"))
    codes.update({"reload_required", "env_unresolved", "setup_env_unprovisioned",
                  "not_ready", "mcp_invalid", "artifact_missing",
                  "verify_exception", "authorization_missing"})
    codes.discard("")
    return codes


def test_the_reason_code_sweep_is_not_vacuous():
    """The sweep below is only as good as its derivation: if the patterns stop
    matching, every parametrised case disappears and the suite goes green while
    testing nothing. Pin a floor and a few codes that must always be found."""
    codes = _live_reason_codes()
    assert len(codes) >= 45, f"sweep collapsed to {len(codes)} codes"
    for expected in ("reload_required", "corrupt_artifact", "event_pending_ack",
                     "callback_pending_ack", "trigger_pending_ack",
                     "setup_episode_failed", "authorization_missing",
                     "event_spool_issue"):
        assert expected in codes, f"{expected} no longer swept"


@pytest.mark.parametrize("code", sorted(_live_reason_codes()))
def test_no_reason_code_reaches_the_operator(code):
    """#551: the operator cannot act on an internal identifier and has no way
    to look one up — there is no health-read tool for any role. Every live code
    must render as something they can act on, including ones minted after this
    test was written (hence the source-derived parametrisation)."""
    line = plugin_health.describe_issue({"name": "plug", "reason_code": code})
    assert code not in line, f"{code!r} leaked verbatim: {line!r}"
    assert line.startswith("plug ")
    assert len(line) > len("plug ")


def test_unknown_reason_code_falls_back_without_leaking():
    line = plugin_health.describe_issue(
        {"name": "plug", "reason_code": "a_shape_nobody_planned"})
    assert line == "plug is not working"


def test_describe_issue_carries_the_detail():
    """#554: the detail is the one actionable fact in the row."""
    line = plugin_health.describe_issue({
        "name": "fx-setup", "reason_code": "setup_episode_failed",
        "detail": "dispatched turn could not run the setup tool"})
    assert "could not finish setting up" in line
    assert "dispatched turn could not run the setup tool" in line


# --- #551: repeat suppression ----------------------------------------------

class TestNoticeCooldown:
    """The memo records what was PUT IN FRONT of a role, never that delivery
    succeeded (no channel reports that truthfully — #556), and it DECAYS, so a
    missed delivery costs one quiet window rather than permanent silence."""

    def setup_method(self):
        plugin_health._notice_memo.clear()

    def _report(self, tmp_path, **kw):
        p = tmp_path / "h.json"
        plugin_health.write_report(
            issues=[_issue(name="fx", target=None,
                           reason_code="corrupt_artifact", **kw)],
            warnings=[], path=p)
        return p

    def test_identical_notice_suppressed_then_returns_after_the_window(
        self, tmp_path, monkeypatch,
    ):
        p = self._report(tmp_path)
        clock = {"t": 1000.0}
        monkeypatch.setattr(plugin_health.time, "monotonic",
                            lambda: clock["t"])

        first = plugin_health.pending_notice("assistant", p)
        assert first is not None
        assert plugin_health.pending_notice("assistant", p) is None

        # The red case for the DECAY property: nothing ever reported a
        # successful send, and the notice must still come back.
        clock["t"] += plugin_health._NOTICE_COOLDOWN_S + 1
        assert plugin_health.pending_notice("assistant", p) == first

    def test_changed_detail_renders_immediately(self, tmp_path):
        p = tmp_path / "h.json"
        plugin_health.write_report(
            issues=[PluginIssue("fx", None, "verify", "env_unresolved",
                                detail="FX_API_KEY")],
            warnings=[], path=p, registry_path=tmp_path / "r.json")
        first = plugin_health.pending_notice("assistant", p)
        assert "FX_API_KEY" in first
        assert plugin_health.pending_notice("assistant", p) is None

        # Same structured fingerprint, different actionable fact: the operator
        # has never seen THIS one, so it must render.
        plugin_health.write_report(
            issues=[PluginIssue("fx", None, "verify", "env_unresolved",
                                detail="FX_ACCOUNT_ID")],
            warnings=[], path=p, registry_path=tmp_path / "r.json")
        second = plugin_health.pending_notice("assistant", p)
        assert second is not None and "FX_ACCOUNT_ID" in second

    def test_resolution_re_arms_immediately(self, tmp_path):
        p = self._report(tmp_path)
        assert plugin_health.pending_notice("assistant", p) is not None

        plugin_health.write_report(issues=[], warnings=[], path=p)
        assert plugin_health.pending_notice("assistant", p) is None  # healthy

        self._report(tmp_path)
        assert plugin_health.pending_notice("assistant", p) is not None

    def test_memo_is_per_role(self, tmp_path):
        p = tmp_path / "h.json"
        plugin_health.write_report(
            issues=[_issue(name="fx", target=None,
                           reason_code="corrupt_artifact")],
            warnings=[], path=p)
        assert plugin_health.pending_notice("assistant", p) is not None
        # A registry-wide issue one role has seen must still reach another.
        assert plugin_health.pending_notice("butler", p) is not None

    def test_forget_notice_releases_only_a_matching_line(self, tmp_path):
        p = self._report(tmp_path)
        first = plugin_health.pending_notice("assistant", p)
        plugin_health.forget_notice("assistant", "something else")
        assert plugin_health.pending_notice("assistant", p) is None
        plugin_health.forget_notice("assistant", first)
        assert plugin_health.pending_notice("assistant", p) == first


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
        # The notification lands while the regeneration is mid-flight. Its
        # generation is the one on disk right now — the in-flight write has not
        # published yet, so this mark is still describing the current report.
        plugin_health.mark_notified(
            [fp_x], path=p,
            generation=plugin_health.load_report(p)["generation"])
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


def test_notice_names_the_detail(tmp_path):
    p = tmp_path / "h.json"
    plugin_health.write_report(
        issues=[PluginIssue("probe", None, "verify", "env_unresolved",
                            detail="MY_API_KEY")],
        warnings=[], path=p, registry_path=tmp_path / "registry.json")
    notice = plugin_health.render_notice("assistant", path=p)
    assert notice is not None and "MY_API_KEY" in notice


# ---------------------------------------------------------------------------
# #559 batch — report shape tolerance (design r3 D4, Sol r3 S1).
#
# A hand-corrupted /data/plugin-health.json must never raise out of a consumer:
# render_notice runs on a resident's turn through Agent._maybe_prepend_health_notice,
# which does not guard it, so a raise there costs the operator their REPLY, not
# merely the notice. Normalization FILTERS rather than rejects — dropping the
# whole report over one bad row would discard a valid blocking issue beside it,
# which is the alert we care most about keeping.
# ---------------------------------------------------------------------------

_GOOD_ROW = {"name": "fx", "target": "resident:assistant", "stage": "verify",
             "reason_code": "env_unresolved", "artifact_id": "a",
             "fingerprint": "fp-good"}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "plugin-health.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_load_report_rejects_non_mapping_json(tmp_path):
    """Valid JSON of the wrong shape reads as absent, not as a report."""
    assert plugin_health.load_report(_write(tmp_path, [1, 2])) is None
    assert plugin_health.load_report(_write(tmp_path, "nope")) is None
    assert plugin_health.load_report(_write(tmp_path, 5)) is None


def test_load_report_drops_bad_rows_and_keeps_the_valid_one(tmp_path):
    """One malformed row must not cost the valid blocking row beside it."""
    path = _write(tmp_path, {"issues": [_GOOD_ROW, [], "x", None],
                             "warnings": "not-a-list",
                             "notified_fingerprints": [[], "fp-old", 7]})
    report = plugin_health.load_report(path)
    assert [d["fingerprint"] for d in report["issues"]] == ["fp-good"]
    assert report["warnings"] == []
    assert report["notified_fingerprints"] == ["fp-old"]


def test_malformed_report_never_raises_out_of_a_consumer(tmp_path):
    """The two consumers reachable from an operator turn (render_notice, via
    Agent._maybe_prepend_health_notice) and from the DM path (new_fingerprints).

    Each case below RAISED before this change: a non-dict row, an unhashable
    `target`, and an unhashable entry in notified_fingerprints."""
    for doc in (
        {"issues": [_GOOD_ROW, []], "warnings": [], "notified_fingerprints": []},
        {"issues": [dict(_GOOD_ROW, target=[])], "warnings": [],
         "notified_fingerprints": []},
        {"issues": [_GOOD_ROW], "warnings": [], "notified_fingerprints": [[]]},
    ):
        path = _write(tmp_path, doc)
        plugin_health.render_notice("assistant", path)          # must not raise
        plugin_health.new_fingerprints(plugin_health.load_report(path))


def test_valid_issue_still_reaches_both_surfaces_beside_a_bad_row(tmp_path):
    """Assert the OUTCOME, not merely the absence of a raise: the good row is
    still announced by both the notice and the DM's fingerprint selection."""
    path = _write(tmp_path, {"issues": [_GOOD_ROW, []], "warnings": [],
                             "notified_fingerprints": []})
    assert "fx" in (plugin_health.render_notice("assistant", path) or "")
    assert plugin_health.new_fingerprints(
        plugin_health.load_report(path)) == ["fp-good"]


# ---------------------------------------------------------------------------
# ONE renderer, PER-SURFACE limits. Two renderers with the same job had already
# diverged: the in-band notice switches prefix for an all-reload_required set
# while the DM did not, so one state was announced two different ways. The
# limits stay deliberately different (2 in-band, 5 by DM) — see
# test_dm_names_five_where_the_in_band_notice_names_two.
# ---------------------------------------------------------------------------

def _row(name, code="env_unresolved", target="resident:assistant", fp=None):
    return {"name": name, "target": target, "stage": "verify",
            "reason_code": code, "artifact_id": "a",
            "fingerprint": fp or f"fp-{name}"}


def test_render_line_is_none_for_no_entries():
    assert plugin_health.render_line([]) is None


def test_render_line_names_up_to_the_limit_then_counts_the_rest():
    rows = [_row(n) for n in ("alpha", "beta", "gamma", "delta")]
    line = plugin_health.render_line(rows)
    assert "alpha" in line and "beta" in line
    assert "gamma" not in line
    assert line.endswith(", and 2 more.")


def test_render_line_keeps_the_reload_required_prefix():
    """The prefix rule is the renderer's, so BOTH surfaces get it — the DM used
    to hardcode the generic prefix, which is why an all-reload_required issue
    could never be deduped."""
    assert plugin_health.render_line(
        [_row("p", code="reload_required")]).startswith(
            "⚠️ An update did not finish:")
    assert plugin_health.render_line(
        [_row("p", code="reload_required"), _row("q")]).startswith(
            "⚠️ Something needs attention:")


def test_render_notice_is_render_line_over_this_roles_issues(tmp_path):
    """render_notice must be the renderer applied to a filtered row set — not a
    second implementation of the same sentence."""
    path = _write(tmp_path, {
        "issues": [_row("mine"), _row("theirs", target="resident:butler"),
                   _row("everyones", target=None)],
        "warnings": [], "notified_fingerprints": []})
    notice = plugin_health.render_notice("assistant", path)
    assert notice == plugin_health.render_line(
        [_row("mine"), _row("everyones", target=None)])
    assert "theirs" not in notice


# ---------------------------------------------------------------------------
# #559 — the in-band notice carries what the DM has not named
# ---------------------------------------------------------------------------


def test_report_generation_increments_on_every_write(tmp_path):
    p = tmp_path / "health.json"
    r1 = plugin_health.write_report(issues=[_issue()], warnings=[], path=p)
    r2 = plugin_health.write_report(issues=[_issue()], warnings=[], path=p)
    assert r2["generation"] == r1["generation"] + 1


def test_mark_notified_is_skipped_when_the_report_moved_on(tmp_path):
    """#559 (Sol/Terra design r1): `mark_notified` used to append whatever it
    was handed, so a row that RESOLVED while its DM was in flight had its
    fingerprint written back into a report that no longer contained it. On
    recurrence `write_report`'s prune then KEEPS that marker — the row reads as
    already announced, the DM is suppressed, and with the notice filter below
    the operator hears about it on neither surface.

    The delivered report's generation is the fence: marking applies only while
    the report the DM described is still the current one."""
    p = tmp_path / "health.json"
    x = _issue()
    fp = plugin_health.fingerprint(x)
    delivered = plugin_health.write_report(issues=[x], warnings=[], path=p)

    plugin_health.write_report(issues=[], warnings=[], path=p)   # x resolved
    plugin_health.mark_notified([fp], path=p,
                                generation=delivered["generation"])
    assert plugin_health.load_report(p)["notified_fingerprints"] == []

    # ...and the recurrence is announced, because nothing marked it.
    recurred = plugin_health.write_report(issues=[x], warnings=[], path=p)
    assert plugin_health.new_fingerprints(recurred) == [fp]


def test_mark_notified_applies_and_preserves_the_generation(tmp_path):
    p = tmp_path / "health.json"
    x = _issue()
    fp = plugin_health.fingerprint(x)
    delivered = plugin_health.write_report(issues=[x], warnings=[], path=p)
    plugin_health.mark_notified([fp], path=p,
                                generation=delivered["generation"])
    after = plugin_health.load_report(p)
    assert after["notified_fingerprints"] == [fp]
    # marking changes no rows, so it is not a new generation
    assert after["generation"] == delivered["generation"]


def test_a_legacy_report_that_moved_mid_send_is_still_fenced(tmp_path):
    """Sol/Terra diff r1: the first shape of this fence took an OPTIONAL
    generation defaulting to None, which made "the caller passed nothing"
    indistinguishable from "the report predates the field" — so across the
    upgrade that introduced it, the fence switched itself off in exactly the
    window it exists for. A report with no generation is a real value to
    compare, not a missing argument."""
    p = tmp_path / "health.json"
    x = _issue()
    fp = plugin_health.fingerprint(x)
    plugin_health.write_report(issues=[x], warnings=[], path=p)
    legacy = json.loads(p.read_text())
    del legacy["generation"]                    # a report written before v0.214.0
    p.write_text(json.dumps(legacy))
    delivered = plugin_health.load_report(p).get("generation")
    assert delivered is None

    plugin_health.write_report(issues=[x], warnings=[], path=p)   # regen mid-send
    plugin_health.mark_notified([fp], path=p, generation=delivered)
    assert plugin_health.load_report(p)["notified_fingerprints"] == []


def test_a_legacy_report_that_did_not_move_still_marks(tmp_path):
    """The other half: an untouched pre-v0.214.0 report still accepts its mark,
    so the upgrade costs at most one duplicate DM and never a lost one."""
    p = tmp_path / "health.json"
    x = _issue()
    plugin_health.write_report(issues=[x], warnings=[], path=p)
    legacy = json.loads(p.read_text())
    del legacy["generation"]
    p.write_text(json.dumps(legacy))
    plugin_health.mark_notified([plugin_health.fingerprint(x)], path=p,
                                generation=None)
    assert plugin_health.load_report(p)["notified_fingerprints"] != []


def test_a_warning_is_never_carried_by_the_in_band_notice(tmp_path):
    """The boundary the contract prose has to state exactly: `render_notice`
    selects `issues` only. A warning truncated behind the DM's "and N more"
    therefore stays unmarked and waits for a later DM — it is not carried
    in-band the way a truncated blocking issue for that role is."""
    p = tmp_path / "health.json"
    plugin_health.write_report(
        issues=[], warnings=[_issue(name="w", target="resident:assistant")],
        path=p)
    assert plugin_health.render_notice("assistant", p) is None


def test_an_executor_targeted_issue_is_never_carried_by_the_in_band_notice(
        tmp_path):
    """The other half of that boundary (Sol diff r2): the notice selects rows
    targeted at ITS role or at none, so an `executor:*` row has no in-band
    surface either — no resident's or specialist's notice matches it. Stated
    because the prose would otherwise promise in-band carriage for every
    truncated blocking issue, which is true only for the targets a role
    answers to."""
    p = tmp_path / "health.json"
    plugin_health.write_report(
        issues=[_issue(name="x", target="executor:configurator")],
        warnings=[], path=p)
    assert plugin_health.render_notice("configurator", p) is None
    assert plugin_health.render_notice("assistant", p) is None


def test_notice_omits_rows_the_dm_already_named(tmp_path):
    """The contract this batch states: the DM is the operator's record of a
    problem, named once; the in-band notice carries what the DM has NOT named.
    Filtering per ROW (rather than suppressing the whole notice) is what makes
    the two surfaces' different row sets stop mattering — the DM named `dm`,
    the notice still has `fresh` to say."""
    p = tmp_path / "health.json"
    dm_row = _issue(name="dm", target="resident:assistant")
    fresh = _issue(name="fresh", target="resident:assistant")
    plugin_health.write_report(issues=[dm_row, fresh], warnings=[], path=p)
    plugin_health.mark_notified(
        [plugin_health.fingerprint(dm_row)], path=p,
        generation=plugin_health.load_report(p)["generation"])
    notice = plugin_health.render_notice("assistant", p)
    assert "fresh" in notice
    assert "dm" not in notice


def test_notice_is_none_when_every_row_was_named(tmp_path):
    p = tmp_path / "health.json"
    rows = [_issue(name="a", target="resident:assistant"),
            _issue(name="b", target=None)]
    plugin_health.write_report(issues=rows, warnings=[], path=p)
    plugin_health.mark_notified(
        [plugin_health.fingerprint(r) for r in rows], path=p,
        generation=plugin_health.load_report(p)["generation"])
    assert plugin_health.render_notice("assistant", p) is None
