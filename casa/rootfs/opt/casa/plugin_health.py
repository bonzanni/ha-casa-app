"""Plugin health — durable report + operator notification (spec §3.10).

Every boot and every mutation regenerates /data/plugin-health.json. Issue
"fingerprints" hash the STRUCTURED fields (name, target, stage, reason_code,
artifact_id) — never free-form reason text — so a wording change never
re-alerts. A post-boot Telegram DM fires (via the deterministic bus, like
notify_config_sync) when the report contains NEW fingerprints; an issue
disappearing from the report clears its fingerprint. While the report holds
unresolved blocking issues, the affected resident prepends a one-line notice
to its first user-visible turn (first_contact_notice).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

HEALTH_PATH = Path("/data/plugin-health.json")

# #353: write_report often runs in a worker thread (asyncio.to_thread via
# tools._regenerate_plugin_health) while mark_notified runs on the event
# loop — both read-modify-write the same report file. A threading.Lock (not
# asyncio) serializes the critical sections across threads so a mid-flight
# regeneration can never overwrite a just-delivered notification marker
# (which would re-DM the same issue).
_REPORT_LOCK = threading.Lock()


def fingerprint(issue) -> str:
    """SHA-256 over the STRUCTURED issue fields only (§3.10). A PluginIssue or
    an already-serialized issue dict both work."""
    def _get(field: str):
        if isinstance(issue, dict):
            return issue.get(field)
        return getattr(issue, field, None)
    body = "\x00".join([
        str(_get("name") or ""),
        str(_get("target") or ""),
        str(_get("stage") or ""),
        str(_get("reason_code") or ""),
        str(_get("artifact_id") or ""),
    ])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _issue_dict(issue, owners: dict | None = None) -> dict:
    def _get(field: str):
        if isinstance(issue, dict):
            return issue.get(field)
        return getattr(issue, field, None)
    d = {
        "name": _get("name"),
        "target": _get("target"),
        "stage": _get("stage"),
        "reason_code": _get("reason_code"),
        "artifact_id": _get("artifact_id"),
        "fingerprint": fingerprint(issue),
    }
    # Task 11: an owned entry's issue/warning row is additionally annotated
    # with its bundle owner (`specialist:<slug>`) — the `name` field is
    # already the entry's SCOPED name (`<slug>.<manifest_name>`, spec §2),
    # unchanged. Never part of the fingerprint (computed above from the raw
    # issue, before this key is added).
    if owners:
        owner = owners.get(d["name"])
        if owner is not None:
            d["owner"] = owner
    return d


def _registry_state(registry_path=None) -> tuple[list, dict]:
    """Best-effort read of the specialist-bundle registry state Task 9 writes:
    the `quarantined_bundles` ledger and a `{scoped name: owner}` map for
    every owned entry currently in the registry. Never raises — a missing or
    corrupt registry degrades to empty state, matching this module's own
    boot-must-never-crash tolerance."""
    try:
        import plugin_registry
        path = registry_path if registry_path is not None else plugin_registry.REGISTRY_PATH
        data = plugin_registry.load_registry(path)
        raw = data.raw if isinstance(data.raw, dict) else {}
        quarantined = raw.get("quarantined_bundles")
        quarantined = list(quarantined) if isinstance(quarantined, list) else []
        owners: dict = {}
        for e in (raw.get("plugins") or []):
            if not isinstance(e, dict):
                continue
            owner = plugin_registry.entry_owner(e)
            name = e.get("name")
            if owner is not None and isinstance(name, str):
                owners[name] = owner
        return quarantined, owners
    except Exception:  # noqa: BLE001 — health must never crash on a bad registry
        logger.exception("plugin_health: registry state read failed")
        return [], {}


def _boot_reconcile_actions() -> list:
    """Task 9's module-level boot-reconciliation actions, if the module has
    run this boot. Never raises."""
    try:
        import specialist_bundle_journal
        return list(specialist_bundle_journal.last_boot_reconcile_actions)
    except Exception:  # noqa: BLE001
        logger.exception("plugin_health: boot reconcile actions read failed")
        return []


def _atomic_write(path: Path, report: dict) -> None:
    from atomic_io import PRIVATE, atomic_write_text
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(Path(path),
                      json.dumps(report, indent=2, sort_keys=True) + "\n",
                      mode=PRIVATE)


def load_report(path: Path = HEALTH_PATH) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_report(*, issues: list, warnings: list,
                 path: Path = HEALTH_PATH,
                 registry_path=None) -> dict:
    """Regenerate the health report atomically. `notified_fingerprints` are
    carried forward from the previous report but pruned to fingerprints still
    present (a resolved issue clears its fingerprint). Returns the report.

    Task 11 (additive, no fingerprint impact — §3.10's fingerprint hashes only
    the first five PluginIssue fields via `fingerprint()`): the report also
    carries `quarantined_bundles` (the registry raw doc's ledger, Task 9) and
    `boot_reconcile_actions` (Task 9's `last_boot_reconcile_actions` module
    state); each owned entry's issue/warning row gains an `owner` field
    alongside its already-scoped `name`. `registry_path` defaults to
    `plugin_registry.REGISTRY_PATH` (production) — tests may override it."""
    quarantined_bundles, owners = _registry_state(registry_path)
    issue_dicts = [_issue_dict(i, owners) for i in issues]
    warning_dicts = [_issue_dict(w, owners) for w in warnings]
    current_fps = {d["fingerprint"] for d in issue_dicts}
    current_fps |= {d["fingerprint"] for d in warning_dicts}
    boot_actions = _boot_reconcile_actions()
    # #353: the previous report is read INSIDE the critical section — reading
    # it earlier lets a concurrent mark_notified land between read and write
    # and be silently erased by this regeneration.
    with _REPORT_LOCK:
        prev = load_report(path) or {}
        prev_notified = set(prev.get("notified_fingerprints") or [])
        report = {
            "schema_version": 1,
            "issues": issue_dicts,
            "warnings": warning_dicts,
            "notified_fingerprints": sorted(prev_notified & current_fps),
            "quarantined_bundles": quarantined_bundles,
            "boot_reconcile_actions": boot_actions,
        }
        _atomic_write(path, report)
    return report


def new_fingerprints(report: dict) -> list[str]:
    """Issue AND warning fingerprints not yet notified (order-preserving,
    deduped). Sol #17: warnings (e.g. ``legacy_provenance`` from an offline
    adopt — a real trust downgrade) are operator-relevant and must also fire the
    one-time DM, not merely land in the report."""
    notified = set(report.get("notified_fingerprints") or [])
    seen: set[str] = set()
    out: list[str] = []
    for d in list(report.get("issues", [])) + list(report.get("warnings", [])):
        fp = d.get("fingerprint")
        if fp and fp not in notified and fp not in seen:
            seen.add(fp)
            out.append(fp)
    return out


def mark_notified(fps: list[str], path: Path = HEALTH_PATH) -> None:
    with _REPORT_LOCK:
        report = load_report(path)
        if report is None:
            return
        notified = list(report.get("notified_fingerprints") or [])
        for fp in fps:
            if fp not in notified:
                notified.append(fp)
        report["notified_fingerprints"] = notified
        _atomic_write(path, report)


def first_contact_notice(role: str, path: Path = HEALTH_PATH) -> str | None:
    """One-line notice for the affected resident's first user-visible turn if
    the report holds a blocking issue targeting this role (or registry-wide,
    target=None); else None (§3.10)."""
    report = load_report(path)
    if not report:
        return None
    ok_targets = {f"resident:{role}", f"specialist:{role}", None}
    matched = [d for d in report.get("issues", [])
               if d.get("target") in ok_targets]
    if not matched:
        return None
    def _line(d) -> str:
        # D4 (v0.74.0): a stale binding is an INCOMPLETE UPDATE — the old
        # artifact stays live until reload. Never say "updating" or "will
        # refresh next use" (false for a cached persistent Agent).
        if d.get("reason_code") == "reload_required":
            where = d.get("target") or "a target"
            return (f"{d.get('name')}: {where} remains bound to the previous "
                    f"artifact (reload_required)")
        return f"{d.get('name')} ({d.get('reason_code')})"

    parts = [_line(d) for d in matched[:2]]
    body = ", ".join(parts)
    if len(matched) > 2:
        body += f" +{len(matched) - 2} more"
    if all(d.get("reason_code") == "reload_required" for d in matched):
        return (f"⚠️ Plugin update incomplete: {body} — an operator has "
                f"been notified.")
    return f"⚠️ Plugin degraded: {body} — an operator has been notified."
