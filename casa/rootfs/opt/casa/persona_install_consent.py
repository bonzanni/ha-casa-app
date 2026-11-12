"""Structural operator-consent gate for the bare-persona install pipeline —
sibling of specialist_install_consent.py, same ChallengeCoordinator pattern.
The card asks about one ``persona_install`` approval record; a persona install
has no settings, so its values are ``{}``."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from persona_install import PersonaInstallAckStore, persona_install_consent_identity
from specialist_install_consent import approval_card_hooks, supersede_card  # noqa: F401 — supersede_card re-exported for persona callers

logger = logging.getLogger(__name__)

INSTALL_CONSENT_TTL_S = 600


@dataclass(frozen=True)
class PersonaInstallConsentKey:
    """Same shape as specialist_install_consent.SpecialistInstallConsentKey —
    a plain frozen dataclass, matching trigger_consent.TriggerConsentKey's
    real pattern (no shared base class exists in the codebase)."""
    persona_id: str
    identity: str
    # #1445: the requesting engagement — see the specialist sibling.
    requester: str = ""
    # The approval record the card asks about — see the specialist sibling.
    approval_rid: str = ""


def render_persona_install_consent_message(inspection: Any) -> str:
    return (
        "\U0001F510 Persona install consent\n\n"
        f"Install '{inspection.persona_id}@{inspection.version}' "
        f"({inspection.display_name})?\n"
        f"Checksum: {inspection.checksum}\n\n"
        "Approve to install; Deny to discard the staged fetch."
    )


def persona_card_request(inspection: Any, *, requester_name: str = "Casa") -> Any:
    """The approval card asked for installing the persona *inspection*: the
    facts a model writes it from (the renderer's inputs, the checksum left
    out) and, as the fallback, :func:`render_persona_install_consent_message`
    kept literally. Display only."""
    import approval_card
    return approval_card.CardRequest(
        kind="persona_install", who=requester_name,
        facts={"requested_by": requester_name, "request": "install a persona",
               "persona": inspection.persona_id, "version": inspection.version,
               "display_name": inspection.display_name},
        fallback=approval_card.text_fallback(
            render_persona_install_consent_message(inspection)))


def prompt_persona_install_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int,
    record: Any, inspection: Any, continuation: str,
    acks: "PersonaInstallAckStore | None" = None,
    requester: str = "",
    requester_name: str = "Casa",
) -> Any:
    """Post the card asking the operator to approve *record*, a
    ``persona_install`` approval record, for the staged persona *inspection*.
    Approve records the consent ack for the record's identity, approves the
    record and has Casa carry the configurator on with *continuation*; the
    specialist sibling (``specialist_install_consent.approval_card_hooks``)
    holds the shared tap and finish behaviour. Raises ``ValueError`` when the
    record's identity is not the identity of *inspection* with the record's
    values. The card is written for the operator by a model
    (:func:`persona_card_request`), titled for *requester_name*, the agent
    whose turn asked."""
    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum,
        values_digest=record.values_digest)
    if identity != record.consent_identity:
        # The ack ledger recomputes every identity from its fields; an ack
        # whose fields do not give its key would invalidate the ledger.
        raise ValueError("the approval record is not for this staged persona")
    acks = acks if acks is not None else PersonaInstallAckStore()
    # #1445: ``requester`` — see the specialist sibling; the record's id keeps a
    # newer request's card apart from the one it replaces.
    key = PersonaInstallConsentKey(persona_id=inspection.persona_id, identity=identity,
                                   requester=requester, approval_rid=record.rid)
    text = render_persona_install_consent_message(inspection)
    # #543: capture the revocation generations AT PROMPT TIME. The Telegram
    # callback commits the broker answer and calls the tap step with no await
    # between them — but a persona_remove/persona_ack_revoke worker runs on a
    # DIFFERENT thread and can revoke in that window, and an already-answered
    # challenge can no longer be cancelled. Carrying the generations through to
    # `record` is what makes the revoke authoritative: a tap that lands after
    # one writes nothing at all.
    generations = acks.revocation_generations(
        persona_id=inspection.persona_id, version=inspection.version)

    def _record_ack() -> bool:
        return acks.record(
            identity=identity, persona_id=inspection.persona_id,
            version=inspection.version, checksum=inspection.checksum,
            values_digest=record.values_digest, expect_generations=generations)

    on_commit_sync, finish_factory = approval_card_hooks(
        record=record, channel=channel, chat_id=chat_id, continuation=continuation,
        name=inspection.persona_id, noun="persona install", record_ack=_record_ack,
        # #543: two cases — a genuine write failure, and an approval that
        # arrived after the persona's consent was revoked (persona_remove /
        # persona_ack_revoke). Neither recorded anything, and the remedy is the
        # same.
        not_recorded_text=("this approval was not recorded (the persona's consent was "
                           "revoked, or the write failed) — re-run the install to be "
                           "prompted again"))

    # The card is Casa's own: *channel* is Casa's endpoint, and the card's
    # broker scope carries that bot.
    return coordinator.register_challenge(
        key, bot_id=channel.bot_id, chat_id=chat_id, operator_id=operator_id,
        channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=on_commit_sync, finish_factory=finish_factory,
        kind="persona_install_consent",
        meta_extra={"persona_id": inspection.persona_id, "persona_version": inspection.version,
                    "approval_rid": record.rid},
        timeout_s=INSTALL_CONSENT_TTL_S,
        card_request=persona_card_request(inspection, requester_name=requester_name),
    )
