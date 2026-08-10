# tests/test_recall_empty_verdict.py
"""#472: an empty recall result must never license "no record exists".

The recall request sends ``tags=readable_tiers(clearance)``, so the backend
pre-filters above-clearance hits server-side and a clearance-blocked search
comes back as the SAME well-formed empty result as a genuine miss. The agent
was then handed a bare ``status=ok, memory=""`` and (honestly, from its view)
told the user "no record about that in memory" — asserting a non-existence it
cannot know. Truncation hides content the same way even at top clearance
(``max_tokens`` is applied server-side, and the types filter and mental-model
overlays are outside this search), so the wording is epistemically scoped at
EVERY tier — no "no record exists" arm anywhere.

Three distinguishable empty shapes, each pinned here:
  1. zero hits            → guidance: not proof of absence, never assert it;
  2. hits that did not fit → readable matches EXIST; refine the query;
  3. non-empty digest      → untouched happy path (no guidance noise).

Same contract for ``query_engager``'s ``status=unknown`` arm, which used to
be documented as "the memory was searched and holds nothing relevant".
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.unit]


def _text(res: dict) -> str:
    return res["content"][0]["text"]


def _hit(text: str = "Nicola keeps the thermostat at 20C."):
    from personality_types import RecallHit
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=(), provenance=None, backend_id="b1",
        document_id=None, chunk_id=None, source_fact_ids=None,
        metadata=None, context=None, score=None,
    )


def _setup(monkeypatch, *, channel: str, hits=(), token_budget: int = 512):
    import agent as agent_mod
    import tools
    sem = AsyncMock()
    sem.recall_items.return_value = tuple(hits)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    cfg = SimpleNamespace(memory=SimpleNamespace(token_budget=token_budget))
    monkeypatch.setattr(tools, "_agent_role_map", {"assistant": cfg}, raising=False)
    agent_mod.origin_var.set({"role": "assistant", "channel": channel})
    return sem


class TestRecallMemoryEmptyIsNotAbsence:
    async def test_zero_hits_below_top_clearance_scopes_the_claim(self, monkeypatch):
        import tools
        _setup(monkeypatch, channel="voice", hits=())
        out = _text(await tools.recall_memory.handler({"query": "the topic"}))
        low = out.lower()
        # The result must instruct the agent NOT to assert non-existence...
        assert "not proof" in low or "do not" in low
        assert "exist" in low
        # ...and must not itself assert it.
        assert "no record" not in low

    async def test_zero_hits_at_top_clearance_uses_the_same_scoped_wording(self, monkeypatch):
        import tools
        _setup(monkeypatch, channel="telegram", hits=())  # telegram = private (top)
        out = _text(await tools.recall_memory.handler({"query": "the topic"}))
        low = out.lower()
        assert "not proof" in low or "do not" in low
        assert "no record" not in low

    async def test_hits_that_did_not_fit_are_reported_as_existing(self, monkeypatch):
        import tools
        # One readable hit far beyond a 1-token render budget → digest "".
        _setup(monkeypatch, channel="telegram",
               hits=(_hit("x " * 4000),), token_budget=1)
        out = _text(await tools.recall_memory.handler({"query": "the topic"}))
        low = out.lower()
        # Readable matches exist — saying so discloses nothing unreadable.
        assert "match" in low or "exist" in low
        assert "narrow" in low or "refine" in low or "specific" in low

    async def test_nonempty_digest_stays_clean(self, monkeypatch):
        import tools
        _setup(monkeypatch, channel="telegram", hits=(_hit(),))
        out = _text(await tools.recall_memory.handler({"query": "temp?"}))
        assert "thermostat at 20C" in out
        assert "not proof" not in out.lower()


class TestQueryEngagerUnknownIsNotAbsence:
    async def test_unknown_carries_do_not_assert_guidance(self, monkeypatch):
        import tools
        from engagement_registry import EngagementRecord

        rec = EngagementRecord(
            id="e-1", kind="executor", role_or_type="configurator",
            driver="in_casa", status="active", topic_id=7, started_at=0.0,
            last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
            sdk_session_id=None,
            origin={"channel": "telegram", "_origin_route": "telegram_dm",
                    "_origin_clearance": "friends"},
            task="t",
        )
        token = tools.engagement_var.set(rec)
        try:
            async def fake_recall(*a, with_stats=False, **kw):
                return ("", 0) if with_stats else ""
            monkeypatch.setattr(tools, "delegated_recall", fake_recall)
            import agent as agent_mod
            monkeypatch.setattr(
                agent_mod, "active_semantic_memory", AsyncMock(), raising=False)
            res = await tools.query_engager.handler({"question": "the topic?"})
        finally:
            tools.engagement_var.reset(token)
        out = _text(res)
        low = out.lower()
        assert '"unknown"' in low or "unknown" in low
        assert "not proof" in low or "do not" in low
        assert "readable" in low or "clearance" in low

    async def test_hits_that_did_not_fit_report_too_broad_not_unknown(
        self, monkeypatch,
    ):
        """Sol diff-gate r1: a rendered '' with a non-zero hit count means
        readable matches EXIST — denying them as 'unknown' is the same
        overclaim #472 fixed on recall_memory."""
        import tools
        from engagement_registry import EngagementRecord

        rec = EngagementRecord(
            id="e-2", kind="executor", role_or_type="configurator",
            driver="in_casa", status="active", topic_id=7, started_at=0.0,
            last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
            sdk_session_id=None,
            origin={"channel": "telegram", "_origin_route": "telegram_dm",
                    "_origin_clearance": "friends"},
            task="t",
        )
        token = tools.engagement_var.set(rec)
        try:
            async def fake_recall(*a, with_stats=False, **kw):
                return ("", 3) if with_stats else ""
            monkeypatch.setattr(tools, "delegated_recall", fake_recall)
            import agent as agent_mod
            monkeypatch.setattr(
                agent_mod, "active_semantic_memory", AsyncMock(), raising=False)
            res = await tools.query_engager.handler({"question": "everything?"})
        finally:
            tools.engagement_var.reset(token)
        out = _text(res)
        assert "too_broad" in out
        assert "narrower" in out.lower()
        assert "unknown" not in out.split("too_broad")[0]

    def test_tool_description_no_longer_claims_holds_nothing_relevant(self):
        import tools
        desc = tools.query_engager.description  # SdkMcpTool description field
        assert "holds nothing relevant" not in desc
        assert "readable" in desc or "not proof" in desc
