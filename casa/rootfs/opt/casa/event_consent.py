"""Operator-consent DM prompts for plugin-declared event subscriptions.

A plugin's declared ``casa.subscribes`` entry delivers events ONLY after the
operator taps Approve on a DM keyboard bound to the subscription's full
consent identity (:func:`plugin_events.ack_identity` — subscriber +
subscriber artifact id + emitter + event + declaration digest + sorted
delivery targets). This module is the event flavor on the generic
:class:`authz_grants.ChallengeCoordinator`, structurally the sibling of
:mod:`callback_consent` (read whole before touching this file — its idioms
are mirrored here) and, on the identity side, of :mod:`trigger_consent`:

* Approve → record the ack in :mod:`event_acks` (the SYNCHRONOUS commit
  step, before any await) and fire a reconcile so delivery starts — never
  an agent-continuation dispatch.
* Deny / expiry → the subscription stays undelivered (``event_pending_ack``);
  the next lifecycle reconcile may re-prompt.

**Deliberately standalone from the plugin-setup round ledger** (Critical-2,
review round 1): unlike the trigger/callback consent flavors, event consent
never feeds :mod:`plugin_setup_episodes`. Spec §6 never couples an event
subscription to a plugin's setup round, and the SUBSCRIBER's own registry
name doubling as ``plugin_setup_episodes``' round key made feeding it
actively dangerous: a subscriber plugin that ALSO happens to have its own
live trigger-consent round open would have an event consent's deny/expiry
insert a bogus, unrelated member into that round (the identity spaces are
disjoint — an event consent identity is never a round member — so the
nonce fence that protects a real member never applies), corrupting its
settlement and firing a wrong "trigger(s)" note; an approve could likewise
synthesize a round and dispatch that plugin's setup tool for an unrelated
reason. Approve/deny/expiry here only ever (a) mutate :mod:`event_acks`
and (b) edit the DM and re-reconcile — nothing else.

What the operator approves is deliberately EXPLICIT about the delivery
target: unlike a callback (which grants no turn or memory access), a
subscription reaches into a subscriber role — so the prompt names the
emitter, the event, and the target role(s) receiving it, exactly as the
identity binds them.

Taps ride the SAME validated Telegram DM callback path as authz grants,
trigger consents, and callback consents (broker scope ``authz:{chat}``): the
handler fail-closes on the meta's ``chat_id``/``operator_id``, so an
unauthorized or stale tap can never ack.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from plugin_events import ack_identity
# The operator-DM rule is one rule for every consent kind — imported rather
# than re-implemented so the two can never drift apart.
from trigger_consent import operator_identity  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

# Same TTL as trigger/callback consent: a consent decision follows an
# operator-driven plugin mutation but is not turn-scoped.
EVENT_CONSENT_TTL_S = 600.0


@dataclass(frozen=True)
class EventConsentKey:
    """Challenge-dedup key for one pending event-subscription consent.

    The field is named ``plugin`` (holding the SUBSCRIBER's registry name,
    not the emitter) to match ``ChallengeCoordinator.cancel_matching``'s
    generic ``plugin=`` filter (``getattr(k, "plugin", None)``) — the same
    field name ``TriggerConsentKey``/``CallbackConsentKey`` use, so a
    plugin-lifecycle revoke kills a subscriber's live event keyboards the
    same way it kills its trigger/callback ones. ``artifact_id`` lets a
    lifecycle artifact invalidation cancel a keyboard whose artifact just
    changed. ``emitter``/``event`` distinguish the subscription itself (a
    subscriber may declare several); ``identity`` binds the full approved
    tuple including the target set.
    """

    plugin: str
    artifact_id: str
    emitter: str
    event: str
    identity: str


def _target_roles(targets: "list[str]") -> str:
    """The human-readable role list for the consent prose: the bare role
    name after the ``resident:``/``specialist:`` prefix, sorted, joined with
    commas. Never empty in a well-formed prompt — an empty target list is a
    verify-time rejection upstream, but this renders gracefully anyway."""
    roles = sorted(t.split(":", 1)[1] if ":" in t else t for t in targets)
    return ", ".join(roles) if roles else "(no target)"


def render_event_consent_message(*, subscriber: str, emitter: str, event: str,
                                  targets: "list[str]") -> str:
    """The verbatim consent prose. Only the subscriber, emitter, event name,
    and target role(s) are interpolated — all grammar-validated identifiers,
    never plugin-authored prose. The target role(s) are named because,
    unlike a callback, this consent grants delivery INTO a subscriber role."""
    roles = _target_roles(targets)
    return (
        "\U0001F510 Plugin event subscription consent\n\n"
        f"Plugin '{subscriber}' wants to receive the '{event}' event from "
        f"'{emitter}', delivered to {roles}.\n\n"
        "Approve to deliver it; Deny to leave it undelivered."
    )


def prompt_event_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int,
    subscriber: str, artifact_id: str, emitter: str, event: str,
    digest: str, targets: "list[str]", acks: Any,
    reconcile_cb: "Callable[[], Awaitable[None]] | None" = None,
) -> Any:
    """Post (or dedupe onto) the consent keyboard for ONE event subscription.

    Returns the coordinator's ``ChallengeHandle``. ``acks`` is the
    :class:`event_acks.EventAckStore`; ``reconcile_cb`` re-runs the event
    reconciler after an approve so delivery starts immediately. Standalone
    from any plugin-setup round — see the module docstring.
    """
    identity = ack_identity(subscriber, artifact_id, emitter, event, digest,
                            targets)
    key = EventConsentKey(plugin=subscriber, artifact_id=artifact_id,
                          emitter=emitter, event=event, identity=identity)
    text = render_event_consent_message(subscriber=subscriber, emitter=emitter,
                                        event=event, targets=targets)

    def _on_commit_sync(idx: int, meta: dict) -> None:
        # Telegram callback, IMMEDIATELY after a successful commit (no await
        # between): idx 0 -> persist the ack atomically; idx 1 -> no-op. An
        # exception here is swallowed+logged by the callback; ``acked`` stays
        # absent and the finish hook edits the internal-error text — a
        # consent that failed to persist must never start delivery.
        import consent_denials
        if idx != 0:
            # #494: latest decision is a Deny — commit-ordered record so the
            # on-demand re-prompt path never nags past it (expiry records
            # nothing; the finish hook owns that path).
            consent_denials.record(consent_denials.key("event", identity))
            return
        consent_denials.clear(consent_denials.key("event", identity))
        acks.record(subscriber, artifact_id, emitter, event, digest, targets,
                   time.time())
        meta["acked"] = True

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"⌛ Expired — consent for '{subscriber}' to receive "
                    f"'{event}' from '{emitter}' was not answered; it stays "
                    "undelivered",
                )
                return
            if outcome.get("option_index") == 0:
                if not req.meta.get("acked"):
                    # Commit landed but the sync step never persisted the ack
                    # (raised + swallowed) — surface the internal error and
                    # NEVER start delivery the store cannot back.
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "internal error recording the event consent — "
                        "re-run the plugin mutation to be prompted again",
                    )
                    return
                # Edit the SUCCESS state FIRST, then reconcile, then overwrite
                # ONLY on failure (the authz edit-first ordering).
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"✅ Enabled — '{subscriber}' now receives '{event}' "
                    f"from '{emitter}' ({_target_roles(targets)})",
                )
                if reconcile_cb is not None:
                    try:
                        await reconcile_cb()
                    except Exception:  # noqa: BLE001 — surface, never raise
                        logger.exception(
                            "post-consent event reconcile failed "
                            "(subscriber=%s emitter=%s event=%s)",
                            subscriber, emitter, event)
                        await channel.edit_dm_message(
                            chat_id, message_id,
                            f"⚠️ Approved, but starting delivery of '{event}' "
                            "failed — run plugin_verify",
                        )
            else:
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"❌ Denied — '{subscriber}' will not receive '{event}' "
                    f"from '{emitter}'",
                )

        return _finish

    return coordinator.register_challenge(
        key, chat_id=chat_id, operator_id=operator_id, channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
        kind="event_consent",
        meta_extra={"event_subscriber": subscriber, "event_emitter": emitter,
                    "event_name": event, "event_targets": sorted(targets)},
        timeout_s=EVENT_CONSENT_TTL_S,
    )
