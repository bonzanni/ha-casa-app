"""Live invariant auditor (sweep type #6) — READ-ONLY.

Runs INSIDE the casa container (docker exec … python3 live_invariant_audit.py)
and checks ground-truth invariants on live state, rather than driving turns.
Catches config/capability regressions and malformed on-disk state — the class no single-turn probe sees.
Never mutates. Prints a JSON report + a PASS/WARN/FAIL summary; exit code is the
number of FAIL-level invariants (0 = clean) so it can gate a cron.

Invariants:
  A config_sync   — /data/config-sync-report.json has empty post_sync_errors.
  B delegates↔tool — every agent with a non-empty delegates.yaml grants the
                     delegate MCP tool in runtime.yaml (boot-fatal if not).
  C schema-valid   — validate_config_repo(/config) returns no errors
                     (env-substituted, so model placeholders resolve).
  D sessions.json  — parses; no 'active' entry missing sdk_session_id.
  E engagements    — engagements.json parses; no active/idle entry missing topic_id.
"""
from __future__ import annotations

import json
import os
import sys
import time

CONFIG_DIR = os.environ.get("CASA_CONFIG_DIR", "/config")
DATA_DIR = os.environ.get("CASA_DATA_DIR", "/data")

report: dict = {"pass": [], "warn": [], "fail": []}


def _ok(inv, msg): report["pass"].append(f"{inv}: {msg}")
def _warn(inv, msg): report["warn"].append(f"{inv}: {msg}")
def _fail(inv, msg): report["fail"].append(f"{inv}: {msg}")


def _read_json(path):
    with open(path) as fh:
        return json.load(fh)


def check_config_sync():
    p = os.path.join(DATA_DIR, "config-sync-report.json")
    if not os.path.exists(p):
        _warn("A/config_sync", f"{p} absent (no reconcile yet?)")
        return
    try:
        r = _read_json(p)
    except Exception as e:
        _fail("A/config_sync", f"unreadable: {e}")
        return
    errs = r.get("post_sync_errors") or []
    conflicts = r.get("conflicts") or []
    if errs:
        _fail("A/config_sync", f"post_sync_errors={errs}")
    elif conflicts:
        _warn("A/config_sync", f"conflicts={conflicts}")
    else:
        _ok("A/config_sync", f"clean (image {r.get('image_version')})")


def check_delegates_tool():
    agents_root = os.path.join(CONFIG_DIR, "agents")
    bad = []
    for root, _dirs, files in os.walk(agents_root):
        if "delegates.yaml" not in files or "runtime.yaml" not in files:
            continue
        import yaml
        deleg = yaml.safe_load(open(os.path.join(root, "delegates.yaml"))) or {}
        entries = deleg.get("delegates") or deleg if isinstance(deleg, list) else deleg.get("delegates", [])
        if not entries:
            continue
        rt = yaml.safe_load(open(os.path.join(root, "runtime.yaml"))) or {}
        allowed = (rt.get("tools") or {}).get("allowed") or []
        if "mcp__casa-framework__delegate_to_agent" not in allowed:
            bad.append(os.path.basename(root))
    if bad:
        _fail("B/delegates", f"non-empty delegates but no delegate tool: {bad}")
    else:
        _ok("B/delegates", "all delegates.yaml backed by the delegate tool")


def _ensure_model_env():
    """validate_config_repo resolves ${PRIMARY_AGENT_MODEL}/${VOICE_AGENT_MODEL}
    in runtime.yaml and rejects unresolved placeholders (the D1 fragility). Boot
    exports these via bashio; a docker-exec caller does not — so mirror boot by
    reading the (non-secret) model keys from options.json. Without this the
    auditor's C check false-positives exactly as config_sync did pre-v0.59.3."""
    for env_key, opt_key in (("PRIMARY_AGENT_MODEL", "primary_agent_model"),
                             ("VOICE_AGENT_MODEL", "voice_agent_model")):
        if os.environ.get(env_key):
            continue
        try:
            with open(os.path.join(DATA_DIR, "options.json")) as fh:
                opts = json.load(fh)
            val = opts.get(opt_key)
            if val:
                os.environ[env_key] = str(val)
        except Exception:
            pass  # leave unset; C will report honestly if truly unresolved


