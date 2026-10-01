# casa/rootfs/opt/casa/memory_provenance.py
"""Central provenance-bearing retain-item builder (personality Task 10).

Every long-term memory writer — session transcripts (session_saver), cold-session
retention, delegated/summary writes (delegated_memory, tools) — funnels its turns
through :func:`build_retain_items` so every retained document carries, uniformly:

* a content-addressed ``document_id`` keyed by KIND — user turns on their trusted
  ``user_peer`` (:func:`content_document_id`), agent turns on their persona
  identity (:func:`agent_document_id`);
* EXACTLY ONE sensitivity tier tag (leak-safe default-private on classifier
  uncertainty, enforced upstream in tier_classifier) AND EXACTLY ONE reserved
  ``casa-source-`` provenance tag (:func:`encode_provenance_tag`), plus any
  caller ``application_tags``;
* the full canonical provenance mapping in ``metadata["casa_source_v1"]`` so a
  recall can reconstruct the exact :class:`SpeakerProvenance` even if tag decoding
  ever changes.

Caller-supplied ``application_tags`` may NOT begin with the reserved
``casa-source-`` namespace or the reserved ``casa-tier-`` namespace, or name a
sensitivity tier — those tag families are owned by this builder. Such a tag is
rejected BEFORE any classification or IO runs, so a forged provenance/tier tag
can never even reach the classifier.

#1123: a save never LOWERS a memory's stored tier. Before the items are
assembled, the builder reads each document's current tags from the bank
(``stored_tags``) and sends the stricter of the stored tier and this save's
verdict. A ``private`` that came only from a classifier failure
(:class:`tier_classifier.FallbackTier`) is not a verdict: over a stored tier it
re-sends that tier, and on a document with none it is stored beside
:data:`UNVERIFIED_TIER_MARK`, which tells a later save the ``private`` sets no
floor. Every stored tier without that marker — including one written before
this rule existed — is a floor. A read that fails fails the whole save.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Collection, Sequence

from canonical_bytes import canonical_json_bytes
from hindsight_ids import (
    agent_document_id,
    automation_document_id,
    content_document_id,
)
from personality_types import RetainedTurn
from speaker_provenance import (
    RESERVED_SOURCE_NAMESPACE,
    encode_provenance_tag,
    provenance_mapping,
    validate_speaker_provenance,
)
from tier_classifier import TIERS, FallbackTier, classify_tier

logger = logging.getLogger(__name__)

# #1123: the builder-owned marker beside a ``private`` that came only from a
# classifier failure. Outside ``casa-source-`` (provenance decoding counts
# every tag there), not a tier, and inside the reserved ``casa-tier-`` family
# no caller may send.
RESERVED_TIER_NAMESPACE = "casa-tier-"
UNVERIFIED_TIER_MARK = RESERVED_TIER_NAMESPACE + "unverified"

StoredTagsReader = Callable[[str], Awaitable["Collection[str] | None"]]


def _stored_floor(stored: object) -> str | None:
    """The stored REAL tier a save may not go below, from one document's
    stored tags (None = never saved). Anything but None or a set/list/tuple
    of strings is a failed read, never "no floor". No tier at all → no floor
    (such a document is unreadable by recall anyway); a marked lone
    ``private`` → no floor; otherwise the MOST restrictive tier present."""
    if stored is None:
        return None
    if not isinstance(stored, (set, frozenset, list, tuple)) or not all(
            isinstance(t, str) for t in stored):
        raise ValueError("stored-tag read returned an unusable value")
    tiers = {t for t in stored if t in TIERS}
    if not tiers or (tiers == {"private"} and UNVERIFIED_TIER_MARK in stored):
        return None
    return max(tiers, key=TIERS.index)


async def build_retain_items(
    turns: Sequence[RetainedTurn], *,
    classify: Callable[[str], Awaitable[str]] = classify_tier,
    stored_tags: StoredTagsReader,
    application_tags: Sequence[str] = (), classify_concurrency: int = 4,
) -> list[dict[str, object]]:
    """Turn provenance-bearing ``turns`` into Hindsight retain items (see module
    docstring). Blank turns are dropped; within-batch duplicate ``document_id``s
    collapse to one item (a collision onto DIFFERENT text is a hard error — never
    silently overwrite one fact with another). Classification is bounded-parallel
    (``classify_concurrency``); ``classify`` is injectable so a writer can thread
    its own module-global (monkeypatchable) ``classify_tier`` through.
    ``stored_tags`` (#1123, required so no writer can forget it) reads one
    document's current tags from the bank this save writes to; any failure
    it raises propagates, and nothing is built."""
    if classify_concurrency < 1:
        raise ValueError("classify_concurrency must be positive")
    # Reserved/tier tag rejection MUST precede any classify/IO — a forged tag
    # never reaches the classifier (a test pins that the classifier is never
    # even called on rejection). #1117: per-turn tags are checked here too,
    # for EVERY turn — a duplicate that later collapses included.
    for tag in (*application_tags, *(t for turn in turns for t in turn.application_tags)):
        if not isinstance(tag, str):
            raise ValueError("application tags must be strings")
        if tag.startswith(RESERVED_SOURCE_NAMESPACE):
            raise ValueError("caller-supplied reserved provenance tag")
        if tag.startswith(RESERVED_TIER_NAMESPACE):
            raise ValueError("caller-supplied reserved tier marker")
        if tag in TIERS:
            raise ValueError("caller-supplied sensitivity application tag")

    pending: list[tuple[RetainedTurn, str, str]] = []
    seen: dict[str, str] = {}
    for turn in turns:
        validate_speaker_provenance(turn.provenance)
        text = turn.text.strip()
        if not text:
            continue
        # Route by KIND: a human peer, an automation's originating trigger, or
        # an agent's persona identity. Automations are NOT agents — their id
        # must key on user_peer, which the agent scheme discards (#204).
        if turn.provenance.speaker_kind == "user":
            document_id = content_document_id(turn.provenance.user_peer or "", text)
        elif turn.provenance.speaker_kind == "automation":
            document_id = automation_document_id(
                turn.provenance.user_peer or "", text)
        else:
            document_id = agent_document_id(turn.provenance, text)
        prior = seen.get(document_id)
        if prior is not None and prior != text:
            raise ValueError("document-id collision maps to different text")
        if prior == text:
            continue
        seen[document_id] = text
        pending.append((turn, text, document_id))

    semaphore = asyncio.Semaphore(classify_concurrency)

    async def bounded_classify(text: str) -> str:
        async with semaphore:
            return await classify(text)

    tiers = await asyncio.gather(*(bounded_classify(text) for _, text, _ in pending))
    for tier in tiers:
        if tier not in TIERS:
            raise ValueError("invalid sensitivity tier returned by classifier")

    # #1123: read the stored tier AFTER classifying, so the read is as close
    # to the retain as this builder can put it. A real ``private`` needs no
    # read — nothing is stricter. ``return_exceptions`` waits for every read,
    # so none is still running once a failure is raised.
    async def bounded_floor(document_id: str) -> str | None:
        async with semaphore:
            return _stored_floor(await stored_tags(document_id))

    to_read = [i for i, tier in enumerate(tiers)
               if isinstance(tier, FallbackTier) or tier != "private"]
    floors: list[str | None] = [None] * len(pending)
    results = await asyncio.gather(
        *(bounded_floor(pending[i][2]) for i in to_read), return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        logger.warning(
            "stored-tier read failed for %d of %d documents (%s: %s); this "
            "save is skipped so no tier is written unchecked",
            len(failures), len(to_read), type(failures[0]).__name__, failures[0],
        )
        raise failures[0]
    for i, floor in zip(to_read, results):
        floors[i] = floor

    items: list[dict[str, object]] = []
    for (turn, text, document_id), verdict, floor in zip(pending, tiers, floors):
        marker: tuple[str, ...] = ()
        if isinstance(verdict, FallbackTier):
            # A failure is not a verdict: keep the stored real tier as it is,
            # or store a provisional private a later real verdict replaces.
            if floor is None:
                tier, marker = "private", (UNVERIFIED_TIER_MARK,)
            else:
                tier = floor
        elif floor is not None and TIERS.index(floor) > TIERS.index(verdict):
            tier = floor
        else:
            tier = str(verdict)
        provenance_json = canonical_json_bytes(provenance_mapping(turn.provenance)).decode("utf-8")
        # #1117: the COMPLETE set, in one list — the backend replaces a stored
        # document's tags with the latest save's set, so a save that sent part
        # of it (a mark without its tier) would make the memory unreadable.
        turn_tags = tuple(
            t for t in dict.fromkeys(turn.application_tags) if t not in application_tags)
        item: dict[str, object] = {
            "content": text,
            "tags": [tier, encode_provenance_tag(turn.provenance), *application_tags,
                     *turn_tags, *marker],
            "metadata": {"casa_source_v1": provenance_json},
            "document_id": document_id,
        }
        # #471: the turn's wall-clock time rides OUT-OF-BAND (a documented
        # retain-item field, semantic_memory.retain) now that the envelope is
        # stripped from the hashed/stored content. Within-batch duplicates keep
        # the first occurrence's timestamp and tags. #1117, measured on
        # Hindsight 0.10.2: a cross-session re-retain of identical content keeps
        # the document's FIRST date and REPLACES its tags with this save's set.
        if turn.timestamp is not None:
            item["timestamp"] = turn.timestamp
        items.append(item)
    return items
