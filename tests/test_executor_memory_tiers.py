"""Unit coverage for _fetch_executor_archive.

An executor launch gets the newest ``engagement_summaries`` of its own type
stamped with the launch's own procedural epoch (#215, INV-MEM-015), at a tier
the originating turn's clearance can read (#336), within its budget, read
once from the memory store. Real store; summaries are written through
capture's own seam.
"""
from __future__ import annotations

import pytest

from memory import capture
from memory_serving_support import ledger_store, wire
from tools import _fetch_executor_archive

pytestmark = [pytest.mark.unit]


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ledger_store(tmp_path)
    wire(monkeypatch, s)
    return s


async def _summary(role, epoch, tier, text):
    receipt = await capture.record(capture.summary(
        role=role, procedural_epoch=epoch, tier=tier, text=text,
        engagement_id=f"eng-{text}"))
    assert receipt is not None and receipt.outcome == capture.STORED


async def _archive(**kw):
    args = {"role": "configurator", "procedural_epoch": "e1",
            "origin_channel": "telegram", "token_budget": 2000}
    return await _fetch_executor_archive(**{**args, **kw})


async def test_operator_launch_reads_its_role_and_epoch_oldest_first(store):
    await _summary("configurator", "e1", "private", "LESSON-ONE")
    await _summary("configurator", "e0", "private", "OTHER-EPOCH")
    await _summary("ha-developer", "e1", "private", "OTHER-ROLE")
    await _summary("configurator", "e1", "public", "LESSON-TWO")
    out = await _archive()
    assert out.startswith("## Prior engagements (lessons learned)\n")
    assert "OTHER-EPOCH" not in out and "OTHER-ROLE" not in out
    assert out.index("LESSON-ONE") < out.index("LESSON-TWO")


async def test_non_operator_launch_gets_public_summaries_only(store):
    """#336: the stamped markers of a stranger's turn read at ``public``."""
    await _summary("configurator", "e1", "private", "PRIVATE-LESSON")
    await _summary("configurator", "e1", "public", "PUBLIC-LESSON")
    out = await _archive(origin_route="telegram", origin_clearance="public")
    assert "PUBLIC-LESSON" in out and "PRIVATE-LESSON" not in out


async def test_voice_clearance_reads_friends_and_public(store):
    await _summary("configurator", "e1", "family", "FAMILY-LESSON")
    await _summary("configurator", "e1", "friends", "FRIENDS-LESSON")
    out = await _archive(origin_channel="voice")
    assert "FRIENDS-LESSON" in out and "FAMILY-LESSON" not in out


async def test_newest_summaries_kept_within_the_budget(store):
    for i in range(5):
        await _summary("configurator", "e1", "private", f"L{i}-" + "x" * 80)
    out = await _archive(token_budget=60)
    assert "L4-" in out and "L3-" in out and "L0-" not in out


async def test_a_record_without_an_epoch_gets_nothing(store):
    await _summary("configurator", "", "private", "UNSTAMPED")
    assert await _archive(procedural_epoch="") == ""


async def test_no_store_or_an_unavailable_one_runs_cold(store, monkeypatch):
    await _summary("configurator", "e1", "private", "LESSON")
    store._unavailable_reason = "a simulated outage"
    assert await _archive() == ""
    wire(monkeypatch, None)
    assert await _archive() == ""


async def test_a_finalized_summary_carries_its_records_epoch(store):
    """INV-MEM-015, the stamping half: the summary captured at finalize
    carries the epoch persisted on the engagement's record at create, which
    is what the next launch of that epoch reads."""
    import tools
    frozen = {"id": "eng-1", "role_or_type": "configurator",
              "procedural_epoch": "e1",
              "origin": {"channel": "telegram", "_origin_route": "telegram",
                         "_origin_clearance": "private"}}
    await tools._capture_summary(frozen, "STAMPED-LESSON", allowed=True)
    assert "STAMPED-LESSON" in await _archive(procedural_epoch="e1")
    assert await _archive(procedural_epoch="e2") == ""
