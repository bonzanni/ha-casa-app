"""The authorization-callback reconciler (runtime seam).

The ONE writer of :class:`trigger_registry.TriggerRegistry`'s CALLBACK
overlay, and the owner of the spool's advisory files (``ready.json`` and the
``.index`` discovery entries). Wired into the same call sites as the
trigger reconciler: casa_core boot, every plugin lifecycle mutation, the
trigger-affecting reload scopes, the consent approve path and the revoke tool.
All entry points serialize on ``_RECONCILE_LOCK``.

Semantics:

* **Complete desired overlay, atomic swap.** Every reconcile derives the FULL
  set of routable plugin callbacks from the CURRENT resolver snapshot and
  swaps it in one operation — a removed / unresolved / revoked / re-declared
  plugin's callback ingress is swept by absence (the handler 404s), and
  readers never see a partial overlay.
* **Gates, in order.** Intrinsic validity of the declaration
  (``callback_invalid``) → the plugin is assigned to at least one role
  (``callback_no_target``) → a persisted operator ack for the exact consent
  identity (``callback_pending_ack``). Unlike a trigger there is no target,
  no clearance and no secret to gate on: a callback grants no turn and no
  memory access, so the pass is the trigger pass with the assignment check
  generalized to "any role" and the secret stage dropped.
* **Fail-closed, per-plugin all-or-nothing.** Any gap in a plugin's set keeps
  its WHOLE set dark plus a ``stage="callbacks"`` ``PluginIssue`` (the mirror
  of INV-TRIG-003). A non-consent gap additionally SUPPRESSES prompting —
  approving a callback that still could not route is a broken promise.
* **Paired marker transaction, on-disk truth.** The ``ready.json`` +
  ``.index`` pair is published/retired as ONE fail-closed transaction driven by
  the DURABLE on-disk inventory (not the in-memory previous overlay, which is
  empty across a restart): retire orphans and stale/partial pairs BEFORE the
  swap, write the routed set's pairs AFTER it, and DELETE BOTH on any write
  failure. After any pass on a trustworthy computation each plugin's pair is
  either both-absent or both-present-and-equal-to-desired — never stale, never
  half-published. The marker can therefore never be falsely positive, and it
  stays advisory: the overlay alone decides what the endpoint serves.
* **Consent survives a routine upgrade.** The consent identity binds the
  DECLARATION digest, not the artifact — an update that leaves ``casa.callbacks``
  untouched keeps its ack (no dark window, no re-tap). Identities no longer
  computable from any installed declaration are pruned opportunistically, and
  only on a CLEAN pass (a resolution hiccup must never vaporize consent).
* **Recomputable health.** :func:`current_issues` recomputes the contextual
  callback issues fresh from the live runtime, so an unrelated health refresh
  can never erase ``callback_pending_ack`` / ``callback_no_target``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import plugin_callbacks
# Aliased: the reconcile functions take a `trigger_registry` INSTANCE
# parameter that shadows the module name (#606).
import trigger_registry as trigger_registry_mod
# ONE shape for both halves of the setup gate (#457): the gate reads a trigger
# state and a callback state and must apply the identical rule to each, so they
# share the type rather than each declaring their own. Safe at module level —
# ``trigger_reconcile`` imports this module only from inside a function, so
# there is no import cycle in either direction.
from trigger_reconcile import IssueState

logger = logging.getLogger(__name__)

# Serializes every callback-overlay writer (boot, mutations, reloads, consent
# approve, revoke) so each swap derives from a self-consistent compute.
_RECONCILE_LOCK = asyncio.Lock()

# Distinguishes "caller passed no spool" from "caller passed None on purpose"
# (the latter is a wiring gap the reconcile must SURFACE, not silently paper
# over with the process singleton).
_UNSET: Any = object()

# Assignment targets a callback's delivery nudge can actually reach. An
# executor-only plugin has no agent to collect the code it accepted, so it is
# `callback_no_target` — the same rule plugin_setup_episodes._compose applies
# when it picks a dispatch target.
_RESIDENT_PREFIX = "resident:"
_SPECIALIST_PREFIX = "specialist:"


# -- injectable defaults (module functions so tests can monkeypatch) ---------


def _default_resolver() -> Callable[[str | None], Any]:
    """ONE registry snapshot for the whole pass (#454) — see
    ``plugin_registry.pinned_resolver``."""
    import plugin_registry

    return plugin_registry.pinned_resolver()


def _default_entries() -> Callable[[], list[dict]]:
    """The registry ENTRIES seam — assignment authority for callbacks.

    A resolved plugin carries no targets, and the callback gate is "assigned
    to at least one role" (any resident/specialist, not one scoped target), so
    the entries of the same snapshot the resolver reads are the natural
    source. Keeping it a seam keeps the compute pure and testable.

    This is the LAST-RESORT source only. #454: "the same snapshot the resolver
    reads" was an aspiration, not a fact — this read the live snapshot
    independently, so a reload between the two handed the pass one generation's
    manifests and another's assignment authority. A pinned resolver now supplies
    its own entries (:func:`_entries_for`); this remains for an injected seam
    that carries none."""
    import plugin_registry

    def entries() -> list[dict]:
        return list(plugin_registry.snapshot_registry().entries)

    return entries


def _entries_for(resolver: Any) -> Callable[[], list[dict]]:
    """The registry entries of the RESOLVER's own snapshot when it has one.

    Resolve the resolver first, then ask it for its entries: the two reads are
    then provably one generation. Falls back to the unpinned default for an
    injected seam that carries no snapshot of its own."""
    ents = getattr(resolver, "entries", None)
    return ents if callable(ents) else _default_entries()


def _default_acks() -> Any:
    from callback_acks import ACKS

    return ACKS


def _default_spool() -> Any:
    """The process-wide spool, or ``None`` before boot wired one."""
    import callback_spool

    return callback_spool.get_spool()


def _base_url() -> str | None:
    """The public base URL the redirect URIs are built from.

    Delegates to ``callback_urls.validated_base`` (full origin
    validation: absolute https, no userinfo/path/query/fragment, host not an
    IP literal) rather than only the bashio ``"null"``/``"None"`` guard
    casa_core uses for ``PUBLIC_URL``. ``None`` means the facility is
    unavailable: consent still works, but no readiness marker or index entry
    is written, and every routed plugin reports
    ``callback_base_url_invalid``. Kept as a module-function seam (not an
    inlined call) so tests can keep monkeypatching ``cr._base_url`` directly.
    """
    import callback_urls

    return callback_urls.validated_base()


def _redirect_uri(base: str, effective: str) -> str:
    """The redirect URI a consumer registers with its provider — the
    urllib-based join in ``callback_urls``, never string concat."""
    import callback_urls

    return callback_urls.redirect_uri(base, effective)


@dataclass
class RoutedCallbacks:
    """One routed plugin's published set (the ready/index payload's source)."""

    plugin: str
    artifact_id: str
    path: str
    callbacks: list[dict] = field(default_factory=list)


@dataclass
class DesiredCallbacks:
    """The pure compute result: what SHOULD route right now."""

    overlay: dict[str, dict] = field(default_factory=dict)
    issues: list = field(default_factory=list)
    # Consent prompts to fire — only for callbacks whose ONLY gap is the ack.
    pending: list[dict] = field(default_factory=list)
    routed: list[RoutedCallbacks] = field(default_factory=list)
    # Every identity computable from a currently-installed declaration (the
    # prune's keep-set), and whether this pass is clean enough to prune at all.
    valid_identities: set[str] = field(default_factory=set)
    prunable: bool = False
    base_url: "str | None" = None
    # #457: the plugins this computation actually SAW — the mirror of
    # ``trigger_reconcile.DesiredTriggers.observed``, which carries the full
    # reasoning. Empty under an invalid registry.
    observed: set[str] = field(default_factory=set)
    # #606: the mirror of ``DesiredTriggers.registry_valid`` — see there.
    registry_valid: bool = False


def compute_desired(
    *, role_configs: dict, acks: Any = None,
    resolver: "Callable[[str | None], Any] | None" = None,
    entries: "Callable[[], list[dict]] | None" = None,
) -> DesiredCallbacks:
    """Side-effect-free derivation of the complete desired callback overlay +
    the contextual callback issues. Never raises for bad plugin data."""
    import plugin_store
    from plugin_registry import PluginIssue

    acks = acks if acks is not None else _default_acks()
    resolver = resolver if resolver is not None else _default_resolver()
    entries = entries if entries is not None else _entries_for(resolver)

    out = DesiredCallbacks(base_url=_base_url())
    all_res = resolver(None)
    if not getattr(all_res, "registry_valid", False):
        # Fail-closed: an invalid registry routes NO callback ingress (its own
        # registry-stage issues surface via the resolver / health pass), and
        # nothing is pruned — a membership set derived from a failed load
        # would drop every consent.
        return out
    out.registry_valid = True
    out.observed = {rp.name for rp in all_res.plugins}
    # Opportunistic prune only on a CLEAN pass: an artifact checksum hiccup or
    # an unreadable manifest drops that plugin from the resolution, and
    # treating its absence as "the declaration is gone" would silently discard
    # the operator's consent. The next clean reconcile prunes instead.
    out.prunable = not list(getattr(all_res, "issues", ()) or ())

    # Assignment authority: the registry entry's OWN declared targets, read
    # once from the same snapshot the resolver reads.
    targets_by_name: dict[str, list] = {}
    for entry in entries():
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            targets_by_name[entry["name"]] = list(entry.get("targets") or [])
    live_roles = {f"{_RESIDENT_PREFIX}{role}" for role in role_configs}

    for rp in all_res.plugins:
        try:
            callbacks = plugin_store.manifest_callbacks(rp.manifest, rp.name)
        except Exception:  # noqa: BLE001 — StoreError("callbacks_invalid"),
            # or any other read failure on a pre-published artifact: the
            # publish gate is younger than the store, so an invalid
            # declaration is a state to SURFACE, never a reconcile crash.
            out.issues.append(PluginIssue(
                name=rp.name, target=None, stage="callbacks",
                reason_code="callback_invalid", artifact_id=rp.artifact_id))
            # An unparseable declaration contributes NO identities,
            # so pruning this pass would destroy the operator's consent for
            # this plugin's still-valid callbacks (all-or-nothing rejects the
            # set, it does not delete it). We cannot read the declaration, so
            # we cannot know any ack is stale — suppress the whole prune until
            # a pass that can.
            out.prunable = False
            continue
        if not callbacks:
            continue

        # The prune's keep-set is about the DECLARATION existing, not about
        # routing: an unassigned (or still-unacked) plugin's consent must
        # survive, so these identities are collected BEFORE any later gate.
        declared = [
            (cb, digest, plugin_callbacks.ack_identity(
                rp.name, cb["effective"], digest))
            for cb, digest in ((cb, plugin_callbacks.declaration_digest(cb))
                               for cb in callbacks)]
        out.valid_identities.update(ident for _cb, _d, ident in declared)

        # Gate 2 — assignment. Per PLUGIN (a callback declares no target):
        # the delivery nudge needs an agent to hand the waiting result to, and
        # routing an unreachable plugin would accept short-lived codes nobody
        # can ever collect.
        if not _reachable(targets_by_name.get(rp.name) or [], live_roles):
            out.issues.append(PluginIssue(
                name=rp.name, target=None, stage="callbacks",
                reason_code="callback_no_target", artifact_id=rp.artifact_id))
            # A non-consent gap: no prompt, and the whole set stays dark.
            continue

        entries_for_plugin: dict[str, dict] = {}
        plugin_pending: list[dict] = []
        for cb, digest, identity in declared:
            if acks.get(identity) is None:
                out.issues.append(PluginIssue(
                    name=rp.name, target=None, stage="callbacks",
                    reason_code="callback_pending_ack",
                    artifact_id=rp.artifact_id))
                plugin_pending.append({
                    "plugin": rp.name, "artifact_id": rp.artifact_id,
                    "declared": cb["declared"], "effective": cb["effective"],
                    "declaration_digest": digest, "identity": identity})
                continue
            entries_for_plugin[cb["effective"]] = {
                "plugin": rp.name, "declared": cb["declared"],
                # Carry the effective name in the value too (it is already the
                # key): the callback handler records it in the result and logs
                # it, and reading it from the entry keeps that on one shape
                # instead of a routed-name fallback.
                "effective": cb["effective"], "path": rp.path}

        # Per-plugin all-or-nothing: any gap unroutes the whole set.
        if plugin_pending:
            out.pending.extend(plugin_pending)
            continue
        out.overlay.update(entries_for_plugin)
        out.routed.append(RoutedCallbacks(
            plugin=rp.name, artifact_id=rp.artifact_id, path=rp.path,
            callbacks=[dict(cb) for cb in callbacks]))
        if out.base_url is None:
            # Consent and routing stand; the facility is simply unavailable
            # until the operator sets a usable public_url — nothing can be
            # published for the consumer to read.
            out.issues.append(PluginIssue(
                name=rp.name, target=None, stage="callbacks",
                reason_code="callback_base_url_invalid",
                artifact_id=rp.artifact_id))
    return out


def _reachable(targets: list, live_roles: set[str]) -> bool:
    """A target the delivery nudge can reach: a LIVE resident role, or any
    specialist (specialists are not in ``role_configs``; the nudge reaches
    them through assistant delegation, exactly as setup episodes do)."""
    for t in targets:
        if not isinstance(t, str):
            continue
        if t in live_roles or t.startswith(_SPECIALIST_PREFIX):
            return True
    return False


# ---------------------------------------------------------------------------
# the spool file phase (ready.json + .index)
# ---------------------------------------------------------------------------


def _ready_payload(base_url: str, routed: RoutedCallbacks) -> dict:
    return {
        "v": 1,
        "base_url": base_url,
        "callbacks": {
            cb["declared"]: {
                "effective": cb["effective"],
                "redirect_uri": _redirect_uri(base_url, cb["effective"]),
            }
            for cb in routed.callbacks
        },
    }


def _spool_issue(issues: list, plugin: str, artifact_id: str | None) -> None:
    """One ``callback_spool_error`` per plugin per pass: an unwired spool
    would otherwise emit a row for every file operation of every plugin, and
    the health report's fingerprint dedup is not a licence to spam it."""
    from plugin_registry import PluginIssue
    if any(getattr(i, "name", None) == plugin
           and getattr(i, "reason_code", None) == "callback_spool_error"
           for i in issues):
        return
    issues.append(PluginIssue(
        name=plugin, target=None, stage="callbacks",
        reason_code="callback_spool_error", artifact_id=artifact_id))


