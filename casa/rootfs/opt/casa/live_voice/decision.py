"""The decision step of a live voice session.

After each thing the agent says, one model call reads the conversation so far
and may ask for one ability. The model decides; this module only builds the
request and reads the answer: the first function call that names a granted
ability, ``None`` when the model asks for nothing, and :data:`FAILED` when no
answer arrived. A failed decision is not "no action": the session retries it
and, if it fails again, stops relying on it.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, Sequence

from live_voice.api import ApiError, LiveApi

if TYPE_CHECKING:
    from live_voice.session import Ability, Line

logger = logging.getLogger(__name__)


class _Failed:
    def __repr__(self) -> str:
        return "FAILED"


#: The decision could not be made (an API error or a timeout).
FAILED: Final = _Failed()

_SPEAKER = {"caller": "Caller", "agent": "Assistant"}


def function_tools(abilities: Sequence[Ability]) -> list[dict]:
    """The abilities as function tools; arguments outside the schema are
    refused by the model API."""
    return [{"type": "function", "name": ability.name,
             "description": ability.description,
             "parameters": {"type": "object", **ability.parameters,
                            "additionalProperties": False}}
            for ability in abilities]


def conversation(lines: Sequence[Line]) -> str:
    """The lines so far, one per row, as the decision model reads them."""
    return "The call so far:\n" + "\n".join(
        f"{_SPEAKER[line.who]}: {line.text}" for line in lines)


async def decide(api: LiveApi, *, model: str, instructions: str,
                 lines: Sequence[Line], abilities: Sequence[Ability],
                 timeout_s: float) -> tuple[str, dict] | None | _Failed:
    """Ask the decision model once: ``(ability, arguments)``, ``None`` for no
    action, or :data:`FAILED`."""
    try:
        calls = await api.respond(
            model=model, instructions=instructions, input=conversation(lines),
            tools=function_tools(abilities), timeout_s=timeout_s)
    except ApiError as exc:
        logger.warning("decision failed: %s (status %s)", exc.code, exc.status)
        return FAILED
    granted = {ability.name for ability in abilities}
    for call in calls:
        if call["name"] in granted:
            return call["name"], call["arguments"]
    return None
