"""Plugin-declared webhook triggers (Release B) — manifest parsing + intrinsic
validation.

Leaf module (stdlib only). A plugin's ``plugin.json`` may carry
``casa.triggers`` — a list of webhook trigger declarations. This module turns
that block into normalized trigger dicts and collects intrinsic-validation
errors (shape, naming, auth policy) that are knowable WITHOUT deployment state.
Contextual validity (does the target resident exist / declare the webhook
channel, name collisions, operator consent) is decided later at reconcile time.

Intrinsic validation is fail-closed and per-plugin all-or-nothing: any error in
the set means the whole plugin's trigger declaration is rejected by callers.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Scope (Release B): only Casa-owned secrets. ``provider`` (import-based) is
# deferred; reject it intrinsically with an actionable message.
_MODES = ("hmac_body", "static_header", "timestamped_hmac")
# Must stay identical to agent_loader._DEFAULT_AUTH_HEADER: a neutral name at
# one site and a vendor's at the other is the same defect relocated (#655).
# This table is also the RECOVERY value for an invalid header token below.
_DEFAULT_HEADER = {
    "hmac_body": "X-Webhook-Signature",
    "static_header": "X-API-Key",
    "timestamped_hmac": "X-Webhook-Signature",
}
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# RFC 7230 header field-name token.
_HEADER_RE = re.compile(r"^[!#$%&'*+._`|~0-9A-Za-z-]+$")
_TARGET_RE = re.compile(r"^resident:[a-z0-9_-]+$")
_CLEARANCES = ("public", "friends", "family")
_MAX_TRIGGERS = 8
_MAX_EFFECTIVE_LEN = 64

_TRIGGER_KEYS = {"name", "type", "target", "clearance", "auth"}
_AUTH_KEYS = {"mode", "header", "tolerance_secs", "secret_owner"}


def effective_name(plugin: str, declared: str) -> str:
    """The routed webhook name for a plugin-declared trigger: ``plg-<plugin>--<declared>``."""
    return f"plg-{plugin}--{declared}"


# The auth fields that participate in the consent identity — exactly the
# normalized policy shape `_validate_auth` emits. Anything else in a caller-
# supplied dict is ignored so an unnormalized copy hashes the same.
_IDENTITY_AUTH_KEYS = ("mode", "header", "tolerance_secs", "secret_owner")


def ack_identity(
    *, plugin: str, artifact_id: str, effective: str, target: str,
    auth: dict[str, Any],
) -> str:
    """The consent identity: sha256 over the exact tuple the operator approves.

    Binds an ack to ``(plugin, artifact_id, effective, target,
    normalized-auth-policy)`` — a plugin update (new artifact), a renamed or
    retargeted trigger, or ANY auth-policy change (mode/header/tolerance/
    owner) yields a different identity, so the old consent can never carry
    over to a policy the operator did not see.
    """
    norm_auth = {k: auth.get(k) for k in _IDENTITY_AUTH_KEYS}
    body = json.dumps(
        [plugin, artifact_id, effective, target, norm_auth],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _validate_auth(auth: Any, errs: list[str], where: str) -> dict[str, Any] | None:
    if not isinstance(auth, dict):
        errs.append(f"{where}: 'auth' must be an object")
        return None
    unknown = set(auth) - _AUTH_KEYS
    if unknown:
        errs.append(f"{where}: unknown auth key(s) {sorted(unknown)}")
    mode = auth.get("mode")
    if mode not in _MODES:
        errs.append(f"{where}: auth.mode must be one of {list(_MODES)}")
        return None
    owner = auth.get("secret_owner", "casa")
    if owner == "provider":
        errs.append(
            f"{where}: secret_owner 'provider' is deferred in this release; "
            "use a Casa-owned static_header or timestamped_hmac trigger")
    elif owner != "casa":
        errs.append(f"{where}: secret_owner must be 'casa'")
    header = auth.get("header", _DEFAULT_HEADER[mode])
    if not isinstance(header, str) or not _HEADER_RE.match(header):
        errs.append(f"{where}: auth.header {header!r} is not a valid header token")
        header = _DEFAULT_HEADER[mode]
    tol = auth.get("tolerance_secs", 300)
    # bool is an int subclass — reject it explicitly.
    if isinstance(tol, bool) or not isinstance(tol, int) or not (60 <= tol <= 3600):
        errs.append(f"{where}: auth.tolerance_secs must be an int in [60, 3600]")
        tol = 300
    return {"mode": mode, "header": header, "tolerance_secs": tol,
            "secret_owner": "casa"}


def parse_and_validate(
    plugin: str, manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(triggers, errors)`` for a plugin's ``casa.triggers``.

    ``triggers`` are the normalized (best-effort) parsed entries; a non-empty
    ``errors`` means callers reject the WHOLE set (per-plugin all-or-nothing).
    An absent/malformed ``casa`` or ``casa.triggers`` is ``([], [])`` (no
    triggers declared, not an error).
    """
    errs: list[str] = []
    casa = manifest.get("casa")
    if not isinstance(casa, dict):
        return [], []
    raw = casa.get("triggers")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["casa.triggers must be a list"]

    # Plugin name itself must be injective-safe (feeds the effective name).
    # Sol shipB-r2 P1-3: a trailing '-' on the plugin (or a leading '-' on a
    # declared name, below) makes the '--' separator ambiguous — plugin "a-"
    # + name "x" and plugin "a" + name "-x" would both route
    # "plg-a---x", and plugin a's revoke prefix "plg-a--" would sweep
    # plugin a-'s secrets. With both edges banned, the FIRST '--' in an
    # effective name is always exactly the separator (unique decomposition).
    if "--" in plugin:
        errs.append(f"plugin name {plugin!r} may not contain '--'")
    if plugin.endswith("-"):
        errs.append(f"plugin name {plugin!r} may not end with '-' "
                    "(ambiguous trigger-name separator)")

    if len(raw) > _MAX_TRIGGERS:
        errs.append(f"too many triggers ({len(raw)} > {_MAX_TRIGGERS})")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"casa.triggers[{i}]"
        if not isinstance(entry, dict):
            errs.append(f"{where}: must be an object")
            continue
        unknown = set(entry) - _TRIGGER_KEYS
        if unknown:
            errs.append(f"{where}: unknown key(s) {sorted(unknown)}")
        if entry.get("type") != "webhook":
            errs.append(f"{where}: type must be 'webhook' (plugin triggers are "
                        "webhook-only this release)")
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name or ""):
            errs.append(f"{where}: name must match [a-zA-Z0-9_-]+")
            name = None
        elif "--" in name:
            errs.append(f"{where}: name {name!r} may not contain '--'")
        elif name.startswith("-"):
            errs.append(f"{where}: name {name!r} may not start with '-' "
                        "(ambiguous plugin-name separator)")
        elif name.startswith("plg-"):
            errs.append(f"{where}: name may not start with the reserved 'plg-' prefix")
        else:
            if name in seen:
                errs.append(f"{where}: duplicate declared name {name!r}")
            seen.add(name)
            eff = effective_name(plugin, name)
            if len(eff) > _MAX_EFFECTIVE_LEN:
                errs.append(f"{where}: effective name {eff!r} too long "
                            f"(>{_MAX_EFFECTIVE_LEN})")
        target = entry.get("target")
        if not isinstance(target, str) or not _TARGET_RE.match(target or ""):
            errs.append(f"{where}: target must be 'resident:<role>'")
        clearance = entry.get("clearance", "public")
        if clearance not in _CLEARANCES:
            errs.append(f"{where}: clearance must be one of {list(_CLEARANCES)}")
            clearance = "public"
        auth = _validate_auth(entry.get("auth", {}), errs, where)
        if name and auth and target:
            out.append({
                "declared": name,
                "effective": effective_name(plugin, name),
                "type": "webhook",
                "target": target,
                "clearance": clearance,
                "auth": auth,
            })
    return out, errs