def _desired_marker_payloads(base_url: "str | None",
                             routed: RoutedCallbacks) -> "tuple[dict, dict]":
    """The ready.json and .index payloads the post-swap write would publish —
    the single source both the pair-state compare and the write use, so the two
    can never drift."""
    ready = _ready_payload(base_url, routed)
    return ready, dict(ready, plugin_dir=routed.plugin)


def _read_marker(spool: Any, op: str, arg: str) -> Any:
    """Read one on-disk marker, total: any failure ⇒ an INVALID marker (never
    ABSENT, so an unreadable marker is republished, and never a reconcile
    crash)."""
    import callback_spool
    try:
        return getattr(spool, op)(arg)
    except Exception:  # noqa: BLE001
        logger.warning("callback marker read %s failed", op, exc_info=True)
        return callback_spool.Marker(callback_spool.MarkerState.INVALID)


def _canonical_bytes(payload: Any) -> "bytes | None":
    """The desired payload's canonical on-disk BYTES, via the SAME
    :func:`callback_spool.canonical_marker_bytes` the marker writer uses.

    The compare is BYTE-STRICT: an on-disk marker's raw bytes must equal this
    to count as unchanged. That makes any drift — a key reorder, a ``true`` /
    ``1.0`` vs ``1`` type diff (which plain ``dict ==`` would call equal), an
    extra key, or a whitespace diff — DIFFER, so it is retired and rewritten;
    and because writer and compare share the helper, casa's own fresh write is
    byte-identical, so a steady-state pass never churns. Returns None for a
    non-serializable desired payload (defensive — the desired form is always
    serializable, and None never equals a real marker's bytes)."""
    import callback_spool
    try:
        return callback_spool.canonical_marker_bytes(payload)
    except (TypeError, ValueError):
        return None


