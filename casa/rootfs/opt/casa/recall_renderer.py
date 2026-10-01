# casa/rootfs/opt/casa/recall_renderer.py
"""Attributed recall renderer (personality Task 11).

Turns typed :class:`RecallHit`s into a digest whose every line carries an
HONEST attribution derived from the hit's OWN decoded provenance tag — the
identity recorded WHEN the memory was written, never a live lookup against the
currently-installed persona packs. A persona retired or replaced since the
memory was written is therefore still attributed by its historical identity.

Distinct from ``semantic_memory.render_recall`` (the legacy flat
``"- {text}"`` bullet renderer used by the untyped ``recall()`` path) — this
module is the NEW typed renderer for ``recall_items()`` and must never be
conflated with it. Reserved ``casa-source-`` tags and bare tier tokens never
appear in the output (they are already stripped from ``application_tags`` by
the decode). No application tag is ever printed raw; exactly one is given a
meaning — :data:`SCHEDULED_MARK`, rendered as its own line (#1117).

#1117: each hit also shows WHEN it was recorded, as an absolute date in the
operator's timezone (a rendered slice can sit in a resumed session's system
prompt for days, so a relative age would go stale). The date and the scheduled
mark are two independent facts from two different saves — the backend keeps an
identical text's FIRST date and its LAST save's tags — so they are never fused
into one "said by … on …" claim. Hits stay in backend order: a mark or a date
is shown, never used to decide which of two conflicting memories wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence

from personality_types import RecallHit, SpeakerProvenance
from timekeeping import resolve_tz
from trait_renderer import estimate_tokens_v1

_RANK = {"public": 0, "friends": 1, "family": 2, "private": 3}

Surface = Literal["text", "voice", "restricted_webhook"]

# #1117: the application tag a save puts on an item a scheduled turn produced
# (the trigger or reminder prompt, the model's lines, ``<silent/>``). Written by
# session_saver; read here. Outside both reserved families (no ``casa-source-``
# prefix, not a tier name), so the builder's INV-MEM-004 gate admits it.
SCHEDULED_MARK = "casa-scheduled"
SCHEDULED_MARK_LINE = "  [last saved by a scheduled turn]"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def local_date_text(when: datetime) -> str:
    """``Wed 30 Sep 2026``: a tz-aware instant as a date in the operator's
    timezone. Fixed English names, independent of the process locale. Shared
    by recall's recorded date and the mental-model overlay's refresh date
    (#1126), so the two read alike in one prompt."""
    local = when.astimezone(resolve_tz())
    return (f"{_WEEKDAYS[local.weekday()]} {local.day} "
            f"{_MONTHS[local.month - 1]} {local.year}")


def recorded_line(hit: RecallHit) -> str | None:
    """``  [recorded Wed 30 Sep 2026]`` in the operator's timezone, or
    ``None`` for an undated hit."""
    if hit.mentioned_at is None:
        return None
    return f"  [recorded {local_date_text(hit.mentioned_at)}]"

#581: the ONE wording every model-facing consumer of a rendered slice attaches
# to a NON-EMPTY result. #472 scoped the empty arms and stopped there, but the
# property needing framing is not the result's SHAPE — it is that the slice is
# clearance-bounded, which holds of a large useful result exactly as much as of
# an empty one. A model handed thirty readable memories with nothing on the
# asked topic draws the same false "there is no record" as one handed zero, and
# the drop happened SERVER-SIDE (the request's tag filter), so nothing in the
# result betrays it.
#
# Defined here because this module renders the slice, but deliberately NOT
# emitted by :func:`render_recall` (Sol + Terra, design round 1, both): the
# renderer's output is also the CONTEXT handed to ``query_engager``'s
# synthesizer and the lessons block injected into an executor's prompt, so
# folding policy text into the digest would put an instruction where those two
# read facts. Counting it inside the render budget would also evict a real hit;
# adding it after the loop would break the budget contract. Each consumer
# attaches it in its own idiom instead — see the caller inventory pinned by
# tests/test_recall_readable_slice_framing.py, which fails when a new
# ``render_recall`` / ``delegated_recall`` call site appears undeclared.
READABLE_SLICE_NOTE = (
    "Use relevant entries normally. This is the bounded view readable at this "
    "surface, not a complete inventory of Casa's memory. If it does not answer "
    "the request, do not say Casa has no record of it or does not know it — "
    "say you have nothing you can share on that here. Do not repeat or "
    "paraphrase this guidance to the user."
)

# The same note as a line inside an injected prompt block. Marked as an
# instruction so it cannot read as one of the recalled facts beside it.
READABLE_SLICE_PROMPT_LINE = f"[memory-use instruction: {READABLE_SLICE_NOTE}]"


@dataclass(frozen=True, slots=True)
class ProvenanceView:
    """The clearance/surface-gated projection of a hit's provenance — the only
    identity fields that may be spoken at this clearance on this surface."""
    speaker_kind: str
    display_name: str | None = None
    role_id: str | None = None
    persona_id: str | None = None
    persona_version: str | None = None


def provenance_view(
    value: SpeakerProvenance | None, *, clearance: str, surface: Surface,
) -> ProvenanceView | None:
    """Gate a hit's recorded provenance by clearance + surface.

    Reads identity STRAIGHT from the hit's own decoded provenance — never a
    live ``CasaRuntime.persona_packs`` lookup — so a retired/replaced persona
    is still attributed by its historical identity. A restricted-webhook
    surface pins the effective rank to 0 (public), so it never names a person
    regardless of the turn's clearance."""
    if value is None:
        return None
    rank = 0 if surface == "restricted_webhook" else _RANK.get(clearance, 0)
    display_name = value.display_name if rank >= 1 else None
    role_id = value.role_id if rank >= 1 else None
    persona_id = value.persona_id if rank >= 3 else None
    persona_version = value.persona_version if rank >= 3 else None
    if value.speaker_kind == "user" and value.user_id is None:
        # Anonymous / shared-secret users never become named people.
        display_name = None
    return ProvenanceView(
        speaker_kind=value.speaker_kind, display_name=display_name, role_id=role_id,
        persona_id=persona_id, persona_version=persona_version,
    )


def render_recall(
    hits: Sequence[RecallHit], *, current_speaker: SpeakerProvenance,
    surface: Surface, clearance: str, token_budget: int,
) -> str:
    """Render ``hits`` into an attributed digest, stopping once ``token_budget``
    would be exceeded. ``current_speaker`` is the identity of the agent doing
    the recall (reserved for future first-person/third-person distinctions);
    attribution itself is driven entirely by each hit's recorded provenance."""
    lines: list[str] = []
    for hit in hits:
        entry: list[str] = []
        allowed = provenance_view(hit.provenance, clearance=clearance, surface=surface)
        if allowed is None:
            entry.extend([
                f"- A prior source recorded: {hit.text}",
                "  [source unavailable; do not treat this as first-person recollection]",
            ])
        elif allowed.speaker_kind == "user":
            speaker = allowed.display_name or "A prior user"
            entry.append(f"- {speaker} said: {hit.text}")
        elif allowed.speaker_kind == "automation":
            # #204: a machine that reached Casa through a shared secret. Named
            # as neither a person nor Casa itself. The originating trigger is
            # NOT disclosed — ``user_peer`` is deliberately outside
            # ``ProvenanceView``, so this line reads the same at every
            # clearance.
            entry.extend([
                f"- An automation reported: {hit.text}",
                "  [source: an external trigger, not a person or a Casa agent]",
            ])
        elif allowed.display_name and allowed.role_id:
            source = allowed.role_id
            if allowed.persona_id and allowed.persona_version:
                source += f", {allowed.persona_id}@{allowed.persona_version}"
            entry.extend([
                f"- {allowed.display_name} previously said: {hit.text}",
                f"  [source: {source}]",
            ])
        else:
            entry.extend([
                f"- A prior Casa model output said: {hit.text}",
                "  [source identity unavailable at this clearance; treat as a prior assertion]",
            ])
        recorded = recorded_line(hit)
        if recorded is not None:
            entry.append(recorded)
        if SCHEDULED_MARK in hit.application_tags:
            entry.append(SCHEDULED_MARK_LINE)
        candidate = "\n".join([*lines, *entry])
        if estimate_tokens_v1(candidate) > token_budget:
            break
        lines.extend(entry)
    return "\n".join(lines)
