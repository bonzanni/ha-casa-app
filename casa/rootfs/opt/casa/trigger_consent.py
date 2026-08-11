"""Operator-consent DM prompts for plugin-declared webhook triggers
(Release B).

A plugin trigger routes ONLY after the operator taps Approve on a DM
keyboard bound to the trigger's full consent identity
(:func:`plugin_triggers.ack_identity` — plugin + artifact + effective name +
target + normalized auth policy). This module supplies the trigger-consent
flavor on the generic :class:`authz_grants.ChallengeCoordinator`:

* Approve → record the ack in :mod:`trigger_acks` (the SYNCHRONOUS commit
  step, mirroring the GrantKey mint) and fire a reconcile so the route goes
  live — never an agent-continuation dispatch.
* Deny / expiry → the trigger stays unrouted (``trigger_pending_ack``);
  the next lifecycle reconcile may re-prompt.

Taps ride the SAME validated Telegram DM callback path as authz grants
(broker scope ``authz:{chat}``): the handler fail-closes on the meta's
``chat_id``/``operator_id``, so an unauthorized or stale tap can never ack.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from plugin_triggers import ack_identity

logger = logging.getLogger(__name__)

# Consent TTL: longer than the 120 s authz-challenge TTL — a consent decision
# follows an operator-driven plugin mutation but is not turn-scoped; give the
# operator ten minutes before the keyboard expires (re-prompted on the next
# reconcile-with-prompt, e.g. any plugin lifecycle mutation).
TRIGGER_CONSENT_TTL_S = 600.0


@dataclass(frozen=True)
class TriggerConsentKey:
    """Challenge-dedup key for one pending trigger consent.

    Carries ``artifact_id`` so ``ChallengeCoordinator.cancel_matching(
    artifact=old)`` — the plugin-lifecycle invalidation — cancels a pending
    keyboard whose artifact just changed: an old keyboard can never ack the
    replacement artifact (``identity`` binds the full approved tuple).
    """

    plugin: str
    artifact_id: str
    effective: str
    target: str
    identity: str


def operator_identity(channel: Any) -> "tuple[int, int] | None":
    """The validated operator ``(chat_id, operator_id)`` from the configured
    Telegram DM channel, or ``None`` (fail-closed: no prompt, the trigger
    stays ``pending_ack``).

    The configured ``channel.chat_id`` is the operator's 1:1 DM chat. For a
    private Telegram chat the chat id EQUALS the operator's user id (both
    positive); group/supergroup ids are negative, so a misconfigured group
    chat yields ``None`` — nobody's tap would validate rather than the wrong
    someone's.
    """
    raw = getattr(channel, "chat_id", None)
    try:
        cid = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if cid <= 0:
        return None
    return cid, cid


def render_trigger_consent_message(
    *, plugin: str, effective: str, role: str, auth: dict,
    clearance: str = "public",
) -> str:
    mode = auth.get("mode", "?")
    header = auth.get("header", "?")
    return (
        "\U0001F510 Plugin ingress consent\n\n"
        f"Plugin '{plugin}' wants to open POST /webhook/{effective} "
        f"→ {role} (auth {mode}, header {header}; memory clearance "
        f"{clearance}).\n\n"
        "Approve to route it; Deny to leave it unrouted."
    )


def prompt_trigger_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int,
    plugin: str, artifact_id: str, effective: str, target: str,
    auth: dict, acks: Any, clearance: str = "public",
    reconcile_cb: "Callable[[], Awaitable[None]] | None" = None,
    setup_nonce: str = "",
) -> Any:
    """Post (or dedupe onto) the consent keyboard for ONE plugin trigger.

    Returns the coordinator's ``ChallengeHandle``. ``acks`` is the
    :class:`trigger_acks.TriggerAckStore`; ``reconcile_cb`` re-runs the
    trigger reconciler after an approve so the route goes live immediately.
    """
    identity = ack_identity(plugin=plugin, artifact_id=artifact_id,
                            effective=effective, target=target, auth=auth)
    key = TriggerConsentKey(plugin=plugin, artifact_id=artifact_id,
                            effective=effective, target=target,
                            identity=identity)
    role = target.partition(":")[2] or target
    text = render_trigger_consent_message(
        plugin=plugin, effective=effective, role=role, auth=auth,
        clearance=clearance)

    # v0.112.0 (elevenlabs#2): the plugin's consent ROUND membership was
    # SEALED by the reconciler (`plugin_setup_episodes.open_round`, one
    # yield-free batch per plugin BEFORE any keyboard posts — impl r4); the
    # caller threads this prompt's NONCE in via ``setup_nonce`` so a
    # superseded keyboard's late deny/expiry can never decide a
    # re-prompted member.

    def _on_commit_sync(idx: int, meta: dict) -> None:
        # Telegram callback, IMMEDIATELY after a successful commit (no await
        # between): idx 0 -> persist the ack atomically; idx 1 -> no-op. An
        # exception here is swallowed+logged by the callback; ``acked`` stays
        # absent and the finish hook edits the internal-error text — a
        # consent that failed to persist must never activate a route.
        import consent_denials
        if idx != 0:
            # #494: latest decision is a Deny — commit-ordered record so the
            # on-demand re-prompt path never nags past it (expiry records
            # nothing; the finish hook owns that path).
            consent_denials.record(consent_denials.key("trigger", identity))
        if idx == 0:
            consent_denials.clear(consent_denials.key("trigger", identity))
            # #494: re-arm a refused setup obligation BEFORE persisting the
            # ack — the order is the crash contract (rearm_refused_sync). A
            # FAILED required re-arm aborts the whole commit (coordinator
            # swallows the raise, ``acked`` stays absent → internal-error
            # text): recording the ack anyway would recreate the no-exit
            # refused-forever window (Sol diff-gate r2).
            import plugin_setup_episodes
            if not plugin_setup_episodes.rearm_refused_sync(
                    plugin=plugin, artifact_id=artifact_id):
                raise RuntimeError(
                    "pre-ack setup re-arm failed — consent not recorded")
            acks.record(identity=identity, plugin=plugin,
                        artifact_id=artifact_id, effective=effective,
                        target=target, auth=auth)
            meta["acked"] = True
            # v0.112.0 (impl r3): record the approval in the setup-round
            # ledger in this SAME yield-free step — a crash before the
            # async finish hook must not strand the round (the boot sweep
            # covers a crash even earlier, from the persisted ack itself).
            try:
                import plugin_setup_episodes
                rec = acks.get(identity) or {}
                plugin_setup_episodes.record_approval_sync(
                    plugin=plugin, artifact_id=artifact_id,
                    identity=identity, gen=str(rec.get("gen", "")))
            except Exception:  # noqa: BLE001
                logger.exception("sync setup-approval record failed "
                                 "(plugin=%s)", plugin)

    async def _feed_setup_episode(approved: bool) -> None:
        # Every TERMINAL decision (approve, deny, expiry) feeds the durable
        # evaluator. Approvals carry the persisted ack's approval GENERATION
        # (re-approval mints a new episode); denials carry this keyboard's
        # NONCE so a superseded keyboard's late expiry is ignored. Never
        # raises into the finish hook.
        try:
            import plugin_setup_episodes
            gen = ""
            if approved:
                try:
                    rec = acks.get(identity)
                    gen = str((rec or {}).get("gen", ""))
                except Exception:  # noqa: BLE001
                    gen = ""
            await plugin_setup_episodes.on_consent_decision(
                plugin=plugin, artifact_id=artifact_id,
                identity=identity, approved=approved, approval_gen=gen,
                nonce=setup_nonce)
        except Exception:  # noqa: BLE001
            logger.exception("setup-episode feed failed (plugin=%s)", plugin)

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"⌛ Expired — consent for POST /webhook/"
                    f"{effective} was not answered; it stays unrouted",
                )
                await _feed_setup_episode(approved=False)
                return
            if outcome.get("option_index") == 0:
                if not req.meta.get("acked"):
                    # Commit landed but the sync step never persisted the ack
                    # (raised + swallowed) — surface the internal error and
                    # NEVER activate a route the store can't back.
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "internal error recording the trigger consent — "
                        "re-run the plugin mutation to be prompted again",
                    )
                    return
                # Edit the SUCCESS state FIRST, then reconcile, then overwrite
                # ONLY on failure (ordered inside this one hook task) —
                # mirrors the authz edit-first → dispatch → overwrite order.
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"✅ Enabled — POST /webhook/{effective} now "
                    f"routes to {role}",
                )
                # v0.112.0 (impl r2): the DECISION is durable here — the ack
                # is persisted (the yield-free commit step above) — so feed
                # the setup evaluator REGARDLESS of the reconcile outcome
                # below. Gating on the reconcile stranded the round forever on
                # a transient reconcile failure (the ack exists, so the trigger
                # is never re-prompted and the member would stay open).
                #
                # #453: what is durable here is the ack and NOTHING ELSE. The
                # per-trigger secret is minted by ``reconcile_cb`` below, and
                # settling the round kicks the setup worker — which can wake
                # before that mint lands. The ordering is safe because the
                # dispatch gate reads the APPLIED state (an unminted or
                # previous-generation secret is a `trigger_secret_missing` gap
                # in ``trigger_reconcile.current_issues``), so setup HOLDS
                # until the mint it needs exists rather than racing it. Casa's
                # route overlay healing remains a separate, surfaced concern.
                await _feed_setup_episode(approved=True)
                if reconcile_cb is not None:
                    try:
                        await reconcile_cb()
                    except Exception:  # noqa: BLE001 — surface, never raise
                        logger.exception(
                            "post-consent trigger reconcile failed "
                            "(plugin=%s effective=%s)", plugin, effective)
                        await channel.edit_dm_message(
                            chat_id, message_id,
                            f"⚠️ Approved, but activating "
                            f"/webhook/{effective} failed — run "
                            "plugin_verify",
                        )
                # impl r4: wake the setup worker — either the settlement
                # above created an episode, or a previously-gated pending
                # episode may now see its routes live post-reconcile.
                try:
                    import plugin_setup_episodes
                    plugin_setup_episodes.kick()
                except Exception:  # noqa: BLE001
                    pass
            else:
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"❌ Denied — POST /webhook/{effective} stays "
                    "unrouted",
                )
                await _feed_setup_episode(approved=False)

        return _finish

    return coordinator.register_challenge(
        key, chat_id=chat_id, operator_id=operator_id, channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
        kind="trigger_consent",
        meta_extra={"trigger_effective": effective, "trigger_plugin": plugin},
        timeout_s=TRIGGER_CONSENT_TTL_S,
    )
