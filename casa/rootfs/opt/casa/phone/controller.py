"""One phone call, from the incoming event to its outcome.

:func:`handle_call` looks the caller up, builds what the call's models are
told and may cause, records the call, answers it, follows the live session
until it ends and records how it ended. Then the call's record is handed on
(``Deps.finish``).

Every read and write of the phone store here is best-effort. A store that is
unavailable, or a write that fails, is logged once and the call goes on: it
is answered with the seed voices, the default models and nobody put through
(what :func:`phone.settings.effective` gives without a store), it gets no row
and no transcript, nothing is handed on, and when it ends the operator gets
one line written here.

A call that comes back from a transfer (the caller's number is the line's own
number) is never given an identity: Casa cannot tell reliably which earlier
call came back, and a wrong guess would give one caller another's name.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from contacts import numbers as phone_numbers
from contacts.lookup import (DEFAULT_LANGUAGE, LANGUAGE_BY_COUNTRY_CODE,
                             CallerInfo, lookup)
from contacts.store import ContactsStore
from live_voice.api import ApiError, LiveApi
from live_voice.session import LiveSession, Line, Profile, Result, Timing
from output_boundary import casa_text
from phone import abilities as phone_abilities
from phone import prompt
from phone import store as pstore
from phone.abilities import Notify
from phone.policy import caller_class, transfer_level
from phone.settings import Effective, Options, effective, voice_param
from phone.store import PhoneStore
from phone.webhook import Incoming

logger = logging.getLogger(__name__)

#: How long a call's words are kept.
TRANSCRIPT_KEEP_S = 30 * 24 * 3600
#: The voice when the store and the seeds name none for the language or
#: for English.
FALLBACK_VOICE = "marin"
RETURNED_LABEL = "the caller you were just put through to"
UNRECORDED_NOTE = "It was not recorded: the phone store is unavailable."

_OUTCOME_WORDS = {
    "caller_hung_up": "the caller hung up",
    "ended_by_casa": "the assistant ended it",
    "transferred": "it was put through to you",
    "accept_failed": "it could not be answered",
    "error": "it stopped on an error",
    "interrupted": "it was cut off when Casa stopped",
}
_CLASS_WORDS = {"priority": "priority contact", "known": "contact",
                "unknown": "unknown caller"}


@dataclass(frozen=True)
class Deps:
    """What a call needs from the running service. ``finish`` hands on the
    record of a recorded call, given its id; ``now`` is wall-clock time;
    ``stopping`` is true once Casa has begun to stop: no call is accepted from
    then on. ``cut_by_stop`` is true once the stop has hung up this call,
    which is then recorded as ``interrupted``; a call that ended by itself
    keeps its own outcome. ``on_row`` is told the id of the call row just
    before it is written, ``forget_row`` when that write failed or once the
    call's outcome is written (not when that write fails), and
    ``on_accept`` is called just before the accept is sent."""
    api: LiveApi
    store: PhoneStore | None
    contacts: Callable[[], ContactsStore | None]
    options: Options
    default_region: str
    notify: Notify | None
    finish: Callable[[str], Awaitable[None]]
    timing: Timing
    now: Callable[[], float] = time.time
    stopping: Callable[[], bool] = lambda: False
    cut_by_stop: Callable[[], bool] = lambda: False
    on_row: Callable[[str], None] = lambda call_id: None
    forget_row: Callable[[str], None] = lambda call_id: None
    on_accept: Callable[[], None] = lambda: None
    make_session: Callable[..., LiveSession] = LiveSession


# --- words for the operator -------------------------------------------------

def number_words(number: str) -> str:
    """A number as the operator is told it: its last four digits only."""
    return f"the number ending {number[-4:]}" if number else "a withheld number"


def caller_label(info: CallerInfo, number: str, *, returned: bool) -> str:
    """How texts to the operator name this caller during the call."""
    if returned:
        return RETURNED_LABEL
    if info.known and info.name:
        return info.name
    if number:
        return f"a caller on {number_words(number)}"
    return "a caller with a withheld number"


def describe(row: dict) -> str:
    """One line about an ended call, from its row: who, class, outcome and
    duration. Code-written; nothing in it comes from a model."""
    if row.get("returned"):
        who = "a caller who came back after being put through"
    else:
        who = row.get("name") or number_words(row.get("number") or "")
    kind = _CLASS_WORDS.get(row.get("caller_class") or "", "unknown caller")
    outcome = row.get("outcome") or ""
    words = _OUTCOME_WORDS.get(outcome, outcome or "it ended")
    started, ended = row.get("started_at"), row.get("ended_at")
    seconds = (max(0, round(ended - started))
               if isinstance(started, (int, float))
               and isinstance(ended, (int, float)) else 0)
    return (f"A phone call from {who} ({kind}) has ended: {words}, "
            f"after {seconds} s.")


# --- the call's facts ----------------------------------------------------------

def _language_of(number: str) -> str:
    code = phone_numbers.country_code(number) if number else None
    return LANGUAGE_BY_COUNTRY_CODE.get(code, DEFAULT_LANGUAGE)


async def _caller(number: str, deps: Deps, *, returned: bool) -> CallerInfo:
    if returned:
        return CallerInfo(False, False, "", "", DEFAULT_LANGUAGE)
    try:
        contacts = deps.contacts()
    except Exception:  # noqa: BLE001 — no contacts means an unknown caller
        logger.warning("phone: the contacts store could not be reached",
                       exc_info=True)
        contacts = None
    if contacts is None:
        return CallerInfo(False, False, "", "", _language_of(number))
    return await asyncio.to_thread(lookup, contacts, number,
                                   default_region=deps.default_region)


def outcome_of(result: Result, final_abilities: frozenset[str]) -> str:
    """How the session ended, as recorded on the call."""
    if result.phase == "handed_off":
        return "transferred"
    reason = result.closed_reason
    if reason.startswith("clock:") or reason == "lost":
        return "ended_by_casa"
    # A final ability that was done ended the session from this side,
    # whichever reason the service gave after it.
    if any(a.get("ability") in final_abilities and a.get("status") == "done"
           for a in result.actions):
        return "ended_by_casa"
    return "caller_hung_up"


def transcript_text(lines: tuple[Line, ...]) -> str:
    return "\n".join(
        f"{'Caller' if line.who == 'caller' else 'Assistant'}: {line.text}"
        for line in lines)


@dataclass(frozen=True)
class _Texts:
    instructions: str
    helper: str
    decision: str
    lines: dict[str, str]


def _texts(ctx: prompt.Context) -> _Texts:
    """Blocking: the prompt files are read here."""
    return _Texts(prompt.instructions(ctx), prompt.helper_instructions(ctx),
                  prompt.decision_instructions(ctx),
                  prompt.spoken_lines(ctx.language, ctx.owner_name))


def _voice(found: Effective, language: str) -> str | dict:
    return voice_param(found.voices.get(language)
                       or found.voices.get("en") or FALLBACK_VOICE)


class _Book:
    """The call's use of the phone store: best-effort. The first failure is
    logged and from then on the call goes on without the store."""

    def __init__(self, store: PhoneStore | None, sid: str) -> None:
        self._sid = sid
        self.store = store if store is not None and store.available else None
        if self.store is None:
            reason = store.unavailable_reason if store is not None \
                else "not configured"
            logger.warning("phone: call %s is not recorded: the phone store "
                           "is unavailable (%s)", sid, reason)

    async def write(self, work: Callable[[sqlite3.Connection], object]) -> bool:
        store = self.store
        if store is None:
            return False

        def run() -> None:
            with store.transaction(write=True) as conn:
                work(conn)
        try:
            await asyncio.to_thread(run)
        except Exception:  # noqa: BLE001 — the call goes on unrecorded
            logger.warning("phone: call %s is not recorded: the phone store "
                           "failed", self._sid, exc_info=True)
            self.store = None
            return False
        return True


async def _tell(notify: Notify | None, text: str) -> None:
    if notify is None:
        logger.warning("phone: no operator channel; a call line was not sent")
        return
    try:
        if not await notify(casa_text(text)):
            logger.warning("phone: a call line was not delivered")
    except Exception:  # noqa: BLE001 — a lost line never fails the call
        logger.warning("phone: a call line was not sent", exc_info=True)


# --- the call ------------------------------------------------------------------

async def handle_call(incoming: Incoming, deps: Deps) -> None:
    """Handle one incoming call to its end. Never raises (a cancellation
    passes through)."""
    try:
        await _handle(incoming, deps)
    except Exception:  # noqa: BLE001 — one call never takes the service down
        logger.exception("phone: call %s could not be handled",
                         incoming.session_id)


async def _handle(incoming: Incoming, deps: Deps) -> None:
    sid = incoming.session_id
    number = incoming.from_number
    returned = bool(number) and number == incoming.to_number
    info = await _caller(number, deps, returned=returned)
    kind = "unknown" if returned else caller_class(info)
    book = _Book(deps.store, sid)
    found = await asyncio.to_thread(effective, book.store)
    level = transfer_level(found, kind, returned=returned,
                           transfer_number=deps.options.transfer_number)
    ctx = prompt.Context(
        assistant_name=await asyncio.to_thread(prompt.assistant_name),
        owner_name=prompt.owner_name(deps.options), info=info,
        caller_class=kind, language=info.language, transfer_level=level,
        returned=returned)
    texts = await asyncio.to_thread(_texts, ctx)
    holder: list[LiveSession] = []
    abilities = phone_abilities.build(
        api=deps.api, sid=sid, session_ref=lambda: holder[0],
        transfer_number=deps.options.transfer_number, transfer_level=level,
        returned=returned, notify=deps.notify,
        caller_label=caller_label(info, number, returned=returned))
    profile = Profile(
        instructions=texts.instructions, voice=_voice(found, info.language),
        voice_model=found.models["voice_model"],
        helper_model=found.models["helper_model"],
        helper_instructions=texts.helper,
        decision_model=found.models["decision_model"],
        decision_instructions=texts.decision, abilities=abilities)
    final = frozenset(a.name for a in abilities if a.kind == "final")

    call_id = uuid.uuid4().hex
    row = {"id": call_id, "session_id": sid, "started_at": deps.now(),
           "number": number, "name": info.name, "caller_class": kind,
           "language": info.language, "returned": int(returned)}
    # Announced before the write, so a committed row of this call is never
    # unknown to the service.
    deps.on_row(call_id)
    recorded = await book.write(lambda conn: pstore.insert_call(conn, row))
    if not recorded:
        deps.forget_row(call_id)

    async def close(outcome: str, actions: list, transcript: str | None
                    ) -> None:
        """Record the outcome and hand the record on; for an unrecorded call,
        one line to the operator. Never raises."""
        ended = deps.now()

        def write(conn: sqlite3.Connection) -> None:
            pstore.finish_call(conn, call_id, ended_at=ended, outcome=outcome,
                               actions=actions, record=None)
            if transcript is not None:
                pstore.put_transcript(conn, call_id, transcript,
                                      ended + TRANSCRIPT_KEEP_S)
        if recorded and await book.write(write):
            # The row has its outcome: no longer one to protect.
            deps.forget_row(call_id)
            try:
                await deps.finish(call_id)
            except Exception:  # noqa: BLE001 — the row stays undelivered
                logger.warning("phone: call %s was not handed on", sid,
                               exc_info=True)
            return
        line = describe({**row, "ended_at": ended, "outcome": outcome})
        await _tell(deps.notify, f"{line} {UNRECORDED_NOTE}")

    answered = False
    try:
        if deps.stopping():
            # Casa began to stop: the call is not answered (the carrier's
            # fallback takes it) and is recorded as cut off.
            logger.info("phone: call %s not accepted: Casa is stopping", sid)
            await close("interrupted", [], None)
            return
        # Nothing is awaited between the check above and the accept.
        deps.on_accept()
        try:
            await deps.api.accept(sid, profile.session_body())
        except ApiError as exc:
            logger.warning("phone: call %s not accepted: %s (status %s)",
                           sid, exc.code, exc.status)
            if exc.status == 0:
                # No answer: the provider may have accepted the call, so it
                # is hung up rather than left open with nobody following it.
                try:
                    await deps.api.hangup(sid)
                except ApiError as gone:
                    logger.info("phone: call %s hang-up after an unanswered "
                                "accept: %s (status %s)", sid, gone.code,
                                gone.status)
            await close("accept_failed", [], None)
            return
        answered = True
        session = deps.make_session(deps.api, sid, profile,
                                    timing=deps.timing, lines=texts.lines)
        holder.append(session)
        result = await session.run()
        # A call the stop hung up is cut off, whatever its close would
        # otherwise imply (a transfer still pending reads as put through).
        # A run that returned before the stop keeps its own outcome: nothing
        # is awaited between the run's end and this check.
        outcome = ("interrupted" if deps.cut_by_stop()
                   else outcome_of(result, final))
        logger.info("phone: call %s ended (%s, %s)", sid, outcome,
                    result.closed_reason)
        await close(outcome, [dict(a) for a in result.actions],
                    transcript_text(result.lines))
    except Exception:  # noqa: BLE001 — the call is still ended and recorded
        logger.exception("phone: call %s stopped on an unexpected error", sid)
        if answered:
            try:
                await deps.api.hangup(sid)
            except Exception:  # noqa: BLE001 — logged, the record still goes
                logger.warning("phone: call %s hang-up failed", sid,
                               exc_info=True)
        await close("error", [], None)
