"""Plugin health — durable report + operator notification (spec §3.10).

Every boot and every mutation regenerates /data/plugin-health.json. Issue
"fingerprints" hash the STRUCTURED fields (name, target, stage, reason_code,
artifact_id) — never free-form reason text — so a wording change never
re-alerts. A post-boot Telegram DM fires (via the deterministic bus, like
notify_config_sync) when the report contains NEW fingerprints; an issue
disappearing from the report clears its fingerprint. While the report holds
unresolved blocking issues, the affected resident prepends a one-line notice
to a user-visible turn (pending_notice).

Both operator-facing surfaces — the in-band notice and the DM — render through
describe_issue(), which translates a reason code into what the operator can DO
about it. Reason codes are an OPEN namespace (every plugin feature mints more,
and verify passes its own `reasons[0]` straight through), so the translation
classifies by suffix family with a small override table and a safe fallback,
rather than an exhaustive map that would silently leak every new code. The raw
code stays in the report and in the log; only the operator's line is translated.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
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
    # #533: bounded elaboration (e.g. unresolved var NAMES) — additive,
    # never part of the fingerprint (computed above from the five
    # structured fields only), present only when the issue carries one.
    detail = _get("detail")
    if detail:
        d["detail"] = detail
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


# #551: what the operator can DO about a reason code. Codes that need their own
# words get an entry here; everything else is classified by suffix below. A code
# absent from both is not a bug — it renders the fallback and is logged.
_REASON_PHRASES = {
    "setup_env_unprovisioned": "still needs a value from you",
    "env_unresolved": "is missing a setting it needs",
    "setup_episode_pending": "has a setup step still to finish",
    "setup_episode_failed": "could not finish setting up",
    "setup_episode_stale": "started setting up and stopped partway",
    "setup_episode_refused": "was not allowed to finish setting up",
    "target_pending": "is waiting for the specialist it belongs to",
    "corrupt_artifact": "could not be loaded",
    "artifact_missing": "could not be found",
    "unsafe_archive": "could not be loaded safely",
    "registry_invalid": "could not be read",
    "verify_exception": "could not be checked",
    "postcondition_failed": "did not finish starting up",
    "snapshot_raced": "hit an error while starting up",
    "legacy_provenance": "was installed without the usual provenance checks",
    "duplicate_name": "clashes with another plugin's name",
    "name_mismatch": "does not match the name it was installed under",
}

# Suffix families close the OPEN code namespace by construction: a code minted
# tomorrow that ends in one of these renders correctly without an edit here.
# Ordered — the first matching suffix wins.
_REASON_SUFFIXES = (
    ("_pending_ack", "is waiting for your approval"),
    ("_unprovisioned", "still needs a value from you"),
    ("_unresolved", "is missing a setting it needs"),
    ("_invalid", "could not be loaded"),
    ("_missing", "could not be loaded"),
    ("_collision", "clashes with something already installed"),
    ("_unavailable", "could not be reached"),
    ("_rejected", "was refused"),
    ("_error", "hit an error"),
    ("_failed", "hit an error"),
)

_REASON_FALLBACK = "is not working"


def describe_issue(d: dict) -> str:
    """One operator-facing clause for a report row (§3.10, #551). Never emits
    the raw reason code: the operator cannot act on an internal identifier and
    has no way to look one up (there is no health-read tool for any role)."""
    name = d.get("name") or "a plugin"
    code = str(d.get("reason_code") or "")
    # D4 (v0.74.0): a stale binding is an INCOMPLETE UPDATE — the old artifact
    # stays live until reload. Never say "updating" or "will refresh next use"
    # (false for a cached persistent Agent).
    if code == "reload_required":
        return f"{name} is still running its previous version"
    phrase = _REASON_PHRASES.get(code)
    if phrase is None:
        for suffix, candidate in _REASON_SUFFIXES:
            if code.endswith(suffix):
                phrase = candidate
                break
    if phrase is None:
        phrase = _REASON_FALLBACK
        logger.debug("plugin_health: no operator phrasing for reason_code %r",
                     code)
    # #554: the detail (an unresolved variable name, a setup episode's
    # last_error) is the one actionable fact in the row, and nothing else
    # carries it to the operator.
    if d.get("detail"):
        return f"{name} {phrase} — {d['detail']}"
    return f"{name} {phrase}"


# #551: the notice is re-derived on every eligible turn, so the same line would
# otherwise be prepended once per Agent reconstruction — and a plugin mutation
# reconstructs the Agent (reload._construct_agent), which is why a multi-step
# setup showed it three or four times unchanged.
#
# The memo records what was PUT IN FRONT of a role, never that delivery
# succeeded: no channel reports that truthfully (see #556), so a memo meaning
# "delivered" would suppress a line the operator never saw. And it DECAYS, so
# the worst case of any missed delivery is one quiet window rather than silence
# for the life of the process. Keyed on the rendered TEXT, so a changed detail,
# a changed "+N more" count or a different plugin all render immediately.
_NOTICE_COOLDOWN_S = 3600.0
_notice_memo: dict[str, tuple[str, float]] = {}
_MEMO_LOCK = threading.Lock()


def render_notice(role: str, path: Path = HEALTH_PATH) -> str | None:
    """The notice text for this role's blocking issues, or None when there are
    none. Pure: no memo read or write (pending_notice applies suppression)."""
    report = load_report(path)
    if not report:
        return None
    ok_targets = {f"resident:{role}", f"specialist:{role}", None}
    matched = [d for d in report.get("issues", [])
               if d.get("target") in ok_targets]
    if not matched:
        return None
    body = ", ".join(describe_issue(d) for d in matched[:2])
    if len(matched) > 2:
        body += f", and {len(matched) - 2} more"
    if all(d.get("reason_code") == "reload_required" for d in matched):
        return f"⚠️ An update did not finish: {body}."
    return f"⚠️ Something needs attention: {body}."


def forget_notice(role: str, text: str) -> None:
    """Drop *role*'s memo when it still holds exactly *text*, so the notice is
    offered again on the very next turn instead of waiting out the cooldown.

    Called when delivery RAISED. A raise is a reliable negative signal even
    though a normal return is not a reliable positive one (#556), so acting on
    it costs nothing and preserves #349's guarantee that a transient send
    failure never swallows the notice."""
    with _MEMO_LOCK:
        previous = _notice_memo.get(role)
        if previous is not None and previous[0] == text:
            del _notice_memo[role]


def pending_notice(role: str, path: Path = HEALTH_PATH) -> str | None:
    """render_notice(), suppressed when the byte-identical line was already put
    in front of this role less than _NOTICE_COOLDOWN_S ago. A role whose issues
    have all resolved drops its memo, so a recurrence re-announces at once."""
    text = render_notice(role, path)
    now = time.monotonic()
    with _MEMO_LOCK:
        if text is None:
            _notice_memo.pop(role, None)
            return None
        previous = _notice_memo.get(role)
        if (previous is not None and previous[0] == text
                and now - previous[1] < _NOTICE_COOLDOWN_S):
            return None
        _notice_memo[role] = (text, now)
    return text
