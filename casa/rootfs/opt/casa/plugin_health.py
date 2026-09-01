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


def _normalize(report: dict) -> dict:
    """Coerce a parsed report to the shapes every consumer assumes.

    #559 batch (Sol design r3): the writer only ever emits well-formed rows, so
    anything else is external corruption of /data/plugin-health.json — but the
    consumers were reading it unguarded. A non-dict row, an unhashable `target`
    or an unhashable entry in `notified_fingerprints` each raised out of
    render_notice AND new_fingerprints; render_notice runs on a resident's turn
    via Agent._maybe_prepend_health_notice, which does not guard it, so the
    operator lost their REPLY, not merely the notice.

    FILTER, never reject: dropping the whole report over one bad row would
    discard a valid blocking issue sitting beside it, and that alert is the
    asset this module exists to protect."""
    def _rows(key: str) -> list:
        raw = report.get(key)
        if not isinstance(raw, list):
            return []
        out = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            # `target` is matched against a SET (render_notice) and `fingerprint`
            # is put INTO one — an unhashable value raises in both.
            if not isinstance(row.get("target"), (str, type(None))):
                continue
            if not isinstance(row.get("fingerprint"), (str, type(None))):
                continue
            out.append(row)
        return out

    raw_fps = report.get("notified_fingerprints")
    report["issues"] = _rows("issues")
    report["warnings"] = _rows("warnings")
    report["notified_fingerprints"] = (
        [fp for fp in raw_fps if isinstance(fp, str)]
        if isinstance(raw_fps, list) else [])
    return report


def load_report(path: Path = HEALTH_PATH) -> dict | None:
    """The report, or None when it is absent, unreadable, unparseable — or valid
    JSON that is not an object. Rows are normalized on read (see _normalize), so
    every consumer is downstream of one tolerance rule rather than each carrying
    its own."""
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    return _normalize(report)


