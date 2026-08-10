# tests/test_engagement_rebuild_fences.py
"""#369: while a downgraded engagement's context rebuild is pending, its OLD
session must be inert — no reads, no work-launching, no media — and an
in-flight read that straddles the clamp must come out filtered at the new
floor.

Three gates pinned here:
  1. the in-process tool fence (in_casa engagements dispatch tools in-process,
     so the internal-socket choke point alone cannot cover them);
  2. the internal-socket choke point (claude_code executors);
  3. the post-await re-filter in recall_memory (a clamp landing while the
     backend call is suspended filters THIS result, not just the next one);
plus the in_casa driver's last-instant launch gate (a clamp landing during
client open aborts the pre-clamp prompt instead of delivering it).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _text(res: dict) -> str:
    return res["content"][0]["text"]


async def _pending_record(tmp_path):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="secret task", topic_id=7,
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": "private"},
    )
    await reg.lower_origin_clearance(rec.id, "public")
    assert rec.context_rebuild_pending is True
    return reg, rec


class TestInProcessToolFence:
    @pytest.mark.parametrize("tool_name,args", [
        ("recall_memory", {"query": "the topic"}),
        ("query_engager", {"question": "the topic?"}),
        ("engage_executor", {"executor_type": "configurator", "task": "t"}),
        ("delegate_to_agent", {"agent": "finance", "task": "t"}),
        ("send_media", {"path": "/tmp/x.pdf", "kind": "document"}),
    ])
    async def test_pending_record_refuses_the_tool(
        self, tmp_path, monkeypatch, tool_name, args,
    ):
        import agent as agent_mod
        import tools
        monkeypatch.setattr(
            agent_mod, "active_semantic_memory", AsyncMock(), raising=False)
        reg, rec = await _pending_record(tmp_path)
        token = tools.engagement_var.set(rec)
        try:
            res = await getattr(tools, tool_name).handler(args)
        finally:
            tools.engagement_var.reset(token)
        out = _text(res)
        assert "rebuilt" in out or "rebuilding" in out.lower()

    async def test_cleared_flag_unfences(self, tmp_path, monkeypatch):
        import agent as agent_mod
        import tools
        sem = AsyncMock()
        sem.recall_items.return_value = ()
        monkeypatch.setattr(
            agent_mod, "active_semantic_memory", sem, raising=False)
        cfg = SimpleNamespace(memory=SimpleNamespace(token_budget=512))
        monkeypatch.setattr(
            tools, "_agent_role_map", {"assistant": cfg}, raising=False)
        reg, rec = await _pending_record(tmp_path)
        await reg.clear_context_rebuild_pending(rec.id)
        token = tools.engagement_var.set(rec)
        try:
            await tools.recall_memory.handler({"query": "q"})
        finally:
            tools.engagement_var.reset(token)
        sem.recall_items.assert_awaited_once()


class TestChokePointFence:
    async def test_internal_tools_call_refuses_pending_engagement(
        self, tmp_path,
    ):
        from aiohttp.test_utils import make_mocked_request  # noqa: F401
        import json as _json

        from internal_handlers import _make_internal_tools_call_handler

        reg, rec = await _pending_record(tmp_path)
        called = {}

        async def fake_tool(arguments):
            called["yes"] = True
            return {"content": [{"type": "text", "text": "{}"}]}

        handler = _make_internal_tools_call_handler(
            tool_dispatch={"recall_memory": fake_tool},
            engagement_registry=reg,
        )

        class _Req:
            async def json(self):
                return {"name": "recall_memory", "arguments": {},
                        "engagement_id": rec.id,
                        "engagement_token": rec.auth_token}

        resp = await handler(_Req())
        body = _json.loads(resp.body.decode())
        assert "engagement_context_rebuilding" in body["error"]["message"]
        assert not called

    async def test_emit_completion_stays_reachable_while_pending(
        self, tmp_path,
    ):
        import json as _json

        from internal_handlers import _make_internal_tools_call_handler

        reg, rec = await _pending_record(tmp_path)
        called = {}

        async def fake_tool(arguments):
            called["yes"] = True
            return {"content": [{"type": "text", "text": "{}"}]}

        handler = _make_internal_tools_call_handler(
            tool_dispatch={"emit_completion": fake_tool},
            engagement_registry=reg,
        )

        class _Req:
            async def json(self):
                return {"name": "emit_completion", "arguments": {},
                        "engagement_id": rec.id,
                        "engagement_token": rec.auth_token}

        resp = await handler(_Req())
        body = _json.loads(resp.body.decode())
        assert "error" not in body
        assert called


class TestInFlightReFilter:
    async def test_clamp_during_recall_filters_this_result(
        self, tmp_path, monkeypatch,
    ):
        """The downgrade lands while the backend call is suspended: the hits
        came back filtered at the OLD clearance and must be re-filtered at the
        new one before anything is rendered."""
        import agent as agent_mod
        import tools
        from engagement_registry import EngagementRegistry
        from personality_types import RecallHit

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", topic_id=7,
            origin={"channel": "telegram", "_origin_route": "telegram",
                    "_origin_clearance": "private"},
        )

        def _hit(text, tier):
            return RecallHit(
                text=text, memory_type="world", sensitivity=tier,
                application_tags=(), provenance=None, backend_id="b",
                document_id=None, chunk_id=None, source_fact_ids=None,
                metadata=None, context=None, score=None)

        class _Sem:
            async def recall_items(self, bank, query, *, tags, max_tokens,
                                   clearance, types=(), tags_match="any",
                                   budget="mid"):
                # The clamp lands MID-CALL. (It sets the rebuild flag too, but
                # this call already passed the fence — exactly the in-flight
                # window under test.)
                await reg.lower_origin_clearance(rec.id, "public")
                return (_hit("the alarm code is 1234", "private"),
                        _hit("bins go out tuesday", "public"))

        monkeypatch.setattr(
            agent_mod, "active_semantic_memory", _Sem(), raising=False)
        cfg = SimpleNamespace(memory=SimpleNamespace(token_budget=512))
        monkeypatch.setattr(
            tools, "_agent_role_map", {"assistant": cfg}, raising=False)
        token = tools.engagement_var.set(rec)
        try:
            res = await tools.recall_memory.handler({"query": "everything"})
        finally:
            tools.engagement_var.reset(token)
        out = _text(res)
        assert "alarm code" not in out
        assert "bins go out tuesday" in out


class TestInCasaLaunchGate:
    async def test_clamp_during_client_open_aborts_the_stale_prompt(self):
        from drivers.driver_protocol import StaleLaunchError
        from drivers.in_casa_driver import InCasaDriver

        rec = SimpleNamespace(
            id="e-launch", topic_id=7, kind="executor",
            context_rebuild_pending=True, sdk_session_id=None, origin={})
        driver = InCasaDriver(
            topic_stream_factory=lambda tid: None,
            record_lookup=lambda eid: rec,
        )
        delivered = {}

        class _FakeClient:
            def __init__(self, options):
                pass

            async def __aenter__(self):
                return self

            async def close(self):
                pass

            async def query(self, text):
                delivered["text"] = text

        import drivers.in_casa_driver as mod
        orig = mod.ClaudeSDKClient
        mod.ClaudeSDKClient = _FakeClient
        try:
            from claude_agent_sdk import ClaudeAgentOptions
            with pytest.raises(StaleLaunchError):
                await driver.start(
                    rec, "pre-clamp prompt", options=ClaudeAgentOptions())
        finally:
            mod.ClaudeSDKClient = orig
        assert "text" not in delivered          # never enqueued
        assert not driver.is_alive(rec)         # client rolled back
