"""Task 5+6: _finalize_engagement retains the engagement summary on the
shared `casa` bank (tier-classified, write-trust gated) instead of writing to
the legacy Honcho meta/executor sessions. The post-back NOTIFICATION is
unchanged."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import delegated_memory

pytestmark = [pytest.mark.unit]


class _Sem:
    """Recording fake mirroring tests/test_delegated_memory.py's _Sem shape."""

    def __init__(self):
        self.recall_calls = []
        self.retain_calls = []

    async def recall(self, bank, query, *, tags, max_tokens, budget="mid", **kw):
        self.recall_calls.append({"bank": bank, "query": query})
        return ""

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append({"bank": bank, "items": items})


def _install_sem(monkeypatch):
    """Inject a recording semantic-memory fake + a deterministic tier stub."""
    import agent as agent_mod

    sem = _Sem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)

    async def _fake_classify(text):
        return "private"

    monkeypatch.setattr(delegated_memory, "classify_tier", _fake_classify)
    return sem


async def _make_engagement(reg, *, channel, kind="specialist"):
    return await reg.create(
        kind=kind, role_or_type="finance", driver="in_casa", task="pay rent",
        origin={"role": "assistant", "channel": channel, "chat_id": "12345"},
        topic_id=42,
    )


async def test_telegram_summary_retained_and_postback_fires(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    sem = _install_sem(monkeypatch)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg, channel="telegram")

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()

    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="summary", artifacts=["sha1"],
        next_steps=[], driver=driver,
    )
    # L33: retains now run as background tasks; drain them before asserting.
    import tools as tools_mod
    await asyncio.gather(*list(tools_mod._specialist_bg_tasks), return_exceptions=True)

    # One retain to the shared `casa` bank with the engagement_summary item.
    assert len(sem.retain_calls) == 1
    call = sem.retain_calls[0]
    assert call["bank"] == "casa"
    assert len(call["items"]) == 1
    item = call["items"][0]
    payload = json.loads(item["content"])
    assert payload["kind"] == "engagement_summary"
    assert payload["engagement_id"] == rec.id
    assert payload["task"] == "pay rent"
    assert payload["status"] == "completed"
    # Task 10: exactly one tier tag + one reserved provenance tag; the summary is
    # attributed to the honest unattributed "system" identity (no origin author
    # present in the fixture) and content-addressed under the m-a- agent id space.
    from hindsight_ids import agent_document_id
    from personality_types import SpeakerProvenance
    assert item["tags"][0] == "private"
    assert sum(1 for t in item["tags"] if t.startswith("casa-source-")) == 1
    assert item["document_id"] == agent_document_id(
        SpeakerProvenance(speaker_kind="system"), item["content"],
    )

    # Post-back NOTIFICATION still fired.
    bus.notify.assert_awaited_once()


async def test_voice_no_retain_but_postback_still_fires(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    sem = _install_sem(monkeypatch)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg, channel="voice")

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()

    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="summary", artifacts=[],
        next_steps=[], driver=driver,
    )

    # Voice is recall-only — write-trust gate blocks the retain.
    assert sem.retain_calls == []
    # But the post-back NOTIFICATION still fired.
    bus.notify.assert_awaited_once()


async def test_executor_kind_retains_distinct_doc_prefix(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    sem = _install_sem(monkeypatch)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg, channel="telegram", kind="executor")

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    telegram.update_topic_state = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()

    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="summary", artifacts=[],
        next_steps=[], driver=driver,
    )
    # L33: retains now run as background tasks; drain them before asserting.
    import tools as tools_mod
    await asyncio.gather(*list(tools_mod._specialist_bg_tasks), return_exceptions=True)

    # Two retains: the engagement summary + the per-executor-type summary,
    # under DISTINCT content-addressed document_ids so they do not clobber
    # each other (the two payloads differ, so their agent ids differ).
    assert len(sem.retain_calls) == 2
    doc_ids = [c["items"][0]["document_id"] for c in sem.retain_calls]
    assert all(d.startswith("m-a-") for d in doc_ids)
    assert len(set(doc_ids)) == 2

    exec_call = next(
        c for c in sem.retain_calls
        if json.loads(c["items"][0]["content"]).get("kind") == "executor_engagement_summary"
    )
    exec_payload = json.loads(exec_call["items"][0]["content"])
    assert exec_payload["kind"] == "executor_engagement_summary"
    assert exec_payload["engagement_id"] == rec.id


async def test_executor_summary_stamped_with_launch_epoch_tag(tmp_path, monkeypatch):
    """#215 (design r1, Sol S-B2/Terra T-B1): the executor summary carries the
    epoch PERSISTED ON THE RECORD at launch — and a record without one (legacy)
    retains untagged rather than borrowing the finalize-time definition."""
    from engagement_registry import EngagementRegistry
    from executor_epoch import epoch_application_tag
    from tools import _finalize_engagement, init_tools

    sem = _install_sem(monkeypatch)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg, channel="telegram", kind="executor")
    await reg.set_procedural_epoch(rec.id, "e" * 64)

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    telegram.update_topic_state = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="summary", artifacts=[],
        next_steps=[], driver=driver,
    )
    import tools as tools_mod
    await asyncio.gather(*list(tools_mod._specialist_bg_tasks), return_exceptions=True)

    exec_call = next(
        c for c in sem.retain_calls
        if json.loads(c["items"][0]["content"]).get("kind") == "executor_engagement_summary"
    )
    expected = epoch_application_tag("finance", "e" * 64)
    assert expected in exec_call["items"][0]["tags"]
    # The plain engagement summary is NOT epoch-stamped.
    plain_call = next(
        c for c in sem.retain_calls
        if json.loads(c["items"][0]["content"]).get("kind") == "engagement_summary"
    )
    assert not any(t.startswith("casa-doctrine-epoch-") for t in plain_call["items"][0]["tags"])


async def test_executor_summary_without_epoch_retains_untagged(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    sem = _install_sem(monkeypatch)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg, channel="telegram", kind="executor")

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    telegram.update_topic_state = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="summary", artifacts=[],
        next_steps=[], driver=driver,
    )
    import tools as tools_mod
    await asyncio.gather(*list(tools_mod._specialist_bg_tasks), return_exceptions=True)

    exec_call = next(
        c for c in sem.retain_calls
        if json.loads(c["items"][0]["content"]).get("kind") == "executor_engagement_summary"
    )
    assert not any(
        t.startswith("casa-doctrine-epoch-") for t in exec_call["items"][0]["tags"]
    )
