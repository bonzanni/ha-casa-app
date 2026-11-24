"""Reading address-book notes with a small model.

What a note says about a person (how they relate to the owner, which language
they speak) is meaning, so a model reads it; code only checks the shape of the
answer. A reading becomes ``note``-origin marks (:mod:`contacts.sync`), the
weakest origin, so the operator's own marks always win. Priority never comes
from a note: a reading carries only ``relation`` and ``language``.

:class:`NoteModel` is the production port: one isolated query with no tools
and no session state, on :data:`MODEL_SHORTNAME`. :func:`read` takes any
object with the same ``complete`` coroutine.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from claude_runtime import (
    CLAUDE_CLI_PATH,
    SDK_MAX_BUFFER_SIZE,
    cli_session_settings,
    ephemeral_extra_args,
    ephemeral_setting_sources,
    with_cross_session_tools_denied,
    with_system_prompt_file,
)
from config import effort_for, resolve_model
from contacts.store import Person

logger = logging.getLogger(__name__)

MODEL_SHORTNAME = "haiku"
#: People sent to the model in one query.
BATCH_SIZE = 20
TIMEOUT_S = 60.0
#: Bumped with every change to the prompt's rules: a note settled under an
#: older version is read once more (:func:`contacts.store.settled_hash`).
PROMPT_VERSION = 2

_PROMPT = Path(__file__).with_name("prompts") / "note_reader.md"


def system_prompt() -> str:
    return _PROMPT.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Reading:
    """What one note says: ``relation`` (free text, ``""`` when unsaid) and
    ``language`` (``""`` or a lowercase ISO 639-1 code)."""
    relation: str
    language: str


class Model(Protocol):
    async def complete(self, *, system: str, prompt: str) -> str: ...


def model_options(system: str):
    """Options for one isolated query: no tools, no session state, no
    settings, on the small model."""
    import claude_agent_sdk as sdk

    model = resolve_model(MODEL_SHORTNAME)
    return sdk.ClaudeAgentOptions(
        model=model,
        effort=effort_for(model),
        cli_path=CLAUDE_CLI_PATH,
        max_buffer_size=SDK_MAX_BUFFER_SIZE,
        # The turn cap is a runaway backstop only. Built-in tools are removed
        # outright (allowed_tools alone is only auto-approval), and Agent/Task
        # and Bash are denied on top.
        system_prompt=system, max_turns=8,
        tools=[], allowed_tools=[],
        disallowed_tools=with_cross_session_tools_denied(
            ["Bash", "Task", "Agent"]),
        settings=cli_session_settings(),
        setting_sources=ephemeral_setting_sources(),
        extra_args=ephemeral_extra_args(),
        # Not bypassPermissions: the CLI refuses it as root, which is how the
        # app runs. With nothing allowed there is nothing to approve.
        permission_mode="acceptEdits",
    )


class NoteModel:
    """The production model port."""

    async def complete(self, *, system: str, prompt: str) -> str:
        import claude_agent_sdk as sdk

        opts = with_system_prompt_file(model_options(system))
        reply = ""
        async with asyncio.timeout(TIMEOUT_S):
            async for msg in sdk.query(prompt=prompt, options=opts):
                if isinstance(msg, sdk.AssistantMessage):
                    for block in getattr(msg, "content", []) or []:
                        text = getattr(block, "text", None)
                        if isinstance(text, str):
                            reply += text
        return reply


def _payload(people: list[Person]) -> str:
    return json.dumps(
        [{"uid": p.uid, "name": p.name, "org": p.org,
          "related": list(p.related), "note": p.note} for p in people],
        ensure_ascii=False)


def _reading(entry: object) -> Reading | None:
    """A valid reading from one entry of the reply, or None."""
    if not isinstance(entry, dict):
        return None
    relation, language = entry.get("relation"), entry.get("language")
    if not isinstance(relation, str) or not isinstance(language, str):
        return None
    language = language.strip().lower()
    if language and not (len(language) == 2 and language.isascii()
                         and language.isalpha()):
        return None
    return Reading(relation=relation.strip(), language=language)


def _parse(reply: str, uids: list[str]) -> dict[str, Reading]:
    """The valid readings for *uids* in *reply*; a reply wrapped in prose or
    a code fence is read from its outermost braces."""
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        obj = json.loads(reply[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(obj, dict):
        return {}
    readings: dict[str, Reading] = {}
    for uid in uids:
        reading = _reading(obj.get(uid))
        if reading is not None:
            readings[uid] = reading
    return readings


async def read(model: Model, people: list[Person]) -> dict[str, Reading]:
    """The valid readings of *people*'s notes, by uid, in batches of
    :data:`BATCH_SIZE`. A uid whose entry is missing or malformed is absent,
    as is every uid of a batch whose query fails. Never raises."""
    readings: dict[str, Reading] = {}
    for i in range(0, len(people), BATCH_SIZE):
        batch = people[i:i + BATCH_SIZE]
        try:
            reply = await model.complete(system=system_prompt(),
                                         prompt=_payload(batch))
        except Exception as exc:  # noqa: BLE001 — a failed read changes nothing
            logger.warning("contact notes not read (%d people): %s",
                           len(batch), type(exc).__name__)
            continue
        got = _parse(reply, [p.uid for p in batch])
        if len(got) < len(batch):
            logger.warning("contact notes: %d of %d readings unusable",
                           len(batch) - len(got), len(batch))
        readings.update(got)
    return readings
