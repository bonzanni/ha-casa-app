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
* Deny / expiry → the subscription stays undelivered; the next lifecycle
  reconcile may re-prompt. A Deny is stored as a denial in :mod:`event_acks`
  (the same synchronous commit step), so it is off on its own, also after a
  restart, and the Deny fires the same reconcile an Approve does, so the
  subscriber's other approved subscriptions route at once; after an expiry
  nothing is stored: it is still ``event_pending_ack`` and holds back the
  subscriber's others.

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
trigger consents, and callback consents (broker scope
``authz:<bot>:<chat>``): the handler fail-closes on the meta's
``bot_id``/``chat_id``/``operator_id``, so an unauthorized or stale tap can
never ack. The card is Casa's own, posted on Casa's bot
(``trigger_consent.casa_consent_target``).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from channels.tg_address import TgAddress
from plugin_events import ack_identity

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
    address: TgAddress


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


def event_card_request(*, subscriber: str, emitter: str, event: str,
                       targets: "list[str]") -> Any:
    """The approval card asked for one event subscription: the facts a
    model writes it from (the renderer's inputs) and, as the fallback,
    :func:`render_event_consent_message` kept literally. Display only."""
    import approval_card
    who = f"the {subscriber} plugin"
    return approval_card.CardRequest(
        kind="event_consent", who=who,
        facts={"requested_by": who, "subscriber": subscriber, "event": event,
               "emitted_by": emitter, "delivered_to": _target_roles(targets)},
        fallback=approval_card.text_fallback(render_event_consent_message(
            subscriber=subscriber, emitter=emitter, event=event, targets=targets)))


def prompt_event_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int,
    subscriber: str, artifact_id: str, emitter: str, event: str,
    digest: str, targets: "list[str]", acks: Any,
    reconcile_cb: "Callable[[], Awaitable[None]] | None" = None,
) -> Any:
    """Post (or dedupe onto) the consent keyboard for ONE event subscription.

    Returns the coordinator's ``ChallengeHandle``. ``acks`` is the
    :class:`event_acks.EventAckStore`; ``reconcile_cb`` re-runs the event
    reconciler after an approve so delivery starts immediately, and after a
    deny so the subscriber's other approved subscriptions route. Standalone
    from any plugin-setup round — see the module docstring.
    """
    identity = ack_identity(subscriber, artifact_id, emitter, event, digest,
                            targets)
    address = TgAddress(channel.bot_id, chat_id)
    key = EventConsentKey(plugin=subscriber, artifact_id=artifact_id,
                          emitter=emitter, event=event, identity=identity,
                          address=address)
    text = render_event_consent_message(subscriber=subscriber, emitter=emitter,
                                        event=event, targets=targets)

    def _on_commit_sync(idx: int, meta: dict) -> None:
        # Telegram callback, IMMEDIATELY after a successful commit (no await
        # between): idx 0 -> persist the ack atomically; idx 1 -> persist
        # the denial. An exception here is swallowed+logged by the callback;
        # ``acked`` stays absent and the finish hook edits the internal-error
        # text — a consent that failed to persist must never start delivery.
        if idx != 0:
            # The latest decision is a Deny, stored beside the acks so the
            # subscription stays off across a restart (#1666) and the
            # on-demand re-prompt path never nags past it (#494). Expiry
            # stores nothing; the finish hook owns that path. A Deny that
            # could not be written leaves the subscription unanswered.
            acks.deny(subscriber, artifact_id, emitter, event, digest,
                      targets, time.time())
            return
        # ``record`` clears an earlier denial of this identity in its write.
        acks.record(subscriber, artifact_id, emitter, event, digest, targets,
                   time.time())
        meta["acked"] = True

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                import ask_retirement

                # #933: the reason the broker delivered names the cause;
                # a withdrawal is not an expiry.
                await channel.edit_dm_message(
                    chat_id, message_id,
                    ask_retirement.retirement_headline(
                        f"consent for '{subscriber}' to receive "
                        f"'{event}' from '{emitter}'",
                        o, outcome.get("reason") if isinstance(outcome, dict)
                        else None,
                        consequence="it stays undelivered"),
                    literal=True,
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
                        literal=True,
                    )
                    return
                # #1642: take the buttons off at once, then write the result
                # from the routing map and issues the reconcile produced. A
                # subscriber's subscriptions are delivered all together, so
                # this approval alone may deliver nothing; the card blames
                # the other approvals only when they are its sole gap.
                await channel.edit_dm_message(
                    chat_id, message_id, "✅ Approved — checking…",
                    literal=True)
                issues: Any = ()
                if reconcile_cb is not None:
                    try:
                        issues = await reconcile_cb() or ()
                    except Exception:  # noqa: BLE001 — surface, never raise
                        logger.exception(
                            "post-consent event reconcile failed "
                            "(subscriber=%s emitter=%s event=%s)",
                            subscriber, emitter, event)
                        await channel.edit_dm_message(
                            chat_id, message_id,
                            f"⚠️ Approved, but starting delivery of '{event}' "
                            "failed — run plugin_verify",
                            literal=True,
                        )
                        return
                import event_reconcile
                routed = event_reconcile.get_routed()
                delivered = isinstance(routed, dict) and subscriber in (
                    routed.get((emitter, event)) or {})
                gaps = {i.get("reason_code") for i in issues
                        if isinstance(i, dict) and i.get("name") == subscriber}
                if delivered:
                    text = (f"✅ Enabled — '{subscriber}' now receives "
                            f"'{event}' from '{emitter}' "
                            f"({_target_roles(targets)})")
                elif gaps == {"event_pending_ack"}:
                    text = (f"✅ Approved — '{subscriber}' starts receiving "
                            f"'{event}' from '{emitter}' once its other "
                            "subscriptions are approved too")
                else:
                    text = (f"✅ Approved — '{subscriber}' is not receiving "
                            f"'{event}' yet. Ask me to check the plugin.")
                await channel.edit_dm_message(
                    chat_id, message_id, text, literal=True)
            else:
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"❌ Denied — '{subscriber}' will not receive '{event}' "
                    f"from '{emitter}'",
                    literal=True,
                )
                # A denied subscription no longer holds back the subscriber's
                # other approved ones (#1656): reconcile now so they route at
                # once instead of on some later pass. The callback reconciles
                # without prompting, so this card is not re-asked.
                if reconcile_cb is not None:
                    try:
                        await reconcile_cb()
                    except Exception:  # noqa: BLE001 — surface, never raise
                        logger.exception(
                            "post-deny event reconcile failed "
                            "(subscriber=%s emitter=%s event=%s)",
                            subscriber, emitter, event)
                        await channel.edit_dm_message(
                            chat_id, message_id,
                            f"❌ Denied — '{subscriber}' will not receive "
                            f"'{event}' from '{emitter}'. ⚠️ Updating its "
                            "other subscriptions failed — run plugin_verify",
                            literal=True,
                        )

        return _finish

    return coordinator.register_challenge(
        key, bot_id=address.bot_id, chat_id=chat_id, operator_id=operator_id,
        channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
        kind="event_consent",
        meta_extra={"event_subscriber": subscriber, "event_emitter": emitter,
                    "event_name": event, "event_targets": sorted(targets)},
        timeout_s=EVENT_CONSENT_TTL_S,
        card_request=event_card_request(subscriber=subscriber, emitter=emitter,
                                        event=event, targets=targets),
    )
