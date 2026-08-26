"""Release B — the plugin-trigger reconciler (runtime seam).

The ONE writer of :class:`trigger_registry.TriggerRegistry`'s plugin overlay.
Wired into: casa_core boot (after resident triggers register), every plugin
lifecycle mutation (``tools._reload_and_verify_targets``, reconcile-LAST
after verify), every trigger-affecting reload scope (``reload.dispatch``),
the consent approve path, and the ``trigger_ack_revoke`` tool. All entry
points serialize on ``_RECONCILE_LOCK``.

Semantics (spec §2 Release B, r2):

* **Complete desired overlay, atomic swap.** Every reconcile derives the FULL
  set of routable plugin triggers from the CURRENT resolver snapshot and
  swaps it in one operation — a removed / unresolved / revoked / corrupt
  plugin's ingress is swept by absence (handler 404s), and readers never see
  a partial overlay.
* **Assignment authority is target-scoped.** A plugin trigger routes to
  ``resident:<role>`` ONLY when target-scoped resolution
  (``plugin_registry.resolve_for``) includes that plugin for that target —
  unassigned / specialist-only plugins route nothing.
* **Fail-closed, per-plugin all-or-nothing.** A plugin routes only when EVERY
  declared trigger is intrinsically valid, targets an existing resident that
  declares the ``webhook`` channel, is assigned, has its secret backing
  (global ``WEBHOOK_SECRET`` for ``hmac_body``), and carries an operator
  consent ack for its full identity. Any gap ⇒ the plugin's whole set is
  unrouted plus a ``stage="triggers"`` ``PluginIssue``.
* **Eager secrets.** Casa-owned per-trigger secrets (``static_header`` /
  ``timestamped_hmac``) are minted at reconcile time — BEFORE any traffic —
  so the plugin's setup tool can read
  ``/data/webhook_secrets/plg-<plugin>--<name>`` right after consent.
* **Recomputable health.** :func:`current_issues` recomputes the contextual
  trigger issues fresh from the live runtime — folded into EVERY
  ``_regenerate_plugin_health`` pass so an unrelated health refresh can never
  erase ``trigger_pending_ack``/``trigger_channel_missing``. Prompting is a
  separate side effect of :func:`reconcile_plugin_triggers` only.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple

import plugin_triggers
# Aliased: the reconcile functions take a `trigger_registry` INSTANCE
# parameter that shadows the module name (#606 needs the module's
# ROUTING_UNAVAILABLE sentinel from inside them).
import trigger_registry as trigger_registry_mod
import webhook_auth
from plugin_triggers import ack_identity

logger = logging.getLogger(__name__)

SECRETS_DIR = Path("/data/webhook_secrets")
_GLOBAL_SECRET_PATH = Path("/data/webhook_secret")

# Serializes every overlay writer (boot, mutations, reloads, consent approve,
# revoke) so each swap derives from a self-consistent compute.
_RECONCILE_LOCK = asyncio.Lock()

# Per-trigger auth modes backed by a casa-minted per-trigger secret file.
# ONE definition, owned by the module that owns secret semantics (#609) —
# the resident mint reads the same tuple, so the two halves cannot drift.
_PER_TRIGGER_SECRET_MODES = webhook_auth.PER_TRIGGER_SECRET_MODES


# -- injectable defaults (module functions so tests can monkeypatch) ---------


def _default_resolver() -> Callable[[str | None], Any]:
    """ONE registry snapshot for the whole pass (#454).

    Built fresh per pass — every call site resolves this immediately before
    computing — so the pin covers exactly one pass and never staleness beyond
    it. See ``plugin_registry.pinned_resolver`` for why the per-target re-read
    it replaces could compose two generations into one overlay."""
    import plugin_registry

    return plugin_registry.pinned_resolver()


def _default_acks() -> Any:
    from trigger_acks import ACKS

    return ACKS


def pin_resolver(resolver: "Callable[[str | None], Any]") -> Any:
    """Wrap a resolver so every target resolves ONCE per pass, and every helper
    in that pass sees the SAME snapshot.

    Sharing a resolver *callable* does not pin anything: each call re-resolves,
    so `compute_desired` can observe artifact A while a concurrent
    `reload_snapshot` publishes B and :func:`setup_candidates` then reports B.
    Sealing over a mixed pair is not a cosmetic inconsistency — rounds are keyed
    by PLUGIN, so a `(plugin, B)` entry overwrites `(plugin, A)`, and a
    zero-member B round then releases setup while B's consent is still
    unapproved. Pin the pass instead.

    The caller passes an ALREADY-RESOLVED resolver: this helper must never
    substitute a default, because each reconciler has its OWN
    ``_default_resolver`` and picking this module's would resolve the callback
    reconciler's pass through the trigger module's seam.
    """
    cache: dict = {}
    gens: set = set()

    def _pinned(target: "str | None") -> Any:
        if target not in cache:
            res = resolver(target)
            cache[target] = res
            gens.add(int(getattr(res, "generation", 0) or 0))
        return cache[target]

    # Caching per target is NOT the same as observing one snapshot: an UNPINNED
    # resolver publishing a new generation between two targets still yields a
    # mixed pass. Record the generations so a consumer whose correctness depends
    # on ONE registry can refuse to act (see :func:`one_generation`) rather than
    # compose two. Since #454 the module defaults are snapshot-pinned, so a
    # production pass cannot drift here; this stays as the safety net for an
    # injected seam that carries no snapshot of its own.
    _pinned.generations = gens          # type: ignore[attr-defined]
    # #454: carry the underlying pin's registry ENTRIES accessor through. The
    # callback and event reconcilers read assignment authority through that
    # seam, and it must describe the same snapshot this pass resolves against.
    _entries = getattr(resolver, "entries", None)
    if callable(_entries):
        _pinned.entries = _entries      # type: ignore[attr-defined]
    # ...and its generation, so a reader asking a provably-pinned pass which
    # registry it describes does not get None from the wrapper.
    _pinned.generation = getattr(resolver, "generation", None)  # type: ignore[attr-defined]
    return _pinned


def _default_global_secret_ok() -> Callable[[], bool]:
    def ok() -> bool:
        if os.environ.get("WEBHOOK_SECRET", ""):
            return True
        try:
            return bool(_GLOBAL_SECRET_PATH.read_text(encoding="utf-8").strip())
        except OSError:
            return False

    return ok


@dataclass
class DesiredTriggers:
    """The pure compute result: what SHOULD route right now."""

    overlay: dict[str, dict] = field(default_factory=dict)
    issues: list = field(default_factory=list)
    # Consent prompts to fire — only for triggers whose ONLY gap is the ack
    # (approving a trigger that still could not route is a broken promise).
    pending: list[dict] = field(default_factory=list)
    # #457: the plugins this computation actually SAW — every name in the
    # pinned resolution, whether or not it declares a trigger. A consumer that
    # reads "no issue names this plugin" as "this plugin has no gap" needs the
    # positive half too: an invalid registry, or one artifact that fails to
    # resolve, drops a plugin out of the iteration entirely and no issue row can
    # ever name it. Empty under an invalid registry, which is what makes the
    # fail-closed return above readable as such by a gate.
    observed: set[str] = field(default_factory=set)
    # #606: did an AUTHORITATIVE computation produce this result? False until
    # the invalid-registry fail-closed return below has been passed. Without
    # it, an unreadable registry returns NORMALLY with an empty overlay and the
    # reconciler's success branch publishes `{}` — a positive claim that
    # nothing should route, made by a pass that read nothing.
    registry_valid: bool = False


def compute_desired(
    *, role_configs: dict, acks: Any = None,
    resolver: "Callable[[str | None], Any] | None" = None,
    global_secret_ok: "Callable[[], bool] | None" = None,
) -> DesiredTriggers:
    """Side-effect-free derivation of the complete desired plugin overlay +
    the contextual trigger issues. Never raises for bad plugin data."""
    from plugin_registry import PluginIssue

    acks = acks if acks is not None else _default_acks()
    resolver = resolver if resolver is not None else _default_resolver()
    global_secret_ok = (global_secret_ok if global_secret_ok is not None
                        else _default_global_secret_ok())

    out = DesiredTriggers()
    all_res = resolver(None)
    if not getattr(all_res, "registry_valid", False):
        # Fail-closed: an invalid registry routes NO plugin ingress (its own
        # registry-stage issues surface via the resolver / health pass).
        return out
    out.registry_valid = True
    out.observed = {rp.name for rp in all_res.plugins}

    # Assignment authority (target-scoped): plugin p may route to
    # resident:<role> only when resolve_for("resident:<role>") includes it.
    assigned: dict[str, set[str]] = {}
    for role in role_configs:
        res = resolver(f"resident:{role}")
        assigned[role] = ({rp.name for rp in res.plugins}
                          if getattr(res, "registry_valid", False) else set())

    for rp in all_res.plugins:
        triggers, errs = plugin_triggers.parse_and_validate(rp.name, rp.manifest)
        if errs:
            # Intrinsically invalid declaration (pre-published artifacts can
            # carry one — the publish gate is younger than the store).
            out.issues.append(PluginIssue(
                name=rp.name, target=None, stage="triggers",
                reason_code="trigger_invalid", artifact_id=rp.artifact_id))
            continue
        if not triggers:
            continue

        entries: dict[str, dict] = {}
        plugin_pending: list[dict] = []
        nonconsent_gap = False
        for t in triggers:
            target = t["target"]
            role = target.partition(":")[2]
            cfg = role_configs.get(role)
            if cfg is None or "webhook" not in (getattr(cfg, "channels", None) or []):
                out.issues.append(PluginIssue(
                    name=rp.name, target=target, stage="triggers",
                    reason_code="trigger_channel_missing",
                    artifact_id=rp.artifact_id))
                nonconsent_gap = True
                continue
            if rp.name not in assigned.get(role, set()):
                out.issues.append(PluginIssue(
                    name=rp.name, target=target, stage="triggers",
                    reason_code="trigger_unassigned_target",
                    artifact_id=rp.artifact_id))
                nonconsent_gap = True
                continue
            if t["auth"]["mode"] == "hmac_body" and not global_secret_ok():
                out.issues.append(PluginIssue(
                    name=rp.name, target=target, stage="triggers",
                    reason_code="trigger_secret_missing",
                    artifact_id=rp.artifact_id))
                nonconsent_gap = True
                continue
            ident = ack_identity(
                plugin=rp.name, artifact_id=rp.artifact_id,
                effective=t["effective"], target=target, auth=t["auth"])
            ack_rec = acks.get(ident)
            if ack_rec is None:
                out.issues.append(PluginIssue(
                    name=rp.name, target=target, stage="triggers",
                    reason_code="trigger_pending_ack",
                    artifact_id=rp.artifact_id))
                plugin_pending.append({
                    "plugin": rp.name, "artifact_id": rp.artifact_id,
                    "effective": t["effective"], "target": target,
                    "auth": t["auth"], "clearance": t["clearance"]})
                continue
            entries[t["effective"]] = {
                "plugin": rp.name, "role": role,
                "clearance": t["clearance"], "auth": t["auth"],
                # the (consent identity, approval generation) this route was
                # approved under — the mint binds the secret to the PAIR, so
                # a re-approval after a revoke (new gen) rekeys even for an
                # identical tuple (Sol shipB-r3)
                "identity": f"{ident}#{ack_rec.get('gen', '')}"}

        # Per-plugin all-or-nothing: any gap unroutes the whole set.
        if not plugin_pending and not nonconsent_gap:
            out.overlay.update(entries)
        elif not nonconsent_gap:
            out.pending.extend(plugin_pending)
    return out


def _secret_backed(desired: DesiredTriggers) -> "list[tuple[str, dict]]":
    """The overlay entries backed by a casa-minted PER-TRIGGER secret file.

    One definition shared by the writer (:func:`_mint_secrets`) and the reader
    (:func:`verify_minted_secrets`) — they must agree on which routes have a
    secret to check, or the gate would either hold on a ``hmac_body`` trigger
    forever or wave through one whose file is missing."""
    return [(eff, entry) for eff, entry in desired.overlay.items()
            if entry["auth"].get("mode") in _PER_TRIGGER_SECRET_MODES]


def _fail_close_plugins(desired: DesiredTriggers, plugins: set[str]) -> None:
    """Drop every route of ``plugins`` from the overlay — the per-plugin
    all-or-nothing rule, applied after the compute."""
    if plugins:
        desired.overlay = {
            eff: entry for eff, entry in desired.overlay.items()
            if entry.get("plugin") not in plugins}


def verify_minted_secrets(desired: DesiredTriggers, secrets_dir: Path) -> None:
    """Fail-close every routed plugin whose per-trigger secret is not ALREADY
    minted under the consent identity this pass computed (#453).

    Read-only *against the filesystem* — it mints nothing — but it does drop the
    unbacked plugins from ``desired.overlay``, exactly as :func:`_mint_secrets`
    does on a mint failure, so a caller that swapped this overlay in would not
    route an unbacked name.

    The mirror of :func:`_mint_secrets`, for the consumers that
    RE-DERIVE the desired state without applying it — :func:`current_issues`,
    and through it the plugin-health report and
    ``casa_core._callback_and_trigger_routes_live``, the setup-dispatch gate.

    Why the gate needs this at all: ``compute_desired`` is a derivation over
    consent, assignment and declarations. The secret a plugin's setup tool reads
    is produced by the reconcile's APPLY half, two steps later than the consent
    approval that settles the setup round — the ack persists and the round
    settles in one yield-free step, the mint happens in the reconcile the finish
    hook awaits afterwards. A gate derived from consent alone therefore reported
    "live" while the file was absent (a first approval), still bound to the
    PREVIOUS approval generation (a re-approval after a revoke, which rekeys),
    or lazily minted unbound by the webhook handler and about to be replaced.
    Setup would then wire the external service to a credential Casa is about to
    change — the exact failure setup exists to prevent.

    Holding is the safe direction and it is not a dead end: ``_mint_secrets``
    runs on EVERY pass with no availability gate of its own, so the gap closes
    on the next reconcile. That property is load-bearing rather than incidental
    — a gate may only demand an artifact the writer will actually write — and
    the callback half needed a fix to acquire it (see
    ``callback_reconcile._reconcile_markers_pre_swap``).
    """
    import webhook_auth
    from plugin_registry import PluginIssue

    unbacked: set[str] = set()
    for eff, entry in _secret_backed(desired):
        if webhook_auth.secret_bound_to_identity(
                eff, identity=entry.get("identity", ""),
                secrets_dir=Path(secrets_dir)):
            continue
        unbacked.add(entry.get("plugin", ""))
        desired.issues.append(PluginIssue(
            name=entry.get("plugin", ""), target=f"resident:{entry['role']}",
            stage="triggers", reason_code="trigger_secret_missing",
            artifact_id=None))
    _fail_close_plugins(desired, unbacked)


def _mint_secrets(desired: DesiredTriggers, secrets_dir: Path) -> None:
    """Eagerly mint casa-owned per-trigger secrets for the routed set; a mint
    failure fail-closes the OWNING PLUGIN's whole set (all-or-nothing)."""
    import webhook_auth
    from plugin_registry import PluginIssue

    failed_plugins: set[str] = set()
    for eff, entry in _secret_backed(desired):
        try:
            # Identity-bound (Terra shipB-r2): a surviving secret minted
            # under a DIFFERENT consent identity is rekeyed here — the old
            # credential can never carry into a new approval even when an
            # earlier retirement silently failed.
            got = webhook_auth.ensure_secret_for_identity(
                eff, identity=entry.get("identity", ""),
                secrets_dir=secrets_dir)
        except Exception:  # noqa: BLE001 — Terra shipB-r1 P1-2: one plugin's
            # mint blow-up (fs error) must fail-close THAT plugin, never
            # abort the whole reconcile (which would retain every stale
            # route, including a just-unassigned plugin's).
            logger.exception("per-trigger secret mint failed (%s)", eff)
            got = None
        if not got:
            failed_plugins.add(entry.get("plugin", ""))
            desired.issues.append(PluginIssue(
                name=entry.get("plugin", ""), target=f"resident:{entry['role']}",
                stage="triggers", reason_code="trigger_secret_missing"))
            continue
        try:
            import log_redact
            log_redact.register_secret(got.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — redaction is best-effort
            pass
    _fail_close_plugins(desired, failed_plugins)


async def _regen_health_safe() -> None:
    """Regenerate the plugin-health report (no operator notify) so a
    just-acked trigger's stale ``trigger_pending_ack`` clears immediately
    (v0.98.2 P2 follow-up) instead of lingering until the next plugin
    mutation/boot. ``current_issues()`` recomputes fresh from the persisted
    acks + resolver, so the routed trigger drops out of the report. Never
    raises — a health-refresh failure must not break the reconcile.

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


async def reconcile_plugin_triggers(
    *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any = None, acks: Any = None,
    secrets_dir: "Path | None" = None, prompt: bool = True,
    resolver: "Callable[[str | None], Any] | None" = None,
    global_secret_ok: "Callable[[], bool] | None" = None,
    regen_health: bool = False,
) -> list:
    """Compute + apply: swap the complete desired overlay into the registry,
    mint eager secrets, fire consent prompts. Returns the trigger issues.

    ``regen_health`` (set by the consent-approve reconcile) additionally
    rewrites plugin-health after the swap so a freshly-acked trigger's stale
    ``trigger_pending_ack`` clears at once. The mutation/boot/reload paths
    leave it False — they regenerate health themselves — so there is no
    double-regen."""
    acks = acks if acks is not None else _default_acks()
    # Resolved at CALL time from the module attribute (not a def-time default)
    # so there is one source of truth for the secrets location.
    secrets_dir = SECRETS_DIR if secrets_dir is None else secrets_dir

    def _compute_and_mint() -> "tuple":
        # ONE snapshot for the whole pass (#451 r3): the pending membership and
        # the setup-candidate sweep must describe the same registry, or the
        # sealed round and the obligation can name different artifacts.
        pinned = pin_resolver(
            resolver if resolver is not None else _default_resolver())
        desired = compute_desired(
            role_configs=role_configs, acks=acks, resolver=pinned,
            global_secret_ok=global_secret_ok)
        _mint_secrets(desired, Path(secrets_dir))
        # The CALLBACK half of the union membership and the setup-candidate
        # sweep are derived here, in the SAME worker thread — both read
        # plugin.json for every resolved plugin, which must never run on the
        # event loop under the reconcile lock. Both are computed strictly
        # before any keyboard posts (sealing and prompting happen below, after
        # this returns).
        #
        # #451: the callback half is computed whenever ``prompt`` is set, NOT
        # only when this pass has pending triggers. Sealing a ZERO-member
        # verdict — the positive statement that an artifact needs no consent —
        # requires knowing that the callback half is empty too, and a plugin
        # can have a pending callback consent while no trigger consent pends.
        # NOT gated on ``prompt``: the obligation sweep and the verdict sealing
        # must run on every reconcile, including the prompt=False BOOT pass.
        # Boot is the only pass that follows a crash between a durable registry
        # publish and its lifecycle reconcile — precisely when the level-
        # triggered sweep is the thing that recovers the missing obligation.
        # Only the KEYBOARDS depend on `prompt`.
        union_ok, union, peer_unknown = _callback_pending_for_union(
            role_configs=role_configs, resolver=pinned)
        cand_ok, cand = setup_candidates(resolver=pinned)
        candidates = cand if cand_ok else None
        return (desired, union, union_ok, candidates, peer_unknown,
                one_generation(pinned))

    try:
      async with _RECONCILE_LOCK:
        try:
            (desired, callback_pending, callback_ok, setup_cands,
             peer_unknown, one_gen) = await asyncio.to_thread(_compute_and_mint)
        except Exception:
            # Terra shipB-r1 P1-2: a compute failure must not RETAIN the old
            # overlay (a just-unassigned/revoked plugin's routes would stay
            # live behind a swallowed warning). Fail closed to NO plugin
            # ingress — resident triggers are untouched — then propagate so
            # the caller logs/surfaces it; the next successful reconcile
            # restores the valid set.
            #
            # #606: the sentinel, not `{}`. Both close ingress; only `{}` is a
            # CLAIM that nothing should route, and publishing that claim from a
            # pass that computed nothing is what let the health regeneration
            # below report all-clear while ingress was shut.
            trigger_registry.replace_plugin_overlay(
                trigger_registry_mod.ROUTING_UNAVAILABLE)
            raise
        # #606: only an authoritative computation may publish a map. An
        # invalid registry returns NORMALLY with an empty overlay, and
        # publishing that would clear the sentinel without anything having been
        # read.
        trigger_registry.replace_plugin_overlay(
            desired.overlay if desired.registry_valid
            else trigger_registry_mod.ROUTING_UNAVAILABLE)
        # v0.112.0 (impl r5, Terra): the overlay is now live — wake the
        # setup-episode worker so any pending episode gated on a
        # previously-down route dispatches. This fires on EVERY reconcile
        # (consent-driven or a plain casa_reload_triggers heal), not just the
        # consent finish hook — otherwise an episode whose approval-time
        # reconcile failed would wait indefinitely for a later heal to notice
        # it. Cheap (an Event.set); the worker re-checks routes_live itself.
        try:
            import plugin_setup_episodes
            plugin_setup_episodes.kick()
        except Exception:  # noqa: BLE001 — never break a reconcile on this
            pass
        # Prompts fire INSIDE the lock (Sol shipB-r2 P1-1): keyboard
        # registration is then ordered BEFORE any later reconcile can
        # acquire the lock — so trigger_ack_revoke's final
        # cancel_matching(plugin=…), which runs after ITS reconcile,
        # provably catches every keyboard an in-flight reconcile posted.
        # register_challenge is synchronous (the Telegram post happens on
        # an owned background driver), so this adds no IO under the lock.
        # #451: SEAL BEFORE the operator-reachability gate, and on EVERY pass
        # including prompt=False. Sealing used to live inside
        # _fire_consent_prompts, AFTER its `channel is None` / `op is None`
        # early returns — so with no DM reachable nothing was sealed at all,
        # and a round could first seal on a later ordinary reload, long after a
        # mutation had already reported which runner owned setup. Sealing here
        # means an unreachable operator yields a members-bearing verdict and
        # the obligation correctly HOLDS.
        nonce_by_identity = seal_setup_state(
            trigger_pending=desired.pending,
            callback_pending=callback_pending,
            pending_complete=callback_ok,
            candidates=setup_cands,
            unknown=(consent_position_unknown(desired.issues)
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
                acks=acks, secrets_dir=secrets_dir, resolver=resolver,
                global_secret_ok=global_secret_ok,
                nonce_by_identity=nonce_by_identity)
    finally:
        # #606: on BOTH exits. The raise path used to skip this entirely — the
        # regeneration sat after the `async with`, and the exception left over
        # it — so an approve-time reconcile that failed left the acked trigger's
        # stale `trigger_pending_ack` row standing with nothing to clear it.
        # Mirrors the event twin (event_reconcile.py). The `finally` is OUTSIDE
        # the `async with`, which is what preserves the lock ORDER:
        # _regen_health_safe takes tools._plugin_tools_guard only after
        # _RECONCILE_LOCK is released, never nested inside it.
        if regen_health:
            await _regen_health_safe()
    return desired.issues


def _fire_consent_prompts(
    pending: list[dict], *, trigger_registry: Any, role_configs: dict,
    channel_manager: Any, acks: Any, secrets_dir: Path,
    resolver: Any, global_secret_ok: Any, nonce_by_identity: dict[str, str],
) -> None:
    import authz_grants
    import trigger_consent

    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None:
        return  # no DM reachable — pending_ack stands; re-prompted later
    op = trigger_consent.operator_identity(channel)
    if op is None:
        return
    chat_id, operator_id = op

    async def _reconcile_again() -> None:
        # Captures THIS reconcile's inputs. If a reload_full rebinds the
        # runtime registries before the tap lands, the swap goes to the old
        # registry object — harmless: the ack is persisted, so the next
        # lifecycle reconcile routes it on the live one.
        await reconcile_plugin_triggers(
            trigger_registry=trigger_registry, role_configs=role_configs,
            channel_manager=channel_manager, acks=acks,
            secrets_dir=secrets_dir, prompt=False, resolver=resolver,
            global_secret_ok=global_secret_ok, regen_health=True)

    # The setup-round membership was SEALED by the caller, before this
    # function's reachability gate (#451) and therefore before any keyboard
    # posts — a fast Approve on the first keyboard can never settle a round
    # still registering members.
    _ack_identity = ack_identity  # module-level import (plugin_triggers)

    for p in pending:
        try:
            ident = _ack_identity(
                plugin=p["plugin"], artifact_id=p["artifact_id"],
                effective=p["effective"], target=p["target"], auth=p["auth"])
            trigger_consent.prompt_trigger_consent(
                coordinator=authz_grants.CHALLENGES, channel=channel,
                chat_id=chat_id, operator_id=operator_id, acks=acks,
                reconcile_cb=_reconcile_again,
                setup_nonce=nonce_by_identity.get(ident, ""), **p)
        except Exception:  # noqa: BLE001 — a prompt failure never breaks
            # the mutation; pending_ack stays in health and re-prompts later.
            logger.exception("trigger consent prompt failed (plugin=%s)",
                             p.get("plugin"))


def trigger_pending_for_union(
    *, role_configs: dict, resolver: Any = None,
) -> tuple[bool, list[dict], set]:
    """The pending TRIGGER consents, for the callback reconciler's union
    sealing (the mirror of ``callback_reconcile.callback_pending_for_union``).
    Side-effect free and never raises.

    Returns ``(ok, pending)``. #451: the success flag is LOAD-BEARING — this
    used to degrade a failure to ``[]``, which is indistinguishable from
    "genuinely nothing pending". Under-reporting pending consents would let a
    verdict be sealed over a subset (a fast Approve then settles a round the
    other consent never joined), and would let a ZERO-member verdict — a
    positive statement that an artifact needs no consent — be sealed when the
    truth is unknown. The caller seals nothing unless both halves report ok.

    NOTE the ack store: this reads the module DEFAULT (``compute_desired``
    resolves ``_default_acks`` when none is passed), because the caller is the
    OTHER reconciler and does not hold this kind's store. Correct in
    production, where the default IS the live store — but a caller injecting a
    non-default store for one kind must inject BOTH, or this half reports an
    already-acked consent as still pending and the verdict it seals never
    settles.
    The third element is the peer's :func:`consent_position_unknown` set. It
    travels WITH the pending rows on purpose: a gap and the absence it causes are
    two views of one computation, and returning only the rows let the peer pass
    seal "needs no consent" while blind to this kind's non-consent gap.
    """
    try:
        d = compute_desired(role_configs=role_configs, resolver=resolver)
        return True, d.pending, consent_position_unknown(d.issues)
    except Exception:  # noqa: BLE001
        logger.exception("trigger union-member compute failed")
        return False, [], set()


def _callback_pending_for_union(
    *, role_configs: dict, resolver: Any = None,
) -> tuple[bool, list[dict], set]:
    """``(ok, pending, unknown)`` — see :func:`trigger_pending_for_union` for why
    the flag and the unknown set matter. A raising lookup reports ``ok=False``,
    never empty-as-none."""
    try:
        import callback_reconcile
        return callback_reconcile.callback_pending_for_union(
            role_configs=role_configs, resolver=resolver)
    except Exception:  # noqa: BLE001
        logger.exception("callback union-member lookup failed")
        return False, [], set()


def setup_candidates(*, resolver: Any = None) -> tuple[bool, list[dict]]:
    """Every resolved plugin declaring ``casa.setupTool``, as
    ``(ok, [{"plugin", "artifact_id"}])`` (#451).

    These are the plugins Casa owes a setup run for. Reads plugin.json for
    every resolved plugin, so it runs in the reconcile's WORKER THREAD, never
    on the event loop under the reconcile lock. ``ok=False`` on any sweep
    failure — the caller then creates no obligations and seals no verdicts,
    leaving every existing obligation to hold until a later pass.

    Reads through the SAME ``resolver`` seam as ``compute_desired`` so a pass
    describes one registry snapshot where it can. Whether the pass ACTUALLY
    spanned generations is not judged here: that belongs to the authority
    predicate in :func:`seal_setup_state`, because it must suppress the
    CONCLUSION without suppressing the debt. Suppressing the candidate list
    itself lost setup outright — a re-consent landing during a mixed pass left a
    terminal obligation un-re-armed, and once the ack persisted no later pass saw
    a pending consent to re-arm from.
    """
    try:
        import plugin_store
        resolver = resolver if resolver is not None else _default_resolver()
        res = resolver(None)
        if not getattr(res, "registry_valid", False):
            # An invalid registry is not an empty one: creating no obligations
            # is right, but reporting ok would seal "needs no consent" verdicts
            # for plugins we simply could not see.
            return False, []
        out: list[dict] = []
        for rp in getattr(res, "plugins", None) or ():
            try:
                setup = plugin_store.manifest_setup_tool(rp.manifest)
            except Exception:  # noqa: BLE001 — verify already refused it; a
                # malformed declaration is not a candidate.
                continue
            if setup:
                # rp.name — the REGISTRY name, matching the ledger key used by
                # the pending rows, by callback identities and by
                # casa_core's _resolve_registry_entry seam. runtime_name
                # (manifest_name for an owned plugin) would key a
                # specialist-bundled plugin's obligation differently from its
                # consent verdict, and it would never release.
                out.append({"plugin": rp.name,
                            "artifact_id": rp.artifact_id})
        return True, out
    except Exception:  # noqa: BLE001
        logger.exception("setup-candidate sweep failed")
        return False, []


# Issue reason codes that mean "this consent IS the gap" — i.e. the plugin's
# consent position is known and it is `pending`. Every OTHER trigger/callback
# issue is a NON-CONSENT gap, which suppresses the pending row entirely and so
# must never be read as "needs no consent" (#451 r4).
_PENDING_ACK_CODES = ("trigger_pending_ack", "callback_pending_ack")


def one_generation(resolver: Any) -> bool:
    """Did this PINNED pass observe exactly one registry generation?

    Caching per target is not the same as observing one snapshot: a reload
    publishing a new generation between two targets yields a mixed pass, whose
    pending membership and candidate set can describe different artifacts.
    Sealing a verdict over that pair can release setup for an artifact whose
    consent was never approved — so the pass may still record debts and fence
    keyboards, but must not CONCLUDE.

    Read this LAST in a pass: the pinned wrapper accumulates generations as
    targets resolve, so an earlier read sees an incomplete set. Both reconcilers
    order it that way (main compute, then the peer union, then the sweep, then
    this)."""
    gens = getattr(resolver, "generations", None)
    if gens is None:
        return True                      # an unpinned resolver makes no claim
    if len(gens) > 1:
        logger.info("pass spans registry generations %s — no setup verdict "
                    "will be sealed", sorted(gens))
        return False
    return True


def consent_position_unknown(issues: "list") -> set:
    """Plugins whose consent position cannot be established from this pass.

    A declared trigger or callback with a NON-CONSENT gap — no webhook channel
    on its target, an unassigned target, a missing global secret, an invalid
    declaration, an invalid public base URL — is omitted from the reconciler's
    ``pending`` rows altogether. Sealing a ZERO-member verdict for such a plugin
    would assert "this artifact needs no consent" when in fact it declares one
    that is unapproved and merely unaskable right now. The route gate would stop
    the dispatch today, but the verdict itself would be false, and a verdict is
    the one thing this design requires to be positive."""
    out = set()
    for i in issues or ():
        code = str(getattr(i, "reason_code", "") or "")
        name = getattr(i, "name", None)
        if name and code and code not in _PENDING_ACK_CODES:
            out.add(name)
    return out


def seal_setup_state(*, trigger_pending: list[dict],
                     callback_pending: list[dict], pending_complete: bool,
                     candidates: "list[dict] | None",
                     unknown: "set | None" = None,
                     single_generation: bool = True) -> dict[str, str]:
    """Create the setup obligations Casa owes, and seal ONE positive consent
    verdict per ``(plugin, artifact_id)`` — in one yield-free batch per plugin
    BEFORE any keyboard posts, so a fast Approve on the first keyboard can
    never settle a round still registering its other members.

    Membership is the UNION of the supplied pending trigger and callback
    consent identities. A plugin that owes setup and has NO pending consent is
    sealed with an EMPTY membership: a positive statement that this artifact
    needs no consent, which releases its obligation. That is deliberately
    distinct from sealing nothing, which means "no verdict yet" and holds.

    Whether a pass may draw a setup CONCLUSION about a plugin is decided by one
    predicate over three inputs — ``pending_complete`` (both consent kinds'
    pending sets were computed), ``candidates is not None`` (the sweep succeeded
    and the pass was single-generation), and the plugin's absence from
    ``unknown`` (no non-consent gap hides part of its position). A pass that
    fails any of them still seals the round, because the consent keyboards need
    their nonces, but seals it NON-AUTHORITATIVE: settlement consumes it and
    leaves the obligation holding. Applying that predicate uniformly — rather
    than as separate conditions on the zero-member and members-bearing paths —
    is what stops a partial pass from releasing setup.

    Returns ``{identity: nonce}`` — each caller threads its own prompts'
    nonces into their decision callbacks (stale-expiry fencing). Never raises:
    a ledger failure leaves the prompts unfenced, never blocked.
    """
    import plugin_setup_episodes

    # The pending identities come FIRST, because whether a consent is pending
    # for an artifact is an input to the obligation decision (a terminal row
    # plus a pending consent means setup is owed again — see
    # ``ensure_obligation``). Membership is sealed for every pending identity
    # regardless of whether setup is owed: the round is also what fences the
    # consent keyboards.
    by_plugin: dict[tuple, list[str]] = {}
    for p in trigger_pending:
        ident = ack_identity(
            plugin=p["plugin"], artifact_id=p["artifact_id"],
            effective=p["effective"], target=p["target"], auth=p["auth"])
        by_plugin.setdefault((p["plugin"], p["artifact_id"]), []).append(ident)
    for c in callback_pending:
        # The callback identity is precomputed by its own compute (it binds
        # the declaration digest, which only that module derives).
        by_plugin.setdefault(
            (c["plugin"], c["artifact_id"]), []).append(c["identity"])

    # ONE predicate decides whether this pass may draw a setup conclusion about
    # a plugin, and it is applied to EVERY round uniformly — zero-member and
    # members-bearing alike. Assembling this decision from separate conditions at
    # separate call sites is what three consecutive review rounds kept breaking:
    # each round added an input, and each new combination found another gap
    # (a zero-member seal blind to the peer kind's gap; a members-bearing seal
    # for a plugin whose OTHER kind was unaskable; a cross-generation pass that
    # still released a pre-existing obligation because only `candidates` was
    # suppressed). A round the pass may not conclude from is still sealed — the
    # keyboards need their nonces — but as NON-AUTHORITATIVE.
    #
    #   pending_complete  both consent kinds' pending sets were computed
    #   candidates        the sweep succeeded and the pass was single-generation
    #   plugin not in unknown   no non-consent gap hides part of its position
    def _authoritative(plugin_name: str) -> bool:
        return bool(pending_complete and single_generation
                    and plugin_name not in (unknown or ())
                    and candidates is not None)

    # A candidate that awaits a verdict joins the sealing pass with (possibly)
    # zero members. One already dispatched, refused or failed with NO pending
    # consent reports False and is left alone — no verdict churn on every
    # reconcile for a plugin whose setup is long settled.
    for cand in candidates or ():
        key = (cand["plugin"], cand["artifact_id"])
        try:
            if plugin_setup_episodes.ensure_obligation(
                    plugin=cand["plugin"], artifact_id=cand["artifact_id"],
                    # Recording a DEBT is the conservative direction, so it is
                    # NOT gated on the authority predicate. Gating it lost setup
                    # for good: a re-consent observed during a non-authoritative
                    # pass left the terminal row un-re-armed, and once the ack
                    # persisted no later pass saw a pending consent to re-arm
                    # from. An extra re-arm only means "setup is owed and holds"
                    # — safe, and the tool is idempotent by contract — whereas a
                    # missed one is unrecoverable. Under-reporting cannot cause a
                    # spurious re-arm; it can only cause a missed one.
                    consent_pending=bool(by_plugin.get(key))):
                # A zero-member round is only worth opening when it can carry a
                # verdict — a non-authoritative empty round decides nothing.
                if _authoritative(cand["plugin"]):
                    by_plugin.setdefault(key, [])
        except Exception:  # noqa: BLE001 — never break a reconcile on this
            logger.exception("setup obligation ensure failed (plugin=%s)",
                             cand.get("plugin"))

    nonce_by_identity: dict[str, str] = {}
    for (plg, art), idents in by_plugin.items():
        authoritative = _authoritative(plg)
        if not authoritative:
            logger.info("sealing round for fencing only (plugin=%s): this pass "
                        "cannot establish its full consent position", plg)
        try:
            nonce_by_identity.update(plugin_setup_episodes.open_round(
                plugin=plg, artifact_id=art, identities=idents,
                verdict=authoritative))
        except Exception:  # noqa: BLE001 — unfenced, never blocking
            logger.exception("setup-round open failed (plugin=%s)", plg)
    return nonce_by_identity


async def reconcile_from_runtime(runtime: Any, *, prompt: bool = True) -> list:
    """Convenience seam for tools/reload callers holding a CasaRuntime."""
    if runtime is None or getattr(runtime, "trigger_registry", None) is None:
        return []
    return await reconcile_plugin_triggers(
        trigger_registry=runtime.trigger_registry,
        role_configs=getattr(runtime, "role_configs", None) or {},
        channel_manager=getattr(runtime, "channel_manager", None),
        prompt=prompt)


async def reprompt_pending(
    runtime: Any, *, report: list, acks: Any = None,
    resolver: "Callable[[str | None], Any] | None" = None,
    global_secret_ok: "Callable[[], bool] | None" = None,
) -> None:
    """#494 — on-demand PROMPT-ONLY repost of pending trigger consents.

    The trigger half of the `consent_reprompt` tool; see
    ``callback_reconcile.reprompt_pending`` for the full contract (this is
    its structural mirror: prompt-only — no overlay swap, no secret mint, no
    setup-round sealing/re-arming — under ``_RECONCILE_LOCK``, with the
    denial-registry skip, the synchronous ack re-read, and the read-only
    open-member nonce). Appends ``{"kind","plugin","name",
    "status"|"handle"}`` rows to ``report``. Never raises."""
    import authz_grants
    import consent_denials
    import plugin_setup_episodes
    import trigger_consent

    if runtime is None:
        return
    channel_manager = getattr(runtime, "channel_manager", None)
    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None:
        return
    op = trigger_consent.operator_identity(channel)
    if op is None:
        return
    chat_id, operator_id = op
    role_configs = getattr(runtime, "role_configs", None) or {}
    acks = acks if acks is not None else _default_acks()

    async def _reconcile_again() -> None:
        import agent as agent_mod
        live = getattr(agent_mod, "active_runtime", None)
        if live is None or getattr(live, "trigger_registry", None) is None:
            return
        await reconcile_plugin_triggers(
            trigger_registry=live.trigger_registry,
            role_configs=getattr(live, "role_configs", None) or {},
            channel_manager=getattr(live, "channel_manager", None),
            prompt=False, regen_health=True)

    async with _RECONCILE_LOCK:
        try:
            desired = await asyncio.to_thread(
                compute_desired, role_configs=role_configs, acks=acks,
                resolver=resolver, global_secret_ok=global_secret_ok)
        except Exception:  # noqa: BLE001 — a compute failure reposts nothing,
            # but must be VISIBLE to the caller (Sol/Terra diff-gate r1: a
            # swallowed compute failure read as "no consent is pending").
            logger.exception("trigger reprompt compute failed")
            report.append({"kind": "trigger", "plugin": "", "name": "",
                           "status": "error"})
            return
        for p in desired.pending:
            row = {"kind": "trigger", "plugin": p["plugin"],
                   "name": p["effective"]}
            identity = ack_identity(
                plugin=p["plugin"], artifact_id=p["artifact_id"],
                effective=p["effective"], target=p["target"], auth=p["auth"])
            if consent_denials.denied(
                    consent_denials.key("trigger", identity)):
                report.append(dict(row, status="denied"))
                continue
            if acks.get(identity) is not None:
                report.append(dict(row, status="already_acked"))
                continue
            nonce = plugin_setup_episodes.open_member_nonce(
                p["plugin"], identity)
            try:
                handle = trigger_consent.prompt_trigger_consent(
                    coordinator=authz_grants.CHALLENGES, channel=channel,
                    chat_id=chat_id, operator_id=operator_id, acks=acks,
                    reconcile_cb=_reconcile_again, setup_nonce=nonce,
                    plugin=p["plugin"], artifact_id=p["artifact_id"],
                    effective=p["effective"], target=p["target"],
                    auth=p["auth"], clearance=p.get("clearance", "public"))
                report.append(dict(row, handle=handle))
            except Exception:  # noqa: BLE001 — one prompt failure must not
                # abort the remaining rows
                logger.exception("trigger reprompt failed (plugin=%s)",
                                 p.get("plugin"))
                report.append(dict(row, status="error"))


class IssueState(NamedTuple):
    """``(ok, issues, observed)`` — see :func:`issue_state`.

    A NamedTuple rather than a plain tuple so ``observed`` could be added
    without silently changing what an existing ``state[1]`` means, and so a
    consumer reads ``state.observed`` instead of positionally.
    """

    ok: bool
    issues: list
    observed: "set[str]"


def issue_state(resolver: Any = None) -> "IssueState":
    """``(ok, issues, observed)`` — the trigger gaps, whether they could be
    computed AT ALL, and which plugins the computation actually saw.

    Two halves, and #453 is about the second: the DERIVED gaps (consent,
    assignment, channel, global secret) come from :func:`compute_desired`, and
    the APPLIED ones — is the per-trigger secret this consent identity needs
    actually on disk — from :func:`verify_minted_secrets`. Only the reconcile
    mints, so a recomputation that skipped the second half described a route as
    fully live during the window between an approval and the mint that backs it.

    The ``ok`` flag is load-bearing for the SETUP GATE and is the same shape the
    rest of this design insists on: an empty issue list is the positive claim
    "this plugin has no gap", and it is exactly the value that opens the gate.
    Degrading a crash — or a runtime that is not up yet — to ``[]`` therefore
    reads as "every plugin is fully live", turning the one check that must fail
    closed into one that fails open. Health has the opposite need and keeps the
    degraded list (:func:`current_issues`): losing a row there hides a problem,
    while inventing one cries wolf.

    ``resolver`` lets a caller supply ONE pinned registry resolution so a
    decision spanning both reconcilers describes a single generation (#454).

    ``observed`` closes the last way the empty list could lie (#457). ``ok``
    reports whether the computation RAN, not whether it saw every plugin: an
    invalid registry — and a single artifact that fails to resolve — yields an
    empty result with no issues, so a plugin absent from the computation read as
    "no gap". Absence is not consent, so the positive claim is now carried
    explicitly: a gate must require the plugin to be IN ``observed`` before
    reading the absence of an issue as a verdict about it."""
    try:
        import agent as agent_mod

        runtime = getattr(agent_mod, "active_runtime", None)
        if runtime is None:
            return IssueState(False, [], set())
        role_configs = getattr(runtime, "role_configs", None)
        if not role_configs:
            return IssueState(False, [], set())
        desired = compute_desired(role_configs=role_configs, resolver=resolver)
        verify_minted_secrets(desired, SECRETS_DIR)
        return IssueState(True, desired.issues, desired.observed)
    except Exception:  # noqa: BLE001 — a trigger-compute crash must never
        # take down the whole health pass; log and degrade to no extras.
        logger.exception("trigger issue recompute failed")
        return IssueState(False, [], set())


def current_issues() -> list:
    """Fresh, side-effect-free trigger issues for health regeneration —
    recomputed on EVERY ``_regenerate_plugin_health`` pass so they survive
    unrelated refreshes. Never raises (health must always regenerate); a
    failure degrades to no extras. The setup gate uses :func:`issue_state`
    instead, because for that consumer "could not compute" must not read as
    "nothing is wrong".

    #606: an ``ok=False`` degradation used to reach here as ``[]`` — "nothing is
    wrong" — while ingress was shut. It now carries the two unavailable rows.
    """
    # ONE issue_state() call, and its result feeds BOTH the state-row decision
    # and the returned issues. Computing it twice let the guard pass on a first
    # call that succeeded while the second silently failed to [] — zero rows,
    # from a runtime whose computation had just failed (review r1 S2).
    state = issue_state()
    return _unavailable_rows(state) + list(state.issues)


def _live_registry():
    """The registry the running system actually routes through, or None. The
    tests that pin the health rows below MUST install it here — a registry
    handed only to a reconciler is not the one this consumer reads, and a test
    that does that reports green while measuring nothing."""
    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    return getattr(runtime, "trigger_registry", None) if runtime else None


def _unavailable_rows(state=None) -> "list[dict]":
    """#606: the two independent honesty rows, as plain PluginIssue-shaped
    dicts — never PluginIssue instances, so they are concatenated DIRECTLY into
    write_report's issues= and never routed through the attribute-only
    _add()/_rediscoverable() helpers, which would degrade a dict row's fields to
    None instead of raising. Same contract as the event sibling.

    They are separate rows because their clearing predicates are independent.
    ``trigger_routing_unavailable`` is an APPLIED-state fact: the live overlay carries no
    authoritative computation, so plugin ingress is shut. ``trigger_state_unavailable`` is a
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
        if registry is not None and registry.plugin_overlay_unavailable():
            rows.append(_health_row("trigger_routing_unavailable"))
    except Exception:  # noqa: BLE001
        logger.exception("trigger routing availability probe failed")
        rows.append(_health_row("trigger_routing_unavailable"))
    try:
        import agent as agent_mod
        runtime = getattr(agent_mod, "active_runtime", None)
        if runtime is not None and getattr(runtime, "role_configs", None):
            if not (state if state is not None else issue_state()).ok:
                rows.append(_health_row("trigger_state_unavailable"))
    except Exception:  # noqa: BLE001
        logger.exception("trigger state availability probe failed")
        rows.append(_health_row("trigger_state_unavailable"))
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
    return PluginIssue(name="*", target=None, stage="triggers",
                       reason_code=reason_code, artifact_id=None)
