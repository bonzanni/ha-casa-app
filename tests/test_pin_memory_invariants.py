"""Pinning tests for memory-subsystem invariants (docs corpus).

Each test names the corpus invariant it pins and records, in its docstring, the
red case that was demonstrated: the code edit that made it fail. A pinning test
never shown red proves nothing.
"""
from unittest.mock import AsyncMock

import pytest

from hindsight_memory import HindsightSemanticMemory
from memory_provenance import build_retain_items
from personality_types import RetainedTurn, SpeakerProvenance
from semantic_memory import NoOpSemanticMemory, RecallProtocolError, RecallUnavailable


async def _never_saved(_document_id):
    """#1123: the stored-tier reader for a bank that holds nothing yet."""
    return None


async def test_pin_inv_mem_001_failures_raise_never_return_empty():
    """INV-MEM-001: recall reports unavailability by raising, never as empty.

    Red case demonstrated: replacing NoOpSemanticMemory.recall's
    `raise RecallUnavailable("not_configured")` with `return ""` fails the first
    assertion; replacing recall_items' malformed-envelope raise with `return ()`
    fails the last.
    """
    noop = NoOpSemanticMemory()
    with pytest.raises(RecallUnavailable):
        await noop.recall("casa", "q", tags=["public"], max_tokens=1)
    with pytest.raises(RecallUnavailable):
        await noop.recall_items(
            "casa", "q", tags=["public"], max_tokens=1, clearance="public",
        )

    memory = HindsightSemanticMemory("http://hs")
    memory._request = AsyncMock(return_value={"results": "invalid"})
    with pytest.raises(RecallUnavailable):
        await memory.recall("casa", "q", tags=["public"], max_tokens=1)
    with pytest.raises(RecallUnavailable):
        await memory.recall_items(
            "casa", "q", tags=["public"], max_tokens=1, clearance="public",
        )


async def test_pin_inv_mem_002_exactly_one_readable_tier():
    """INV-MEM-002: a typed hit is readable only with exactly one recognised
    tier at or below clearance; all-dropped is unavailable, not empty.

    Red case demonstrated: in hindsight_memory._decode_sensitivity, changing
    `if len(occurrences) != 1` to `if not occurrences` lets the duplicate-tier
    hit through and this test fails on its RecallProtocolError expectation.
    """
    memory = HindsightSemanticMemory("http://hs")
    memory._request = AsyncMock(return_value={
        "results": [{"text": "readable", "tags": ["public"]}],
    })
    hits = await memory.recall_items(
        "casa", "q", tags=["public"], max_tokens=1, clearance="friends",
    )
    assert [hit.sensitivity for hit in hits] == ["public"]

    for tags in ([], ["public", "public"], ["private"], ["unrecognised"]):
        memory._request = AsyncMock(return_value={
            "results": [{"text": "unreadable", "tags": tags}],
        })
        with pytest.raises(RecallProtocolError):
            await memory.recall_items(
                "casa", "q", tags=["public"], max_tokens=1, clearance="friends",
            )


async def test_pin_inv_mem_004_reserved_tags_refused_before_io():
    """INV-MEM-004: application tags cannot forge a tier or a provenance tag,
    and the refusal happens before any classification I/O.

    Red case demonstrated: deleting the `tag.startswith(RESERVED_SOURCE_NAMESPACE)`
    rejection in build_retain_items admits the forged provenance tag; deleting
    the `tag in TIERS` rejection admits the tier tag. Either deletion fails this
    test.
    """
    classifier_called = False

    async def classify(_text):
        nonlocal classifier_called
        classifier_called = True
        return "public"

    turn = RetainedTurn("fact", SpeakerProvenance(speaker_kind="system"))
    for tag in ("private", "casa-source-v1.forged"):
        with pytest.raises(ValueError):
            await build_retain_items([turn], classify=classify,
                                     stored_tags=_never_saved,
                                     application_tags=[tag])
        assert classifier_called is False
