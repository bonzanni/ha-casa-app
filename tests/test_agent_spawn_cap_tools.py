"""#283 — the agent-spawn cap at the engagement-creating launch.

Pins: (a) an agent-context engagement launch at the cap is refused typed
and side-effect-free (no Telegram call of any kind); (b) an operator-marked
turn is exempt (no occupancy taken); (c) a post-acquire failure releases the
reservation (leak-free); (d) engagement-bound and delegated turns classify
as agent context even when a forged ``_operator_turn`` rides the origin;
(e) the delegation depth gate reads the bound engagement record's origin
when the ambient origin carries no depth (in_casa turns inherit the parent
task's origin_var); (f) the interactive branch stamps depth 1 on the child
record and transfers the spawn reservation to it.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import (
    AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig, SessionConfig,
    ToolsConfig,
)
from specialist_limits import AgentSpawnLimiter

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _channel(ok=True):
    ch = MagicMock()
    ch.engagement_supergroup_id = -100123 if ok else 0
    ch.engagement_permission_ok = ok
    ch.setup_engagement_features = AsyncMock()
    ch.open_engagement_topic = AsyncMock(return_value=42)
    ch.send_to_topic = AsyncMock()
    return ch


def _wire(tmp_path, *, limiter, channel_ok=True, engagement_registry=None):
    from tools import init_tools
    try:
        from tests.test_delegate_to_agent_interactive import (
            _make_alex_cfg, _make_assistant_cfg)
    except ImportError:
        from test_delegate_to_agent_interactive import (
            _make_alex_cfg, _make_assistant_cfg)
    ch = _channel(channel_ok)
    cm = MagicMock(); cm.get.return_value = ch
    specialist_reg = MagicMock()
    specialist_reg.get.return_value = _make_alex_cfg()
    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=specialist_reg, mcp_registry=MagicMock(),
        trigger_registry=MagicMock(),
        engagement_registry=engagement_registry or MagicMock(),
        agent_role_map={"assistant": _make_assistant_cfg(),
                        "finance": _make_alex_cfg()},
        agent_spawn_limiter=limiter,
    )
    return ch


_AGENT_ORIGIN = {
    "role": "assistant", "channel": "telegram", "chat_id": "c1",
    "cid": "x", "user_text": "hi",
}


async def _engage(origin):
    """One engagement launch — a specialist-hosted job through the launch
    owner, the engagement-creating site left — under *origin*."""
    try:
        from tests.test_delegate_to_agent_interactive import launch_job
    except ImportError:
        from test_delegate_to_agent_interactive import launch_job
    r = await launch_job("do the thing", origin=dict(origin))
    return json.loads(r["content"][0]["text"])


class TestEngageExecutorSpawnCap:
    """The cap at the engagement launch (the class keeps the name it had when
    ``engage_executor`` was the launcher under test)."""

    async def test_agent_context_at_cap_refused_before_any_telegram_call(
            self, tmp_path):
        lim = AgentSpawnLimiter(max_spawns=1)
        held = lim.try_acquire()
        assert held is not None
        ch = _wire(tmp_path, limiter=lim)
        payload = await _engage(_AGENT_ORIGIN)  # no _operator_turn = agent
        assert payload["kind"] == "agent_spawn_cap_exceeded"
        ch.setup_engagement_features.assert_not_awaited()
        ch.open_engagement_topic.assert_not_awaited()
        assert lim.occupancy == 1  # nothing acquired for the refused call

    async def test_operator_marked_turn_takes_no_occupancy(self, tmp_path):
        lim = AgentSpawnLimiter(max_spawns=1)
        held = lim.try_acquire()  # cap saturated — operator must not care
        assert held is not None
        _wire(tmp_path, limiter=lim)
        payload = await _engage({**_AGENT_ORIGIN, "channel": "webhook",
                                 "_operator_turn": True})
        # Sailed past the cap gate (else agent_spawn_cap_exceeded) and
        # failed later on its non-Telegram origin.
        assert payload["kind"] == "engagement_wrong_origin"
        assert lim.occupancy == 1

    async def test_post_acquire_failure_releases_reservation(self, tmp_path):
        lim = AgentSpawnLimiter(max_spawns=3)
        _wire(tmp_path, limiter=lim)
        payload = await _engage({**_AGENT_ORIGIN, "channel": "webhook"})
        assert payload["kind"] == "engagement_wrong_origin"
        assert lim.occupancy == 0  # leak-free: wrapper finally released it

    async def test_engagement_bound_turn_is_agent_context_despite_marker(
            self, tmp_path):
        import tools as tools_mod
        lim = AgentSpawnLimiter(max_spawns=1)
        assert lim.try_acquire() is not None
        _wire(tmp_path, limiter=lim)
        eng = MagicMock()
        eng.origin = {}
        eng.context_rebuild_pending = False
        tok = tools_mod.engagement_var.set(eng)
        try:
            payload = await _engage({**_AGENT_ORIGIN, "_operator_turn": True})
        finally:
            tools_mod.engagement_var.reset(tok)
        assert payload["kind"] == "agent_spawn_cap_exceeded"

    async def test_delegated_turn_is_agent_context_despite_marker(
            self, tmp_path):
        """A delegated turn reaches the cap only by starting its specialist's
        OWN job (S7a self-host: no depth gate); the depth it carries still
        makes it agent context, whatever marker rides the origin."""
        import agent as agent_mod
        import tools as tools_mod
        try:
            from tests.test_delegate_to_agent_interactive import _job
        except ImportError:
            from test_delegate_to_agent_interactive import _job
        lim = AgentSpawnLimiter(max_spawns=1)
        assert lim.try_acquire() is not None
        ch = _wire(tmp_path, limiter=lim)
        origin = {**_AGENT_ORIGIN, "role": "finance", "_operator_turn": True,
                  "delegation_depth": 1, "plugin_job": {"plugin": "finplan"}}
        token = agent_mod.origin_var.set(origin)
        try:
            r = await tools_mod._launch_interactive_engagement(
                "finance", "do the thing", "", origin, job=_job(),
                self_host=True)
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "agent_spawn_cap_exceeded"
        ch.open_engagement_topic.assert_not_awaited()
        assert lim.occupancy == 1

    async def test_no_limiter_wired_means_cap_off(self, tmp_path):
        _wire(tmp_path, limiter=None)
        payload = await _engage({**_AGENT_ORIGIN, "channel": "webhook"})
        assert payload["kind"] == "engagement_wrong_origin"


class TestDepthRecordFallback:
    async def test_depth_gate_reads_bound_record_origin(self, tmp_path):
        """An interactively-engaged specialist's turn carries no ambient
        depth (origin_var is inherited from the parent task), but its bound
        record does — the gate must read it (INV-ENG-004 gap 2)."""
        import agent as agent_mod
        import tools as tools_mod
        from tools import delegate_to_agent, init_tools

        caller_cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
                                 role="finance")
        caller_cfg.delegates = [
            DelegateEntry(agent="butler", purpose="p", when="w")]
        init_tools(
            channel_manager=None, bus=None,
            specialist_registry=MagicMock(), mcp_registry=None,
            agent_role_map={"finance": caller_cfg},
        )
        eng = MagicMock()
        eng.origin = {"delegation_depth": 1}
        eng.context_rebuild_pending = False
        origin_token = agent_mod.origin_var.set({
            "role": "finance", "channel": "telegram", "chat_id": "1",
            "user_id": 1, "cid": "abc", "user_text": "x",
        })
        eng_token = tools_mod.engagement_var.set(eng)
        try:
            r = await delegate_to_agent.handler({
                "agent": "butler", "task": "nested", "context": "",
                "mode": "sync",
            })
        finally:
            tools_mod.engagement_var.reset(eng_token)
            agent_mod.origin_var.reset(origin_token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "delegation_depth_exceeded"


class TestJobEngagementSpawn:
    """A specialist-hosted job runs as an engagement of its specialist: the
    launch owner stamps the child's depth and, in agent context, holds an
    agent-spawn token on the record."""

    def _alex_cfg(self):
        cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="finance")
        cfg.character = CharacterConfig(name="Alex", archetype="finance",
                                        card="", prompt="You are Alex.")
        cfg.enabled = True
        cfg.model = "sonnet"
        cfg.tools = ToolsConfig(allowed=["Read"], disallowed=[],
                                permission_mode="acceptEdits", max_turns=20)
        cfg.mcp_server_names = []
        cfg.memory = MemoryConfig(token_budget=0)
        cfg.session = SessionConfig(strategy="ephemeral", idle_timeout=0)
        cfg.channels = []
        cfg.system_prompt = "You are Alex."
        return cfg

    async def test_a_job_child_stamps_depth_and_transfers_token(
            self, tmp_path):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        from test_delegate_to_agent_interactive import launch_job

        lim = AgentSpawnLimiter(max_spawns=3)
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"),
            agent_spawn_limiter=lim)
        tch = _channel(ok=True)
        tch.open_engagement_topic = AsyncMock(return_value=555)
        cm = MagicMock(); cm.get.return_value = tch
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = self._alex_cfg()
        caller = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
                             role="assistant")
        caller.delegates = [
            DelegateEntry(agent="finance", purpose="p", when="w")]
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
            agent_role_map={"assistant": caller},
            agent_spawn_limiter=lim,
        )
        driver = MagicMock()
        driver.open = AsyncMock()
        agent_mod.active_engagement_driver = driver

        res = await launch_job("Plan Q2", origin=dict(_AGENT_ORIGIN))  # agent context
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending"
        rec = reg.get(payload["engagement_id"])
        assert rec is not None
        assert rec.origin["delegation_depth"] == 1
        assert rec.origin["_agent_spawned"] is True
        assert rec.agent_spawn_permit is not None
        assert lim.occupancy == 1
        # Terminal transition releases the transferred token.
        await reg.mark_cancelled(rec.id)
        assert lim.occupancy == 0

    async def test_an_operator_job_child_stamps_depth_without_marker(
            self, tmp_path):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        from test_delegate_to_agent_interactive import launch_job

        lim = AgentSpawnLimiter(max_spawns=3)
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"),
            agent_spawn_limiter=lim)
        tch = _channel(ok=True)
        tch.open_engagement_topic = AsyncMock(return_value=556)
        cm = MagicMock(); cm.get.return_value = tch
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = self._alex_cfg()
        caller = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
                             role="assistant")
        caller.delegates = [
            DelegateEntry(agent="finance", purpose="p", when="w")]
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
            agent_role_map={"assistant": caller},
            agent_spawn_limiter=lim,
        )
        driver = MagicMock()
        driver.open = AsyncMock()
        agent_mod.active_engagement_driver = driver

        res = await launch_job("Plan Q2", origin={**_AGENT_ORIGIN, "_operator_turn": True})
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending"
        rec = reg.get(payload["engagement_id"])
        # Depth stamps for EVERY job child (operator included) so the
        # child cannot delegate onward; the spawn marker/occupancy is
        # agent-context-only.
        assert rec.origin["delegation_depth"] == 1
        assert "_agent_spawned" not in rec.origin
        assert rec.agent_spawn_permit is None
        assert lim.occupancy == 0