def write_report(*, issues: list, warnings: list,
                 path: Path = HEALTH_PATH,
                 registry_path=None, prune: bool = True) -> dict:
    """Regenerate the health report atomically. `notified_fingerprints` are
    carried forward from the previous report but pruned to fingerprints still
    present (a resolved issue clears its fingerprint). Returns the report.

    #669: pruning is how this report says a row RESOLVED, so only a writer that
    could have OBSERVED the resolution may do it. `prune=False` carries the
    previous notified set forward unchanged instead. Exactly one caller sets it:
    `plugin_boot.main()`, whose report is built from `plugin_registry.
    resolve_all()` alone and therefore contains no runtime, trigger, callback,
    event or setup row — pruning against that partial set claimed every such row
    had resolved, erasing its mark, so step 13c's full regeneration re-announced
    unchanged standing problems on EVERY boot. Only fingerprints cross a partial
    write: the rows, warnings, ledger and generation always describe the new
    pass, and `tools._regenerate_plugin_health` stays the one authority that
    CLEARS a mark. A partial write never adds a mark either (`mark_notified` is
    the only adder), so a problem that is genuinely new at this boot is unmarked
    and is still announced.

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
            # #559: this function is the ONLY writer of rows, so its own
            # counter is what "the report the DM described" means. mark_notified
            # marks nothing across a bump — see its docstring for the silence
            # that buys.
            "generation": _next_generation(prev),
            "issues": issue_dicts,
            "warnings": warning_dicts,
            "notified_fingerprints": sorted(
                prev_notified & current_fps if prune else prev_notified),
            "quarantined_bundles": quarantined_bundles,
            "boot_reconcile_actions": boot_actions,
        }
        _atomic_write(path, report)
    return report


def _next_generation(prev: dict) -> int:
    """The successor of *prev*'s generation. A report written before this field
    existed, or one whose value was corrupted outside Casa, restarts at 1 —
    which is a value no in-flight notification can be holding, so the first
    mark after such a report is skipped and its DM simply re-fires."""
    raw = prev.get("generation")
    return raw + 1 if isinstance(raw, int) and not isinstance(raw, bool) else 1


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


def mark_notified(fps: list[str], path: Path = HEALTH_PATH, *,
                  generation: "int | None") -> None:
    """Record *fps* as announced to the operator.

    ``generation`` is the generation of the report the delivered message
    DESCRIBED — read from that very report, so ``None`` is the right value to
    pass for one written before the field existed. When it no longer matches,
    nothing is marked and the caller's message is simply re-sent on the next
    notification pass (#559).

    It is REQUIRED, and keyword-only, because there is no honest way to mark
    without it: naming rows means having read a report, and a caller allowed to
    omit the fence would silently opt out of the guarantee below. An optional
    parameter defaulting to ``None`` was the first shape and was wrong for a
    sharper reason (Sol/Terra diff r1) — it made "the caller passed nothing"
    indistinguishable from "the report carried no generation", so across the
    upgrade that introduced the field the fence disabled itself in exactly the
    window it was written for.

    Without that fence this function appended whatever it was handed, which is
    a lie whenever the report moved underneath the send: a row that RESOLVED
    while its DM was in flight had its fingerprint written back into a report
    that no longer contained it, and `write_report`'s prune
    (``prev_notified & current_fps``) then preserved that marker the moment the
    row recurred — so a genuine recurrence read as already announced and was
    suppressed on the DM surface, and (since the in-band notice now filters on
    the same field) on both. A bounded duplicate is the correct failure
    direction here; a silent one is not.

    Marking changes no rows, so it does NOT bump the generation."""
    with _REPORT_LOCK:
        report = load_report(path)
        if report is None:
            return
        if report.get("generation") != generation:
            logger.info(
                "plugin_health: notification mark skipped — report moved from "
                "generation %r to %r during delivery; it will be re-announced",
                generation, report.get("generation"))
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
    # #747: the registry-global row (name "*") the setup-episode store emits
    # while its history cannot be read or was reset after damage.
    "setup_history_unavailable": "setup history could not be read",
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
    the raw reason code: the operator cannot act on an internal identifier, and
    the tool that can now read this report for them (#555's plugin_status)
    renders through THIS function too, so a code has no reader anywhere."""
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


# #551 said both operator-facing surfaces "render through one translation", and
# describe_issue() was that translation — but the SENTENCE around it was written
# twice, and the two copies had already drifted: this one switches prefix for an
# all-reload_required set, while the DM in casa_core hardcoded the generic
# "Something needs attention" for every case, so an incomplete update was
# announced as a generic fault by DM and as an incomplete update in-band. One
# renderer, two callers, and the documented contract becomes true of the code.
#
# The LIMIT stays per-caller: the in-band line rides on top of a reply and must
# stay short, while the DM is a message of its own and naming five is useful
# there. (A shared limit was considered as part of the #559 duplicate-suppression
# work and rejected with it — see that issue: lowering the DM to 2 loses names
# the operator has no other way to see.)
_NOTICE_LIMIT = 2


def render_line(entries: list, limit: int = _NOTICE_LIMIT) -> str | None:
    """One operator-facing sentence for a set of report rows, or None for an
    empty set. `limit` rows are named; the rest become ", and N more"."""
    if not entries:
        return None
    body = ", ".join(describe_issue(d) for d in entries[:limit])
    if len(entries) > limit:
        body += f", and {len(entries) - limit} more"
    # D4 (v0.74.0): an all-stale-binding set is an INCOMPLETE UPDATE, and says so.
    if all(d.get("reason_code") == "reload_required" for d in entries):
        return f"⚠️ An update did not finish: {body}."
    return f"⚠️ Something needs attention: {body}."


def render_notice(role: str, path: Path = HEALTH_PATH) -> str | None:
    """The notice text for this role's blocking issues not already recorded as
    named by an operator DM, or None when there are none. Pure: no memo read or write
    (pending_notice applies suppression).

    #559 — the two operator-facing surfaces used to fire together: a plugin
    mutation that raised its first blocking issue showed the operator the same
    warning twice in one turn, once as the DM the tool awaits and once on the
    reply that follows it. They dedup on unconnected state (the DM on
    ``notified_fingerprints``, the notice on the rendered-line memo), and
    coordinating those two STORES was designed, attacked and cut: the surfaces
    select different row sets, so byte-matching the rendered lines almost never
    fires, and every clearing rule for a second store had a lifetime that did
    not match its condition.

    Filtering per ROW removes that whole class. There is no new state: the
    field read here is the one ``write_report`` already prunes to fingerprints
    still present, so the report's own resolution pruning IS the "already
    delivered" lifetime — nothing to clear, nothing to race, nothing to go
    stale. A DM that recorded ``beta`` while this role stands on
    ``alpha, beta`` leaves ``alpha`` to render here: the duplicate is gone and
    ``alpha`` is still said.

    What makes that safe is the narrowness of the mark, which is the DM side's
    obligation (INV-PLUG-013): a fingerprint is recorded only for a row a
    message actually NAMED, and only while the report it described is still
    current (see :func:`mark_notified` and ``casa_core.notify_plugin_health``).
    A send the channel REPORTED as undelivered, one with no deliverable channel
    at all, and a row left behind the DM's own "and N more" count each record
    nothing, so nothing here suppresses them. A send whose outcome is unknown
    does record — "cannot report" must not read as "failed" (#556), or a
    non-reporting channel would re-announce forever.

    The converse does not hold, and assuming it is the way to misread this
    function: an unrecorded row does not necessarily appear here. This selects
    blocking ``issues`` addressed to THIS role or to no target, and names at
    most :data:`_NOTICE_LIMIT` of them. A warning, an issue addressed to an
    ``executor:*``, and anything past that limit are therefore not named here
    under any condition — they stay unrecorded, so a later notification can
    name them, and ``plugin_status`` reports the whole standing set unfiltered
    at any time.

    ``target=None`` rows are operator-global: one DM naming such a row records
    it for every role, since the record is the fingerprint and not the role."""
    report = load_report(path)
    if not report:
        return None
    notified = set(report.get("notified_fingerprints") or [])
    ok_targets = {f"resident:{role}", f"specialist:{role}", None}
    return render_line([d for d in report.get("issues", [])
                        if d.get("target") in ok_targets
                        and d.get("fingerprint") not in notified])


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
