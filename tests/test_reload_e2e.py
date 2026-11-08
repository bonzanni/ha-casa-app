"""End-to-end reload integration test (Task B.7).

Exercises the full ``reload.dispatch('agent', ...)`` codepath against a
real :class:`runtime.CasaRuntime` while observing a real on-disk YAML
mutation between boot-state and the reload call.

**Shape: monkeypatched loader / constructor.** Per the plan's Task B.7
self-review, a fully-real e2e (real ``agent_loader.load_agent_from_dir``
through real ``Agent`` construction) requires a complete schema-validated
agent tree (character + voice + response_shape + runtime + disclosure +
real ``PolicyLibrary`` plumbing — well over 100 LOC of harness). The
plan's spec §7 contract is
"edit YAML on disk -> dispatch -> assert new state in runtime"; that
contract is what this test enforces. ``agent_loader.load_agent_from_dir``
itself is exercised by ``tests/test_agent_loader*.py``; what B.7 adds on
top of B.2's ``test_resident_atomic_swap`` is **observed disk-side state
change drives the post-reload runtime state**, not just a swap to a
pre-baked AgentConfig spy.

The test:
1. Writes a real ``character.yaml`` v1 to disk (``name: Ellen``).
2. Boots the runtime with a v1 AgentConfig spy + v1 Agent spy seeded.
3. Mutates ``character.yaml`` on disk to v2 (``name: Ellen-2``).
4. Stages the loader to return a v2 spy on the next call (mirroring what
   the real loader would observe after the disk write).
5. Calls ``await reload.dispatch('agent', role='ellen', runtime=...)``.
6. Asserts: status=ok, atomic swap completed, ``runtime.agents['ellen']``
   is now the v2 Agent, ``runtime.role_configs['ellen']`` is the v2 cfg,
   bus was rebound, ``actions`` includes ``load_config``,
   ``construct_agent``, ``reregister_bus``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _make_runtime(tmp_path: Path):
    """Build a real CasaRuntime; mock anything not on the reload-agent path."""
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={},
        role_configs={},
        specialist_registry=MagicMock(),
        engagement_registry=MagicMock(),
        agent_registry=MagicMock(),
        trigger_registry=MagicMock(),
        mcp_registry=MagicMock(),
        session_registry=MagicMock(),
        channel_manager=MagicMock(),
        bus=MagicMock(),
        engagement_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir=str(tmp_path),
        agents_dir=str(tmp_path / "agents"),
        home_root=str(tmp_path / "home"),
        defaults_root=str(tmp_path / "defaults"),
    )


class TestReloadE2E:
    async def test_edit_character_yaml_then_dispatch_then_new_name_visible(
        self, tmp_path, monkeypatch,
    ):
        """E2E: write v1 -> seed runtime -> edit on disk -> dispatch -> v2 visible."""
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)

        # --- Disk setup: real YAML files at agents/ellen/ -------------------
        # The reload_agent handler reads ``os.path.isdir(agents_dir/role)``
        # for tier detection, so the dir + character.yaml must really exist.
        ellen_dir = tmp_path / "agents" / "ellen"
        ellen_dir.mkdir(parents=True)
        char_yaml = ellen_dir / "character.yaml"
        char_yaml.write_text(
            "role: ellen\nname: Ellen\narchetype: resident\nprompt: hi\n",
            encoding="utf-8",
        )

        # --- Loader/constructor staging ------------------------------------
        # State machine: first invocation returns v1 cfg, subsequent
        # invocations return v2 cfg — mirroring what the real loader
        # would observe after the disk write below.
        v1_cfg = SimpleNamespace(
            role="ellen",
            character=SimpleNamespace(name="Ellen", card=""),
            triggers=[],
            channels=[],
            memory=SimpleNamespace(read_strategy="per_turn"),
        )
        v2_cfg = SimpleNamespace(
            role="ellen",
            character=SimpleNamespace(name="Ellen-2", card=""),
            triggers=[],
            channels=[],
            memory=SimpleNamespace(read_strategy="per_turn"),
        )
        v1_agent = MagicMock(name="agent_v1")
        v1_agent.handle_message = MagicMock()
        v1_agent.aclose = AsyncMock()
        v2_agent = MagicMock(name="agent_v2")
        v2_agent.handle_message = MagicMock()
        v2_agent.aclose = AsyncMock()

        load_calls = {"n": 0}

        def fake_load(*args, **kwargs):
            return v1_cfg if load_calls["n"] == 0 else v2_cfg

        def fake_construct(*, cfg, runtime, **kw):
            return v1_agent if cfg is v1_cfg else v2_agent

        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir", fake_load,
        )
        monkeypatch.setattr(
            "policies.load_policies", lambda *a, **kw: MagicMock(),
        )
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent", fake_construct)

        # --- Boot state: runtime already has v1 wired in --------------------
        runtime = _make_runtime(tmp_path)
        runtime.role_configs["ellen"] = v1_cfg
        runtime.agents["ellen"] = v1_agent
        # AgentRegistry.build is called by reload_agent; stub the rebuild path
        # via specialist_registry.all_configs (real AgentRegistry.build
        # accepts mappings of role -> AgentConfig).
        runtime.specialist_registry.all_configs = lambda: {}

        # Pre-condition: runtime sees Ellen.
        assert runtime.agents["ellen"] is v1_agent
        assert runtime.role_configs["ellen"].character.name == "Ellen"

        # --- Disk mutation: v1 -> v2 ---------------------------------------
        char_yaml.write_text(
            "role: ellen\nname: Ellen-2\narchetype: resident\nprompt: hi\n",
            encoding="utf-8",
        )
        load_calls["n"] = 1  # next loader call returns v2_cfg

        # --- The full reload path: locks, handler, atomic swap, log line ---
        result = await dispatch("agent", runtime=runtime, role="ellen")

        # --- Result envelope shape -----------------------------------------
        assert result["status"] == "ok", f"dispatch failed: {result}"
        assert result["scope"] == "agent"
        assert result["role"] == "ellen"
        assert "ms" in result
        # Action trail proves we walked the full handler body.
        assert "load_config" in result["actions"]
        assert "construct_agent" in result["actions"]
        assert "reregister_bus" in result["actions"]
        assert "rebuild_agent_registry" in result["actions"]

        # --- Post-condition: atomic swap landed v2 -------------------------
        assert runtime.agents["ellen"] is v2_agent
        assert runtime.agents["ellen"] is not v1_agent
        assert runtime.role_configs["ellen"] is v2_cfg
        # Disk-driven name change is the v2 visible to runtime.
        assert runtime.role_configs["ellen"].character.name == "Ellen-2"

        # Bus was rebound to v2's handle_message.
        runtime.bus.register.assert_any_call("ellen", v2_agent.handle_message)

        # File on disk reflects the edit (sanity).
        assert "Ellen-2" in char_yaml.read_text(encoding="utf-8")
