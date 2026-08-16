"""Resident webhook-trigger secrets: mint at registration, report the truth.

Before #609 the only mint for a RESIDENT webhook secret happened inside
request verification, on the read that decides whether a caller is authentic.
So a `static_header` trigger created through chat was reported live and
committed while its secret did not exist, and the only thing that could create
it was a call that could not succeed without it. The configurator's own recipe
told the operator to go and read a file that was not there.

This module is the other half: a WRITER that creates a slot when the trigger is
registered, and a READER that says what is actually on disk. Three properties
hold it together, and each exists because its absence was a defect:

* **Nothing here deletes or overwrites.** No `retire_*`, no
  `ensure_secret_for_identity` (which retires and re-mints whenever the
  `.ident` sidecar is absent — and for a resident it is ALWAYS absent), no
  `os.replace` onto a live slot. The worst outcome in this area is destroying
  an operator-provisioned credential Casa cannot regenerate and has no import
  surface for, so the writer only ever CREATES a file that is not there.
* **Only `absent` may be minted into.** `webhook_auth.read_secret` collapses
  absent, unreadable, non-regular, symlinked and present-but-invalid to
  ``None``; minting on that would re-enter `_publish` for a slot that already
  exists, raising on a read-only directory on every pass, forever. The writer
  asks `probe_secret`, which discriminates them.
* **The reader's authority is the REGISTRY, not the declaration.** What a
  request is verified with comes from `trigger_registry`; what the file says
  is a different question, and the two do diverge. A report derived from the
  declaration states something the request path does not do.

The writer never raises and never changes routing: a filesystem fault must not
decide which route serves. A trigger whose mint failed stays registered and
returns 401 — 404 and 401 both fail closed, and unrouting would let a full disk
silently re-shape the surface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Sequence

import log_redact
import webhook_auth

logger = logging.getLogger(__name__)

# The per-trigger values a REQUEST actually reads out of the registry. A
# divergence in any of them changes what the route does, so a report that
# compares fewer reads clean while the route misbehaves. Measured against
# every ``trigger_registry.`` read on the request path in casa_core.
_EFFECTIVE_KEYS = ("mode", "header", "tolerance_secs", "secret_owner")


def slot_for(spec: Any) -> tuple[str, str] | None:
    """``(name, owner)`` iff *spec* is backed by a per-trigger secret file.

    ``type`` is read BEFORE ``auth`` so a non-webhook spec short-circuits:
    interval, cron and date triggers carry no auth block at all.
    """
    if getattr(spec, "type", None) != "webhook":
        return None
    auth = getattr(spec, "auth", None) or {}
    if auth.get("mode") not in webhook_auth.PER_TRIGGER_SECRET_MODES:
        return None
    return spec.name, auth.get("secret_owner", "casa")


def mint_for_specs(specs: Iterable[Any], *, secrets_dir: Path) -> list[tuple[str, str]]:
    """Create the missing casa-owned slots. Returns ``[(name, reason)]``.

    NEVER raises: every call site either runs at boot, where an exception
    escapes `main()` and s6's `finish` then STOPS the app rather than
    restarting it, or inside a reload, where a filesystem fault must not
    abort a pass that has already applied other work.

    A failure is recorded ONLY for a RAISED exception. `ensure_secret`
    returning ``None`` without raising is the owner-mismatch stuck state — a
    report row, not a failure. Treating it as one made `casa_reload` return an
    error for that role on every subsequent call, forever, and the only exit
    was host filesystem access the operator does not have.

    Provider-owned slots are never touched: Casa does not mint them, and the
    operator's value may already be there.
    """
    failures: list[tuple[str, str]] = []
    for spec in specs:
        slot = slot_for(spec)
        if slot is None:
            continue
        name, owner = slot
        if owner != "casa":
            continue
        state, detail = webhook_auth.probe_secret(
            name, owner="casa", secrets_dir=secrets_dir)
        if state != "absent":
            # readable: already provisioned. invalid/unreadable: something is
            # there that is not ours to replace — reported, never overwritten.
            continue
        try:
            value = webhook_auth.ensure_secret(
                name, owner="casa", secrets_dir=secrets_dir)
        except Exception as exc:  # noqa: BLE001 — reported, never propagated
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            logger.warning(
                "webhook secret mint failed for trigger %r: %s", name, exc)
            continue
        if value is None:
            continue
        # The lazy path registered every minted value with the log redactor
        # (casa_core's `_secret_for`); minting earlier must not lose that, or
        # the value is unredacted in this process until its first request —
        # and forever for a trigger that is never called.
        try:
            log_redact.register_secret(value.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — redaction is best-effort
            pass
    return failures


def _declared_effective(spec: Any) -> dict[str, Any]:
    auth = getattr(spec, "auth", None) or {}
    return {
        "mode": auth.get("mode", "hmac_body"),
        "header": auth.get("header"),
        "tolerance_secs": auth.get("tolerance_secs"),
        "secret_owner": auth.get("secret_owner", "casa"),
        "clearance": getattr(spec, "clearance", "public") or "public",
        "role": None,  # filled by the caller, which knows the role
    }


def snapshot_rows(
    *, specs: Sequence[Any], registry: Any, role: str, global_secret_usable: bool,
) -> list[dict[str, Any]]:
    """Every row that can be decided WITHOUT touching the filesystem.

    Synchronous and allocation-only by design: it is taken under the reload
    lock in one stretch with no ``await``, so `registered` and the rows
    describe ONE state of the world. Rows that still need a probe carry
    ``_probe_owner``; `resolve_rows` finishes them off the event loop.

    The row SET is the UNION of what the role declares and what the registry
    routes for it. Taking it from the declaration alone hides the dangerous
    half: a webhook the declaration no longer names but the registry still
    routes serves 200 and appears nowhere.
    """
    declared = {s.name: s for s in specs}
    routed = set(registry.webhook_names_for(role)) if registry is not None else set()
    rows: list[dict[str, Any]] = []
    for name in sorted(set(declared) | routed):
        spec = declared.get(name)
        if spec is None:
            rows.append({
                "name": name, "state": "routed_undeclared",
                "detail": ("this role routes the name but no longer declares it; "
                           "requests still reach it"),
            })
            continue
        if getattr(spec, "type", None) != "webhook":
            rows.append({"name": name, "state": "not_applicable"})
            continue
        if name not in routed:
            # Not routed BY THIS ROLE — but the secrets directory is keyed by
            # NAME alone, with no role namespacing, and so is the registry. So
            # ask globally: a name another role serves is not "unregistered"
            # (which says requests 404), it is a live route this role's
            # declaration also claims, sharing the one secret file.
            elsewhere = (registry.get_webhook_target(name)
                         if registry is not None else None)
            if elsewhere is None:
                rows.append({
                    "name": name, "state": "unregistered",
                    "detail": "declared but not routed; requests 404 until a reload",
                })
                continue
            declared_side = _declared_effective(spec)
            declared_side["role"] = role
            effective = {k: None for k in _EFFECTIVE_KEYS}
            effective.update({
                k: (registry.get_auth_policy(name) or {}).get(k)
                for k in _EFFECTIVE_KEYS})
            effective["clearance"] = registry.get_clearance(name)
            effective["role"] = elsewhere
            rows.append({
                "name": name, "state": "misrouted",
                "effective": effective, "declared": declared_side,
                "detail": (f"this role declares the name but {elsewhere!r} routes "
                           f"it; both would share the one secret file"),
                "_probe_owner": effective.get("secret_owner") or "casa",
            })
            continue
        policy = (registry.get_auth_policy(name) or {}) if registry is not None else {}
        effective = {k: policy.get(k) for k in _EFFECTIVE_KEYS}
        effective["clearance"] = registry.get_clearance(name)
        effective["role"] = registry.get_webhook_target(name)
        declared_side = _declared_effective(spec)
        declared_side["role"] = role
        if effective != declared_side:
            rows.append({
                "name": name, "state": "misrouted",
                "effective": effective, "declared": declared_side,
                "detail": ("what a request is verified with differs from what "
                           "the declaration says"),
                # The file's own condition is still reported, so a routing
                # divergence never hides a broken slot underneath it.
                "_probe_owner": effective.get("secret_owner") or "casa",
            })
            continue
        mode = effective.get("mode")
        if mode not in webhook_auth.PER_TRIGGER_SECRET_MODES:
            # hmac_body rides the ONE global secret, which is blank when the
            # option is an unresolved op:// reference or generation failed —
            # in which case every request to this route 401s permanently.
            rows.append({"name": name, "state": "global_secret"} if global_secret_usable
                        else {"name": name, "state": "global_secret_absent",
                              "detail": ("the global webhook secret is unset or "
                                         "unresolved; every request 401s until it "
                                         "is set and Casa is restarted")})
            continue
        rows.append({
            "name": name, "state": None, "owner": effective.get("secret_owner") or "casa",
            "_probe_owner": effective.get("secret_owner") or "casa",
        })
    return rows


def resolve_rows(rows: list[dict[str, Any]], *, secrets_dir: Path) -> list[dict[str, Any]]:
    """Finish the rows that need a filesystem read. Runs off the event loop.

    Probes under the EFFECTIVE owner — what the request path would use — not
    the declared one. A casa-minted token is a valid provider value, so a
    declaration flipped from provider to casa carries the live credential over
    and a declaration-derived probe would describe a different file than the
    one that authenticates.
    """
    finished: list[dict[str, Any]] = []
    for row in rows:
        owner = row.pop("_probe_owner", None)
        if owner is None:
            finished.append(row)
            continue
        state, detail = webhook_auth.probe_secret(
            row["name"], owner=owner, secrets_dir=secrets_dir)
        if row.get("state") == "misrouted":
            row["probe"] = {"state": state, "detail": detail}
            finished.append(row)
            continue
        row["owner"] = owner
        if state == "readable":
            row["state"], row["detail"] = "readable", detail
        elif state == "absent" and owner == "provider":
            row["state"] = "awaiting_import"
            row["detail"] = ("no Casa surface can place a provider secret today "
                             "(#621); this trigger cannot authenticate until one "
                             "exists")
        elif state == "absent":
            row["state"] = "missing"
            row["detail"] = "no file; the next reload or restart mints it"
        else:
            row["state"] = state
            row["detail"] = (
                f"{detail} — this file cannot be repaired or removed through any "
                f"Casa surface; use a different trigger name")
        finished.append(row)
    return finished


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Counts per state. Deliberately NOT a boolean: a rollup has to decide
    what "ready" means for `awaiting_import` and `global_secret`, and every
    answer to that was wrong in a different direction."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return dict(sorted(counts.items()))
