# tests/test_engagement_rebuild_fences.py
"""#369: while a downgraded engagement's context rebuild is pending, its OLD
session must be inert — no reads, no work-launching, no media — and an
in-flight read that straddles the clamp must come out filtered at the new
floor.

Two gates pinned here:
  1. the in-process tool fence (engagements dispatch tools in-process);
  2. the re-read in the memory tools (a clamp landing while a read runs
     reads THIS result again at the new floor, not just the next one);
plus the in_casa driver's last-instant launch gate (a clamp landing during
client open aborts the pre-clamp prompt instead of delivering it).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _text(res: dict) -> str:
    return res["content"][0]["text"]


async def _pending_record(tmp_path):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="secret task", topic_id=7,
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": "private"},
    )
    await reg.lower_origin_clearance(rec.id, "public")
    assert rec.context_rebuild_pending is True
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
        await reg.clear_context_rebuild_pending(rec.id)
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
            task="t", topic_id=7,
            origin={"channel": "telegram", "_origin_route": "telegram",
                    "_origin_clearance": "private"},
        )
        real = memory_search.search
        reads = []

        def clamped_mid_read(*a, **k):
            # The clamp lands MID-READ. (It sets the rebuild flag too, but
            # this call already passed the fence — exactly the in-flight
            # window under test.)
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


class TestVerifiedTeardown:
    """Sol diff-gate r1: a rebuild must never clear the fence around a session
    whose teardown was not CONFIRMED."""

    async def test_in_casa_invalidate_propagates_a_failed_close(self):
        from drivers.in_casa_driver import InCasaDriver

        rec = SimpleNamespace(id="e-td", topic_id=7)
        driver = InCasaDriver(topic_stream_factory=lambda tid: None)

        class _StuckClient:
            async def close(self):
                raise RuntimeError("subprocess did not exit")

        driver._clients[rec.id] = _StuckClient()
        driver._ctx_stack[rec.id] = object()
        with pytest.raises(RuntimeError, match="did not exit"):
            await driver.invalidate_session(rec)
        # Sol diff-gate r2: the stale client is RETAINED until a close
        # succeeds — popping first would make the retry see an empty map and
        # report teardown "confirmed" over a surviving subprocess. (Deliveries
        # are refused meanwhile by the rebuild-pending fence.)
        assert driver.is_alive(rec)
        with pytest.raises(RuntimeError, match="did not exit"):
            await driver.invalidate_session(rec)  # retry still refuses

class TestInCasaLaunchGate:
    async def test_clamp_during_client_open_aborts_the_stale_prompt(self):
        from drivers.driver_protocol import StaleLaunchError
        from drivers.in_casa_driver import InCasaDriver

        rec = SimpleNamespace(
            id="e-launch", topic_id=7, kind="specialist",
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

    async def test_completed_rebuild_cycle_aborts_stale_launch_by_generation(self):
        """Terra diff-gate r2: a clamp→rebuild cycle COMPLETING while the old
        launch is suspended clears the pending flag — only the generation
        comparison can still see it, and the stale launch must not overwrite
        the fresh client the rebuild registered."""
        from drivers.driver_protocol import StaleLaunchError
        from drivers.in_casa_driver import InCasaDriver

        rec = SimpleNamespace(
            id="e-gen", topic_id=7, kind="specialist",
            context_rebuild_pending=False,   # rebuild already cleared it
            context_generation=1,            # ...but the clamp bumped this
            sdk_session_id=None, origin={})
        driver = InCasaDriver(
            topic_stream_factory=lambda tid: None,
            record_lookup=lambda eid: rec,
        )
        fresh = object()                     # the rebuild's floor client
        driver._clients[rec.id] = fresh
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
                    rec, "pre-clamp prompt", options=ClaudeAgentOptions(),
                    expected_generation=0)
        finally:
            mod.ClaudeSDKClient = orig
        assert "text" not in delivered
        # The fresh floor client the rebuild registered is untouched.
        assert driver._clients[rec.id] is fresh


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
            task="t", topic_id=7, origin=dict(parent))
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
