# tests/test_semantic_memory.py
"""SemanticMemory seam (spec §5): ABC contract + NoOp degraded impl."""
from __future__ import annotations

import pytest

from semantic_memory import (
    NoOpSemanticMemory,
    RecallUnavailable,
    SemanticMemory,
    render_recall,
)

pytestmark = [pytest.mark.unit]


def test_noop_is_semantic_memory() -> None:
    assert issubclass(NoOpSemanticMemory, SemanticMemory)


def test_semantic_memory_is_abstract() -> None:
    # The seam is an ABC: the abstract methods must prevent direct instantiation
    # (guards against an @abstractmethod being dropped from any of the four).
    with pytest.raises(TypeError):
        SemanticMemory()


async def test_noop_retain_is_silent() -> None:
    mem = NoOpSemanticMemory()
    assert await mem.retain("casa-assistant", [{"content": "x"}]) is None


async def test_noop_recall_raises_unavailable() -> None:
    """A NoOp backend cannot CHECK memory, so its recall is UNAVAILABLE —
    returning '' would fabricate a genuine-looking zero-hit search and let
    agents claim "nothing found" where no search ever ran."""
    mem = NoOpSemanticMemory()
    with pytest.raises(RecallUnavailable) as ei:
        await mem.recall("casa-assistant", "q", tags=["house"], max_tokens=512)
    assert ei.value.reason == "not_configured"


def test_recall_unavailable_is_a_typed_exception() -> None:
    """Three-outcome contract: UNAVAILABLE is a typed exception on the seam,
    never collapsed into the zero-hit '' return."""
    exc = RecallUnavailable("timeout")
    assert isinstance(exc, Exception)
    assert exc.reason == "timeout"
    assert "timeout" in str(exc)


def test_render_recall_empty() -> None:
    assert render_recall({"results": []}) == ""
    assert render_recall({}) == ""


def test_render_recall_formats_facts() -> None:
    resp = {"results": [
        {"text": "Nicola keeps the thermostat at 20C.", "type": "world", "tags": ["house"]},
        {"text": "Nicola prefers terse replies.", "type": "observation", "tags": ["house"]},
    ]}
    out = render_recall(resp)
    assert "thermostat at 20C" in out
    assert "prefers terse replies" in out
    # one line per fact, no empty placeholder lines
    assert out.count("\n") <= 2
    assert "None" not in out