def _pair_state(spool: Any, base_url: "str | None",
                routed: RoutedCallbacks) -> "tuple[bool, bool]":
    """Classify a routed plugin's on-disk (ready, index) pair against the
    desired payloads. Returns ``(needs_republish, has_on_disk)``:

    * ``needs_republish`` — the pair is NOT both-PRESENT-and-equal-to-desired
      (absent, invalid, stale, or half-published). Only a pair that already
      equals the desired one is a no-op (no churn).
    * ``has_on_disk`` — either marker is present-or-invalid, i.e. there is
      something to retire BEFORE the swap; a fully-ABSENT pair is a fresh
      publish with nothing to delete first.

    A pathless routed plugin (an overlay anomaly) has no index entry — its key
    would be ``sha256(realpath(""))`` = the process CWD — so the index side is
    skipped entirely rather than read or written from an empty path."""
    import callback_spool
    present = callback_spool.MarkerState.PRESENT
    absent = callback_spool.MarkerState.ABSENT
    ready, index = _desired_marker_payloads(base_url, routed)

    # BYTE-STRICT compare (see _canonical_bytes): an on-disk marker counts as
    # unchanged only when its RAW bytes equal canonical(desired) — a payload
    # merely ``==`` the desired one, or byte-different by reorder/whitespace,
    # is NOT "unchanged".
    desired_ready = _canonical_bytes(ready)
    on_ready = _read_marker(spool, "read_marker", routed.plugin)
    ready_ok = (on_ready.state == present and desired_ready is not None
                and on_ready.raw == desired_ready)
    ready_on_disk = on_ready.state != absent

    if routed.path:
        desired_index = _canonical_bytes(index)
        on_index = _read_marker(spool, "read_index_marker", routed.path)
        index_ok = (on_index.state == present and desired_index is not None
                    and on_index.raw == desired_index)
        index_on_disk = on_index.state != absent
    else:
        index_ok, index_on_disk = True, False   # no index for a pathless plugin

    return (not (ready_ok and index_ok)), (ready_on_disk or index_on_disk)


