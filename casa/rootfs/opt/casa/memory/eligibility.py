"""Which live text is captured: the eligibility table as one function.

:func:`admit` decides from mechanical facts only. Each seam computes those
facts with the predicates Casa already uses for them, and passes them in:

* ``channel_writes`` — ``channel_policy.writes_to_bank`` on the channel the
  text arrived on (INV-MEM-005); voice and webhook turns are not captured;
* ``role_writes_memory`` — whether the role whose exchange this is writes
  memory (``delegated_memory.role_writes_memory``, or a delegation's own
  ``memory.token_budget > 0`` gate);
* ``excluded_notice`` — ``delegated_memory.notice_from_excluded_role`` on a
  turn's opening text: Casa's notice of a specialist that writes no memory
  carries that specialist's answer, so it is not captured, while the
  resident's relay of it is (INV-MEM-032);
* ``resident_request`` — the delegation chain's shape: the request is the
  resident's own words only when the resident asked one specialist directly,
  on a turn Casa did not compose (not a continuation);
* ``answer_incomplete`` — the delegated output's own completeness predicate
  (INV-MEM-016).

The package imports none of those modules (it must import without the Casa
process), which is why the facts arrive as arguments. Nothing here reads what
the text means, and nothing compares it with what is already stored.

:func:`admit` returns the admitting rule, which the event records in its
``eligibility`` field, or ``None`` when the text is not captured.
"""
from __future__ import annotations

import enum


class Source(str, enum.Enum):
    #: The text a resident turn opens with: an operator message, or the
    #: prompt of a scheduled, reminder, trigger or other system turn.
    TURN = "turn"
    #: One text-bearing message a resident writes during a turn.
    RESIDENT_MESSAGE = "resident_message"
    #: The task a delegated run is asked, captured before the run starts.
    DELEGATED_REQUEST = "delegated_request"
    #: A delegated run's answer, captured when the run completes.
    DELEGATED_ANSWER = "delegated_answer"
    #: An operator message in an engagement topic.
    ENGAGEMENT_TURN = "engagement_turn"
    #: One text-bearing message a specialist writes in its engagement topic.
    ENGAGEMENT_ANSWER = "engagement_answer"
    #: A finalized engagement's summary (engagement or executor summary).
    ENGAGEMENT_SUMMARY = "engagement_summary"
    #: Casa's own line that a specialist posted to the operator's chat, kept
    #: only for a specialist that keeps no memory (INV-MEM-040): its post is
    #: kept out, so the line is all memory learns of it.
    SPECIALIST_POST = "specialist_post"
    #: The operator's own words replying on the desk of a specialist that
    #: keeps no memory (INV-MEM-043): his message, never the specialist's.
    DESK_REPLY = "desk_reply"


def admit(
    source: Source,
    *,
    text: str,
    channel_writes: bool,
    scheduled: bool = False,
    excluded_notice: bool = False,
    role_writes_memory: bool = True,
    stored_call: bool = False,
    resident_request: bool = True,
    answer_incomplete: bool = False,
) -> str | None:
    """The admitting rule for *text* from *source*, or ``None``."""
    if not (text or "").strip() or not channel_writes:
        return None
    if source is Source.TURN:
        if excluded_notice:
            return None
        return "scheduled_turn" if scheduled else "write_trusted_turn"
    if source is Source.RESIDENT_MESSAGE:
        return "resident_message"
    if source is Source.DELEGATED_REQUEST:
        if not role_writes_memory or stored_call or not resident_request:
            return None
        return "delegated_request"
    if source is Source.DELEGATED_ANSWER:
        if not role_writes_memory or stored_call or answer_incomplete:
            return None
        return "delegated_answer"
    if source in (Source.ENGAGEMENT_TURN, Source.ENGAGEMENT_ANSWER,
                  Source.ENGAGEMENT_SUMMARY):
        return source.value if role_writes_memory else None
    if source in (Source.SPECIALIST_POST, Source.DESK_REPLY):
        return None if role_writes_memory else source.value
    raise ValueError(f"unknown capture source {source!r}")