def check_schema_valid():
    _ensure_model_env()
    sys.path.insert(0, "/opt/casa")
    try:
        from agent_loader import validate_config_repo
    except ImportError as e:
        # Not in the casa container (or code moved) — an environment issue, not
        # a prod invariant failure. WARN so a misuse-run can't look like a FAIL.
        _warn("C/schema", f"validator unavailable in this environment: {e}")
        return
    try:
        errs = validate_config_repo(CONFIG_DIR)
    except Exception as e:
        _fail("C/schema", f"validator raised: {e}")
        return
    if errs:
        _fail("C/schema", f"{len(errs)} error(s): {errs[:3]}")
    else:
        _ok("C/schema", "/config validates")


def check_sessions():
    p = os.path.join(DATA_DIR, "sessions.json")
    if not os.path.exists(p):
        _ok("D/sessions", "no sessions.json (empty)")
        return
    try:
        data = _read_json(p)
    except Exception as e:
        _fail("D/sessions", f"unparseable: {e}")
        return
    orphan = [k for k, v in (data or {}).items()
              if isinstance(v, dict) and v.get("status") == "active"
              and not v.get("sdk_session_id")]
    if orphan:
        _warn("D/sessions", f"active entries missing sdk_session_id: {orphan}")
    else:
        _ok("D/sessions", f"{len(data or {})} entries, well-formed")


def check_engagements():
    p = os.path.join(DATA_DIR, "engagements.json")
    if not os.path.exists(p):
        _ok("E/engagements", "no engagements.json (empty)")
        return
    try:
        data = _read_json(p)
    except Exception as e:
        _fail("E/engagements", f"unparseable: {e}")
        return
    # Malformed-but-parseable JSON must record a FAIL, not crash the auditor
    # (codex review, v0.69.6): a non-list/dict top level, non-dict rows, or a
    # non-numeric last_user_turn_ts would otherwise raise past this function.
    if isinstance(data, list):
        recs = data
    elif isinstance(data, dict):
        recs = data.get("engagements", [])
    else:
        _fail("E/engagements", f"top-level is {type(data).__name__}, not list/object")
        return
    if not isinstance(recs, list) or any(not isinstance(r, dict) for r in recs):
        _fail("E/engagements", "records is not a list of objects")
        return
    bad = [str(r.get("id", "?"))[:8] for r in recs
           if r.get("status") in ("active", "idle") and not r.get("topic_id")]
    # D-4 (v0.69.0): the daily sweep auto-reaps active/idle records past
    # ENGAGEMENT_REAP_DAYS (default 7). Anything older than TTL + 2d grace
    # means the reap is not running — the block-M stale-engagement gap.
    try:
        ttl_days = float(os.environ.get("ENGAGEMENT_REAP_DAYS", "") or 7)
    except ValueError:
        ttl_days = 7.0

    def _last_turn(r):
        try:
            return float(r.get("last_user_turn_ts") or 0)
        except (TypeError, ValueError):
            return 0.0  # unparseable ts → treat as epoch (won't hide a stale one)

    stale = []
    if ttl_days > 0:
        cutoff = time.time() - (ttl_days + 2) * 86400
        stale = [str(r.get("id", "?"))[:8] for r in recs
                 if r.get("status") in ("active", "idle")
                 and _last_turn(r) < cutoff]
    if bad:
        _fail("E/engagements", f"active/idle without topic_id: {bad}")
    elif stale:
        _fail("E/engagements",
              f"active/idle older than reap TTL+2d — reap not running? {stale}")
    else:
        _ok("E/engagements", f"{len(recs)} records, well-formed")


def main():
    for fn in (check_config_sync, check_delegates_tool, check_schema_valid,
               check_sessions, check_engagements):
        try:
            fn()
        except Exception as e:  # an auditor bug must not look like a prod fail
            _warn(fn.__name__, f"auditor error: {e}")
    print(json.dumps(report, indent=2))
    print(f"\nSUMMARY: {len(report['pass'])} pass, "
          f"{len(report['warn'])} warn, {len(report['fail'])} fail")
    sys.exit(len(report["fail"]))


if __name__ == "__main__":
    main()
