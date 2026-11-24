"""The only things a phone call can cause: end the call, put it through to the
operator's number, and, on a call that came back from such a transfer, alert
the operator.

Each is an :class:`live_voice.session.Ability` whose runner is plain Casa
code with one free-text argument. The texts sent to the operator are written
here and carry nothing a model wrote: only the caller's label, which Casa
builds from the contact's name or the number.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from live_voice.actions import Outcome
from live_voice.api import ApiError, LiveApi
from live_voice.session import Ability, LiveSession
from output_boundary import Admitted, casa_text

logger = logging.getLogger(__name__)

TRANSFER_REFUSED = "The transfer did not go through. Take a message instead."

#: Sends one Casa-written text to the operator; True when it was delivered.
Notify = Callable[[Admitted], Awaitable[bool]]


def _schema(name: str, description: str) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": [name],
            "properties": {name: {"type": "string",
                                  "description": description}}}


async def _send(notify: Notify | None, text: Admitted) -> bool:
    """Send *text* to the operator; a failure is logged and answered False,
    never raised."""
    if notify is None:
        return False
    try:
        return bool(await notify(text))
    except Exception:  # noqa: BLE001 — a failed notice never fails the call
        logger.warning("phone: a notice to the operator was not sent",
                       exc_info=True)
        return False


def build(*, api: LiveApi, sid: str, session_ref: Callable[[], LiveSession],
          transfer_number: str, transfer_level: str, returned: bool,
          notify: Notify | None, caller_label: str) -> tuple[Ability, ...]:
    """The abilities of one call. ``session_ref`` gives the session, which is
    created after its abilities; ``caller_label`` names the caller in texts
    to the operator."""

    def _current(round: int) -> Outcome | None:
        """Why a request for *round* must not act now, or None."""
        actions = session_ref().actions
        if actions.round > round:
            return Outcome("superseded", "")
        if actions.phase != "active":
            return Outcome("final", actions.phase)
        return None

    async def _end_call(args: dict, round: int) -> Outcome:
        try:
            await api.hangup(sid)
        except ApiError as exc:
            logger.warning("phone: hang-up refused: %s (status %s)",
                           exc.code, exc.status)
            return Outcome("refused", "The call could not be ended.")
        return Outcome("done", "")

    async def _transfer(args: dict, round: int) -> Outcome:
        stop = _current(round)
        if stop is not None:
            return stop
        # The operator's phone will show Casa's own line, not the caller, so
        # the operator is told who it is first.
        await _send(notify, casa_text("Putting through: " + caller_label))
        stop = _current(round)
        if stop is not None:
            return stop
        # Nothing is awaited between the check above and the request.
        try:
            await api.refer(sid, "tel:" + transfer_number)
        except ApiError as exc:
            if 400 <= exc.status < 500:
                logger.info("phone: transfer refused: %s (status %s)",
                            exc.code, exc.status)
                return Outcome("refused", TRANSFER_REFUSED)
            # No answer or a server error: the provider may have accepted
            # it, so it is treated as accepted and never sent again.
            logger.warning("phone: transfer uncertain: %s (status %s)",
                           exc.code, exc.status)
        session_ref().actions.mark_pending("transfer")
        return Outcome("done", "")

    async def _alert(args: dict, round: int) -> Outcome:
        # The model's reason is not sent: this line is code-written alone,
        # and what the caller said reaches the operator with the call record.
        text = casa_text(
            f"Urgent, from the phone: {caller_label} was put through, you did "
            f"not pick up, and they say it is an emergency. The details "
            f"follow when the call ends.")
        if await _send(notify, text):
            return Outcome("done", "The alert was sent.")
        return Outcome("refused", "The alert could not be sent.")

    found = [Ability(
        "end_call",
        "End the call. Only after goodbye has been said and the caller has "
        "nothing more to say.",
        _schema("why", "Why the call ends, in a few words."),
        _end_call, "final")]
    if transfer_level != "off":
        found.append(Ability(
            "transfer",
            "Put the call through to the person whose phone this is, when the "
            "rules for this caller allow it.",
            _schema("why", "Why the call is put through, in a few words."),
            _transfer, "plain"))
    if returned:
        found.append(Ability(
            "alert_operator",
            "Send the person whose phone this is an urgent alert. Only when "
            "the caller, who was just put through and not answered, says it "
            "is an emergency.",
            _schema("reason", "The emergency, in the caller's words, in one "
                              "short line."),
            _alert, "plain"))
    return tuple(found)
