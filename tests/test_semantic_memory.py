# tests/test_semantic_memory.py
"""SemanticMemory seam (spec §5): ABC contract + NoOp degraded impl."""
from __future__ import annotations

import pytest

from semantic_memory import (
    NoOpSemanticMemory,
    RecallUnavailable,
    SemanticMemory,
    render_mental_models,
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


async def test_noop_profile_returns_empty_string() -> None:
    # The overlay is optional enrichment (nothing claims absence from it),
    # so the NoOp profile stays a silent ''.
    mem = NoOpSemanticMemory()
    assert await mem.profile("casa-assistant") == ""


def test_recall_unavailable_is_a_typed_exception() -> None:
    """Three-outcome contract: UNAVAILABLE is a typed exception on the seam,
    never collapsed into the zero-hit '' return."""
    exc = RecallUnavailable("timeout")
    assert isinstance(exc, Exception)
    assert exc.reason == "timeout"
    assert "timeout" in str(exc)


def test_render_mental_models_empty() -> None:
    assert render_mental_models({"mental_models": []}) == ""
    assert render_mental_models({}) == ""


_REFRESHED = "2026-09-30T05:00:00Z"


def test_render_mental_models_formats_entries() -> None:
    resp = {"items": [
        {"name": "Operator profile", "content": "Nicola: terse, prefers metric units.",
         "last_refreshed_at": _REFRESHED},
        {"name": "Open commitments", "content": "Guest mode disables personal data.",
         "last_refreshed_at": _REFRESHED},
    ]}
    out = render_mental_models(resp)
    assert "terse" in out
    assert "Guest mode" in out
    assert "None" not in out


def test_render_mental_models_tolerates_alt_keys() -> None:
    for key in ("items", "models", "mental_models"):
        assert "terse" in render_mental_models(
            {key: [{"content": "terse", "last_refreshed_at": _REFRESHED}]})


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
