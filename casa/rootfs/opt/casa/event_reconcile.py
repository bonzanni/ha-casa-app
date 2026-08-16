"""The plugin-event reconciler (runtime seam).

The ONE writer of the published event-routing map — the structure the
delivery worker (:mod:`event_episodes`) and the pre-send identity gate read
every pass. Structurally the sibling of :mod:`callback_reconcile` (read that
module whole before touching this one — its idioms are mirrored here,
including the reconcile lock discipline at ``callback_reconcile.py:57/:613``),
with the roles reversed: a callback routes on the CALLBACK-declaring
plugin's own assignment; an event subscription is declared by the
SUBSCRIBER and reaches into the subscriber's own role/target, so consent
(and the routing gate) is evaluated from the subscriber's side.

Semantics (spec §6, decisions 26/27/29/30/36):

* **Complete desired map, atomic swap.** Every reconcile derives the FULL
  set of currently-routable ``(emitter, event) -> {subscriber: snapshot}``
  entries from the CURRENT resolver snapshot and swaps it in one operation.
* **Gates, in order, per subscription.** Intrinsic validity (the
  subscriber's whole ``casa.subscribes`` block must parse —
  ``event_invalid``) -> the referenced emitter is installed AND declares
  that event (``event_emitter_missing``) -> the subscriber itself is
  reachable by a delivery nudge (``event_no_target``, via
  ``callback_reconcile._reachable``) -> an artifact+targets-bound operator
  ack exists for the exact consent identity (``event_pending_ack``).
* **Fail-closed, per-subscriber all-or-nothing.** Any gap in a subscriber's
  whole subscribe set keeps its WHOLE set unrouted (the mirror of
  INV-TRIG-003 / callback's own per-plugin all-or-nothing).
* **Consent binds artifact + targets.** The ack identity
  (:func:`plugin_events.ack_identity`) folds in the subscriber's artifact_id
  and its sorted delivery targets — a routine plugin upgrade OR a
  retargeted assignment mints a new identity, so neither can silently carry
  an old consent forward (decision 17/27, INV-EV-003).
* **Typed sentinel on compute failure (decision 26).** A crashed compute
  publishes :data:`event_spool.ROUTING_UNAVAILABLE`, never an empty map — an
  empty map is an AUTHORITATIVE result that licenses the worker's
  destructive sweep; the sentinel licenses none of it. Routing and full
  sweeping resume the moment a compute succeeds.
* **No spool mutation, ever (decision 12).** This module never touches
  ``/data/events`` — only the worker's single writer thread does.
* **Dispatch-admission-locked revocation (decision 29/36).** Unrouting a
  pair from the published map happens BEFORE its ack record is deleted, and
  both happen under the SAME asyncio lock the worker's pre-send gate and
  dispatch enqueue share (:mod:`event_episodes`'s ``DISPATCH_LOCK``) — a
  revoked route can never be admitted once the revoke call returns.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import event_spool
from callback_reconcile import _reachable

logger = logging.getLogger(__name__)

# Serializes every event-routing writer (boot, plugin lifecycle mutations,
# reload scopes, consent approve, the revoke tool) so each swap derives from
# a self-consistent compute — mirrors callback_reconcile._RECONCILE_LOCK
# exactly, including holding it for the WHOLE compute-swap-prompt sequence
# (never just the swap) so a slower concurrent compute can never publish
# after a newer one already has.
_RECONCILE_LOCK = asyncio.Lock()

_RESIDENT_PREFIX = "resident:"

# The published routing map: dict[(emitter, event), dict[subscriber, snapshot]]
# or event_spool.ROUTING_UNAVAILABLE before the first successful compute (or
# after a compute failure). Never read directly outside this module — use
# get_routed().
_routed: Any = event_spool.ROUTING_UNAVAILABLE


# -- injectable defaults (module functions so tests can monkeypatch) --------


def _default_resolver() -> Callable[["str | None"], Any]:
    """ONE registry snapshot for the whole pass (#454) — see
    ``plugin_registry.pinned_resolver``."""
    import plugin_registry

    return plugin_registry.pinned_resolver()


def _default_entries() -> Callable[[], list[dict]]:
    """The registry ENTRIES seam — assignment authority for a subscriber's
    own delivery targets. Mirrors ``callback_reconcile._default_entries``.

    The LAST-RESORT source only: a pass whose resolver is snapshot-pinned reads
    its entries off that pin instead (:func:`_entries_for`), because a separate
    ``snapshot_registry()`` read is pinned to nothing and can hand the pass a
    different generation's assignment authority (#454)."""
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
    from event_acks import ACKS

    return ACKS


def normalize_targets(targets: Any) -> "list[str]":
    """The ONE canonical targets sanitization feeding the consent identity
    (Minor-8, review round 1): sorted, string-only. Shared by BOTH
    :func:`compute_desired` (the identity computed at reconcile time) and
    :func:`event_episodes._gate_ok` (the identity RECOMPUTED live at
    pre-send) — a byte-for-byte divergence between two independently
    maintained copies of "sanitize a targets list" would make the gate's
    recompute never match a freshly-published, entirely valid snapshot: an
    infinite defer loop indistinguishable from a genuine mismatch."""
    return sorted(t for t in (targets or []) if isinstance(t, str))


def to_spool_shape(routed: Any) -> Any:
    """The spool's narrower routed shape — ``dict[(emitter, event),
    set[subscriber]]`` — derived from the reconciler's published
    per-subscriber-snapshot map. :data:`event_spool.ROUTING_UNAVAILABLE`
    passes through unchanged (the spool's own contract already treats it as
    a strict no-op sentinel; there is nothing here to narrow)."""
    if routed is event_spool.ROUTING_UNAVAILABLE:
        return routed
    return {key: set(subs.keys()) for key, subs in routed.items()}


@dataclass
class DesiredEvents:
    """The pure compute result: what SHOULD route right now.

    ``routed`` values carry the consented snapshot the worker's pre-send
    gate compares against:
    ``{"subscriber", "artifact_id", "targets": sorted[...], "ack_identity"}``.
    """

    routed: "dict[tuple[str, str], dict[str, dict]]" = field(default_factory=dict)
    issues: "list[dict]" = field(default_factory=list)
    # Consent prompts to fire — only for subscriptions whose ONLY gap is the
    # ack.
    consent_needed: "list[dict]" = field(default_factory=list)
    # False iff the resolver's registry snapshot itself was invalid — a
    # FAILURE TO KNOW, never a computed empty (Critical-1, review round 1).
    # ``routed`` is always {} in that case, but it must NEVER be treated as
    # the authoritative empty map: reconcile_plugin_events publishes the
    # ROUTING_UNAVAILABLE sentinel instead, exactly as it does for a raised
    # compute exception, so the worker's destructive sweep never runs
    # against a registry casa could not even read.
    registry_valid: bool = True
    # Adjudication-f (review round 1): every ack identity still computable
    # from a CURRENTLY INSTALLED subscriber's declaration — the prune's
    # keep-set. Populated for EVERY subscribe entry before any gate
    # (self-check, emitter-check, reachable, ack) is applied, mirroring
    # callback_reconcile's own "about the declaration existing, not about
    # routing" discipline: a gap in ONE gate must not make the operator's
    # still-current consent for that exact subscription look stale.
    valid_identities: "set[str]" = field(default_factory=set)
    # Whether this pass is trustworthy enough to prune AT ALL: False unless
    # the resolver reported zero issues AND every subscriber's own
    # casa.subscribes parsed — an unparseable declaration or a resolution
    # hiccup must never make a still-current consent look stale.
    prunable: bool = False

    def to_spool_shape(self) -> Any:
        return to_spool_shape(self.routed)


def _issue(name: "str | None", reason_code: str,
          artifact_id: "str | None" = None) -> dict:
    return {"name": name, "target": None, "stage": "events",
            "reason_code": reason_code, "artifact_id": artifact_id}


def _is_self_subscription(subscriber_name: str, subscriber_manifest_name: str,
                          emitter: str) -> bool:
    """Refuse a subscription naming its OWN subscriber plugin as the
    emitter, under EITHER spelling: the plain registry name and the (owned
    entry's) manifest name. ``plugin_events.parse_and_validate_subscribes``
    only compares against the single ``plugin_name`` it was called with at
    PARSE time (the subscriber's unscoped manifest name) — a bundled
    dependency naming itself via its own SCOPED registry name would slip
    past that check (Task 2 review finding). Re-checked here against BOTH
    spellings of the LIVE resolved subscriber identity."""
    return emitter == subscriber_name or emitter == subscriber_manifest_name


def compute_desired(
    *, role_configs: dict, acks: Any = None,
    resolver: "Callable[[str | None], Any] | None" = None,
    entries: "Callable[[], list[dict]] | None" = None,
) -> DesiredEvents:
    """Side-effect-free derivation of the complete desired routing map +
    the contextual event issues. Never raises for bad plugin data (a
    per-subscriber manifest read failure is recorded as ``event_invalid``
    and that subscriber's whole set is skipped); a bad REGISTRY (invalid
    snapshot) is the one case this returns an empty, non-routing result —
    the caller (:func:`reconcile_plugin_events`) treats an EXCEPTION from
    this function as the fail-closed trigger, never this function's own
    return value."""
    import plugin_store
    from plugin_events import ack_identity

    acks = acks if acks is not None else _default_acks()
    resolver = resolver if resolver is not None else _default_resolver()
    entries = entries if entries is not None else _entries_for(resolver)

    out = DesiredEvents()
    all_res = resolver(None)
    if not getattr(all_res, "registry_valid", False):
        # A failure to KNOW, not a computed empty (Critical-1): the caller
        # must publish the sentinel, never this {} as an authoritative
        # result (its own registry-stage issues surface via the resolver /
        # health pass regardless).
        out.registry_valid = False
        return out
    # Opportunistic-prune availability gate (adjudication-f): a resolution
    # hiccup on ANY plugin (registry-stage issues, not just a subscriber's
    # own) must suppress pruning for the WHOLE pass — a membership set
    # derived from a partially-failed resolve would drop a still-current
    # consent. Narrowed to False below the moment any SUBSCRIBER's own
    # declaration fails to parse.
    out.prunable = not list(getattr(all_res, "issues", ()) or ())

    # Assignment authority for a SUBSCRIBER's own delivery targets, read
    # once from the same snapshot the resolver reads.
    targets_by_name: dict[str, list] = {}
    for entry in entries():
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            targets_by_name[entry["name"]] = list(entry.get("targets") or [])
    live_roles = {f"{_RESIDENT_PREFIX}{role}" for role in role_configs}

    plugins = list(all_res.plugins)
    by_name = {rp.name: rp for rp in plugins}

    # Every emitter's OWN currently-declared event names. A read failure on
    # one emitter's manifest contributes nothing here (never crashes the
    # pass) — any subscriber referencing it correctly falls through to
    # event_emitter_missing below, exactly as an uninstalled emitter would.
    declared_events: dict[str, set[str]] = {}
    for rp in plugins:
        try:
            emits = plugin_store.manifest_emits(rp.manifest, rp.name)
        except Exception:  # noqa: BLE001 — a bad EMITTER declaration is that
            # emitter's own concern; it must never abort the whole pass.
            continue
        declared_events[rp.name] = {e["declared"] for e in emits}

    for rp in plugins:
        subscriber = rp.name
        try:
            subs = plugin_store.manifest_subscribes(rp.manifest, subscriber)
        except Exception:  # noqa: BLE001 — StoreError("subscribes_invalid"),
            # or any other read failure on a pre-published artifact: a state
            # to SURFACE, never a reconcile crash. An unparseable
            # declaration contributes NO identities, so pruning this pass
            # would destroy the operator's consent for this subscriber's
            # still-valid subscriptions — suppress the whole prune until a
            # pass that can read it.
            out.issues.append(_issue(subscriber, "event_invalid",
                                     rp.artifact_id))
            out.prunable = False
            continue
        if not subs:
            continue

        subscriber_targets = normalize_targets(targets_by_name.get(subscriber))
        subscriber_dark = False
        no_target_reported = False
        entries_for_subscriber: dict[tuple[str, str], dict] = {}

        for sub_entry in subs:
            emitter = sub_entry["plugin"]
            event = sub_entry["event"]
            digest = sub_entry["digest"]

            # The prune's keep-set is about the DECLARATION existing, not
            # about routing (adjudication-f): collected BEFORE any later
            # gate, so a gap in ONE gate (self-check, missing emitter,
            # unreachable, pending ack) can never make the operator's
            # still-current consent for THIS exact subscription look stale.
            out.valid_identities.add(ack_identity(
                subscriber, rp.artifact_id, emitter, event, digest,
                subscriber_targets))

            if _is_self_subscription(subscriber, rp.manifest_name, emitter):
                out.issues.append(_issue(subscriber, "event_invalid",
                                         rp.artifact_id))
                subscriber_dark = True
                continue

            emitter_rp = by_name.get(emitter)
            if emitter_rp is None or event not in declared_events.get(
                    emitter, set()):
                out.issues.append(_issue(subscriber, "event_emitter_missing",
                                         rp.artifact_id))
                subscriber_dark = True
                continue

            if not _reachable(targets_by_name.get(subscriber) or [],
                              live_roles):
                # Once per SUBSCRIBER, not per subscription (Minor-7,
                # review round 1) — reachability never varies across a
                # subscriber's own subscribe entries.
                if not no_target_reported:
                    out.issues.append(_issue(subscriber, "event_no_target",
                                             rp.artifact_id))
                    no_target_reported = True
                subscriber_dark = True
                continue

            identity = ack_identity(subscriber, rp.artifact_id, emitter,
                                    event, digest, subscriber_targets)
            if acks.get(identity) is None:
                out.issues.append(_issue(subscriber, "event_pending_ack",
                                         rp.artifact_id))
                out.consent_needed.append({
                    "subscriber": subscriber, "artifact_id": rp.artifact_id,
                    "emitter": emitter, "event": event, "digest": digest,
                    "targets": subscriber_targets, "identity": identity})
                subscriber_dark = True
                continue

            entries_for_subscriber[(emitter, event)] = {
                "subscriber": subscriber, "artifact_id": rp.artifact_id,
                "targets": subscriber_targets, "ack_identity": identity}

        if subscriber_dark:
            # Per-subscriber all-or-nothing: any gap unroutes the WHOLE set.
            continue
        for key, snapshot in entries_for_subscriber.items():
            out.routed.setdefault(key, {})[subscriber] = snapshot

    return out


# ---------------------------------------------------------------------------
# the published map + revocation
# ---------------------------------------------------------------------------


def get_routed() -> Any:
    """The reconciler's published routing map — ``dict[(emitter, event),
    dict[subscriber, snapshot]]`` or :data:`event_spool.ROUTING_UNAVAILABLE`.
    Every consumer (the worker's fold/sweep/pre-send gate) must handle the
    sentinel explicitly (decision 26)."""
    return _routed


def _unroute_locked(subscriber: str, emitter: str = "", event: str = "") -> None:
    """Remove *subscriber* from the published map — from every routed pair
    when neither ``emitter`` nor ``event`` is given, else from exactly the
    one named pair. MUST be called with ``event_episodes.DISPATCH_LOCK``
    already held (the caller's responsibility, via
    :func:`revoke_and_unroute`) so the worker's pre-send gate can never
    observe a stale membership between this write and the ack-store revoke
    that follows it (decision 29/36). A published
    :data:`event_spool.ROUTING_UNAVAILABLE` has nothing to unroute."""
    global _routed
    if _routed is event_spool.ROUTING_UNAVAILABLE:
        return
    if emitter and event:
        keys = [(emitter, event)] if (emitter, event) in _routed else []
    else:
        keys = list(_routed.keys())
    if not keys:
        return
    new_routed = dict(_routed)
    changed = False
    for key in keys:
        subs = new_routed.get(key)
        if subs and subscriber in subs:
            subs = dict(subs)
            del subs[subscriber]
            new_routed[key] = subs
            changed = True
    if changed:
        _routed = new_routed


async def revoke_and_unroute(subscriber: str, emitter: str = "",
                             event: str = "", *, acks: Any = None,
                             ) -> "list[dict]":
    """Unroute *subscriber* from the published map, THEN drop its
    :class:`event_acks.EventAckStore` record(s).

    Takes ``_RECONCILE_LOCK`` (outer) THEN ``event_episodes.DISPATCH_LOCK``
    (inner) — two locks, two reasons (Important-3, review round 1):
    ``_routed`` has two writers (this function and
    :func:`reconcile_plugin_events`), each previously serialized only
    against ITS OWN kind, so a revoke racing an in-flight compute could
    unroute-then-have-the-compute's-later-publish silently REPUBLISH the
    just-revoked pair (the compute read the acks store before the revoke's
    delete landed). Taking ``_RECONCILE_LOCK`` first makes a revoke and a
    reconcile's entire compute-and-swap fully serialize — a revoke started
    mid-compute waits for that compute's publish to complete, then
    unroutes what it just published; a revoke started first makes the next
    reconcile's compute observe the already-revoked ack. Either order
    converges on the pair excluded once both complete. ``DISPATCH_LOCK``
    (inner) is still what the worker's pre-send gate and dispatch enqueue
    share, so a concurrent dispatch can never be admitted between the
    unroute and the ack delete (decision 29/36, Sol-r4 #2 / Sol-r5 #2).
    Lock graph stays deadlock-free: the worker takes only ``DISPATCH_LOCK``
    and :func:`reconcile_plugin_events` takes only ``_RECONCILE_LOCK`` —
    neither ever takes the other's lock, so only this function ever nests
    them, always in the same order.

    Drops every ack for *subscriber* when ``emitter``/``event`` are both
    empty, else exactly the one ``(subscriber, emitter, event)``
    subscription. Returns the removed ack records
    (:class:`event_acks.EventAckStore`'s own list[dict] shape)."""
    acks = acks if acks is not None else _default_acks()
    import event_episodes

    async with _RECONCILE_LOCK:
        async with event_episodes.DISPATCH_LOCK:
            _unroute_locked(subscriber, emitter, event)
            if emitter and event:
                removed = await asyncio.to_thread(
                    acks.revoke_pair, subscriber, emitter, event)
            else:
                removed = await asyncio.to_thread(
                    acks.revoke_subscriber, subscriber)
    return removed


async def _regen_health_safe() -> None:
    """Regenerate the plugin-health report (no operator notify) so a just-acked
    subscription's stale ``event_pending_ack`` clears immediately instead of
    lingering until the next plugin mutation, reload or boot (#582).
    ``current_issues()`` recomputes fresh from the persisted acks + resolver, so
    the routed subscription drops out of the report. Never raises — a health
    refresh must not break the reconcile.

    Runs under ``tools._plugin_tools_guard()`` (Sol/Terra design r1): the report
    LOCK serializes the write, not the computation that precedes it, so a pass
    that started before a concurrent plugin mutation committed would otherwise
    write its older result last and delete the row that mutation just added —
    reproduced against the real writer, and nothing schedules another
    regeneration to repair it. The guard is taken only AFTER
    ``_RECONCILE_LOCK`` has been released, so no task ever holds the reconcile
    lock while waiting for the plugin-tools lock.
    """
    try:
        import tools
        async with tools._plugin_tools_guard():
            await asyncio.to_thread(tools._regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001
        logger.warning("post-consent plugin-health regen failed", exc_info=True)


def _kick_worker() -> None:
    try:
        import event_episodes
        event_episodes.kick_all()
    except Exception:  # noqa: BLE001 — never break a reconcile on this
        logger.warning("event-episodes kick failed", exc_info=True)


def kick() -> None:
    """Fire-and-forget: schedule a reconcile pass off the LIVE runtime
    (mirrors ``plugin_setup_episodes.kick()``'s wake-the-worker idiom, but
    this facility has no persistent background reconcile loop to wake — so
    this schedules the recompute itself). Used by the worker's pre-send
    gate on a consent-identity mismatch (imported lazily there to avoid a
    cycle). Never raises; a missing/unbound runtime is a silent no-op — the
    next real lifecycle mutation reconciles normally."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_kick_reconcile())


async def _kick_reconcile() -> None:
    try:
        import agent as agent_mod
        runtime = getattr(agent_mod, "active_runtime", None)
        if runtime is None:
            return
        await reconcile_plugin_events(runtime, prompt=False)
    except Exception:  # noqa: BLE001 — a background kick must never raise
        logger.exception("event reconcile kick failed")


# ---------------------------------------------------------------------------
# consent prompts
# ---------------------------------------------------------------------------


def _fire_consent_prompts(pending: "list[dict]", *, role_configs: dict,
                          channel_manager: Any, acks: Any,
                          resolver: Any, entries: Any) -> None:
    import authz_grants
    import event_consent

    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None:
        return  # no DM reachable — pending_ack stands; re-prompted later
    op = event_consent.operator_identity(channel)
    if op is None:
        return
    chat_id, operator_id = op

    async def _reconcile_again() -> None:
        # SOL-P2a: the reconcile fired at COMMIT time (when the operator
        # taps Approve, possibly minutes/hours after this prompt was
        # posted) must re-derive role_configs/channel_manager from the
        # LIVE runtime, never reuse the snapshot captured HERE at prompt
        # time — a role removed or reassigned in between must be
        # reflected, not silently overridden by a stale one that could
        # republish a route to a role that no longer exists. Mirrors
        # _kick_reconcile's live-runtime lookup exactly. `resolver`/
        # `entries` stay as originally supplied (production callers never
        # pass non-default ones; only tests inject fakes, which must
        # still see their own doubles on retry).
        import agent as agent_mod
        live_runtime = getattr(agent_mod, "active_runtime", None)
        await reconcile_plugin_events(
            live_runtime, acks=acks, resolver=resolver, entries=entries,
            prompt=False, regen_health=True)

    for p in pending:
        try:
            event_consent.prompt_event_consent(
                coordinator=authz_grants.CHALLENGES, channel=channel,
                chat_id=chat_id, operator_id=operator_id,
                subscriber=p["subscriber"], artifact_id=p["artifact_id"],
                emitter=p["emitter"], event=p["event"], digest=p["digest"],
                targets=p["targets"], acks=acks, reconcile_cb=_reconcile_again)
        except Exception:  # noqa: BLE001 — a prompt failure never breaks the
            # reconcile; pending_ack stays in health and re-prompts later.
            logger.exception("event consent prompt failed (subscriber=%s)",
                             p.get("subscriber"))


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


async def reconcile_plugin_events(
    runtime: Any, *, role_configs: "dict | None" = None,
    channel_manager: Any = None, acks: Any = None,
    resolver: "Callable[[str | None], Any] | None" = None,
    entries: "Callable[[], list[dict]] | None" = None,
    prompt: bool = True, regen_health: bool = False,
) -> "list[dict]":
    """Compute + publish under ONE reconcile lock (mirrors
    ``callback_reconcile.py:613`` exactly, including holding the lock for
    the WHOLE compute-swap-prompt sequence so a slower concurrent compute
    can never publish after a newer one already has).

    ``runtime`` supplies ``role_configs``/``channel_manager`` when the
    explicit keyword overrides are not given. Boot
    (``casa_core._boot_reconcile_plugin_events``) is the one caller that
    passes ``runtime=None`` with explicit ``role_configs``/
    ``channel_manager`` overrides — a snapshot captured once, evaluated at
    boot. Every OTHER caller instead passes the LIVE ``runtime``
    positionally, re-derived from ``agent.active_runtime`` at call time
    (SOL-P2a): :func:`kick`'s ``_kick_reconcile``, and
    :func:`_fire_consent_prompts`'s ``reconcile_cb``, which used to
    capture ``role_configs``/``channel_manager`` at PROMPT time the same
    way boot does — a role removed or reassigned between the prompt and
    the operator's tap would then be invisible to the reconcile it fires.
    Both now read ``role_configs``/``channel_manager`` fresh off the live
    runtime instead of a snapshot captured earlier.

    On a compute failure: publish the typed sentinel
    :data:`event_spool.ROUTING_UNAVAILABLE` (never an empty map — decision
    26), kick the worker so it re-evaluates under the sentinel, then
    propagate the exception so the caller logs/surfaces it. No spool
    mutation happens here, ever (decision 12).

    ``regen_health`` (set by the consent-approve reconciles, #582) rewrites
    plugin-health after the pass so a freshly-acked subscription's stale
    ``event_pending_ack`` clears at once. The mutation/boot/reload/revoke paths
    leave it False — they regenerate health themselves — so there is no
    double-regen. It fires on the FAILURE path too (Sol design r1): the ack is
    already durable when this runs, so a compute failure would otherwise leave
    the report saying "waiting for your approval" while the consent DM says
    delivery could not be started."""
    if role_configs is None:
        role_configs = getattr(runtime, "role_configs", None) or {}
    if channel_manager is None:
        channel_manager = getattr(runtime, "channel_manager", None)
    acks = acks if acks is not None else _default_acks()

    def _compute() -> DesiredEvents:
        return compute_desired(role_configs=role_configs, acks=acks,
                               resolver=resolver, entries=entries)

    async def _locked_pass() -> "list[dict]":
        global _routed
        async with _RECONCILE_LOCK:
            try:
                desired = await asyncio.to_thread(_compute)
            except Exception:
                # Fail closed to the SENTINEL, never an empty map (decision 26)
                # — an empty map is an authoritative result that would license
                # the worker's destructive sweep.
                _routed = event_spool.ROUTING_UNAVAILABLE
                _kick_worker()
                raise

            if not desired.registry_valid:
                # Same fail-closed treatment as a raised exception (Critical-1)
                # — an invalid registry snapshot is a FAILURE TO KNOW, not a
                # computed empty, so it must never license the destructive
                # sweep an authoritative {} would. Unlike the exception branch
                # this is a normal (non-crash) outcome, so it does not raise —
                # callers read ``desired.issues`` as usual.
                _routed = event_spool.ROUTING_UNAVAILABLE
                _kick_worker()
                return desired.issues

            _routed = desired.routed

            if desired.prunable:
                # Opportunistic prune (adjudication-f): only on a pass trusted
                # enough to know the COMPLETE keep-set (decision mirrors
                # callback_reconcile's own valid_identities/prunable
                # suppression) — an ack whose identity no installed
                # subscriber's declaration can still compute is stale.
                try:
                    removed = await asyncio.to_thread(
                        acks.prune_stale, desired.valid_identities)
                    if removed:
                        logger.info("pruned %d stale event ack(s)", len(removed))
                except Exception:  # noqa: BLE001 — an opportunistic prune must
                    # never break the reconcile; the next pass retries.
                    logger.warning("event ack prune failed", exc_info=True)

            if prompt and desired.consent_needed:
                _fire_consent_prompts(
                    desired.consent_needed, role_configs=role_configs,
                    channel_manager=channel_manager, acks=acks,
                    resolver=resolver, entries=entries)

            _kick_worker()

        return desired.issues

    try:
        return await _locked_pass()
    finally:
        # OUTSIDE the reconcile lock, and on BOTH paths. The lock order is the
        # reason this cannot sit inside: `_regen_health_safe` takes the
        # plugin-tools guard, and a task holding `_RECONCILE_LOCK` while
        # waiting for that guard would invert the order a plugin mutation
        # takes them in.
        if regen_health:
            await _regen_health_safe()


async def reprompt_pending(
    runtime: Any, *, report: list, acks: Any = None,
    resolver: "Callable[[str | None], Any] | None" = None,
    entries: "Callable[[], list[dict]] | None" = None,
) -> None:
    """#494 — on-demand PROMPT-ONLY repost of pending event-subscription
    consents.

    The event half of the `consent_reprompt` tool; see
    ``callback_reconcile.reprompt_pending`` for the full contract (this is
    its structural mirror: prompt-only — no routing-map publish, no ack
    prune, no worker kick — under ``_RECONCILE_LOCK``, with the
    denial-registry skip and the synchronous ack re-read). Event consents
    live OUTSIDE the plugin-setup round ledger, so there is no nonce to
    thread. Appends ``{"kind","plugin","name","status"|"handle"}`` rows to
    ``report``. Never raises."""
    import authz_grants
    import consent_denials
    import event_consent

    if runtime is None:
        return
    channel_manager = getattr(runtime, "channel_manager", None)
    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None:
        return
    op = event_consent.operator_identity(channel)
    if op is None:
        return
    chat_id, operator_id = op
    role_configs = getattr(runtime, "role_configs", None) or {}
    acks = acks if acks is not None else _default_acks()

    async def _reconcile_again() -> None:
        # SOL-P2a discipline: live-runtime lookup at approve time.
        import agent as agent_mod
        live = getattr(agent_mod, "active_runtime", None)
        if live is None:
            return
        await reconcile_plugin_events(live, prompt=False, regen_health=True)

    async with _RECONCILE_LOCK:
        try:
            desired = await asyncio.to_thread(
                compute_desired, role_configs=role_configs, acks=acks,
                resolver=resolver, entries=entries)
        except Exception:  # noqa: BLE001 — a compute failure reposts nothing,
            # but must be VISIBLE to the caller (Sol/Terra diff-gate r1: a
            # swallowed compute failure read as "no consent is pending").
            logger.exception("event reprompt compute failed")
            report.append({"kind": "event", "plugin": "", "name": "",
                           "status": "error"})
            return
        for p in desired.consent_needed:
            row = {"kind": "event", "plugin": p["subscriber"],
                   "name": f"{p['emitter']}:{p['event']}"}
            if consent_denials.denied(
                    consent_denials.key("event", p["identity"])):
                report.append(dict(row, status="denied"))
                continue
            if acks.get(p["identity"]) is not None:
                report.append(dict(row, status="already_acked"))
                continue
            try:
                handle = event_consent.prompt_event_consent(
                    coordinator=authz_grants.CHALLENGES, channel=channel,
                    chat_id=chat_id, operator_id=operator_id,
                    subscriber=p["subscriber"], artifact_id=p["artifact_id"],
                    emitter=p["emitter"], event=p["event"],
                    digest=p["digest"], targets=p["targets"], acks=acks,
                    reconcile_cb=_reconcile_again)
                report.append(dict(row, handle=handle))
            except Exception:  # noqa: BLE001 — one prompt failure must not
                # abort the remaining rows
                logger.exception("event reprompt failed (subscriber=%s)",
                                 p.get("subscriber"))
                report.append(dict(row, status="error"))


def current_issues() -> "list[dict]":
    """Fresh, side-effect-free event issues for health regeneration —
    recomputed on EVERY health pass so they survive unrelated refreshes
    (mirrors ``callback_reconcile.current_issues()``). Includes the live
    ``event_spool.spool_issues()`` passthrough, remapped to this module's
    issue-dict shape. Never raises.

    Important-5c (review round 1): while the PUBLISHED map is the
    :data:`event_spool.ROUTING_UNAVAILABLE` sentinel, this surfaces one
    ``event_routing_unavailable`` row — otherwise a stuck sentinel (a
    reconcile that keeps failing, or one that has simply never run yet)
    is invisible to the operator: no dispatch happens and no OTHER issue
    code describes "routing is currently unknown" on its own.

    Task 10 wiring note (Minor-10, review round 1): every row here is a
    plain ``dict`` with the PluginIssue-shaped key set (``name``,
    ``target``, ``stage``, ``reason_code``, ``artifact_id``) — never a
    ``PluginIssue`` instance. When ``tools._regenerate_plugin_health``
    eventually merges this in, concatenate the list DIRECTLY into
    ``plugin_health.write_report``'s ``issues=`` (exactly like
    ``trigger_issues``/``callback_issues`` already do) — never route it
    through that function's ``_add``/``_rediscoverable`` helpers, which
    read ``PluginIssue`` ATTRIBUTES only; a ``getattr(a_dict, "stage",
    None)`` on one of these rows silently degrades to ``None`` instead of
    raising, so a wrong integration would misfile rather than crash.
    ``plugin_health.write_report``'s own ``fingerprint``/``_issue_dict``
    already handle either shape correctly (dict-vs-attribute dual
    accessor) — proven end-to-end by
    ``test_event_current_issues_shape_is_health_report_compatible`` in
    ``tests/test_tools_ack_event.py``. Wiring it in also touches every
    OTHER test that calls ``_regenerate_plugin_health`` without mocking
    this function (a stuck sentinel contributes a surprise
    ``event_routing_unavailable`` row) — budget that audit into Task 10,
    not this review round."""
    issues: list = []
    if _routed is event_spool.ROUTING_UNAVAILABLE:
        issues.append(_issue(None, "event_routing_unavailable"))
    try:
        import agent as agent_mod

        runtime = getattr(agent_mod, "active_runtime", None)
        role_configs = getattr(runtime, "role_configs", None) if runtime else None
        if role_configs:
            issues.extend(compute_desired(role_configs=role_configs).issues)
    except Exception:  # noqa: BLE001 — a compute crash must never take down
        # the whole health pass.
        logger.exception("event issue recompute failed")
    try:
        for si in event_spool.spool_issues():
            issues.append(_issue(si.get("emitter"), "event_spool_issue"))
    except Exception:  # noqa: BLE001
        logger.exception("event spool issue recompute failed")
    return issues