def _retire_marker(spool: Any, op: str, arg: str) -> bool:
    """Type-aware retirement (idempotent delete) of one marker/index entry of
    ANY type. Returns True when the entry is now ABSENT (removed or already
    gone), False when a genuine removal FAILED (the entry survives) — so a
    caller that CAN attribute the failure to a plugin surfaces it rather than
    swallowing it into "looks absent". A raised exception is also a failure.

    The overlay — the sole routing authority — 404s the deposit regardless, so
    on the fail-closed post-swap path a False is harmless (``_guard`` already
    recorded the write failure); it matters on the orphan path, where a
    surviving invalid marker would otherwise block republication unseen."""
    if spool is None:
        return True
    try:
        return bool(getattr(spool, op)(arg))
    except Exception:  # noqa: BLE001
        logger.warning("callback marker retire %s failed", op, exc_info=True)
        return False


def _reconcile_markers_pre_swap(
    spool: Any, desired: DesiredCallbacks,
) -> list[RoutedCallbacks]:
    """Pre-swap half of the paired marker transaction, driven by the DURABLE
    on-disk inventory (not the in-memory previous overlay, which is empty
    across a restart). Returns the routed plugins whose pair must be written
    after the swap.

    One rule, every subcase:

    * **Orphans** — a published ``ready.json`` / index key the desired routed
      set no longer covers (plugin removed, unacked, base-URL-invalid, or an
      artifact path that changed) — retired here. Gated on ``desired.prunable``:
      the SAME availability double-gate the stale-ack prune uses, so a wholesale
      bad compute (invalid registry / resolution hiccup) can never nuke a valid
      plugin's markers.
    * **Routed but not already exactly desired** — absent, invalid, stale, or
      half-published (one file present, one gone): BOTH files are retired here
      so the post-swap rewrite is the sole writer and a failed rewrite leaves
      them ABSENT (fail-closed) rather than stale/partial. This happens on
      EVERY pass, trustworthy or not — ``prunable`` does NOT gate it (#453).
      A plugin reaches this branch only by being in THIS pass's routed set,
      which means it resolved cleanly here and holds a persisted ack, so its
      desired pair is derived from its own good resolution and rewriting it
      destroys nothing. Gating it made an unrelated broken artifact — the flag
      is registry-GLOBAL — freeze every other plugin's markers indefinitely,
      which since the setup gate reads the pair is a hold with no exit. Do not
      restore the condition here; it belongs to the orphan retirement above,
      where the conclusion being protected against is "absent from desired",
      the one a bad compute really can get wrong.
    * **Unchanged** — a pair already equal to the desired one is left exactly as
      it is (no retire, no rewrite).

    Runs BEFORE the overlay swap so a crash mid-change leaves the route closed
    with the marker already gone, never the reverse. With no spool wired there
    is nothing on disk to reconcile — the routed set is returned so each plugin
    still gets its one health issue from the (failing) post-swap writes."""
    if spool is None:
        return list(desired.routed) if desired.base_url else []
    import callback_spool

    if desired.base_url:
        routed_by_plugin = {r.plugin: r for r in desired.routed}
        desired_keys = {callback_spool.index_key(r.path)
                        for r in desired.routed if r.path}
    else:
        routed_by_plugin, desired_keys = {}, set()

    if desired.prunable:
        try:
            on_disk_plugins = list(spool.published_plugins())
        except Exception:  # noqa: BLE001 — a read failure leaves markers in
            on_disk_plugins = []           # place, never a reconcile crash
        for plugin in on_disk_plugins:
            if plugin not in routed_by_plugin:
                if not _retire_marker(spool, "delete_ready", plugin):
                    # A genuinely-failed orphan retirement is SURFACED, not
                    # swallowed into "looks absent" — the invalid marker
                    # survives and would otherwise block republication unseen.
                    _spool_issue(desired.issues, plugin, None)
        try:
            on_disk_keys = list(spool.index_keys())
        except Exception:  # noqa: BLE001
            on_disk_keys = []
        for key in on_disk_keys:
            if key not in desired_keys:
                # A failed index-key retirement is logged inside the spool;
                # there is no routed plugin to attribute a health row to.
                _retire_marker(spool, "delete_index_key", key)

    republish: list[RoutedCallbacks] = []
    for routed in routed_by_plugin.values():
        needs, has_on_disk = _pair_state(spool, desired.base_url, routed)
        if not needs:
            continue                     # unchanged — no churn
        # #453: the availability double-gate does NOT cover the routed pair —
        # it guards the ORPHAN retirement above, where a bad compute's wrong
        # "absent from desired" conclusion destroys a live consumer's marker.
        # A plugin reaching this loop is in THIS pass's routed set: it resolved
        # cleanly here and carries a persisted ack, so its desired pair is
        # derived from its own good resolution and rewriting it destroys
        # nothing. Gating it on `prunable` — which is registry-GLOBAL, set by
        # ANY plugin's resolve issue — made an unrelated broken artifact freeze
        # every other plugin's markers for as long as it stayed broken. That
        # was survivable while the pair was merely advisory; it is not now that
        # the setup gate holds on it, because the state it produces has no exit:
        # the reader demands a pair the writer has decided never to write, and
        # no operator action on the held plugin clears it. The ordinary trigger
        # is a plugin UPDATE — the artifact path moves, so the index entry goes
        # absent while a byte-identical `ready.json` keeps `has_on_disk` true.
        if has_on_disk:
            if not _retire_marker(spool, "delete_ready", routed.plugin):
                _spool_issue(desired.issues, routed.plugin, routed.artifact_id)
            if routed.path and not _retire_marker(
                    spool, "delete_index_entry", routed.path):
                _spool_issue(desired.issues, routed.plugin, routed.artifact_id)
        republish.append(routed)
    return republish


