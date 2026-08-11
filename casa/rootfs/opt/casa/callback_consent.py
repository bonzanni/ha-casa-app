"""Operator-consent DM prompts for plugin-declared authorization callbacks.

A plugin callback opens ``GET /callback/<effective>`` ONLY after the operator
taps Approve on a DM keyboard bound to the callback's consent identity
(:func:`plugin_callbacks.ack_identity` — plugin + effective name + declaration
digest). This module is the callback flavor on the generic
:class:`authz_grants.ChallengeCoordinator`, structurally the sibling of
:mod:`trigger_consent`:

* Approve → record the ack in :mod:`callback_acks` (the SYNCHRONOUS commit
  step, before any await) and fire a reconcile so the endpoint opens — never
  an agent-continuation dispatch.
* Deny / expiry → the callback stays closed (``callback_pending_ack``); the
  next lifecycle reconcile may re-prompt.

What the operator is approving is deliberately NARROWER than a trigger, and
the prose says so: a callback grants no turn into a role and no memory access,
only "an unauthenticated GET may deposit a query blob into this plugin's
spool". There is no target, no clearance and no auth policy to disclose.

Taps ride the SAME validated Telegram DM callback path as authz grants and
trigger consents (broker scope ``authz:{chat}``): the handler fail-closes on
the meta's ``chat_id``/``operator_id``, so an unauthorized or stale tap can
never ack.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from plugin_callbacks import ack_identity
# The operator-DM rule is one rule for every consent kind — imported rather
# than re-implemented so the two can never drift apart.
from trigger_consent import operator_identity  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

# Same TTL as trigger consent: a consent decision follows an operator-driven
# plugin mutation but is not turn-scoped.
CALLBACK_CONSENT_TTL_S = 600.0


@dataclass(frozen=True)
class CallbackConsentKey:
    """Challenge-dedup key for one pending callback consent.

    Carries ``plugin`` so a revoke can kill the plugin's live keyboards
    (``cancel_matching(plugin=…)``) and ``artifact_id`` so a lifecycle
    artifact invalidation cancels a keyboard whose artifact just changed.
    The ACK identity itself is artifact-free by design (a routine upgrade
    keeps consent); the KEY carries the artifact only so the setup round this
    keyboard reports into stays artifact-correct.
    """

    plugin: str
    artifact_id: str
    effective: str
    identity: str


def render_callback_consent_message(*, plugin: str, effective: str) -> str:
    """The verbatim consent prose. Only the plugin name and the
    effective callback name are interpolated — both grammar-validated
    identifiers, never plugin-authored prose."""
    return (
        "\U0001F510 Plugin callback consent\n\n"
        f"Plugin '{plugin}' wants to receive browser redirects at\n"
        f"GET /callback/{effective} (authorization callback — no agent turn, "
        "no memory access).\n\n"
        "Approve to open it; Deny to leave it closed."
    )


def prompt_callback_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int,
    plugin: str, artifact_id: str, declared: str, effective: str,
    declaration_digest: str, acks: Any,
    reconcile_cb: "Callable[[], Awaitable[None]] | None" = None,
    setup_nonce: str = "",
) -> Any:
    """Post (or dedupe onto) the consent keyboard for ONE plugin callback.

    Returns the coordinator's ``ChallengeHandle``. ``acks`` is the
    :class:`callback_acks.CallbackAckStore`; ``reconcile_cb`` re-runs the
    callback reconciler after an approve so the endpoint opens immediately.
    ``setup_nonce`` is this prompt's nonce in the plugin's UNION setup round
    (sealed by the reconciler before any keyboard posted), so a superseded
    keyboard's late deny/expiry can never decide a re-prompted member.
    """
    identity = ack_identity(plugin, effective, declaration_digest)
    key = CallbackConsentKey(plugin=plugin, artifact_id=artifact_id,
                             effective=effective, identity=identity)
    text = render_callback_consent_message(plugin=plugin, effective=effective)

    def _on_commit_sync(idx: int, meta: dict) -> None:
        # Telegram callback, IMMEDIATELY after a successful commit (no await
        # between): idx 0 -> persist the ack atomically; idx 1 -> no-op. An
        # exception here is swallowed+logged by the callback; ``acked`` stays
        # absent and the finish hook edits the internal-error text — a consent
        # that failed to persist must never open an endpoint.
        import consent_denials
        if idx != 0:
            # #494: the operator's latest decision is a Deny — recorded in
            # this same commit-ordered step so the on-demand re-prompt path
            # can never nag past it. Expiry records nothing (finish hook).
            consent_denials.record(consent_denials.key("callback", identity))
            return
        consent_denials.clear(consent_denials.key("callback", identity))
        rec = acks.record(plugin=plugin, effective=effective,
                          declaration_digest=declaration_digest)
        meta["acked"] = True
        # Record the approval in the setup-round ledger in this SAME yield-free
        # step (the trigger-consent discipline): a crash before the async
        # finish hook must not strand the union round.
        try:
            import plugin_setup_episodes
            plugin_setup_episodes.record_approval_sync(
                plugin=plugin, artifact_id=artifact_id, identity=identity,
                gen=str((rec or {}).get("gen", "")))
        except Exception:  # noqa: BLE001
            logger.exception("sync setup-approval record failed (plugin=%s)",
                             plugin)

    async def _feed_setup_episode(approved: bool) -> None:
        # Every TERMINAL decision (approve, deny, expiry) feeds the durable
        # evaluator. Approvals carry the persisted ack's approval GENERATION;
        # denials carry this keyboard's NONCE so a superseded keyboard's late
        # expiry is ignored. Never raises into the finish hook.
        try:
            import plugin_setup_episodes
            gen = ""
            if approved:
                try:
                    gen = str((acks.get(identity) or {}).get("gen", ""))
                except Exception:  # noqa: BLE001
                    gen = ""
            await plugin_setup_episodes.on_consent_decision(
                plugin=plugin, artifact_id=artifact_id, identity=identity,
                approved=approved, approval_gen=gen, nonce=setup_nonce)
        except Exception:  # noqa: BLE001
            logger.exception("setup-episode feed failed (plugin=%s)", plugin)

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"⌛ Expired — consent for GET /callback/{effective} was "
                    "not answered; it stays closed",
                )
                await _feed_setup_episode(approved=False)
                return
            if outcome.get("option_index") == 0:
                if not req.meta.get("acked"):
                    # Commit landed but the sync step never persisted the ack
                    # (raised + swallowed) — surface the internal error and
                    # NEVER open an endpoint the store cannot back.
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "internal error recording the callback consent — "
                        "re-run the plugin mutation to be prompted again",
                    )
                    return
                # Edit the SUCCESS state FIRST, then reconcile, then overwrite
                # ONLY on failure (the authz edit-first ordering).
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"✅ Open — GET /callback/{effective} now accepts "
                    f"authorization redirects for '{plugin}'",
                )
                # The approval is DURABLE here (the ack is persisted) — feed
                # the setup evaluator REGARDLESS of the reconcile outcome
                # below; gating on the reconcile would strand the round on a
                # transient failure (the ack exists, so no re-prompt follows).
                await _feed_setup_episode(approved=True)
                if reconcile_cb is not None:
                    try:
                        await reconcile_cb()
                    except Exception:  # noqa: BLE001 — surface, never raise
                        logger.exception(
                            "post-consent callback reconcile failed "
                            "(plugin=%s effective=%s)", plugin, effective)
                        await channel.edit_dm_message(
                            chat_id, message_id,
                            f"⚠️ Approved, but opening /callback/{effective} "
                            "failed — run plugin_verify",
                        )
                try:
                    import plugin_setup_episodes
                    plugin_setup_episodes.kick()
                except Exception:  # noqa: BLE001
                    pass
            else:
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"❌ Denied — GET /callback/{effective} stays closed",
                )
                await _feed_setup_episode(approved=False)

        return _finish

    return coordinator.register_challenge(
        key, chat_id=chat_id, operator_id=operator_id, channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
        kind="callback_consent",
        meta_extra={"callback_effective": effective,
                    "callback_declared": declared,
                    "callback_plugin": plugin},
        timeout_s=CALLBACK_CONSENT_TTL_S,
    )
