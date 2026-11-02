# tests/test_engagement_rebuild_fences.py
"""#369: while a downgraded engagement's context rebuild is pending, its OLD
session must be inert — no reads, no work-launching, no media — and an
in-flight read that straddles the clamp must come out filtered at the new
floor.

Three gates pinned here:
  1. the in-process tool fence (in_casa engagements dispatch tools in-process,
     so the internal-socket choke point alone cannot cover them);
  2. the internal-socket choke point (claude_code executors);
  3. the re-read in the memory tools (a clamp landing while a read runs
     reads THIS result again at the new floor, not just the next one);
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
        ("search_memory", {"query": "the topic"}),
        ("browse_memory_day", {"date": "2026-10-05"}),
        ("open_memory", {"ids": ["o0000000000000000"]}),
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
            tool_dispatch={"search_memory": fake_tool},
            engagement_registry=reg,
        )

        class _Req:
            async def json(self):
                return {"name": "search_memory", "arguments": {},
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
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
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

    async def test_claude_code_invalidate_refuses_unconfirmed_down(
        self, monkeypatch, tmp_path,
    ):
        import drivers.claude_code_driver as ccd

        async def _never_down(*, engagement_id, attempts=3):
            return False
        monkeypatch.setattr(ccd.s6_rc, "ensure_service_down", _never_down)
        driver = ccd.ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://x",
        )
        rec = SimpleNamespace(id="e-cc", allocated_uid=0)
        with pytest.raises(RuntimeError, match="did not confirm down"):
            await driver.invalidate_session(rec)


class TestClaudeCodeStaleLaunch:
    """Sol+Terra diff-gate r6: the stale gate must run BEFORE the launch
    installs its background task/spool set — a stale launch spawning them
    would overwrite the rebuilt engagement's tracked machinery, orphaning
    the live tasks."""

    def _harness(self, monkeypatch, tmp_path, order):
        from drivers import s6_rc
        from drivers.claude_code_driver import ClaudeCodeDriver
        from test_claude_code_driver import _patch_uid_drop_ok

        _patch_uid_drop_ok(monkeypatch)

        async def fake_cau():
            return None

        async def fake_start_kw(*, engagement_id):
            order.append("start_service")

        async def fake_down(*, engagement_id, attempts=3):
            order.append("ensure_down")
            return True

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_kw)
        monkeypatch.setattr(s6_rc, "ensure_service_down", fake_down)
        monkeypatch.setattr(
            s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()
        monkeypatch.setattr(
            ClaudeCodeDriver, "_spawn_background_tasks",
            lambda self, engagement: order.append("spawn_bg"))

        async def _noop_write(self, engagement, text):
            order.append("fifo_write")
        monkeypatch.setattr(ClaudeCodeDriver, "_write_to_fifo", _noop_write)

    async def test_completed_rebuild_interleaving_aborts_before_spawn(
        self, monkeypatch, tmp_path,
    ):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers.driver_protocol import StaleLaunchError
        from test_claude_code_driver import _make_defn, _make_record

        order: list[str] = []
        self._harness(monkeypatch, tmp_path, order)

        rec = _make_record(allocated_uid=200005)
        # The registry's live record: a clamp→rebuild cycle COMPLETED while
        # this launch was suspended — flag cleared, generation moved.
        latest = SimpleNamespace(
            context_rebuild_pending=False, context_generation=1)

        class FakeReg:
            def get(self, eid):
                return latest

        async def send(topic_id, text, **kw):
            return 4242
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=send,
            casa_framework_mcp_url="http://x",
            registry=FakeReg(),
        )
        (tmp_path / "engagements").mkdir()

        with pytest.raises(StaleLaunchError) as exc_info:
            await drv.start(
                rec, prompt="pre-clamp prompt",
                options=_make_defn(tmp_path), expected_generation=0)

        assert exc_info.value.record_live is True
        # The stale launch installed NO machinery over the rebuilt
        # engagement's, delivered nothing, and left its service alone.
        assert "spawn_bg" not in order
        assert "fifo_write" not in order
        assert "ensure_down" not in order


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

    async def test_completed_rebuild_cycle_aborts_stale_launch_by_generation(self):
        """Terra diff-gate r2: a clamp→rebuild cycle COMPLETING while the old
        launch is suspended clears the pending flag — only the generation
        comparison can still see it, and the stale launch must not overwrite
        the fresh client the rebuild registered."""
        from drivers.driver_protocol import StaleLaunchError
        from drivers.in_casa_driver import InCasaDriver

        rec = SimpleNamespace(
            id="e-gen", topic_id=7, kind="executor",
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
            tombstone_path=str(tmp_path / "e.json"), bus=None)
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