def _publish_markers_post_swap(
    spool: Any, desired: DesiredCallbacks, republish: list[RoutedCallbacks],
) -> None:
    """Post-swap half: write BOTH files for each routed plugin that needs it,
    AFTER the overlay swap so a marker can never advertise a route that is not
    live. If EITHER write fails, DELETE BOTH (fail-closed to absent) and record
    one ``callback_spool_error`` — a partial pair (one present, one absent or
    stale) must NEVER survive a pass. A pathless plugin publishes ready only
    (its index key cannot be derived from an empty path)."""
    if desired.base_url is None:
        return
    for routed in republish:
        ready, index = _desired_marker_payloads(desired.base_url, routed)
        ok = _guard(spool, desired, routed.plugin, routed.artifact_id,
                    "ensure_plugin_dirs", routed.plugin)
        if ok:
            ok = _guard(spool, desired, routed.plugin, routed.artifact_id,
                        "write_ready", routed.plugin, ready)
        if ok and routed.path:
            ok = _guard(spool, desired, routed.plugin, routed.artifact_id,
                        "write_index_entry", routed.path, index)
        if not ok:
            # Fail closed to ABSENT — never a half-published pair. The health
            # issue was already recorded by the failing _guard.
            _retire_marker(spool, "delete_ready", routed.plugin)
            if routed.path:
                _retire_marker(spool, "delete_index_entry", routed.path)


def verify_published_markers(desired: DesiredCallbacks, spool: Any) -> None:
    """Record a gap for every routed plugin whose on-disk marker pair does not
    ALREADY equal the pair this pass would publish (#453).

    The read-only mirror of :func:`_publish_markers_post_swap`, for the
    consumers that RE-DERIVE the desired state without applying it —
    :func:`current_issues`, and through it the plugin-health report and
    ``casa_core._callback_and_trigger_routes_live``, the setup-dispatch gate.

    The trigger half of #453 stated for callbacks: ``compute_desired`` derives
    consent and assignment, but the ``ready.json`` + ``.index`` pair is written
    by the APPLY half, after the consent approval that settles the setup round.
    The redirect URI a plugin's setup tool registers with its provider is read
    out of that pair, so a gate blind to it dispatched setup with nothing (or a
    previous artifact's pair) on disk. ``callback_spool_error`` is the existing
    code for "the consumer cannot discover its redirect URI", which is exactly
    this state; per-plugin all-or-nothing, so one row is enough.

    Byte-strict, via the SAME :func:`_pair_state` the reconcile's own
    pre-swap compare uses — a marker that merely resembles the desired one is
    not the one the consumer would read. An unwired spool is a gap for every
    routed plugin, matching what the reconcile's own writes would surface.
    """
    if desired.base_url is None:
        # Nothing is publishable at all, and every routed plugin already
        # carries `callback_base_url_invalid` from the compute.
        return
    for routed in desired.routed:
        if spool is None:
            _spool_issue(desired.issues, routed.plugin, routed.artifact_id)
            continue
        needs_republish, _has_on_disk = _pair_state(
            spool, desired.base_url, routed)
        if needs_republish:
            _spool_issue(desired.issues, routed.plugin, routed.artifact_id)


def _guard(spool: Any, desired: DesiredCallbacks, plugin: str,
           artifact_id: "str | None", op: str, *args) -> bool:
    """Run one spool operation, converting any failure into a health issue.

    A spool failure never unroutes: the overlay is the authority and the
    published files are advisory, so the fail-closed direction here is "the
    consumer cannot discover its redirect URI", which the operator sees as
    ``callback_spool_error`` rather than a silently dead endpoint."""
    if spool is None:
        _spool_issue(desired.issues, plugin, artifact_id)
        return False
    try:
        getattr(spool, op)(*args)
        return True
    except Exception:  # noqa: BLE001 — a marker failure must never break the
        # reconcile for OTHER plugins (or leave the overlay half-swapped).
        logger.warning("callback spool %s failed (plugin=%s)", op, plugin,
                       exc_info=True)
        _spool_issue(desired.issues, plugin, artifact_id)
        return False


