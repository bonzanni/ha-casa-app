"""Structural operator-consent gate for the bare-persona install pipeline —
sibling of specialist_install_consent.py, same ChallengeCoordinator pattern
(Round-2, finding #3)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from persona_install import PersonaInstallAckStore, persona_install_consent_identity

logger = logging.getLogger(__name__)

INSTALL_CONSENT_TTL_S = 600


@dataclass(frozen=True)
class PersonaInstallConsentKey:
    """Same shape as specialist_install_consent.SpecialistInstallConsentKey —
    a plain frozen dataclass, matching trigger_consent.TriggerConsentKey's
    real pattern (no shared base class exists in the codebase)."""
    persona_id: str
    identity: str


def render_persona_install_consent_message(inspection: Any) -> str:
    return (
        "\U0001F510 Persona install consent\n\n"
        f"Install '{inspection.persona_id}@{inspection.version}' "
        f"({inspection.display_name})?\n"
        f"Checksum: {inspection.checksum}\n\n"
        "Approve to install; Deny to discard the staged fetch."
    )


def prompt_persona_install_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int, inspection: Any,
    acks: "PersonaInstallAckStore",
    reconcile_cb: "Callable[[], Awaitable[bool]] | None" = None,
    inbound_reservation: Any | None = None,
) -> Any:
    # #663: sibling of specialist_install_consent — the requesting
    # engagement's SYNCHRONOUS ingress lease, duck-typed (``take`` /
    # ``release``) so this module stays ignorant of engagements.
    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum)
    key = PersonaInstallConsentKey(persona_id=inspection.persona_id, identity=identity)
    text = render_persona_install_consent_message(inspection)
    # #543: capture the revocation generations AT PROMPT TIME. The Telegram
    # callback commits the broker answer and calls `_on_commit_sync` with no
    # await between them — but a persona_remove/persona_ack_revoke worker runs
    # on a DIFFERENT thread and can revoke in that window, and an
    # already-answered challenge can no longer be cancelled. Carrying the
    # generations through to `record` is what makes the revoke authoritative:
    # a tap that lands after one writes nothing at all.
    generations = acks.revocation_generations(
        persona_id=inspection.persona_id, version=inspection.version)

    def _on_commit_sync(idx: int, meta: dict) -> None:
        if idx == 0:
            meta["acked"] = acks.record(
                identity=identity, persona_id=inspection.persona_id,
                version=inspection.version, checksum=inspection.checksum,
                expect_generations=generations)
            # #663: AFTER ``acks.record`` and after its RETURN VALUE has been
            # consumed — #543's revocation generations decide whether anything
            # was written at all, and this step must not reorder around that.
            # Approve only; Deny dispatches no continuation.
            if inbound_reservation is not None:
                inbound_reservation.take()

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            # #663: release-guarantee — see the specialist sibling. The whole
            # body is wrapped so every arm that never reaches the delivery
            # seam still disposes of the reservation; the lease is idempotent
            # because the seam releases it too on the ordinary path.
            try:
                await _finish_inner(outcome)
            finally:
                if inbound_reservation is not None:
                    inbound_reservation.release()

        async def _finish_inner(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"⌛ Expired — persona install consent for {inspection.persona_id!r} was not "
                    "answered; nothing was installed")
                return
            if outcome.get("option_index") == 0:
                if not req.meta.get("acked"):
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        # #543: this now covers two cases — a genuine write
                        # failure, and an approval that arrived after the
                        # persona's consent was revoked (persona_remove /
                        # persona_ack_revoke). Neither recorded anything, and
                        # the remedy is the same.
                        "this approval was not recorded (the persona's consent was "
                        "revoked, or the write failed) — re-run the install to be "
                        "prompted again")
                    return
                # #662: sibling of specialist_install_consent — the outcome
                # decides the wording, so the edit happens AFTER reconciliation
                # and only a literal True selects the success text. What True
                # establishes is stated once, in INV-PERS-011 (which defers to
                # INV-SPEC-010 for the detail).
                outcome: object = False
                raised = False
                if reconcile_cb is not None:
                    try:
                        outcome = await reconcile_cb()
                    except Exception:  # noqa: BLE001 — never-raise contract;
                        # defence in depth, CancelledError stays control flow.
                        logger.exception(
                            "post-consent persona install reconcile raised "
                            "(persona_id=%s)", inspection.persona_id)
                        outcome, raised = False, True
                if outcome is True:
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "✅ Approved — requested an automatic configurator "
                        f"continuation for {inspection.persona_id!r}")
                elif raised:
                    # See the specialist sibling: the one branch where the
                    # outcome is unknown rather than known-negative.
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"⚠️ Approved and saved — but the install of "
                        f"{inspection.persona_id!r} hit an internal error and "
                        "its automatic start could not be confirmed. Check the "
                        "configurator topic; re-running the install is safe "
                        "either way, and the approval recorded for this exact "
                        "version is reused if it still applies.")
                else:
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"⚠️ Approved and saved — but the install of "
                        f"{inspection.persona_id!r} was not started "
                        "automatically. Start a new configurator engagement "
                        "and re-run the install; the approval recorded for "
                        "this exact version is reused if it still applies.")
            else:
                await channel.edit_dm_message(
                    chat_id, message_id, f"❌ Denied — {inspection.persona_id!r} was not installed")

        return _finish

    return coordinator.register_challenge(
        key, chat_id=chat_id, operator_id=operator_id, channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
        kind="persona_install_consent",
        meta_extra={"persona_id": inspection.persona_id, "persona_version": inspection.version},
        timeout_s=INSTALL_CONSENT_TTL_S,
    )
