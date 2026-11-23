"""Plugin-declared events — manifest parsing + intrinsic validation.

Leaf module (stdlib only). A plugin's ``plugin.json`` may carry two peers of
``casa.triggers``/``casa.callbacks``:

* ``casa.emits`` — events the plugin may raise, named the same way a
  callback names its redirect target.
* ``casa.subscribes`` — events, DECLARED BY ANOTHER PLUGIN'S ``casa.emits``,
  that this plugin wants delivered to it.

This module turns those blocks into normalized dicts and collects intrinsic-
validation errors (shape, naming, self-reference, duplication) that are
knowable WITHOUT deployment state. Contextual validity (does the referenced
emitter/event actually exist, operator consent) is decided later at
reconcile time — mirroring ``plugin_callbacks.py`` and ``plugin_triggers.py``.

Like a callback, an emit entry grants no turn into a role and no memory
access by itself — it is just a declared name. A subscribe entry is the
thing that later needs operator consent (it wires a delivery), which is why
``ack_identity`` — unlike a callback's — folds in an artifact id and a
target set, mirroring ``plugin_triggers.ack_identity``.

Intrinsic validation is fail-closed and per-plugin all-or-nothing: any error
in a block's entry set means the whole block is rejected by callers.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import plugin_registry

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_EMITS = 4
MAX_SUBSCRIBES = 4
# Not the trigger cap (64): mirrors plugin_callbacks.py — bundled plugins'
# registry names are `slug.manifest_name` (up to 73 chars) and an emitted
# event has no secret files to bound, so the cap is looser.
MAX_EFFECTIVE_LEN = 128

_EMIT_KEYS = {"name"}
_SUBSCRIBE_KEYS = {"plugin", "event"}

# Single source of truth for "what is a valid registry plugin name" —
# plugin_registry.py:27-60. A subscribe's emitter reference must accept
# both the plain form and the scoped `slug.manifest_name` (owned-plugin)
# form, since a bundled specialist plugin publishes events under its
# scoped name.
_PLUGIN_NAME_RE = plugin_registry.NAME_RE
_OWNED_PLUGIN_NAME_RE = plugin_registry.OWNED_NAME_RE


def effective_name(plugin: str, event: str) -> str:
    """The routed event name for a plugin-declared emit: ``plg-<plugin>--<event>``."""
    return f"plg-{plugin}--{event}"


def subscribe_declaration_digest(entry: dict[str, Any]) -> str:
    """sha256 hex over the canonical JSON of a subscribe's normalized
    declaration: the referenced emitter plugin and event name.

    Same canonicalization policy as
    ``plugin_callbacks.declaration_digest`` (sorted keys, compact
    separators). One input to ``ack_identity``.
    """
    body = json.dumps(
        {"event": entry["event"], "plugin": entry["plugin"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def ack_identity(
    subscriber: str, subscriber_artifact_id: str, emitter: str, event: str,
    digest: str, targets: list[str],
) -> str:
    """The consent identity: sha256 over ``(subscriber, subscriber_artifact_id,
    emitter, event, digest, sorted(targets))``.

    Unlike a callback's ack (which excludes the artifact id, since a
    callback grants no turn or memory access), a subscribe delivery reaches
    into a subscriber role/target the operator must approve — mirroring
    ``plugin_triggers.ack_identity``, this folds in the subscriber's
    artifact id (a plugin upgrade mints a new identity, so the old consent
    can never carry over silently) and the delivery target set (retargeting
    a subscription is a new consent question). ``targets`` is sorted before
    hashing so delivery-target ORDER never changes the identity.
    """
    body = json.dumps(
        [subscriber, subscriber_artifact_id, emitter, event, digest, sorted(targets)],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def parse_and_validate_emits(
    plugin: str, manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(emits, errors)`` for a plugin's ``casa.emits``.

    ``emits`` are the normalized (best-effort) parsed entries; a non-empty
    ``errors`` means callers reject the WHOLE set (per-plugin all-or-
    nothing). An absent/malformed ``casa`` or ``casa.emits`` is ``([], [])``
    (no events declared, not an error).
    """
    errs: list[str] = []
    casa = manifest.get("casa")
    if not isinstance(casa, dict):
        return [], []
    raw = casa.get("emits")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["casa.emits must be a list"]

    # Plugin name itself must be injective-safe (feeds the effective name).
    # Same rails as plugin_callbacks.py/plugin_triggers.py — with both dash
    # edges banned, the FIRST '--' in an effective name is always exactly
    # the separator (unique decomposition).
    if "--" in plugin:
        errs.append(f"plugin name {plugin!r} may not contain '--'")
    if plugin.endswith("-"):
        errs.append(f"plugin name {plugin!r} may not end with '-' "
                    "(ambiguous event-name separator)")

    if len(raw) > MAX_EMITS:
        errs.append(f"too many emits ({len(raw)} > {MAX_EMITS})")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"casa.emits[{i}]"
        if not isinstance(entry, dict):
            errs.append(f"{where}: must be an object")
            continue
        unknown = set(entry) - _EMIT_KEYS
        if unknown:
            errs.append(f"{where}: unknown key(s) {sorted(unknown)}")
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name or ""):
            errs.append(f"{where}: name must match [a-zA-Z0-9_-]+")
            name = None
        elif "--" in name:
            errs.append(f"{where}: name {name!r} may not contain '--'")
        elif name.startswith("-"):
            errs.append(f"{where}: name {name!r} may not start with '-' "
                        "(ambiguous plugin-name separator)")
        elif name.endswith("-"):
            errs.append(f"{where}: name {name!r} may not end with '-' "
                        "(ambiguous event/token separator — "
                        "`<event>--<u32hex>.json` misparses)")
        elif name.startswith("plg-"):
            errs.append(f"{where}: name may not start with the reserved 'plg-' prefix")
        else:
            if name in seen:
                errs.append(f"{where}: duplicate declared name {name!r}")
            seen.add(name)
            eff = effective_name(plugin, name)
            if len(eff) > MAX_EFFECTIVE_LEN:
                errs.append(f"{where}: effective name {eff!r} too long "
                            f"(>{MAX_EFFECTIVE_LEN})")
        if name:
            out.append({"declared": name, "effective": effective_name(plugin, name)})
    if errs:
        return [], errs
    return out, errs