async def _regen_health_safe() -> None:
    """Regenerate the plugin-health report (no operator notify) so a
    just-acked callback's stale ``callback_pending_ack`` clears immediately
    instead of lingering until the next plugin mutation/boot. Never raises.

    Runs under ``tools._plugin_tools_guard()`` (#582 batch, Sol/Terra design
    r1): the report lock serializes the WRITE, not the computation preceding
    it, so a pass that started before a concurrent plugin mutation committed
    would otherwise write its older result last and delete the row that
    mutation just added — reproduced against the real writer, with nothing
    scheduling another regeneration to repair it. Taken only after
    ``_RECONCILE_LOCK`` is released, so the two are never nested here."""
    try:
        import tools
        async with tools._plugin_tools_guard():
            await asyncio.to_thread(tools._regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001
        logger.warning("post-consent plugin-health regen failed", exc_info=True)


async def reconcile_plugin_callbacks(
    *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any = None, acks: Any = None, spool: Any = _UNSET,
    resolver: "Callable[[str | None], Any] | None" = None,
    entries: "Callable[[], list[dict]] | None" = None,
    prompt: bool = True, regen_health: bool = False,
) -> list:
    """Compute + apply: retire the files of everything about to stop being
    published, swap the complete desired overlay, publish the routed set's
    files, prune stale acks, fire consent prompts. Returns the callback
    issues."""
    acks = acks if acks is not None else _default_acks()
    spool = _default_spool() if spool is _UNSET else spool

    def _compute() -> "tuple":
        # The union-membership compute and the setup-candidate sweep read
        # plugin.json for every resolved plugin, so they belong in the SAME
        # worker thread as the main compute — never on the event loop under the
        # reconcile lock. Both still run strictly before any keyboard posts
        # (sealing and prompting fire below, after this returns).
        #
        # #451: the trigger half is computed whenever ``prompt`` is set, not
        # only when this pass has pending callbacks — sealing a ZERO-member
        # verdict requires knowing the trigger half is empty too.
        # ONE snapshot for the whole pass (#451 r3) — see
        # trigger_reconcile.pin_resolver for why sharing the callable is not
        # enough.
        import trigger_reconcile as _tr
        pinned = _tr.pin_resolver(
            resolver if resolver is not None else _default_resolver())
        computed = compute_desired(
            role_configs=role_configs, acks=acks, resolver=pinned,
            entries=entries)
        # NOT gated on ``prompt`` — see the trigger reconciler: boot runs
        # prompt=False and is exactly the pass that must recover an obligation
        # missing because a crash landed between a durable registry publish and
        # its lifecycle reconcile. Only the KEYBOARDS depend on `prompt`.
        union_ok, union, peer_unknown = _trigger_pending_for_union(
            role_configs=role_configs, resolver=pinned)
        cand_ok, cand = _setup_candidates(resolver=pinned)
        candidates = cand if cand_ok else None
        return (computed, union, union_ok, candidates, peer_unknown,
                _tr.one_generation(pinned))

    try:
      async with _RECONCILE_LOCK:
        try:
            (desired, union_pending, union_ok, setup_cands,
             peer_unknown, one_gen) = await asyncio.to_thread(_compute)
        except Exception:
            # A compute failure must not RETAIN the old overlay (a
            # just-revoked plugin's callback would stay open behind a
            # swallowed warning). Fail closed to NO callback ingress, then
            # propagate so the caller logs/surfaces it; the next successful
            # reconcile restores the valid set. The spool files are left
            # untouched — they are advisory and the closed overlay already
            # 404s every deposit.
            #
            # #606: the sentinel, not `{}` — see the trigger mirror. Both close
            # ingress; only `{}` claims that nothing SHOULD route.
            trigger_registry.replace_callback_overlay(
                trigger_registry_mod.ROUTING_UNAVAILABLE)
            raise

        # One paired marker transaction driven by DURABLE on-disk truth: the
        # pre-swap half retires orphans + stale/partial pairs (delete-before-
        # swap), the post-swap half writes the routed set's pairs and fails
        # closed to ABSENT on any write error. The in-memory previous overlay
        # is not consulted — it is empty across a restart and would miss a
        # stale marker (the r2/r3 finding this replaces).
        republish = await asyncio.to_thread(
            _reconcile_markers_pre_swap, spool, desired)
        # #606: only an authoritative computation may publish a map.
        trigger_registry.replace_callback_overlay(
            desired.overlay if desired.registry_valid
            else trigger_registry_mod.ROUTING_UNAVAILABLE)
        await asyncio.to_thread(
            _publish_markers_post_swap, spool, desired, republish)

        if desired.prunable:
            try:
                removed = await asyncio.to_thread(
                    acks.prune_stale, desired.valid_identities)
                if removed:
                    logger.info("pruned %d stale callback ack(s)", len(removed))
            except Exception:  # noqa: BLE001 — an opportunistic prune must
                # never break the reconcile; the next pass retries.
                logger.warning("callback ack prune failed", exc_info=True)

        try:
            import plugin_setup_episodes
            plugin_setup_episodes.kick()
        except Exception:  # noqa: BLE001 — never break a reconcile on this
            pass

        # Prompts fire INSIDE the lock (the trigger-reconcile discipline):
        # keyboard registration is then ordered BEFORE any later reconcile can
        # acquire the lock, so a revoke's final cancel_matching(plugin=…)
        # provably catches every keyboard an in-flight reconcile posted.
        # #451: seal BEFORE the operator-reachability gate inside
        # _fire_consent_prompts, and on EVERY pass — with no DM reachable
        # nothing used to be sealed at all, leaving a mutation's routing
        # decision to be contradicted by a round that first sealed on a later
        # reload.
        import trigger_reconcile
        nonce_by_identity = trigger_reconcile.seal_setup_state(
            trigger_pending=union_pending,
            callback_pending=desired.pending,
            pending_complete=union_ok,
            candidates=setup_cands,
            unknown=(trigger_reconcile.consent_position_unknown(desired.issues)
                     | (peer_unknown or set())),
            single_generation=one_gen)
        try:
            import plugin_setup_episodes
            plugin_setup_episodes.kick()   # a zero-member verdict releases
        except Exception:  # noqa: BLE001
            pass
        if prompt and desired.pending:
            _fire_consent_prompts(
                desired.pending, trigger_registry=trigger_registry,
                role_configs=role_configs,
                channel_manager=channel_manager,
                acks=acks, spool=spool, resolver=resolver,
                entries=entries, nonce_by_identity=nonce_by_identity)
    finally:
        # #606: on BOTH exits, and OUTSIDE the `async with` so the lock order
        # (_RECONCILE_LOCK released before tools._plugin_tools_guard) is
        # preserved. See the trigger mirror for the full reasoning.
        if regen_health:
            await _regen_health_safe()
    return desired.issues


def _fire_consent_prompts(
    pending: list[dict], *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any, acks: Any, spool: Any, resolver: Any,
    entries: Any, nonce_by_identity: dict[str, str],
) -> None:
    import authz_grants
    import callback_consent

    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None:
        return  # no DM reachable — pending_ack stands; re-prompted later
    op = callback_consent.operator_identity(channel)
    if op is None:
        return
    chat_id, operator_id = op

    async def _reconcile_again() -> None:
        # Captures THIS reconcile's inputs. If a reload rebinds the runtime
        # registries before the tap lands, the swap goes to the old registry
        # object — harmless: the ack is persisted, so the next lifecycle
        # reconcile routes it on the live one.
        await reconcile_plugin_callbacks(
            trigger_registry=trigger_registry, role_configs=role_configs,
            channel_manager=channel_manager, acks=acks, spool=spool,
            resolver=resolver, entries=entries, prompt=False,
            regen_health=True)

    # The setup-round membership — the UNION of this plugin's pending TRIGGER
    # and CALLBACK consents — was SEALED by the caller, before this function's
    # reachability gate (#451) and therefore before any keyboard posts. The two
    # reconcilers run as a pair at every call site, and whichever prompts first
    # opens the complete membership: otherwise a fast Approve on this keyboard
    # could settle a round whose other kind has not registered yet, running the
    # plugin's setup tool while a consent is still open.
    for p in pending:
        try:
            callback_consent.prompt_callback_consent(
                coordinator=authz_grants.CHALLENGES, channel=channel,
                chat_id=chat_id, operator_id=operator_id, acks=acks,
                reconcile_cb=_reconcile_again,
                setup_nonce=nonce_by_identity.get(p["identity"], ""),
                plugin=p["plugin"], artifact_id=p["artifact_id"],
                declared=p["declared"], effective=p["effective"],
                declaration_digest=p["declaration_digest"])
        except Exception:  # noqa: BLE001 — a prompt failure never breaks the
            # mutation; pending_ack stays in health and re-prompts later.
            logger.exception("callback consent prompt failed (plugin=%s)",
                             p.get("plugin"))


def _trigger_pending_for_union(
    *, role_configs: dict, resolver: Any = None,
) -> tuple[bool, list[dict], set]:
    """The peer (TRIGGER) half of the union membership, wrapped.

    The mirror of ``trigger_reconcile._callback_pending_for_union``. The peer
    is documented never to raise, but the wrap is what makes that safe to rely
    on: an escaping exception here would abort the whole callback reconcile,
    which fails the callback overlay CLOSED and 404s every live callback. A
    peer failure must degrade to "seal no verdict this pass" instead."""
    try:
        import trigger_reconcile
        return trigger_reconcile.trigger_pending_for_union(
            role_configs=role_configs, resolver=resolver)
    except Exception:  # noqa: BLE001
        logger.exception("trigger union-member lookup failed")
        return False, [], set()


def _setup_candidates(*, resolver: Any = None) -> tuple[bool, list[dict]]:
    """The setup-obligation candidate sweep, wrapped — see
    :func:`_trigger_pending_for_union` for why the wrap is load-bearing."""
    try:
        import trigger_reconcile
        return trigger_reconcile.setup_candidates(resolver=resolver)
    except Exception:  # noqa: BLE001
        logger.exception("setup-candidate lookup failed")
        return False, []


def callback_pending_for_union(
    *, role_configs: dict, resolver: Any = None,
) -> tuple[bool, list[dict], set]:
    """The pending CALLBACK consents, for the trigger reconciler's union
    sealing. Side-effect free and never raises; it must never break trigger
    prompting.

    Returns ``(ok, pending)``. #451: a failure reports ``ok=False`` rather than
    degrading to ``[]`` — an empty list is a claim that nothing is pending, and
    the caller uses exactly that claim to seal a positive "needs no consent"
    verdict.

    The third element is this kind's ``consent_position_unknown`` set. It travels
    WITH the pending rows because a non-consent gap and the absent pending row it
    causes are two views of one computation; returning only the rows let the peer
    reconciler seal "needs no consent" while blind to this kind's gap."""
    try:
        import trigger_reconcile
        d = compute_desired(role_configs=role_configs, resolver=resolver)
        return True, d.pending, trigger_reconcile.consent_position_unknown(
            d.issues)
    except Exception:  # noqa: BLE001
        logger.exception("callback union-member compute failed")
        return False, [], set()


async def reconcile_from_runtime(runtime: Any, *, prompt: bool = True) -> list:
    """Convenience seam for tools/reload callers holding a CasaRuntime."""
    if runtime is None or getattr(runtime, "trigger_registry", None) is None:
        return []
    return await reconcile_plugin_callbacks(
        trigger_registry=runtime.trigger_registry,
        role_configs=getattr(runtime, "role_configs", None) or {},
        channel_manager=getattr(runtime, "channel_manager", None),
        prompt=prompt)


async def reprompt_pending(
    runtime: Any, *, report: list, acks: Any = None,
    resolver: "Callable[[str | None], Any] | None" = None,
    entries: "Callable[[], list[dict]] | None" = None,
) -> None:
    """#494 — on-demand PROMPT-ONLY repost of pending callback consents.

    The `consent_reprompt` tool's callback half. Deliberately NOT a
    reconcile pass: no overlay swap, no marker writes, no ack prune, no
    ``seal_setup_state`` / ``ensure_obligation`` / ``open_round``, no worker
    kick — whatever the last lifecycle reconcile sealed stays exactly as
    sealed (design rounds 2–3: an on-demand pass that reseals re-opens
    members its own denial filter then refuses to prompt, wedging setup).

    Runs under ``_RECONCILE_LOCK`` so a revoke's post-reconcile
    ``cancel_matching`` provably catches any keyboard registered here (the
    same serialization argument the mutation-time prompts rely on). For each
    pending consent it:

    * skips identities the operator DENIED on a live keyboard
      (:mod:`consent_denials` — agent-driven re-issue must not nag past a
      Deny; mutations/reloads re-prompt as always),
    * re-reads the ack store synchronously (design r3: the worker-thread
      compute can race a concurrent Approve — a freshly-acked identity gets
      no new keyboard),
    * threads the CURRENT open round member's nonce in READ-ONLY
      (:func:`plugin_setup_episodes.open_member_nonce`) so the fresh
      keyboard decides exactly the member the last reconcile sealed,
    * registers the keyboard via the ordinary committing prompt.

    Appends one row per pending consent to ``report``:
    ``{"kind","plugin","name", "status"|"handle"}`` — ``handle`` rows carry
    the coordinator's ``ChallengeHandle`` for the caller to classify via
    ``settled_post()`` AFTER this lock is released. Never raises."""
    import authz_grants
    import callback_consent
    import consent_denials
    import plugin_setup_episodes

    if runtime is None:
        return
    channel_manager = getattr(runtime, "channel_manager", None)
    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None:
        return
    op = callback_consent.operator_identity(channel)
    if op is None:
        return
    chat_id, operator_id = op
    role_configs = getattr(runtime, "role_configs", None) or {}
    acks = acks if acks is not None else _default_acks()

    async def _reconcile_again() -> None:
        # Approve-time reconcile: live-runtime lookup (the event-reconciler
        # discipline) — the tap may land long after this repost.
        import agent as agent_mod
        live = getattr(agent_mod, "active_runtime", None)
        if live is None or getattr(live, "trigger_registry", None) is None:
            return
        await reconcile_plugin_callbacks(
            trigger_registry=live.trigger_registry,
            role_configs=getattr(live, "role_configs", None) or {},
            channel_manager=getattr(live, "channel_manager", None),
            prompt=False, regen_health=True)

    async with _RECONCILE_LOCK:
        try:
            desired = await asyncio.to_thread(
                compute_desired, role_configs=role_configs, acks=acks,
                resolver=resolver, entries=entries)
        except Exception:  # noqa: BLE001 — a compute failure reposts nothing,
            # but must be VISIBLE to the caller (Sol/Terra diff-gate r1: a
            # swallowed compute failure read as "no consent is pending").
            logger.exception("callback reprompt compute failed")
            report.append({"kind": "callback", "plugin": "", "name": "",
                           "status": "error"})
            return
        for p in desired.pending:
            row = {"kind": "callback", "plugin": p["plugin"],
                   "name": p["effective"]}
            if consent_denials.denied(
                    consent_denials.key("callback", p["identity"])):
                report.append(dict(row, status="denied"))
                continue
            if acks.get(p["identity"]) is not None:
                report.append(dict(row, status="already_acked"))
                continue
            nonce = plugin_setup_episodes.open_member_nonce(
                p["plugin"], p["identity"])
            try:
                handle = callback_consent.prompt_callback_consent(
                    coordinator=authz_grants.CHALLENGES, channel=channel,
                    chat_id=chat_id, operator_id=operator_id, acks=acks,
                    reconcile_cb=_reconcile_again, setup_nonce=nonce,
                    plugin=p["plugin"], artifact_id=p["artifact_id"],
                    declared=p["declared"], effective=p["effective"],
                    declaration_digest=p["declaration_digest"])
                report.append(dict(row, handle=handle))
            except Exception:  # noqa: BLE001 — one prompt failure must not
                # abort the remaining rows
                logger.exception("callback reprompt failed (plugin=%s)",
                                 p.get("plugin"))
                report.append(dict(row, status="error"))


def issue_state(resolver: Any = None) -> "IssueState":
    """``(ok, issues, observed)`` — the callback gaps, whether they could be
    computed AT ALL, and which plugins the computation actually saw. The mirror
    of ``trigger_reconcile.issue_state``, which carries the full reasoning for
    why both the flag and the observed set exist.

    Two halves (#453): the DERIVED gaps from :func:`compute_desired`, and the
    APPLIED one — is the marker pair the consumer reads its redirect URI from
    actually published — from :func:`verify_published_markers`. Only the
    reconcile publishes, so a recomputation that skipped the second half
    described a callback as fully live during the window between an approval and
    the write that backs it.

    ``observed`` closes the last way the empty list could lie (#457): ``ok``
    reports whether the computation RAN, not whether it saw every plugin, so a
    plugin an invalid registry — or one unresolvable artifact — dropped out of
    the iteration read as "no gap". A gate must require the plugin to be IN
    ``observed`` before reading the absence of an issue as a verdict about it."""
    try:
        import agent as agent_mod

        runtime = getattr(agent_mod, "active_runtime", None)
        if runtime is None:
            return IssueState(False, [], set())
        role_configs = getattr(runtime, "role_configs", None)
        if not role_configs:
            return IssueState(False, [], set())
        desired = compute_desired(role_configs=role_configs, resolver=resolver)
        verify_published_markers(desired, _default_spool())
        return IssueState(True, desired.issues, desired.observed)
    except Exception:  # noqa: BLE001 — a callback-compute crash must never
        # take down the whole health pass; log and degrade to no extras.
        logger.exception("callback issue recompute failed")
        return IssueState(False, [], set())


def current_issues() -> list:
    """Fresh, side-effect-free callback issues for health regeneration —
    recomputed on EVERY ``_regenerate_plugin_health`` pass so they survive
    unrelated refreshes. Never raises (health must always regenerate). The
    setup gate uses :func:`issue_state` instead — see its docstring.

    #606: an ``ok=False`` degradation used to reach here as ``[]`` — "nothing is
    wrong" — while ingress was shut. It now carries the two unavailable rows.
    """
    return _unavailable_rows() + issue_state()[1]


def _live_registry():
    """The registry the running system actually routes through, or None. The
    tests that pin the health rows below MUST install it here — a registry
    handed only to a reconciler is not the one this consumer reads, and a test
    that does that reports green while measuring nothing."""
    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    return getattr(runtime, "trigger_registry", None) if runtime else None


def _unavailable_rows() -> "list[dict]":
    """#606: the two independent honesty rows, as plain PluginIssue-shaped
    dicts — never PluginIssue instances, so they are concatenated DIRECTLY into
    write_report's issues= and never routed through the attribute-only
    _add()/_rediscoverable() helpers, which would degrade a dict row's fields to
    None instead of raising. Same contract as the event sibling.

    They are separate rows because their clearing predicates are independent.
    ``callback_routing_unavailable`` is an APPLIED-state fact: the live overlay carries no
    authoritative computation, so plugin ingress is shut. ``callback_state_unavailable`` is a
    RECOMPUTATION fact: a fresh compute for this health pass could not run. One
    can be true without the other — a one-shot failure publishes the sentinel
    and then recomputes fine, which is one row, not two.

    The state row is gated on a live runtime WITH role configs, because
    issue_state() legitimately reports ok=False before the runtime is up and
    crying wolf on every boot is how a real row stops being read. Never raises:
    a probe that explodes is treated as unavailable, which is the fail-closed
    direction for a disclosure.
    """
    rows: list = []
    try:
        import trigger_registry as _treg          # noqa: F401  (identity only)
        registry = _live_registry()
        if registry is not None and registry.callback_overlay_unavailable():
            rows.append(_health_row("callback_routing_unavailable"))
    except Exception:  # noqa: BLE001
        logger.exception("callback routing availability probe failed")
        rows.append(_health_row("callback_routing_unavailable"))
    try:
        import agent as agent_mod
        runtime = getattr(agent_mod, "active_runtime", None)
        if runtime is not None and getattr(runtime, "role_configs", None):
            if not issue_state().ok:
                rows.append(_health_row("callback_state_unavailable"))
    except Exception:  # noqa: BLE001
        logger.exception("callback state availability probe failed")
        rows.append(_health_row("callback_state_unavailable"))
    return rows


def _health_row(reason_code: str):
    """A registry-GLOBAL health row (``name="*"``, the established spelling for
    one — see plugin_boot's ``registry_invalid``).

    A ``PluginIssue``, not the plain dict the EVENT sibling emits. That sibling
    uses dicts because its rows would otherwise pass through
    ``_regenerate_plugin_health``'s attribute-only ``_add``/``_rediscoverable``
    helpers, which degrade a dict's fields to None rather than raising. These
    rows do not: this module's issues are concatenated straight into
    ``write_report``'s ``issues=``, and every other row this function's callers
    return is already a ``PluginIssue``. Matching the module's own type keeps a
    consumer that reads ``.reason_code`` working, which a dict would silently
    break."""
    from plugin_registry import PluginIssue
    return PluginIssue(name="*", target=None, stage="callbacks",
                       reason_code=reason_code, artifact_id=None)
