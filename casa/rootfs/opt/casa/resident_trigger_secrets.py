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

* **The MINT never deletes or overwrites.** No `ensure_secret_for_identity`
  (which retires and re-mints whenever the `.ident` sidecar is absent — and for
  a resident it is ALWAYS absent), no `os.replace` onto a live slot. The worst
  outcome in this area is destroying an operator-provisioned credential Casa
  cannot regenerate and has no import surface for, so the writer only ever
  CREATES a file that is not there. Retirement (#620) is a SEPARATE operation
  with one authority: a receipt Casa itself wrote, naming the role it minted
  for, that still certifies the live bytes. Nothing else is ever unlinked.
* **Only `absent` may be minted into.** `webhook_auth.read_secret` collapses
  absent, unreadable, non-regular, symlinked and present-but-invalid to
  ``None``; minting on that would re-enter `_publish` for a slot that already
  exists, raising on a read-only directory on every pass, forever. The writer
  asks `probe_secret`, which discriminates them.
* **The reader's authority is the REGISTRY, not the declaration.** What a
  request is verified with comes from `trigger_registry`; what the file says
  is a different question, and the two do diverge. A report derived from the
  declaration states something the request path does not do.
* **A mint records that Casa generated those exact bytes, for which role**
  (#620). The receipt sidecar is written BEFORE the value is linked and only
  for a slot the call itself creates, never onto a value it merely found — a
  43-byte operator credential is indistinguishable from a Casa token, so
  backfilling would certify precisely the value that must never be destroyed.
* **A Casa-minted secret lives exactly as long as its route** (#620,
  INV-TRIG-016). `retire_for_role` retires a certified slot when the role its
  receipt names no longer backs the name with a per-trigger secret, or when
  another role is about to route the name; boot retires the certified slots of
  a role whose directory is gone. A slot Casa cannot prove it minted is never
  destroyed — the request path refuses it (`webhook_auth.read_certified_secret`)
  and the report says so (`unproven_blocked`). The removal event is always a
  successful route registration or a restart, never a declaration write, never
  a teardown, never an absence sweep.

The writer never raises and never changes routing: a filesystem fault must not
decide which route serves. A trigger whose mint failed stays registered and
returns 401 — 404 and 401 both fail closed, and unrouting would let a full disk
silently re-shape the surface.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple, Sequence

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


def slot_names(specs: Iterable[Any]) -> set[str]:
    """The names *specs* backs with a per-trigger secret, ANY owner."""
    return {slot_for(s)[0] for s in specs if slot_for(s) is not None}


def webhook_names(specs: Iterable[Any]) -> set[str]:
    """Every webhook-type name in *specs*, whatever its mode or owner — the set
    a registration is about to route (arm (b)'s input)."""
    return {s.name for s in specs if getattr(s, "type", None) == "webhook"}


class RetireOutcome(NamedTuple):
    retired: list[str]
    failed: list[str]


class InventoryUnavailable(OSError):
    """The secrets directory exists but could not be enumerated. An EMPTY
    inventory and an UNREADABLE one are different answers, and the caller must
    know which (seam S22): the reload aborts its registration, boot skips the
    directory-gone sweep with a warning."""


def _inventory(secrets_dir: Path) -> list[str]:
    """Receipt-bearing resident names — the ONLY candidates a retirement ever
    considers. A directory that does not exist is an empty inventory (a fresh
    install has none until its first mint, seam S35); ENOENT is the ONLY such
    answer. A regular file at the path (ENOTDIR) is an existing inventory that
    cannot be enumerated — the mint would skip every slot as `unreadable` and
    publish routes that 401 forever — so it is refused like any other fault.
    """
    try:
        entries = os.listdir(secrets_dir)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise InventoryUnavailable(f"secrets inventory unavailable: {exc}") from exc
    suffix = webhook_auth.MINT_RECEIPT_SUFFIX
    return sorted(n[: -len(suffix)] for n in entries
                  if n.endswith(suffix) and not n.startswith("plg-"))


def _retire_one(name: str, *, secrets_dir: Path, out: RetireOutcome) -> None:
    remaining = webhook_auth.retire_secret(name, secrets_dir=secrets_dir)
    (out.failed if remaining else out.retired).append(name)
    if remaining:
        logger.warning(
            "webhook secret retirement of %r left %s in place; the registration "
            "that asked for it is refused", name, remaining)


def retire_for_role(role: str, *, declared: Iterable[Any], staged: Iterable[str],
                    secrets_dir: Path) -> RetireOutcome:
    """Retire, for a role about to (re)register *declared*, the slots its
    receipts no longer entitle. Both arms, in this order:

    (a) every slot whose receipt certifies the live bytes for **this** role and
        whose name *declared* no longer backs with a per-trigger secret
        (deleted, renamed, type changed, or mode set to ``hmac_body``) — run to
        completion FIRST; any refusal or unreadable artifact ends the call
        here, with arm (b) not started (seam S24);
    (b) for every name in *staged* — the webhook names the caller is about to
        route, any owner or mode — a slot whose receipt certifies the live bytes
        for **another** role. The caller has established that no other role
        currently routes the name (the collision check precedes this), so that
        slot is an orphan of a registration that is gone, and Casa's own token.

    Only a certified receipt is authority. `unproven` (no receipt, malformed,
    v1, stale digest) is skipped and never touched; an artifact that exists but
    cannot be read NOW is a refusal (`failed`), never silently uncertified
    (seam S25). Raises :class:`InventoryUnavailable` when the directory cannot
    be enumerated. Never unlinks anything else.
    """
    secrets_dir = Path(secrets_dir)
    out = RetireOutcome([], [])
    keep = slot_names(declared)
    for name in _inventory(secrets_dir):
        got = webhook_auth._certified_read(name, secrets_dir)
        if got is webhook_auth.UNAVAILABLE:
            out.failed.append(name)
            logger.warning("webhook secret %r could not be read; retirement refused", name)
            continue
        if not isinstance(got, webhook_auth.Certified) or got.role != role:
            continue
        if name in keep:
            continue
        _retire_one(name, secrets_dir=secrets_dir, out=out)
    if out.failed:
        return out
    for name in sorted(set(staged)):
        if name.startswith("plg-"):
            continue
        got = webhook_auth._certified_read(name, secrets_dir)
        if got is webhook_auth.UNAVAILABLE:
            out.failed.append(name)
            logger.warning("webhook secret %r could not be read; retirement refused", name)
            continue
        if not isinstance(got, webhook_auth.Certified) or got.role == role:
            continue
        _retire_one(name, secrets_dir=secrets_dir, out=out)
    return out


def retire_for_roles_without_directory(
    *, secrets_dir: Path, role_dir_exists: Callable[[str], bool],
) -> RetireOutcome:
    """Boot's directory-gone sweep: retire every certified slot whose receipt
    names a role for which *role_dir_exists* is False. The predicate is the
    caller's (it knows the layout); any exception from it reads as "exists".
    An unreadable artifact is a `failed` entry (reported, left in place); an
    unreadable inventory raises :class:`InventoryUnavailable`. Nothing here
    decides from a declaration — only from Casa's own receipts and the
    existence of the role's directory.
    """
    secrets_dir = Path(secrets_dir)
    out = RetireOutcome([], [])
    for name in _inventory(secrets_dir):
        got = webhook_auth._certified_read(name, secrets_dir)
        if got is webhook_auth.UNAVAILABLE:
            out.failed.append(name)
            logger.warning("webhook secret %r could not be read at boot; left in place", name)
            continue
        if not isinstance(got, webhook_auth.Certified):
            continue
        try:
            present = bool(role_dir_exists(got.role))
        except Exception:  # noqa: BLE001 — a probe that fails reads as present
            present = True
        if present:
            continue
        _retire_one(name, secrets_dir=secrets_dir, out=out)
    return out


def mint_for_specs(specs: Iterable[Any], *, secrets_dir: Path, role: str) -> list[tuple[str, str]]:
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

    Mints through `webhook_auth.mint_resident_secret` rather than
    `ensure_secret`: the resident primitive records a value-bound, role-bound
    mint receipt (#620) BEFORE it links the value, for *role*, and only for a
    slot this call actually creates. A receipt that cannot be written is a
    raised, reported mint failure — the route 401s until the next pass.
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
            value = webhook_auth.mint_resident_secret(
                name, secrets_dir=secrets_dir, role=role)
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
    # ONE route record per name (seam S6): target, policy and clearance read
    # through three getters can straddle a concurrent re-registration and
    # describe a route no request ever saw. `webhook_route` is the same
    # atomic record the wildcard handler reads.
    routes: dict[str, dict | None] = {}

    def _route(n: str) -> dict | None:
        if n not in routes:
            routes[n] = registry.webhook_route(n) if registry is not None else None
        return routes[n]

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
            record = _route(name)
            if record is None:
                rows.append({
                    "name": name, "state": "unregistered",
                    "detail": "declared but not routed; requests 404 until a reload",
                })
                continue
            elsewhere = record["role"]
            declared_side = _declared_effective(spec)
            declared_side["role"] = role
            effective = {k: (record.get("auth") or {}).get(k) for k in _EFFECTIVE_KEYS}
            effective["clearance"] = record["clearance"]
            effective["role"] = elsewhere
            rows.append({
                "name": name, "state": "misrouted",
                "effective": effective, "declared": declared_side,
                "detail": (f"this role declares the name but {elsewhere!r} routes "
                           f"it; both would share the one secret file"),
                "_probe_owner": effective.get("secret_owner") or "casa",
                "_probe_role": elsewhere,
                "_probe_resident": bool(record.get("resident", True)),
            })
            continue
        record = _route(name) or {}
        policy = record.get("auth") or {}
        effective = {k: policy.get(k) for k in _EFFECTIVE_KEYS}
        effective["clearance"] = record.get("clearance")
        effective["role"] = record.get("role")
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
                "_probe_role": effective.get("role"),
                "_probe_resident": bool(record.get("resident", True)),
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
            "_probe_role": effective.get("role"),
            "_probe_resident": bool(record.get("resident", True)),
        })
    return rows


_BLOCKED_DETAIL = ("bytes are present that Casa cannot prove it minted for the role that "
                   "routes this name; requests are refused; delete the file on the host "
                   "or use a different trigger name")


def resolve_rows(rows: list[dict[str, Any]], *, secrets_dir: Path) -> list[dict[str, Any]]:
    """Finish the rows that need a filesystem read. Runs off the event loop.

    Probes under the EFFECTIVE owner — what the request path would use — not
    the declared one. A casa-minted token is a valid provider value, so a
    declaration flipped from provider to casa carries the live credential over
    and a declaration-derived probe would describe a different file than the
    one that authenticates.

    A `readable` row also carries `provenance` (#620): whether Casa can prove
    it minted the bytes that actually authenticate. `readable` alone cannot
    say that — the owner rules accept a casa token as a provider value — so
    the row could not distinguish the operator's own credential from one a
    deleted trigger left behind. The FACT is emitted and the reading is
    owner-relative, which is why it is not folded into `detail`: under
    `casa`, `unproven` is the notable answer; under `provider`,
    `casa_minted` is (the route authenticates with a Casa token, not the
    operator's credential). `misrouted` rows carry it inside their nested
    `probe`, where the rest of that row's file condition already lives —
    cross-role adoption surfaces as `misrouted`, so dropping it there would
    drop it from the row that most needs it.
    """
    finished: list[dict[str, Any]] = []
    for row in rows:
        owner = row.pop("_probe_owner", None)
        probe_role = row.pop("_probe_role", None)
        resident = row.pop("_probe_resident", True)
        if owner is None:
            finished.append(row)
            continue
        state, detail = webhook_auth.probe_secret(
            row["name"], owner=owner, secrets_dir=secrets_dir)
        # #620 (INV-TRIG-016): a casa-owned RESIDENT route authenticates only
        # with bytes its receipt certifies for the role that routes the name.
        # `readable` says what a request would do, so bytes that are present
        # but uncertified are `unproven_blocked`, not `readable`.
        blocked = (state == "readable" and owner == "casa" and resident
                   and webhook_auth.read_certified_secret(
                       row["name"], role=probe_role or "", secrets_dir=secrets_dir) is None)
        if row.get("state") == "misrouted":
            row["probe"] = {"state": "unproven_blocked" if blocked else state,
                            "detail": _BLOCKED_DETAIL if blocked else detail}
            if state == "readable":
                row["probe"]["provenance"] = (
                    webhook_auth.resident_secret_provenance(
                        row["name"], secrets_dir=secrets_dir))
            finished.append(row)
            continue
        row["owner"] = owner
        if blocked:
            row["state"], row["detail"] = "unproven_blocked", _BLOCKED_DETAIL
        elif state == "readable":
            row["state"], row["detail"] = "readable", detail
            row["provenance"] = webhook_auth.resident_secret_provenance(
                row["name"], secrets_dir=secrets_dir)
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
