# tests/test_memory_provenance.py
"""Task 10: the central provenance-bearing retain-item builder + provenance-aware
document ids. Every retained turn carries exactly one sensitivity tier tag and
exactly one reserved ``casa-source-`` provenance tag; caller-supplied reserved or
tier tags are rejected BEFORE any classify/IO; and agent document ids are stable
across persona_version but distinct per persona_id."""
import pytest

from hindsight_ids import agent_document_id, content_document_id
from memory_provenance import build_retain_items
from personality_types import RetainedTurn, SpeakerProvenance

pytestmark = [pytest.mark.unit]


async def _never_saved(_document_id):
    """#1123: the stored-tier reader for a bank that holds nothing yet."""
    return None


def _agent(version: str, persona: str = "casa/tina") -> SpeakerProvenance:
    return SpeakerProvenance(
        speaker_kind="resident", role_id="resident:butler", persona_id=persona,
        persona_version=version, display_name="Tina", binding_digest="sha256:" + "a" * 64,
    )


def test_user_document_id_remains_exact() -> None:
    assert content_document_id("telegram_3899230", "same text") == content_document_id(
        "telegram_3899230", "same text",
    )


def test_agent_document_id_ignores_persona_version_but_not_persona_id() -> None:
    text = "The office is dark."
    assert agent_document_id(_agent("0.1.0"), text) == agent_document_id(_agent("0.2.0"), text)
    assert agent_document_id(_agent("0.1.0"), text) != agent_document_id(_agent("0.1.0", "casa/ellen"), text)


@pytest.mark.asyncio
async def test_writer_emits_exactly_one_tier_and_one_reserved_tag() -> None:
    async def classify_as_friends(_text: str) -> str:
        return "friends"

    items = await build_retain_items(
        [RetainedTurn("The office is dark.", _agent("0.1.0"))], classify=classify_as_friends,
        stored_tags=_never_saved,
    )
    assert items[0]["tags"][0] == "friends"
    assert len([t for t in items[0]["tags"] if t.startswith("casa-source-")]) == 1


@pytest.mark.asyncio
async def test_caller_supplied_reserved_or_tier_tag_is_rejected_pre_io() -> None:
    called = False

    async def classify(_text: str) -> str:
        nonlocal called
        called = True
        return "friends"

    with pytest.raises(ValueError):
        await build_retain_items(
            [RetainedTurn("text", _agent("0.1.0"))],
            classify=classify, stored_tags=_never_saved,
            application_tags=["casa-source-v1.forged"],
        )
    assert called is False


@pytest.mark.asyncio
async def test_caller_supplied_tier_application_tag_is_rejected_pre_io() -> None:
    called = False

    async def classify(_text: str) -> str:
        nonlocal called
        called = True
        return "friends"

    with pytest.raises(ValueError):
        await build_retain_items(
            [RetainedTurn("text", _agent("0.1.0"))],
            classify=classify, stored_tags=_never_saved,
            application_tags=["private"],
        )
    assert called is False


@pytest.mark.asyncio
async def test_duplicate_document_id_with_identical_text_collapses_to_one_item() -> None:
    async def classify(_text: str) -> str:
        return "friends"

    turns = [RetainedTurn("Same fact.", _agent("0.1.0")), RetainedTurn("Same fact.", _agent("0.2.0"))]
    items = await build_retain_items(turns, classify=classify, stored_tags=_never_saved)
    assert len(items) == 1  # ignores persona_version per agent_document_id's own contract
