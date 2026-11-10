# tests/test_engagement_rebuild_fences.py
"""#369: while an engagement's record is marked ``context_rebuild_pending``
(a memory wipe removed the session it runs in), that session must be inert —
no reads, no work-launching, no media — and an in-flight read that straddles
a lowering of the record's clearance must come out filtered at the new floor.

Two gates pinned here:
  1. the in-process tool fence (engagements dispatch tools in-process);
  2. the re-read in the memory tools (a clearance lowered while a read runs
     reads THIS result again at the new floor, not just the next one).
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _text(res: dict) -> str:
    return res["content"][0]["text"]


async def _pending_record(tmp_path):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="secret task",
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": "private"},
    )
    rec.context_rebuild_pending = True
    await reg.persist_origin(rec.id)
    return reg, rec


class TestInProcessToolFence:
    @pytest.mark.parametrize("tool_name,args", [
        ("search_memory", {"query": "the topic"}),
        ("browse_memory_day", {"date": "2026-10-05"}),
        ("open_memory", {"ids": ["o0000000000000000"]}),
        ("query_engager", {"question": "the topic?"}),
        ("start_job", {"job": "finplan:plan", "task": "t"}),
        ("delegate_to_agent", {"agent": "finance", "task": "t"}),
        ("send_media", {"path": "/tmp/x.pdf", "kind": "document"}),
    ])
    async def test_pending_record_refuses_the_tool(
        self, tmp_path, monkeypatch, tool_name, args,
    ):
        import tools
        reg, rec = await _pending_record(tmp_path)
        token = tools.engagement_var.set(rec)
        try:
            res = await getattr(tools, tool_name).handler(args)
        finally:
            tools.engagement_var.reset(token)
        out = _text(res)
        assert "rebuilt" in out or "rebuilding" in out.lower()

    async def test_cleared_flag_unfences(self, tmp_path, monkeypatch):
        import json

        import tools
        from memory import capture
        from memory_tools_support import new_store
        monkeypatch.setitem(capture._wiring, "store",
                            new_store(tmp_path / "mem"))
        reg, rec = await _pending_record(tmp_path)
        rec.context_rebuild_pending = False
        token = tools.engagement_var.set(rec)
        try:
            res = await tools.search_memory.handler({"query": "q"})
        finally:
            tools.engagement_var.reset(token)
        assert json.loads(_text(res))["status"] == "ok"


class TestInFlightReFilter:
    async def test_clamp_during_recall_filters_this_result(
        self, tmp_path, monkeypatch,
    ):
        """The downgrade lands while the read runs: what it read at the OLD
        clearance is read again at the new one before anything is
        returned."""
        import tools
        from engagement_registry import EngagementRegistry
        from memory import capture
        from memory import search as memory_search
        from memory_tools_support import new_store, store_event

        store = new_store(tmp_path / "mem")
        monkeypatch.setitem(capture._wiring, "store", store)
        store_event(store, "the alarm code is 1234", tier="private")
        store_event(store, "bins go out tuesday, the alarm is fine",
                    tier="public")
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t",
            origin={"channel": "telegram", "_origin_route": "telegram",
                    "_origin_clearance": "private"},
        )
        real = memory_search.search
        reads = []

        def clamped_mid_read(*a, **k):
            # The record's clearance is lowered MID-READ, after this call
            # passed the fence — exactly the in-flight window under test.
            reads.append(k["clearance"])
            rec.origin["_origin_clearance"] = "public"
            return real(*a, **k)
        monkeypatch.setattr(memory_search, "search", clamped_mid_read)
        token = tools.engagement_var.set(rec)
        try:
            res = await tools.search_memory.handler({"query": "alarm"})
        finally:
            tools.engagement_var.reset(token)
        out = _text(res)
        assert reads == ["private", "public"]
        assert "alarm code" not in out
        assert "bins go out tuesday" in out


class TestInCasaClampMidRead:
    """An in_casa engagement's tool call inherits the launching turn's
    ambient origin (private), while the clamp lowers the engagement's own
    record. The record is the authority: a clamp landing mid-read makes
    every memory tool read again at the record's new clearance."""

    @pytest.mark.parametrize("tool_name,seam,args", [
        ("search_memory", ("memory.search", "search"), {"query": "alarm"}),
        ("browse_memory_day", ("memory.browse", "browse"),
         {"date": "2026-10-05"}),
        ("open_memory", ("memory.view", "open_objects"), None),
    ])
    async def test_every_memory_tool_rereads_at_the_clamped_record(
        self, tmp_path, monkeypatch, tool_name, seam, args,
    ):
        import importlib
        import json

        import agent as agent_mod
        import tools
        from engagement_registry import EngagementRegistry
        from memory import capture
        from memory_serving_support import add, fact
        from memory_tools_support import canary, new_store, store_event

        secret = canary()
        store = new_store(tmp_path / "mem")
        monkeypatch.setitem(capture._wiring, "store", store)
        store_event(store, f"the alarm code is {secret}", tier="private",
                    day="2026-10-05")
        store_event(store, "the alarm is tested on mondays", tier="public",
                    day="2026-10-05")
        _, (entry,) = add(store, [fact("alarm", f"code {secret}")])
        if args is None:
            args = {"ids": [entry]}
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"))
        parent = {"role": "assistant", "channel": "telegram", "chat_id": "1",
                  "_origin_route": "telegram", "_origin_clearance": "private"}
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin=dict(parent))
        module = importlib.import_module(seam[0])
        real = getattr(module, seam[1])
        reads = []

        def clamped_mid_read(*a, **k):
            reads.append(k["clearance"])
            rec.origin["_origin_clearance"] = "public"
            return real(*a, **k)
        monkeypatch.setattr(module, seam[1], clamped_mid_read)
        agent_mod.origin_var.set(dict(parent))      # the inherited ambient
        token = tools.engagement_var.set(rec)
        try:
            res = await getattr(tools, tool_name).handler(args)
        finally:
            tools.engagement_var.reset(token)
        out = json.loads(_text(res))
        assert reads == ["private", "public"]
        assert secret not in json.dumps(out)
