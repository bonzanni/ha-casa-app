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
``casa-source-`` namespace or name a sensitivity tier — those two tag families
are owned by this builder. Such a tag is rejected BEFORE any classification or
IO runs, so a forged provenance/tier tag can never even reach the classifier.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Sequence

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
from tier_classifier import TIERS, classify_tier


async def build_retain_items(
    turns: Sequence[RetainedTurn], *,
    classify: Callable[[str], Awaitable[str]] = classify_tier,
    application_tags: Sequence[str] = (), classify_concurrency: int = 4,
) -> list[dict[str, object]]:
    """Turn provenance-bearing ``turns`` into Hindsight retain items (see module
    docstring). Blank turns are dropped; within-batch duplicate ``document_id``s
    collapse to one item (a collision onto DIFFERENT text is a hard error — never
    silently overwrite one fact with another). Classification is bounded-parallel
    (``classify_concurrency``); ``classify`` is injectable so a writer can thread
    its own module-global (monkeypatchable) ``classify_tier`` through."""
    if classify_concurrency < 1:
        raise ValueError("classify_concurrency must be positive")
    # Reserved/tier tag rejection MUST precede any classify/IO — a forged tag
    # never reaches the classifier (a test pins that the classifier is never
    # even called on rejection).
    for tag in application_tags:
        if not isinstance(tag, str):
            raise ValueError("application tags must be strings")
        if tag.startswith(RESERVED_SOURCE_NAMESPACE):
            raise ValueError("caller-supplied reserved provenance tag")
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
    items: list[dict[str, object]] = []
    for (turn, text, document_id), tier in zip(pending, tiers):
        if tier not in TIERS:
            raise ValueError("invalid sensitivity tier returned by classifier")
        provenance_json = canonical_json_bytes(provenance_mapping(turn.provenance)).decode("utf-8")
        item: dict[str, object] = {
            "content": text,
            "tags": [tier, encode_provenance_tag(turn.provenance), *application_tags],
            "metadata": {"casa_source_v1": provenance_json},
            "document_id": document_id,
        }
        # #471: the turn's wall-clock time rides OUT-OF-BAND (a documented
        # retain-item field, semantic_memory.retain) now that the envelope is
        # stripped from the hashed/stored content. Within-batch duplicates keep
        # the first occurrence's timestamp; a cross-session re-retain upserts
        # the whole document, timestamp included.
        if turn.timestamp is not None:
            item["timestamp"] = turn.timestamp
        items.append(item)
    return items