def _valid_emitter_ref(emitter: Any) -> bool:
    """The registry-name grammar for a subscribe's emitter reference —
    accepts both the plain and scoped (owned-plugin) forms."""
    return (isinstance(emitter, str)
            and bool(_PLUGIN_NAME_RE.match(emitter)
                     or _OWNED_PLUGIN_NAME_RE.match(emitter)))


def parse_and_validate_subscribes(
    plugin: str, manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(subscribes, errors)`` for a plugin's ``casa.subscribes``.

    ``subscribes`` are the normalized (best-effort) parsed entries; a non-
    empty ``errors`` means callers reject the WHOLE set (per-plugin all-or-
    nothing). An absent/malformed ``casa`` or ``casa.subscribes`` is
    ``([], [])`` (no subscriptions declared, not an error).

    A subscribe entry names the EMITTER (registry-name grammar, accepting
    scoped `slug.name` forms) and the event on that emitter — never this
    plugin's own name (self-subscription is refused) and never a duplicate
    (emitter, event) pair.
    """
    errs: list[str] = []
    casa = manifest.get("casa")
    if not isinstance(casa, dict):
        return [], []
    raw = casa.get("subscribes")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["casa.subscribes must be a list"]

    if len(raw) > MAX_SUBSCRIBES:
        errs.append(f"too many subscribes ({len(raw)} > {MAX_SUBSCRIBES})")

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for i, entry in enumerate(raw):
        where = f"casa.subscribes[{i}]"
        if not isinstance(entry, dict):
            errs.append(f"{where}: must be an object")
            continue
        unknown = set(entry) - _SUBSCRIBE_KEYS
        if unknown:
            errs.append(f"{where}: unknown key(s) {sorted(unknown)}")

        emitter = entry.get("plugin")
        if not _valid_emitter_ref(emitter):
            errs.append(f"{where}: plugin must be a valid registry name "
                        "([a-z0-9][a-z0-9-]* or scoped slug.name)")
            emitter = None
        elif emitter == plugin:
            errs.append(f"{where}: plugin {plugin!r} may not subscribe to its own events")
            emitter = None

        event = entry.get("event")
        if not isinstance(event, str) or not _NAME_RE.match(event or ""):
            errs.append(f"{where}: event must match [a-zA-Z0-9_-]+")
            event = None
        elif "--" in event:
            errs.append(f"{where}: event {event!r} may not contain '--'")
        elif event.startswith("-"):
            errs.append(f"{where}: event {event!r} may not start with '-' "
                        "(ambiguous plugin-name separator)")
        elif event.endswith("-"):
            errs.append(f"{where}: event {event!r} may not end with '-' "
                        "(ambiguous event/token separator — "
                        "`<event>--<u32hex>.json` misparses)")
        elif event.startswith("plg-"):
            errs.append(f"{where}: event may not start with the reserved 'plg-' prefix")

        if emitter and event:
            key = (emitter, event)
            if key in seen:
                errs.append(f"{where}: duplicate subscription to "
                            f"{emitter!r}/{event!r}")
            else:
                seen.add(key)
                norm = {"plugin": emitter, "event": event}
                out.append({**norm, "digest": subscribe_declaration_digest(norm)})
    if errs:
        return [], errs
    return out, errs
