"""Tests for reload.py dispatcher + per-scope handlers."""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest.fixture(autouse=True)
def _restore_active_specialist_index():
    """Stale-index fix (Plan 2 review, no GH issue): reload.py's
    `_specialist_roles_dir` — invoked by every specialist-tier reload path
    this file exercises (`reload_agents` in particular, unconditionally) —
    now publishes its freshly loaded `InstalledSpecialistIndex` via
    `specialist_registry.set_active_installed_index`, a process-wide global.
    Save/restore it around every test in this file so that real refresh can
    never leak into an unrelated test file (mirrors the same pattern already
    used in tests/test_personality_admin_handlers.py for its own, mocked,
    mutations of this global)."""
    import specialist_registry as specialist_registry_mod

    original = specialist_registry_mod._active_index
    yield
    specialist_registry_mod._active_index = original


def _make_runtime():
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(), trigger_registry=MagicMock(),
        mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(),
        claude_code_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir="/x", agents_dir="/x/agents",
        home_root="/x/home", defaults_root="/opt/casa",
    )


class TestDispatchUnknownScope:
    async def test_returns_error_envelope(self):
        from reload import dispatch
        runtime = _make_runtime()
        result = await dispatch("nope", runtime=runtime)
        assert result["status"] == "error"
        assert result["kind"] == "unknown_scope"
        assert "nope" in result["message"]

    async def test_includes_scope_and_ms(self):
        from reload import dispatch
        runtime = _make_runtime()
        result = await dispatch("nope", runtime=runtime)
        assert result["scope"] == "nope"
        assert "ms" in result and result["ms"] >= 0


class TestReloadExecutorsHealth:
    async def test_executors_reload_regenerates_plugin_health(self, monkeypatch):
        """v0.71.1 (Sol Task-5): enabling/disabling an executor changes plugin
        authorization, so an executors reload must refresh plugin-health (regen +
        notify) — else a newly-enabled executor whose plugin lacks a grant stays
        stale-green with no DM until an unrelated trigger."""
        import tools as tools_mod
        from reload import reload_executors
        regen_calls, notify_calls = [], []
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                            lambda extra: regen_calls.append(extra))

        async def _fake_notify():
            notify_calls.append(True)

        monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible",
                            _fake_notify)
        runtime = _make_runtime()          # empty role_configs, MagicMock registry
        actions = await reload_executors(runtime)
        assert "rebuild_executor_registry" in actions
        assert "plugin_health_regenerated" in actions
        assert regen_calls == [[]]         # regen once, no mutation-specific extras
        assert notify_calls == [True]

    async def test_executors_reload_survives_health_regen_failure(self, monkeypatch):
        """The health refresh must never fail the reload itself."""
        import tools as tools_mod
        from reload import reload_executors

        def _boom(extra):
            raise RuntimeError("regen blew up")

        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", _boom)
        runtime = _make_runtime()
        actions = await reload_executors(runtime)
        assert "rebuild_executor_registry" in actions
        assert "plugin_health_regenerated" not in actions   # skipped, not fatal


class TestReloadExecutorsHookPolicies:
    async def test_reload_rebuilds_http_hook_policy_map_in_place(
        self, tmp_path, monkeypatch,
    ):
        """#340: the /hooks/resolve handlers capture the executor policy map
        built once at boot; reload_executors must rebuild it by MUTATING that
        shared dict (clear+update), so a new/edited executor's parameters take
        effect and stale-broader callbacks stop enforcing. Pre-fix the map
        stayed frozen at boot."""
        import tools as tools_mod
        from types import SimpleNamespace
        from executor_registry import ExecutorRegistry
        from reload import reload_executors

        monkeypatch.setattr(
            tools_mod, "_regenerate_plugin_health", lambda extra: None)

        async def _noop():
            return None

        monkeypatch.setattr(
            tools_mod, "_notify_plugin_health_if_possible", _noop)

        hooks_path = tmp_path / "hooks.yaml"
        hooks_path.write_text(
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: path_scope\n"
            "    writable: [/data/engagements/]\n"
            "    readable: [/data/engagements/]\n"
        )
        reg = ExecutorRegistry(str(tmp_path / "executors"))

        def _fake_load():
            reg._defs["newexec"] = SimpleNamespace(
                driver="claude_code", hooks_path=str(hooks_path),
            )

        monkeypatch.setattr(reg, "load", _fake_load)

        runtime = _make_runtime()
        runtime.executor_registry = reg
        # The instance the handlers captured at boot, holding a stale entry.
        captured = {"stale-exec": {"path_scope": ("Read", None)}}
        runtime.executor_cc_policies = captured

        actions = await reload_executors(runtime)

        assert "rebuild_executor_hook_policies" in actions
        assert "newexec" in captured          # fresh entry visible to handlers
        assert "stale-exec" not in captured   # stale entry gone
        assert runtime.executor_cc_policies is captured  # same object, mutated

    async def test_reload_keeps_old_callbacks_for_failed_executor(
        self, tmp_path, monkeypatch,
    ):
        """Sol r1-1: an executor whose reload FAILS must keep its pre-reload
        callbacks — dropping them would fall its live engagements back to the
        broader defaults (commit_size_guard default 20 vs a configured 5). A
        genuinely REMOVED executor's entry is still dropped."""
        import tools as tools_mod
        from executor_registry import ExecutorRegistry
        from reload import reload_executors

        monkeypatch.setattr(
            tools_mod, "_regenerate_plugin_health", lambda extra: None)

        async def _noop():
            return None

        monkeypatch.setattr(
            tools_mod, "_notify_plugin_health_if_possible", _noop)

        reg = ExecutorRegistry(str(tmp_path / "executors"))

        def _fake_load():
            reg._failed_types.add("broken-exec")  # load failure this round

        monkeypatch.setattr(reg, "load", _fake_load)

        runtime = _make_runtime()
        runtime.executor_registry = reg
        tight_entry = {"commit_size_guard": ("Write|Edit", object())}
        captured = {
            "broken-exec": tight_entry,       # fails to reload → must survive
            "removed-exec": {"path_scope": ("Read", object())},  # gone → drop
        }
        runtime.executor_cc_policies = captured

        actions = await reload_executors(runtime)

        assert captured["broken-exec"] is tight_entry
        assert "removed-exec" not in captured
        assert "executor_hook_policies_kept_stale:broken-exec" in actions

    async def test_reload_without_boot_map_skips_rebuild(self, monkeypatch):
        """A runtime with no captured map (narrow test stand-ins) skips the
        rebuild instead of crashing."""
        import tools as tools_mod
        from reload import reload_executors

        monkeypatch.setattr(
            tools_mod, "_regenerate_plugin_health", lambda extra: None)

        async def _noop():
            return None

        monkeypatch.setattr(
            tools_mod, "_notify_plugin_health_if_possible", _noop)

        runtime = _make_runtime()
        assert runtime.executor_cc_policies is None
        actions = await reload_executors(runtime)
        assert "rebuild_executor_hook_policies" not in actions


class TestReloadError:
    async def test_reload_error_caught_and_converted(self, monkeypatch):
        # Inject a fake handler that raises ReloadError; dispatch must
        # convert to result envelope.
        from reload import ReloadError, dispatch
        import reload as reload_mod

        async def boom(runtime, role=None):
            raise ReloadError("synthetic", "deliberate failure")

        monkeypatch.setitem(reload_mod._HANDLERS, "synthetic_scope", boom)
        runtime = _make_runtime()
        result = await dispatch("synthetic_scope", runtime=runtime)
        assert result["status"] == "error"
        assert result["kind"] == "synthetic"
        assert result["message"] == "deliberate failure"


class TestLockSerialization:
    async def test_same_scope_serializes(self, monkeypatch):
        # Two concurrent dispatches for the same scope must run sequentially.
        from reload import dispatch
        import reload as reload_mod

        ordering: list[str] = []

        async def slow_handler(runtime, role=None):
            ordering.append(f"start:{role}")
            await asyncio.sleep(0.05)
            ordering.append(f"end:{role}")
            return ["did_work"]

        monkeypatch.setitem(reload_mod._HANDLERS, "test_scope", slow_handler)
        # Both calls share the role-less lock-key for test_scope.
        runtime = _make_runtime()
        await asyncio.gather(
            dispatch("test_scope", runtime=runtime, role="a"),
            dispatch("test_scope", runtime=runtime, role="b"),
        )
        # No interleaving possible if locks work — second start AFTER first end.
        assert ordering[0].startswith("start:")
        assert ordering[1].startswith("end:")
        assert ordering[2].startswith("start:")
        assert ordering[3].startswith("end:")


class TestReloadTriggers:
    async def test_unknown_role_raises_load_error(self, tmp_path):
        from reload import dispatch, register_handler
        from reload import reload_triggers  # implemented below
        register_handler("triggers", reload_triggers)
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(tmp_path / "agents")
        # No agents/<role>/ dir on disk
        result = await dispatch("triggers", runtime=runtime, role="ghost")
        assert result["status"] == "error"
        assert result["kind"] == "unknown_role"

    async def test_happy_path_calls_reregister(self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_triggers
        register_handler("triggers", reload_triggers)
        # Set up a fake agent dir with minimal files agent_loader expects.
        agents_dir = tmp_path / "agents"
        ellen_dir = agents_dir / "ellen"
        ellen_dir.mkdir(parents=True)
        # Stub load_agent_from_dir + load_policies via monkeypatch.
        import reload as reload_mod
        from types import SimpleNamespace
        fake_cfg = SimpleNamespace(
            triggers=[SimpleNamespace(name="t1")],
            channels=["telegram"],
        )
        async def fake_load(*a, **kw): return None
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: fake_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.trigger_registry.reregister_for = MagicMock()

        result = await dispatch("triggers", runtime=runtime, role="ellen")
        assert result["status"] == "ok"
        runtime.trigger_registry.reregister_for.assert_called_once_with(
            "ellen", [fake_cfg.triggers[0]], ["telegram"],
        )

    async def test_updates_runtime_role_configs_for_resident(
        self, tmp_path, monkeypatch,
    ):
        """Q-1 regression: after dispatch('triggers'), the runtime
        role_configs cache MUST reflect the new cfg.triggers - not the
        boot-time list. Without this, tools.casa_reload_triggers
        returns a stale `registered` field that misleads the LLM.
        """
        from reload import dispatch, register_handler, reload_triggers
        register_handler("triggers", reload_triggers)

        agents_dir = tmp_path / "agents"
        ellen_dir = agents_dir / "ellen"
        ellen_dir.mkdir(parents=True)

        from types import SimpleNamespace
        boot_cfg = SimpleNamespace(
            triggers=[SimpleNamespace(name="boot-trigger")],
            channels=["telegram"],
        )
        new_cfg = SimpleNamespace(
            triggers=[
                SimpleNamespace(name="boot-trigger"),
                SimpleNamespace(name="probe-q1"),
            ],
            channels=["telegram"],
        )
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": boot_cfg}
        runtime.trigger_registry.reregister_for = MagicMock()

        result = await dispatch("triggers", runtime=runtime, role="ellen")

        assert result["status"] == "ok"
        assert runtime.role_configs["ellen"] is new_cfg
        names = [t.name for t in runtime.role_configs["ellen"].triggers]
        assert names == ["boot-trigger", "probe-q1"]

    async def test_specialist_branch_refreshes_registry(
        self, tmp_path, monkeypatch,
    ):
        """Q-1 specialist symmetry: when the role lives under
        agents/specialists/<role>/, dispatch('triggers') MUST trigger
        a SpecialistRegistry refresh so the back-compat consumer sees
        the post-reload state. Mirrors reload_agent's specialist branch
        at reload.py:341-348.

        (Specialists can't actually carry triggers per the file-set
        rules - this test asserts the codepath fires for symmetry.)

        Task N1b Step 25b: the refresh call now threads roles_dir (the
        reconciled specialist roles overlay) through — the fix this whole
        task exists to make. The assertion below was updated from
        ``assert_called_once_with()`` (zero args) to pin the NEW call
        signature; this is not a guard being loosened, it is the exact
        code path Step 25b intentionally changes.
        """
        from reload import dispatch, register_handler, reload_triggers
        register_handler("triggers", reload_triggers)

        agents_dir = tmp_path / "agents"
        spec_dir = agents_dir / "specialists" / "finance"
        spec_dir.mkdir(parents=True)

        from types import SimpleNamespace
        new_cfg = SimpleNamespace(triggers=[], channels=[])
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": SimpleNamespace(triggers=[])}
        runtime.trigger_registry.reregister_for = MagicMock()
        runtime.specialist_registry.load = MagicMock()

        result = await dispatch("triggers", runtime=runtime, role="finance")

        assert result["status"] == "ok"
        assert "finance" not in runtime.role_configs
        runtime.specialist_registry.load.assert_called_once()
        assert runtime.specialist_registry.load.call_args.kwargs["roles_dir"]


class TestReloadAgent:
    async def test_unknown_role_raises(self, tmp_path):
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)
        runtime = _make_runtime()
        runtime.agents_dir = str(tmp_path / "agents")
        result = await dispatch("agent", runtime=runtime, role="ghost")
        assert result["status"] == "error"
        assert result["kind"] == "unknown_role"

    async def test_resident_atomic_swap(self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agent
        from types import SimpleNamespace
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)

        # The new AgentConfig + Agent we'll observe post-swap.
        new_cfg = SimpleNamespace(role="ellen",
                                  character=SimpleNamespace(name="Ellen-2", card=""),
                                  triggers=[], channels=[])
        new_agent = MagicMock()
        new_agent.handle_message = MagicMock()

        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        # Patch reload_agent's Agent constructor to return our spy.
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent", lambda *a, **kw: new_agent)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs["ellen"] = SimpleNamespace(
            role="ellen",
            character=SimpleNamespace(name="Ellen", card=""),
        )
        old_agent = MagicMock()
        old_agent.aclose = AsyncMock()
        runtime.agents["ellen"] = old_agent

        result = await dispatch("agent", runtime=runtime, role="ellen")
        assert result["status"] == "ok"
        # Atomic-swap completed.
        assert runtime.agents["ellen"] is new_agent
        # Bus was rebound.
        runtime.bus.register.assert_any_call("ellen", new_agent.handle_message)
        # role_configs updated.
        assert runtime.role_configs["ellen"] is new_cfg

    async def test_agent_reload_kicks_setup_episode_worker(self, tmp_path,
                                                           monkeypatch):
        # #423 r2 (Sol 1 / Terra 1): the agent reload is what makes a
        # "waiting for target agent" setup episode dispatchable — the swap
        # must wake the worker. Today the wake arrives via the swap's
        # trigger re-registration (trigger_reconcile kicks on every
        # reconcile); this pins the PROPERTY, not the mechanism.
        from reload import dispatch, register_handler, reload_agent
        from types import SimpleNamespace
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)
        new_cfg = SimpleNamespace(
            role="ellen",
            character=SimpleNamespace(name="Ellen-2", card=""),
            triggers=[], channels=[])
        new_agent = MagicMock()
        new_agent.handle_message = MagicMock()
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda *a, **kw: new_agent)

        import plugin_setup_episodes as pse
        kicked = {"n": 0}
        monkeypatch.setattr(
            pse, "kick", lambda: kicked.__setitem__("n", kicked["n"] + 1))

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs["ellen"] = SimpleNamespace(
            role="ellen",
            character=SimpleNamespace(name="Ellen", card=""))
        old_agent = MagicMock()
        old_agent.aclose = AsyncMock()
        runtime.agents["ellen"] = old_agent

        result = await dispatch("agent", runtime=runtime, role="ellen")
        assert result["status"] == "ok"
        assert kicked["n"] >= 1

    async def test_load_failure_leaves_runtime_untouched(self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)
        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)

        def boom(*a, **kw):
            raise RuntimeError("yaml is broken")

        monkeypatch.setattr("agent_loader.load_agent_from_dir", boom)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        old_agent = MagicMock()
        runtime.agents["ellen"] = old_agent

        result = await dispatch("agent", runtime=runtime, role="ellen")
        assert result["status"] == "error"
        assert result["kind"] == "load_error"
        # Old agent still in place.
        assert runtime.agents["ellen"] is old_agent

    async def test_scope_agent_provisions_agent_home_for_new_role(
        self, tmp_path, monkeypatch,
    ):
        """G-2 v0.37.7: configurator's
        ``casa_reload(scope=agent role=<new>)`` for a freshly-created
        specialist must produce ``<home_root>/<role>/.claude/settings.json``.

        Before the fix, only ``scope=agents`` (plural — the diff-based
        adds/evicts path) provisioned the agent-home; the granular
        per-role scope skipped it, and the first ``delegate_to_agent``
        failed with ``Working directory does not exist:
        /addon_configs/casa/agent-home/<role>``. The fix moves
        provisioning into ``_construct_agent`` so it fires regardless of
        which scope triggered the construction.
        """
        from reload import dispatch, register_handler, reload_agent
        from types import SimpleNamespace
        register_handler("agent", reload_agent)

        # Disk layout: assistant (resident) + butler (resident) +
        # specialists/probe (the new role under test).
        agents_dir = tmp_path / "agents"
        (agents_dir / "assistant").mkdir(parents=True)
        (agents_dir / "butler").mkdir(parents=True)
        (agents_dir / "specialists" / "probe").mkdir(parents=True)
        home_root = tmp_path / "home"
        defaults_root = tmp_path / "defaults"

        new_cfg = SimpleNamespace(
            role="probe",
            character=SimpleNamespace(name="Probe", card=""),
            triggers=[], channels=[],
            memory=SimpleNamespace(read_strategy="per_turn"),
        )
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        # Let `_construct_agent` run for real (so provision_agent_home
        # fires). Stub the Agent class used inside.
        monkeypatch.setattr("agent.Agent", lambda **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.home_root = str(home_root)
        runtime.defaults_root = str(defaults_root)
        runtime.role_configs = {
            "assistant": MagicMock(), "butler": MagicMock(),
        }
        runtime.agents = {
            "assistant": MagicMock(), "butler": MagicMock(),
        }
        # specialist tier path through reload_agent.
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = lambda: {"probe": new_cfg}

        result = await dispatch("agent", runtime=runtime, role="probe")
        assert result["status"] == "ok", result
        # The fix: agent-home was provisioned even though scope=agent
        # (not scope=agents) triggered the construction.
        settings_path = home_root / "probe" / ".claude" / "settings.json"
        assert settings_path.is_file(), (
            f"expected agent-home settings.json at {settings_path}; "
            "scope=agent did not call provision_agent_home"
        )


class TestReloadPolicies:
    async def test_reloads_policy_lib_and_swaps_agents(
        self, tmp_path, monkeypatch,
    ):
        from reload import dispatch, register_handler, reload_policies
        register_handler("policies", reload_policies)

        new_policy_lib = MagicMock()

        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: new_policy_lib)
        # Patch _reload_role_after_policies (helper) to avoid real load.
        import reload as reload_mod
        called_roles: list[str] = []
        async def fake_reload_role(runtime, role):
            called_roles.append(role)
        monkeypatch.setattr(reload_mod, "_reload_role_after_policies", fake_reload_role)

        runtime = _make_runtime()
        runtime.role_configs = {"ellen": MagicMock(), "tina": MagicMock()}
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("policies", runtime=runtime)
        assert result["status"] == "ok"
        assert runtime.policy_lib is new_policy_lib
        assert sorted(called_roles) == ["ellen", "tina"]


class TestReloadPluginEnv:
    async def test_resolves_and_pushes_to_environ(self, monkeypatch):
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)
        # P4b: stub health regen — these tests cover env application
        # only; the real regen writes /data/plugin-health.json.
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda extra: None)

        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"FOO": "bar", "BAZ": "op://x"})
        monkeypatch.setattr("secrets_resolver.resolve",
                            lambda v: "RESOLVED" if "op://" in v else v)
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)

        runtime = _make_runtime()
        result = await dispatch("plugin_env", runtime=runtime)
        assert result["status"] == "ok"
        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "RESOLVED"

    async def test_kicks_setup_episode_worker(self, monkeypatch):
        # #423: a plugin_env reload is the moment a "waiting for plugin
        # secrets" setup episode can dispatch — the reload must kick the
        # worker, or the episode waits for an unrelated reconcile.
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda extra: None)
        monkeypatch.setattr("plugin_env_conf.read_entries", lambda: {})

        import plugin_setup_episodes as pse
        kicked = {"n": 0}
        monkeypatch.setattr(pse, "kick", lambda: kicked.__setitem__("n", kicked["n"] + 1))

        runtime = _make_runtime()
        result = await dispatch("plugin_env", runtime=runtime)
        assert result["status"] == "ok"
        assert kicked["n"] == 1

    async def test_every_successful_reload_scope_kicks_setup_worker(
            self, monkeypatch):
        # #423 r3 (Sol r2-3): ANY reload can change setup-episode readiness
        # (policies reconstructs agents; agent/agents swap bindings;
        # plugin_env lands secrets). One dispatch-level kick covers every
        # scope — present and future — instead of per-handler arms.
        from reload import dispatch, register_handler

        async def noop_handler(runtime, *, role=None):
            return ["noop"]

        register_handler("policies", noop_handler)
        import plugin_setup_episodes as pse
        kicked = {"n": 0}
        monkeypatch.setattr(
            pse, "kick", lambda: kicked.__setitem__("n", kicked["n"] + 1))
        runtime = _make_runtime()
        result = await dispatch("policies", runtime=runtime)
        assert result["status"] == "ok"
        assert kicked["n"] >= 1

    async def test_op_rotation_is_picked_up_on_reload(self, monkeypatch):
        """#345: resolve() lru-caches plaintext by the unchanged op:// reference,
        so without a cache invalidation a rotated 1Password field keeps feeding
        the revoked credential to reload (which then reports success). A
        plugin_env reload must re-read the vault."""
        from unittest.mock import MagicMock
        from reload import dispatch, register_handler, reload_plugin_env
        import secrets_resolver
        register_handler("plugin_env", reload_plugin_env)
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda extra: None)

        monkeypatch.setattr("plugin_env_conf.read_entries", lambda: {"KEY": "op://v/i/f"})
        secrets_resolver.resolve.cache_clear()
        values = iter(["old-secret", "new-secret"])

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.stdout = next(values) + "\n"
            result.returncode = 0
            return result

        monkeypatch.setattr(secrets_resolver.subprocess, "run", fake_run)
        monkeypatch.delenv("KEY", raising=False)

        runtime = _make_runtime()
        result = await dispatch("plugin_env", runtime=runtime)
        assert result["status"] == "ok"
        assert os.environ["KEY"] == "old-secret"
        # The operator rotates the 1Password field, then reloads again.
        result = await dispatch("plugin_env", runtime=runtime)
        assert result["status"] == "ok"
        assert os.environ["KEY"] == "new-secret"
        secrets_resolver.resolve.cache_clear()  # never leak the fake into other tests

    async def test_failed_resolution_leaves_the_variable_unset(self, monkeypatch):
        """#580: an op:// reference that cannot be resolved is not a value.

        The literal used to be installed instead, and a `.mcp.json` writing the
        DEFAULTED form `${VAR:-default}` then expanded to the literal — measured
        on the pinned CLI (2.1.220), only an UNSET variable takes the default,
        so the MCP server was launched holding `op://vault/item/field` where a
        credential belongs. Absence is what the boot path already produces
        (casa_core step 1b assigns inside its try)."""
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                            lambda extra: None)

        def _boom(value):
            raise RuntimeError("op read failed: service account revoked")

        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"CASA_PLUGIN_X": "op://v/i/f"})
        monkeypatch.setattr("secrets_resolver.resolve", _boom)
        monkeypatch.setenv("CASA_PLUGIN_X", "op://v/i/f")

        runtime = _make_runtime()
        result = await dispatch("plugin_env", runtime=runtime)
        assert result["status"] == "ok"      # the reload itself still succeeds
        assert "CASA_PLUGIN_X" not in os.environ

    async def test_failed_resolution_pops_a_previously_resolved_value(
            self, monkeypatch):
        """#345's rotation contract: the reload re-reads the vault precisely so
        a REVOKED credential stops being served. Keeping the last good value on
        a failed re-resolution would restore exactly the silent no-op that fix
        removed, so absence — which withholds the plugin loudly — is the fail
        direction."""
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                            lambda extra: None)

        def _boom(value):
            raise RuntimeError("op read failed")

        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"CASA_PLUGIN_Y": "op://v/i/f"})
        monkeypatch.setattr("secrets_resolver.resolve", _boom)
        monkeypatch.setenv("CASA_PLUGIN_Y", "the-previous-secret")

        runtime = _make_runtime()
        await dispatch("plugin_env", runtime=runtime)
        assert "CASA_PLUGIN_Y" not in os.environ

    async def test_failed_resolution_is_counted_in_the_actions(self, monkeypatch):
        """The reload result is what the configurator reads back, so a variable
        the reload could NOT apply must be visible there — `set_N_vars` counts
        what was actually set, never what was attempted."""
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                            lambda extra: None)

        def _resolve(value):
            if value.startswith("op://"):
                raise RuntimeError("op read failed")
            return value

        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"PLAIN_A": "a", "REF_B": "op://v/i/f"})
        monkeypatch.setattr("secrets_resolver.resolve", _resolve)
        monkeypatch.delenv("PLAIN_A", raising=False)
        monkeypatch.delenv("REF_B", raising=False)

        runtime = _make_runtime()
        result = await dispatch("plugin_env", runtime=runtime)
        assert "set_1_vars" in result["actions"]
        assert "unresolved_1_vars" in result["actions"]
        assert os.environ["PLAIN_A"] == "a"
        assert "REF_B" not in os.environ

    async def test_regenerates_plugin_health_after_env_applied(self, monkeypatch):
        """P4b (2026-07-18 self-containment plan): a secrets-only repair must
        clear a stale-red plugin-health.json — reload regenerates + notifies
        AFTER the new env is in os.environ (so the fresh verify pass sees the
        resolved secrets)."""
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)

        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"GMAIL_SA": "sa@x"})
        monkeypatch.setattr("secrets_resolver.resolve", lambda v: v)
        monkeypatch.delenv("GMAIL_SA", raising=False)

        env_at_regen: dict = {}
        notified: list[bool] = []
        import tools as tools_mod

        def fake_regen(extra):
            env_at_regen["GMAIL_SA"] = os.environ.get("GMAIL_SA")
        async def fake_notify():
            notified.append(True)
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", fake_regen)
        monkeypatch.setattr(
            tools_mod, "_notify_plugin_health_if_possible", fake_notify)

        runtime = _make_runtime()
        result = await dispatch("plugin_env", runtime=runtime)
        assert result["status"] == "ok"
        assert env_at_regen["GMAIL_SA"] == "sa@x"   # regen saw the new env
        assert notified == [True]
        assert "plugin_health_regenerated" in result["actions"]

    async def test_health_regen_serialized_under_plugin_tools_lock(self, monkeypatch):
        """Sol r4-2: the regen+notify must hold tools._PLUGIN_TOOLS_LOCK so it
        cannot interleave with a §3.9 registry mutation's own write→notify."""
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)

        monkeypatch.setattr("plugin_env_conf.read_entries", lambda: {"F": "1"})
        monkeypatch.setattr("secrets_resolver.resolve", lambda v: v)
        import tools as tools_mod
        held: list[bool] = []
        def fake_regen(extra):
            held.append(tools_mod._PLUGIN_TOOLS_LOCK.locked())
        async def fake_notify():
            held.append(tools_mod._PLUGIN_TOOLS_LOCK.locked())
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", fake_regen)
        monkeypatch.setattr(
            tools_mod, "_notify_plugin_health_if_possible", fake_notify)

        runtime = _make_runtime()
        result = await dispatch("plugin_env", runtime=runtime)
        assert result["status"] == "ok"
        assert held == [True, True]

    async def test_health_regen_failure_does_not_fail_reload(self, monkeypatch):
        """Env refresh is the primary contract — a health-regen crash must
        not turn a successful reload into an error."""
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)

        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"FOO": "1"})
        monkeypatch.setattr("secrets_resolver.resolve", lambda v: v)
        import tools as tools_mod
        def boom(extra):
            raise RuntimeError("health exploded")
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", boom)

        runtime = _make_runtime()
        result = await dispatch("plugin_env", runtime=runtime)
        assert result["status"] == "ok"
        assert os.environ.get("FOO") == "1"
        assert "plugin_health_regenerated" not in result["actions"]

    async def test_removes_dropped_keys(self, monkeypatch):
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)
        # P4b: stub health regen — these tests cover env application
        # only; the real regen writes /data/plugin-health.json.
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda extra: None)

        # First call: FOO + BAR present; remember snapshot.
        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"FOO": "1", "BAR": "2"})
        monkeypatch.setattr("secrets_resolver.resolve", lambda v: v)
        runtime = _make_runtime()
        await dispatch("plugin_env", runtime=runtime)
        assert os.environ.get("FOO") == "1"
        assert os.environ.get("BAR") == "2"

        # Second call: FOO only — BAR must be popped.
        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"FOO": "1"})
        await dispatch("plugin_env", runtime=runtime)
        assert os.environ.get("FOO") == "1"
        assert "BAR" not in os.environ


@pytest.mark.unit
class TestPluginEnvBootSeed:
    """M22 (v0.49.0): the boot path must seed _PLUGIN_ENV_LAST_KEYS.

    Pre-fix the snapshot started empty and only reload itself ever
    wrote it, so the FIRST casa_reload(scope='plugin_env') computed
    dropped = {} - new_keys = {} and could never remove a key that was
    applied at boot and has since been deleted from plugin-env.conf —
    a revoked plugin secret survived in os.environ (and kept being
    inherited by plugin MCP subprocesses) for the container's life.
    """

    async def test_first_reload_drops_key_applied_at_boot(self, monkeypatch):
        import reload as reload_mod
        from reload import dispatch, note_boot_plugin_env

        # Isolate the module-level snapshot from other tests.
        monkeypatch.setattr(reload_mod, "_PLUGIN_ENV_LAST_KEYS", set())
        # Simulate boot: the var was sourced into the process env and the
        # snapshot seeded (casa_core.main step 1b).
        monkeypatch.setenv("CASA_TEST_BOOT_SECRET", "s3cr3t")
        note_boot_plugin_env({"CASA_TEST_BOOT_SECRET"})
        # Operator then removed the line from plugin-env.conf.
        monkeypatch.setattr("plugin_env_conf.read_entries", lambda: {})
        monkeypatch.setattr("secrets_resolver.resolve", lambda v: v)

        result = await dispatch("plugin_env", runtime=_make_runtime())

        assert result["status"] == "ok"
        assert "dropped_1_vars" in result["actions"], (
            f"boot-applied key was not dropped: {result['actions']!r}"
        )
        assert "CASA_TEST_BOOT_SECRET" not in os.environ

    async def test_note_boot_plugin_env_copies_the_set(self, monkeypatch):
        """Mutating the caller's set afterwards must not leak into the
        snapshot."""
        import reload as reload_mod
        from reload import note_boot_plugin_env

        monkeypatch.setattr(reload_mod, "_PLUGIN_ENV_LAST_KEYS", set())
        keys = {"CASA_TEST_A"}
        note_boot_plugin_env(keys)
        keys.add("CASA_TEST_B")
        assert reload_mod._PLUGIN_ENV_LAST_KEYS == {"CASA_TEST_A"}


class TestReloadAgents:
    async def test_adds_new_resident(self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agents
        from types import SimpleNamespace
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()       # known
        (agents_dir / "newcomer").mkdir()    # added on disk

        new_cfg = SimpleNamespace(
            role="newcomer",
            character=SimpleNamespace(name="Newcomer", card=""),
            triggers=[], channels=[],
            memory=SimpleNamespace(read_strategy="per_turn"),
        )

        def fake_load(d, **kw):
            return new_cfg if "newcomer" in d else MagicMock(role="ellen")

        monkeypatch.setattr("agent_loader.load_agent_from_dir", fake_load)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
        provisioned: list[str] = []
        monkeypatch.setattr(
            "agent_home.provision_agent_home",
            lambda *, role, home_root, defaults_root: provisioned.append(role),
        )
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": MagicMock()}
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "newcomer" in runtime.role_configs
        assert "newcomer" in runtime.agents
        assert "newcomer" in provisioned
        # ellen still there — no double-load
        assert "ellen" in runtime.role_configs

    async def test_evicts_deleted_resident(self, tmp_path, monkeypatch):
        """H11 (v0.49.0): eviction against the REAL MessageBus. The old
        version of this test used a MagicMock bus whose auto-created
        ``.unregister`` masked that MessageBus had no such method — the
        AttributeError was swallowed in prod and 'evicted' residents
        kept their queue + handler (ghost agents)."""
        from bus import MessageBus
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()  # ellen still on disk
        # tina was in role_configs but no dir on disk → evict.

        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: MagicMock())
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        bus = MessageBus()
        bus.register("ellen", MagicMock())
        bus.register("tina", MagicMock())
        runtime.bus = bus
        runtime.role_configs = {"ellen": MagicMock(), "tina": MagicMock()}
        ellen_agent = MagicMock()
        ellen_agent.aclose = AsyncMock()
        tina_agent = MagicMock()
        tina_agent.aclose = AsyncMock()
        runtime.agents = {"ellen": ellen_agent, "tina": tina_agent}
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "tina" not in runtime.role_configs
        assert "tina" not in runtime.agents
        # Real-bus teardown: queue + handler gone, triggers unwound.
        assert "tina" not in bus.queues
        assert "tina" not in bus.handlers
        runtime.trigger_registry.reregister_for.assert_any_call(
            "tina", [], [],
        )
        # Survivor untouched.
        assert "ellen" in bus.queues and "ellen" in bus.handlers

    async def test_surfaces_specialist_load_failures(
        self, tmp_path, monkeypatch,
    ):
        """O-2b (v0.37.9): when a specialist directory fails to load
        (e.g. missing required file), reload_agents must surface the
        failure in the returned actions list. Pre-v0.37.9 the registry
        swallowed LoadError via logger.error and reload returned
        ok=True with no trace of the failed specialist — operators
        running ``casactl reload --scope=agents`` had no way to know
        their new specialist did not land without grepping addon logs.

        Live evidence: 2026-05-14 P22 row 4 first attempt — probe22
        specialist missing response_shape.yaml + voice.yaml, registry
        logged ERROR but reload returned ok=True.
        """
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()
        specialists_dir = agents_dir / "specialists"
        specialists_dir.mkdir()
        (specialists_dir / "broken").mkdir()  # no files — load will fail

        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: MagicMock(role="ellen"))
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": MagicMock()}
        runtime.agents = {"ellen": MagicMock()}

        # Real registry behavior: load() catches per-specialist LoadError
        # and records it as a load failure. The stub mimics this contract.
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(return_value={})
        runtime.specialist_registry.load_failures = MagicMock(return_value=[
            ("broken", "missing required file(s): ['runtime.yaml']"),
        ])

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        # Action trail must include a failed:<role>:<msg> entry.
        failed_actions = [
            a for a in result["actions"] if a.startswith("failed:")
        ]
        assert failed_actions, (
            f"reload_agents must surface specialist load failures in "
            f"actions; got {result['actions']!r}"
        )
        assert any("broken" in a for a in failed_actions)
        assert any("runtime.yaml" in a for a in failed_actions)


class TestReloadAgentsSpecialistReporting:
    """S-3 (block-S live finding 2026-07-15, N150 log 07:49:56Z): the FIRST
    ``--scope=agents`` reload after boot reported ``added_specialist_finance``
    although finance had been installed and untouched since boot. Root cause:
    boot never puts specialists into ``runtime.agents`` (they are
    direct-loaded via the SpecialistRegistry), while ``reload_agents`` diffs
    "added" against ``runtime.agents`` — so every boot-loaded specialist is
    mis-reported as added (and silently re-constructed) once. The action
    list must instead reflect the REGISTRY diff: what genuinely appeared on
    / vanished from disk across this reload's re-scan."""

    def _setup_common(self, tmp_path, monkeypatch):
        from reload import register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()

        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: MagicMock(role="ellen"))
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())
        monkeypatch.setattr(
            "agent_home.provision_agent_home",
            lambda *, role, home_root, defaults_root: None,
        )
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": MagicMock()}
        runtime.agents = {"ellen": MagicMock()}
        runtime.specialist_registry.load_failures = MagicMock(return_value=[])
        return runtime, agents_dir

    async def test_boot_loaded_specialist_not_reported_added(
        self, tmp_path, monkeypatch,
    ):
        """finance was in the registry BEFORE this reload's re-scan (boot
        loaded it) and is still there after — the action list must not call
        it added, even though runtime.agents has no entry for it yet."""
        from reload import dispatch

        runtime, agents_dir = self._setup_common(tmp_path, monkeypatch)
        (agents_dir / "specialists").mkdir()
        (agents_dir / "specialists" / "finance").mkdir()

        # Registry knows finance before AND after load() — boot loaded it.
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(
            return_value={"finance": MagicMock(role="finance")})

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "added_specialist_finance" not in result["actions"], (
            "a boot-loaded, untouched specialist must not be re-reported "
            f"as added; actions={result['actions']!r}"
        )
        # The runtime.agents backfill itself stays (plugin-verify grades
        # specialists through runtime.agents) — only the REPORT was wrong.
        assert "finance" in runtime.agents

    async def test_genuinely_new_specialist_reported_added(
        self, tmp_path, monkeypatch,
    ):
        """A specialist whose directory appeared since the last scan IS
        added — the report must keep saying so."""
        from reload import dispatch

        runtime, agents_dir = self._setup_common(tmp_path, monkeypatch)
        (agents_dir / "specialists").mkdir()
        (agents_dir / "specialists" / "probe").mkdir()

        # Registry: empty before load(), knows probe after (fresh dir picked
        # up by the re-scan).
        configs: dict = {}
        runtime.specialist_registry.all_configs = MagicMock(
            side_effect=lambda: dict(configs))
        runtime.specialist_registry.load = MagicMock(
            side_effect=lambda **kw: configs.update(
                {"probe": MagicMock(role="probe")}))

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "added_specialist_probe" in result["actions"]
        assert "probe" in runtime.agents

    async def test_removed_specialist_reported_evicted_even_if_never_backfilled(
        self, tmp_path, monkeypatch,
    ):
        """A boot-loaded specialist whose directory was removed before any
        agents reload was never backfilled into runtime.agents — the
        registry re-scan drops it, and the action list must still say
        evicted."""
        from reload import dispatch

        runtime, agents_dir = self._setup_common(tmp_path, monkeypatch)
        (agents_dir / "specialists").mkdir()  # ghost's dir already gone

        configs: dict = {"ghost": MagicMock(role="ghost")}
        runtime.specialist_registry.all_configs = MagicMock(
            side_effect=lambda: dict(configs))
        runtime.specialist_registry.load = MagicMock(
            side_effect=lambda **kw: configs.clear())

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "evicted_specialist_ghost" in result["actions"], (
            f"registry-dropped specialist must be reported evicted; "
            f"actions={result['actions']!r}"
        )

    async def test_removed_backfilled_specialist_reported_evicted_exactly_once(
        self, tmp_path, monkeypatch,
    ):
        """The probe-spec live case: specialist present in runtime.agents
        (a prior reload backfilled it), dir removed → exactly ONE
        evicted_specialist action, and the runtime.agents teardown still
        happens."""
        from bus import MessageBus
        from reload import dispatch

        runtime, agents_dir = self._setup_common(tmp_path, monkeypatch)
        (agents_dir / "specialists").mkdir()

        bus = MessageBus()
        bus.register("ellen", MagicMock())
        bus.register("probe", MagicMock())
        runtime.bus = bus
        probe_agent = MagicMock()
        probe_agent.aclose = AsyncMock()
        runtime.agents["probe"] = probe_agent

        configs: dict = {"probe": MagicMock(role="probe")}
        runtime.specialist_registry.all_configs = MagicMock(
            side_effect=lambda: dict(configs))
        runtime.specialist_registry.load = MagicMock(
            side_effect=lambda **kw: configs.clear())

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        evicted = [a for a in result["actions"]
                   if a == "evicted_specialist_probe"]
        assert len(evicted) == 1, (
            f"expected exactly one eviction action; "
            f"actions={result['actions']!r}"
        )
        assert "probe" not in runtime.agents
        assert "probe" not in bus.queues


@pytest.mark.unit
class TestReloadBusLoopLifecycle:
    """H10 + H11 (v0.49.0): the resident add/remove lifecycle against a
    REAL MessageBus.

    Add half (H10): a resident added via reload must get a
    ``run_agent_loop`` consumer — pre-fix its queue was registered but
    never consumed, so every trigger firing / NOTIFICATION sat in the
    queue until the next add-on restart.

    Remove half (H11): an evicted resident's consumer must be cancelled
    and its queue/handler/triggers dropped — pre-fix the ghost kept
    consuming and executing scheduled prompts forever.
    """

    async def _drain_bus_loops(self, bus) -> None:
        """Cancel + await any consumers the real bus spawned (no leaks)."""
        tasks = list(getattr(bus, "_loop_tasks", {}).values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_message_to_reload_added_resident_is_dispatched(
        self, tmp_path, monkeypatch,
    ):
        from bus import BusMessage, MessageBus, MessageType
        import reload as reload_mod
        from reload import reload_agents

        agents_dir = tmp_path / "agents"
        (agents_dir / "newrole").mkdir(parents=True)

        received: list = []

        class _InertAgent:
            async def handle_message(self, msg):
                received.append(msg)
                return None

        monkeypatch.setattr(
            "policies.load_policies", lambda *a, **kw: MagicMock(),
        )
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda d, policies=None: MagicMock(role="newrole"),
        )
        monkeypatch.setattr(
            "agent_home.provision_agent_home", lambda **kw: None,
        )
        monkeypatch.setattr(
            reload_mod, "_construct_agent",
            lambda *, cfg, runtime: _InertAgent(),
        )

        runtime = _make_runtime()
        runtime.bus = MessageBus()  # REAL bus — the seam MagicMock hid
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.specialist_registry.all_configs = lambda: {}
        runtime.specialist_registry.load_failures = lambda: []

        try:
            actions = await reload_agents(runtime)
            assert "added_newrole" in actions

            await runtime.bus.send(BusMessage(
                type=MessageType.SCHEDULED, source="scheduler",
                target="newrole", content="cron tick",
            ))
            for _ in range(50):  # up to ~0.5s for the consumer to drain
                if received:
                    break
                await asyncio.sleep(0.01)

            assert received, (
                "SCHEDULED message to a reload-added resident was never "
                "dispatched: reload_agents registered the queue but "
                "spawned no run_agent_loop consumer (H10)"
            )
            assert received[0].content == "cron tick"
            assert runtime.bus.queues["newrole"].empty()
        finally:
            await self._drain_bus_loops(runtime.bus)

    async def test_evicted_resident_consumer_cancelled_no_ghost_turns(
        self, tmp_path, monkeypatch,
    ):
        from bus import BusMessage, MessageBus, MessageType
        from reload import reload_agents

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)
        # tina: known to the runtime, deleted from disk → evict.

        monkeypatch.setattr(
            "policies.load_policies", lambda *a, **kw: MagicMock(),
        )
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir", lambda *a, **kw: MagicMock(),
        )

        ghost_turns: list = []

        async def tina_handler(msg):
            ghost_turns.append(msg)
            return None

        runtime = _make_runtime()
        bus = MessageBus()
        runtime.bus = bus
        bus.register("ellen", MagicMock())
        bus.register("tina", tina_handler)
        tina_task = bus.start_agent_loop("tina")  # boot-style live consumer
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": MagicMock(), "tina": MagicMock()}
        ellen_agent = MagicMock()
        ellen_agent.aclose = AsyncMock()
        tina_agent = MagicMock()
        tina_agent.aclose = AsyncMock()
        runtime.agents = {"ellen": ellen_agent, "tina": tina_agent}
        runtime.specialist_registry.all_configs = lambda: {}
        runtime.specialist_registry.load_failures = lambda: []

        try:
            actions = await reload_agents(runtime)
            assert "evicted_tina" in actions

            # The consumer is cancelled AND awaited by the eviction path.
            assert tina_task.done() and tina_task.cancelled()
            assert "tina" not in bus.queues
            assert "tina" not in bus.handlers

            # A scheduler-style send after eviction is silently dropped —
            # no queueing, no ghost dispatch.
            await bus.send(BusMessage(
                type=MessageType.SCHEDULED, source="scheduler",
                target="tina", content="cron prompt",
            ))
            await asyncio.sleep(0.05)
            assert not ghost_turns, (
                "evicted resident processed a message (ghost agent)"
            )
            assert "tina" not in bus.queues

            # Trigger jobs + webhook allowlist unwound.
            runtime.trigger_registry.reregister_for.assert_any_call(
                "tina", [], [],
            )
        finally:
            await self._drain_bus_loops(bus)


class TestReloadFull:
    async def test_calls_other_handlers_in_order(self, monkeypatch):
        from reload import dispatch, register_handler, reload_full
        register_handler("full", reload_full)

        called: list[str] = []

        async def stub_policies(rt, *, role=None):
            called.append("policies"); return ["p"]

        async def stub_agents(rt, *, role=None):
            called.append("agents"); return ["a"]

        async def stub_agent(rt, *, role=None):
            called.append(f"agent:{role}"); return ["x"]

        async def stub_env(rt, *, role=None):
            called.append("plugin_env"); return ["e"]

        import reload as reload_mod
        monkeypatch.setitem(reload_mod._HANDLERS, "policies", stub_policies)
        monkeypatch.setitem(reload_mod._HANDLERS, "agents", stub_agents)
        monkeypatch.setitem(reload_mod._HANDLERS, "agent", stub_agent)
        monkeypatch.setitem(reload_mod._HANDLERS, "plugin_env", stub_env)
        monkeypatch.setitem(reload_mod._HANDLERS, "full", reload_full)

        runtime = _make_runtime()
        runtime.role_configs = {"ellen": MagicMock(), "tina": MagicMock()}
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("full", runtime=runtime, include_env=False)
        assert result["status"] == "ok"
        # Policies first, then agents, then per-role agent reload.
        assert called[0] == "policies"
        assert called[1] == "agents"
        assert "agent:ellen" in called[2:]
        assert "agent:tina" in called[2:]
        assert "plugin_env" not in called  # include_env=False

    async def test_include_env_calls_plugin_env(self, monkeypatch):
        from reload import dispatch, register_handler, reload_full
        register_handler("full", reload_full)

        called: list[str] = []
        async def stub(name):
            async def _h(rt, *, role=None):
                called.append(name); return [name]
            return _h
        import reload as reload_mod
        for s in ("policies", "agents", "agent", "plugin_env"):
            monkeypatch.setitem(reload_mod._HANDLERS, s, await stub(s))
        monkeypatch.setitem(reload_mod._HANDLERS, "full", reload_full)

        runtime = _make_runtime()
        runtime.role_configs = {}
        runtime.specialist_registry.all_configs = lambda: {}

        await dispatch("full", runtime=runtime, include_env=True)
        assert "plugin_env" in called


@pytest.mark.unit
class TestConstructAgentMemoryWiring:
    """H9 (v0.49.0): reload._construct_agent must pass the runtime's
    semantic_memory into Agent(...).

    The v0.45.0 memory retirement removed the old ``memory=`` wiring here
    and never re-added the Hindsight seam, so every reload-constructed
    resident silently fell back to NoOpSemanticMemory (long-term amnesia
    + permanently lost retains) until the next add-on restart. The bug
    was invisible because every other reload test monkeypatches
    ``_construct_agent`` — this class deliberately runs the REAL factory
    and the REAL ``agent.Agent``.
    """

    async def test_reload_constructed_agent_reuses_runtime_semantic_memory(
        self, monkeypatch,
    ):
        import agent as agent_mod
        import reload as reload_mod
        from types import SimpleNamespace
        from semantic_memory import NoOpSemanticMemory

        # Keep the test hermetic: no home-provisioning I/O, no hook
        # resolution. Everything else in Agent.__init__ runs for real.
        monkeypatch.setattr(
            "agent_home.provision_agent_home", lambda **kw: None,
        )
        monkeypatch.setattr(
            agent_mod, "resolve_hooks", lambda *a, **kw: MagicMock(),
        )

        sentinel = object()  # stands in for HindsightSemanticMemory
        runtime = _make_runtime()
        runtime.semantic_memory = sentinel
        cfg = SimpleNamespace(role="probe", hooks=None, cwd="")

        constructed = reload_mod._construct_agent(cfg=cfg, runtime=runtime)

        assert constructed._semantic_memory is sentinel, (
            "reload._construct_agent dropped semantic_memory — reloaded "
            "agents silently fall back to NoOpSemanticMemory (long-term "
            "amnesia + lost retains)"
        )
        assert not isinstance(constructed._semantic_memory, NoOpSemanticMemory)

    async def test_runtime_without_semantic_memory_falls_back_to_noop(
        self, monkeypatch,
    ):
        """Contract pin: a runtime whose ``semantic_memory`` is unset
        (the CasaRuntime default, and every MagicMock-light test
        stand-in) still constructs — Agent maps None to NoOp."""
        import agent as agent_mod
        import reload as reload_mod
        from types import SimpleNamespace
        from semantic_memory import NoOpSemanticMemory

        monkeypatch.setattr(
            "agent_home.provision_agent_home", lambda **kw: None,
        )
        monkeypatch.setattr(
            agent_mod, "resolve_hooks", lambda *a, **kw: MagicMock(),
        )

        runtime = _make_runtime()  # semantic_memory left at its default
        cfg = SimpleNamespace(role="probe", hooks=None, cwd="")

        constructed = reload_mod._construct_agent(cfg=cfg, runtime=runtime)

        assert isinstance(constructed._semantic_memory, NoOpSemanticMemory)


@pytest.mark.unit
class TestFullScopeExclusion:
    """M21 (v0.49.0): the _RWLock writer must actually exclude readers.

    Pre-fix, acquire_write recorded no lock state at all, so a
    scope='full' reload was NOT mutually exclusive with other scopes —
    a concurrent scope='agent' dispatch interleaved with reload_full's
    multi-step mutation of runtime.agents / role_configs /
    agent_registry, directly contradicting the module docstring.
    """

    async def test_full_excludes_other_scopes(self, monkeypatch):
        """A reader arriving while the writer is active must wait for
        release_write. Pre-fix ordering was
        ["full:start", "agent:start", "agent:end", "full:end"].

        Pins INV-CFG-002. Red case demonstrated: dropping _RWLock.acquire_write's writer flag fails this test.
        """
        import reload as reload_mod
        from reload import dispatch

        ordering: list[str] = []

        async def slow_full(runtime, *, role=None, include_env=False):
            ordering.append("full:start")
            await asyncio.sleep(0.05)  # yield so a reader could sneak in
            ordering.append("full:end")
            return ["full_work"]

        async def fast_agent(runtime, *, role=None):
            ordering.append("agent:start")
            ordering.append("agent:end")
            return ["agent_work"]

        monkeypatch.setitem(reload_mod._HANDLERS, "full", slow_full)
        monkeypatch.setitem(reload_mod._HANDLERS, "agent", fast_agent)
        # Fresh RW lock bound to THIS test's event loop.
        monkeypatch.setattr(reload_mod, "_GLOBAL_RW", None)
        runtime = _make_runtime()

        async def run_agent_later():
            await asyncio.sleep(0.01)  # start after full holds the write lock
            return await dispatch("agent", runtime=runtime, role="x")

        results = await asyncio.gather(
            dispatch("full", runtime=runtime),
            run_agent_later(),
        )
        assert all(r["status"] == "ok" for r in results), results
        assert ordering == [
            "full:start", "full:end", "agent:start", "agent:end",
        ], (
            f"scope='full' did not exclude scope='agent': {ordering!r} — "
            "the reader ran inside the writer's critical section"
        )

    async def test_reader_in_flight_blocks_writer(self, monkeypatch):
        """The opposite direction already worked pre-fix (writer waits
        for _readers == 0) and must not regress."""
        import reload as reload_mod
        from reload import dispatch

        ordering: list[str] = []

        async def slow_agent(runtime, *, role=None):
            ordering.append("agent:start")
            await asyncio.sleep(0.05)
            ordering.append("agent:end")
            return ["agent_work"]

        async def fast_full(runtime, *, role=None, include_env=False):
            ordering.append("full:start")
            ordering.append("full:end")
            return ["full_work"]

        monkeypatch.setitem(reload_mod._HANDLERS, "agent", slow_agent)
        monkeypatch.setitem(reload_mod._HANDLERS, "full", fast_full)
        monkeypatch.setattr(reload_mod, "_GLOBAL_RW", None)
        runtime = _make_runtime()

        async def run_full_later():
            await asyncio.sleep(0.01)  # start after agent holds a read lock
            return await dispatch("full", runtime=runtime)

        results = await asyncio.gather(
            dispatch("agent", runtime=runtime, role="x"),
            run_full_later(),
        )
        assert all(r["status"] == "ok" for r in results), results
        assert ordering == [
            "agent:start", "agent:end", "full:start", "full:end",
        ]


class TestRWLockFifoAdmission:
    """#311: `_RWLock` had no writer preference — `acquire_read` only blocked
    on an ACTIVE writer, so a steady stream of shared-scope reloads (readers)
    starved a pending `full` reload (writer) indefinitely. The fix is a FIFO
    wait-queue: any queued waiter bars new entrants, consecutive readers are
    admitted in batches, and no arrival order starves anyone forever.

    These tests exercise the lock class directly for deterministic
    interleaving control; TestFullScopeExclusion keeps pinning the
    dispatch-level exclusion behaviour (INV-CFG-002).
    """

    async def test_waiting_writer_blocks_new_readers(self):
        """THE starvation fix: a reader arriving while a writer WAITS must
        queue behind it, not overtake it. Pre-fix ordering was r2 before w."""
        import reload as reload_mod

        rw = reload_mod._RWLock()
        order: list[str] = []

        await rw.acquire_read()  # R1 active

        async def writer():
            await rw.acquire_write()
            order.append("w")
            await rw.release_write()

        async def reader2():
            await rw.acquire_read()
            order.append("r2")
            await rw.release_read()

        w_task = asyncio.ensure_future(writer())
        for _ in range(3):
            await asyncio.sleep(0)  # let the writer queue behind R1
        r2_task = asyncio.ensure_future(reader2())
        for _ in range(3):
            await asyncio.sleep(0)
        assert order == [], (
            "nobody may be admitted while R1 holds the read lock and a "
            f"writer waits: {order!r}"
        )
        await rw.release_read()  # R1 done — writer first, THEN r2
        await asyncio.gather(w_task, r2_task)
        assert order == ["w", "r2"], order

    async def test_queued_reader_admitted_before_later_writer(self):
        """Phase fairness (design r1 finding): a queued reader must not be
        overtaken by a writer that queued AFTER it — an uninterrupted writer
        stream cannot starve readers."""
        import reload as reload_mod

        rw = reload_mod._RWLock()
        order: list[str] = []

        await rw.acquire_write()  # W1 active

        async def reader():
            await rw.acquire_read()
            order.append("r")
            await rw.release_read()

        async def writer2():
            await rw.acquire_write()
            order.append("w2")
            await rw.release_write()

        r_task = asyncio.ensure_future(reader())
        for _ in range(3):
            await asyncio.sleep(0)  # reader queues first
        w2_task = asyncio.ensure_future(writer2())
        for _ in range(3):
            await asyncio.sleep(0)  # writer2 queues behind it
        await rw.release_write()
        await asyncio.gather(r_task, w2_task)
        assert order == ["r", "w2"], order

    async def test_consecutive_queued_readers_admitted_together(self):
        """A reader batch at the head of the queue is admitted concurrently
        (readers share), not one per release cycle."""
        import reload as reload_mod

        rw = reload_mod._RWLock()
        holding = 0
        peak = 0
        release = asyncio.Event()

        await rw.acquire_write()

        async def reader():
            nonlocal holding, peak
            await rw.acquire_read()
            holding += 1
            peak = max(peak, holding)
            await release.wait()
            holding -= 1
            await rw.release_read()

        tasks = [asyncio.ensure_future(reader()) for _ in range(3)]
        for _ in range(3):
            await asyncio.sleep(0)
        await rw.release_write()
        for _ in range(5):
            await asyncio.sleep(0)
        assert peak == 3, f"queued readers were not admitted as a batch: {peak}"
        release.set()
        await asyncio.gather(*tasks)

    async def test_cancelled_waiting_writer_unblocks_queued_reader(self):
        """Cancelling a QUEUED (not yet admitted) writer must remove it from
        the queue so the reader behind it is admitted — while another reader
        still holds the lock (readers share)."""
        import reload as reload_mod

        rw = reload_mod._RWLock()
        admitted = asyncio.Event()

        await rw.acquire_read()  # R1 active, held throughout

        async def writer():
            await rw.acquire_write()  # queues; never admitted

        async def reader2():
            await rw.acquire_read()
            admitted.set()
            await rw.release_read()

        w_task = asyncio.ensure_future(writer())
        for _ in range(3):
            await asyncio.sleep(0)
        r2_task = asyncio.ensure_future(reader2())
        for _ in range(3):
            await asyncio.sleep(0)
        assert not admitted.is_set()  # r2 correctly queued behind the writer
        w_task.cancel()
        try:
            await w_task
        except asyncio.CancelledError:
            pass
        await asyncio.wait_for(asyncio.wait_for(r2_task, 1), 1)
        assert admitted.is_set(), (
            "reader stayed blocked behind a cancelled writer"
        )
        await rw.release_read()

    async def test_writer_cancelled_at_admission_rolls_back(self):
        """The admitted-but-cancelled race: release_read admits the writer
        synchronously, then the writer task is cancelled before it resumes.
        The cancel arm must release the write lock it already owns, or every
        later acquire hangs forever."""
        import reload as reload_mod

        rw = reload_mod._RWLock()

        await rw.acquire_read()

        async def writer():
            await rw.acquire_write()

        w_task = asyncio.ensure_future(writer())
        for _ in range(3):
            await asyncio.sleep(0)  # writer is queued
        await rw.release_read()  # synchronous admission: writer now OWNS it
        w_task.cancel()  # cancellation delivered before the task resumes
        try:
            await w_task
        except asyncio.CancelledError:
            pass
        # The lock must be free again — a reader acquires without hanging.
        await asyncio.wait_for(rw.acquire_read(), 1)
        await rw.release_read()


class TestExecutorsScope:
    """A-1: 7th reload scope for ExecutorRegistry."""

    async def test_executors_scope_calls_registry_load(self):
        from reload import dispatch
        runtime = _make_runtime()
        # Drive the registry mock to assert load() was awaited.
        runtime.executor_registry.load = MagicMock()
        result = await dispatch("executors", runtime=runtime)
        assert result["status"] == "ok"
        assert result["scope"] == "executors"
        # O-2a (v0.37.9): rebuild_executor_registry is first; per-resident
        # reload_agent fan-out follows so cached system-prompt state
        # regenerates. role_configs is empty in this fixture, so the only
        # action remains the registry rebuild.
        assert result["actions"] == ["rebuild_executor_registry"]
        runtime.executor_registry.load.assert_called_once()

    async def test_executors_scope_fans_out_to_residents(self, monkeypatch):
        """O-2a (v0.37.9): after rebuilding the executor registry,
        reload_executors must call ``reload_agent`` for each resident so
        the resident's cached ``<executors>`` system-prompt block (built
        from a snapshot at construct_agent time) regenerates. Without
        this, an operator who flips ``enabled: true`` on a previously-
        disabled executor and runs ``casactl reload --scope=executors``
        sees the registry rebuild but Ellen's prompt block stays stale
        until the next ``--scope=agent`` fires.

        Live evidence: 2026-05-14 P22 row5b — Ellen replied "No" to
        "is plugin-developer enabled?" between an executor scope reload
        and the next agent-scope reload, contradicting the live
        registry state.
        """
        from reload import dispatch
        import reload as reload_mod

        # Stub the executors handler so it just records and returns the
        # registry-rebuild action; we want to assert the fan-out shape
        # at the dispatcher level, not re-run executor_registry.load.
        runtime = _make_runtime()
        runtime.role_configs = {"assistant": MagicMock(), "butler": MagicMock()}

        fan_out_calls: list[str] = []

        async def fake_agent(rt, role=None):
            fan_out_calls.append(role)
            return ["load_config", "construct_agent", "reregister_bus"]

        monkeypatch.setitem(reload_mod._HANDLERS, "agent", fake_agent)
        runtime.executor_registry.load = MagicMock()

        result = await dispatch("executors", runtime=runtime)

        assert result["status"] == "ok"
        # Each resident must have been refreshed.
        assert sorted(fan_out_calls) == ["assistant", "butler"]
        # Action trail surfaces the per-resident sub-actions with prefix.
        assert "rebuild_executor_registry" in result["actions"]
        for role in ("assistant", "butler"):
            for sub in ("load_config", "construct_agent", "reregister_bus"):
                assert f"agent:{role}:{sub}" in result["actions"], (
                    f"expected agent:{role}:{sub} in actions, got "
                    f"{result['actions']!r}"
                )

    async def test_executors_load_raises_becomes_load_error(self):
        from reload import dispatch
        runtime = _make_runtime()
        runtime.executor_registry.load = MagicMock(
            side_effect=RuntimeError("synthetic")
        )
        result = await dispatch("executors", runtime=runtime)
        assert result["status"] == "error"
        assert result["kind"] == "load_error"
        assert "synthetic" in result["message"]

    async def test_full_scope_includes_executors_rebuild(self, monkeypatch):
        """reload_full chains executors BEFORE per-role agent reload."""
        from reload import dispatch
        import reload as reload_mod

        # Capture handler invocation order.
        order: list[str] = []

        async def fake_policies(runtime, role=None):
            order.append("policies")
            return ["pol"]

        async def fake_agents(runtime, role=None):
            order.append("agents")
            return ["ag"]

        async def fake_executors(runtime, role=None):
            order.append("executors")
            return ["rebuild_executor_registry"]

        async def fake_agent(runtime, role=None):
            order.append(f"agent:{role}")
            return ["a_load"]

        monkeypatch.setitem(reload_mod._HANDLERS, "policies", fake_policies)
        monkeypatch.setitem(reload_mod._HANDLERS, "agents", fake_agents)
        monkeypatch.setitem(reload_mod._HANDLERS, "executors", fake_executors)
        monkeypatch.setitem(reload_mod._HANDLERS, "agent", fake_agent)

        runtime = _make_runtime()
        runtime.role_configs = {"assistant": MagicMock()}
        runtime.specialist_registry.all_configs = MagicMock(return_value={})

        result = await dispatch("full", runtime=runtime)
        assert result["status"] == "ok"
        # executors must precede per-role agent reload.
        executors_idx = order.index("executors")
        agent_idx = order.index("agent:assistant")
        assert executors_idx < agent_idx
        assert any(
            a == "executors:rebuild_executor_registry"
            for a in result["actions"]
        )


class TestReloadRefreshesDelegationRoleMap:
    """P-6 (live run 2026-07-11): ``tools._agent_role_map`` is populated once
    at boot and no reload handler refreshed it, so ``delegate_to_agent``
    resolved BOOT-TIME AgentConfigs forever — a specialist ``tools.allowed``
    grant (lesina-invoice) stayed inert for fresh delegations until a full
    add-on restart, while ``casa_reload`` reported ok=True."""

    async def test_agent_scope_specialist_reload_refreshes_role_map(
        self, tmp_path, monkeypatch,
    ):
        from types import SimpleNamespace
        import tools as tools_mod
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "specialists" / "finance").mkdir(parents=True)

        new_cfg = SimpleNamespace(
            role="finance",
            character=SimpleNamespace(name="Alex", card=""),
            triggers=[], channels=[],
        )
        new_agent = MagicMock()
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir", lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies", lambda *a, **kw: MagicMock(),
        )
        import reload as reload_mod
        monkeypatch.setattr(
            reload_mod, "_construct_agent", lambda *a, **kw: new_agent,
        )

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(
            return_value={"finance": new_cfg},
        )

        # Boot-time wiring left the OLD config in the delegation map.
        old_cfg = SimpleNamespace(role="finance")
        monkeypatch.setattr(tools_mod, "_agent_role_map", {"finance": old_cfg})

        result = await dispatch("agent", runtime=runtime, role="finance")
        assert result["status"] == "ok"
        # The delegation-resolution map must now hold the POST-reload config.
        assert tools_mod._agent_role_map["finance"] is new_cfg
        assert "refresh_role_map" in result["actions"]

    async def test_agents_scope_sweep_refreshes_role_map(
        self, tmp_path, monkeypatch,
    ):
        from types import SimpleNamespace
        import tools as tools_mod
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)
        monkeypatch.setattr(
            "policies.load_policies", lambda *a, **kw: MagicMock(),
        )

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        resident_cfg = SimpleNamespace(
            role="ellen",
            character=SimpleNamespace(name="Ellen", card=""),
            triggers=[], channels=["telegram"],
        )
        runtime.role_configs["ellen"] = resident_cfg
        spec_cfg = SimpleNamespace(
            role="finance",
            character=SimpleNamespace(name="Alex", card=""),
            triggers=[], channels=[],
        )
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.load_failures = MagicMock(return_value=[])
        runtime.specialist_registry.all_configs = MagicMock(
            return_value={"finance": spec_cfg},
        )

        monkeypatch.setattr(tools_mod, "_agent_role_map", {})

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert tools_mod._agent_role_map == {
            "ellen": resident_cfg, "finance": spec_cfg,
        }


class TestReloadFullSnapshotOrdering:
    async def test_snapshot_refreshed_before_reconstruction(self, monkeypatch):
        """§3.9 manual-edit seam: reload_full must reload the plugin resolver
        snapshot BEFORE any agent-reconstructing handler runs (a stale snapshot
        would let desired==active verification false-pass)."""
        import reload as reload_mod
        import plugin_registry
        order: list[str] = []
        monkeypatch.setattr(plugin_registry, "reload_snapshot",
                            lambda: order.append("snapshot"))

        def _rec(name):
            async def h(runtime, role=None, **kw):
                order.append(name)
                return []
            return h

        for name in ("policies", "agents", "executors", "agent", "plugin_env"):
            monkeypatch.setitem(reload_mod._HANDLERS, name, _rec(name))

        await reload_mod.reload_full(_make_runtime())
        assert order[0] == "snapshot"
        assert order.index("snapshot") < order.index("agents")
        assert order.index("snapshot") < order.index("executors")


class TestDisabledSpecialistReload:
    """v0.74.1 (Sol B1, live proxy-drive finding): reload of a DISABLED
    specialist tears it down instead of constructing + registering it — a
    registered handler stays reachable via /invoke and would execute with an
    empty wrong-tier plugin binding."""

    async def test_disabled_specialist_torn_down_not_registered(
            self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "specialists" / "finance").mkdir(parents=True)

        new_cfg = SimpleNamespace(enabled=False, role="finance",
                                  triggers=[], channels=[])
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {}
        stale_instance = MagicMock()
        stale_instance.aclose = AsyncMock()      # real Agent.aclose is async
        stale_instance.active_plugin_binding = {}
        runtime.agents = {"finance": stale_instance}
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(return_value={})
        runtime.trigger_registry.reregister_for = MagicMock()

        result = await dispatch("agent", runtime=runtime, role="finance")

        assert result["status"] == "ok"
        assert "teardown_disabled_specialist" in result["actions"]
        assert "construct_agent" not in result["actions"]
        assert "finance" not in runtime.agents            # instance gone
        # #343(b): teardown goes through the awaiting variant, which also
        # cancels the role's in-flight dispatch tasks (real-method
        # existence is pinned by test_bus.py's unregister_and_wait tests).
        runtime.bus.unregister_and_wait.assert_called_with("finance")
        runtime.bus.register.assert_not_called()          # never re-registered
        # Triggers unwound (the _teardown_role path).
        runtime.trigger_registry.reregister_for.assert_called_with(
            "finance", [], [])

    async def test_enabled_specialist_still_constructed(
            self, tmp_path, monkeypatch):
        """The teardown gate must not touch ENABLED specialists."""
        from types import SimpleNamespace
        from reload import dispatch, register_handler, reload_agent
        import reload as reload_mod
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "specialists" / "finance").mkdir(parents=True)

        new_cfg = SimpleNamespace(enabled=True, role="finance",
                                  triggers=[], channels=[])
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())
        built = MagicMock()
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda *, cfg, runtime, **kw: built)
        from agent_registry import AgentRegistry
        monkeypatch.setattr(AgentRegistry, "build",
                            classmethod(lambda cls, **kw: MagicMock()))

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {}
        runtime.agents = {}
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(return_value={})
        runtime.trigger_registry.reregister_for = MagicMock()

        result = await dispatch("agent", runtime=runtime, role="finance")
        assert result["status"] == "ok"
        assert "construct_agent" in result["actions"]
        assert runtime.agents["finance"] is built


# --- A:§3.3/§3.4 lifecycle invalidation ordering (v0.76.0, r1-B8/r2-B5) ------


def _patch_authz(monkeypatch):
    """Spy on the process-wide GRANTS/CHALLENGES singletons so each
    enumerated reload seam's purge/cancel calls can be observed in order,
    without touching their real (side-effect-free-for-empty-state) logic."""
    import authz_grants
    calls: list[tuple] = []
    monkeypatch.setattr(authz_grants.GRANTS, "purge_role",
                        lambda role: calls.append(("purge_role", role)) or 0)
    monkeypatch.setattr(
        authz_grants.CHALLENGES, "cancel_matching",
        lambda **kw: calls.append(("cancel_matching", kw.get("role"))) or 0)
    return calls


class TestGrantInvalidationSeams:
    """Each of the enumerated seams (A:§3.7 r2-B5) purges grants + cancels
    challenges by NORMALIZED role BEFORE the replacement/removed agent
    becomes dispatchable."""

    async def test_reload_agent_normal_swap_invalidates_before_new_agent_live(
            self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agent
        from types import SimpleNamespace
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)
        new_cfg = SimpleNamespace(role="ellen",
                                  character=SimpleNamespace(name="E2", card=""),
                                  triggers=[], channels=[])
        new_agent = MagicMock()
        new_agent.handle_message = MagicMock()
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda *a, **kw: new_agent)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs["ellen"] = SimpleNamespace(
            role="ellen", character=SimpleNamespace(name="Ellen", card=""))
        old_agent = MagicMock()
        old_agent.aclose = AsyncMock()
        runtime.agents["ellen"] = old_agent

        calls = _patch_authz(monkeypatch)
        # Assert BEFORE-dispatchable ordering at call time: the swap must
        # not yet have happened when invalidation runs.
        original = runtime.agents.get
        seen_before_swap = {}

        def spy_purge(role):
            seen_before_swap["ellen_is_old"] = runtime.agents.get("ellen") is old_agent
            calls.append(("purge_role", role))
            return 0
        monkeypatch.setattr(__import__("authz_grants").GRANTS, "purge_role", spy_purge)

        result = await dispatch("agent", runtime=runtime, role="ellen")
        assert result["status"] == "ok"
        assert runtime.agents["ellen"] is new_agent
        assert ("purge_role", "ellen") in calls
        assert seen_before_swap["ellen_is_old"] is True

    async def test_disabled_specialist_teardown_invalidates_before_pop(
            self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "specialists" / "finance").mkdir(parents=True)
        new_cfg = SimpleNamespace(enabled=False, role="finance",
                                  triggers=[], channels=[])
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {}
        stale_instance = MagicMock()
        stale_instance.aclose = AsyncMock()
        stale_instance.active_plugin_binding = {}
        runtime.agents = {"finance": stale_instance}
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(return_value={})
        runtime.trigger_registry.reregister_for = MagicMock()

        calls = _patch_authz(monkeypatch)
        seen = {}

        def spy_purge(role):
            seen["still_present"] = "finance" in runtime.agents
            calls.append(("purge_role", role))
            return 0
        monkeypatch.setattr(__import__("authz_grants").GRANTS, "purge_role", spy_purge)

        result = await dispatch("agent", runtime=runtime, role="finance")
        assert result["status"] == "ok"
        assert "finance" not in runtime.agents
        assert ("purge_role", "finance") in calls
        assert seen["still_present"] is True

    async def test_reload_role_after_policies_invalidates_before_swap(
            self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from reload import _reload_role_after_policies
        import reload as reload_mod

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)
        new_cfg = SimpleNamespace(role="ellen",
                                  character=SimpleNamespace(name="E2", card=""))
        new_agent = MagicMock()
        new_agent.handle_message = MagicMock()
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda *a, **kw: new_agent)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs["ellen"] = SimpleNamespace(role="ellen")
        old_agent = MagicMock()
        old_agent.aclose = AsyncMock()
        runtime.agents["ellen"] = old_agent

        calls = _patch_authz(monkeypatch)
        seen = {}

        def spy_purge(role):
            seen["ellen_is_old"] = runtime.agents.get("ellen") is old_agent
            calls.append(("purge_role", role))
            return 0
        monkeypatch.setattr(__import__("authz_grants").GRANTS, "purge_role", spy_purge)

        await _reload_role_after_policies(runtime, "ellen")
        assert runtime.agents["ellen"] is new_agent
        assert ("purge_role", "ellen") in calls
        assert seen["ellen_is_old"] is True

    async def test_reload_agents_resident_add_invalidates_before_dispatchable(
            self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agents
        from types import SimpleNamespace
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()
        (agents_dir / "newcomer").mkdir()
        new_cfg = SimpleNamespace(
            role="newcomer", character=SimpleNamespace(name="N", card=""),
            triggers=[], channels=[],
            memory=SimpleNamespace(read_strategy="per_turn"))
        new_agent = MagicMock()

        def fake_load(d, **kw):
            return new_cfg if "newcomer" in d else MagicMock(role="ellen")
        monkeypatch.setattr("agent_loader.load_agent_from_dir", fake_load)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
        monkeypatch.setattr("agent_home.provision_agent_home",
                            lambda *, role, home_root, defaults_root: None)
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda **kw: new_agent)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": MagicMock()}
        runtime.specialist_registry.all_configs = lambda: {}

        calls = _patch_authz(monkeypatch)
        seen = {}

        def spy_purge(role):
            if role == "newcomer":
                seen["not_dispatchable_yet"] = "newcomer" not in runtime.agents
            calls.append(("purge_role", role))
            return 0
        monkeypatch.setattr(__import__("authz_grants").GRANTS, "purge_role", spy_purge)

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert runtime.agents["newcomer"] is new_agent
        assert ("purge_role", "newcomer") in calls
        assert seen["not_dispatchable_yet"] is True

    async def test_reload_agents_resident_evict_invalidates_before_teardown(
            self, tmp_path, monkeypatch):
        from bus import MessageBus
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()   # tina absent on disk -> evict

        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: MagicMock())
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        bus = MessageBus()
        bus.register("ellen", MagicMock())
        bus.register("tina", MagicMock())
        runtime.bus = bus
        runtime.role_configs = {"ellen": MagicMock(), "tina": MagicMock()}
        tina_agent = MagicMock()
        tina_agent.aclose = AsyncMock()
        runtime.agents = {"ellen": MagicMock(), "tina": tina_agent}
        runtime.specialist_registry.all_configs = lambda: {}

        calls = _patch_authz(monkeypatch)
        seen = {}

        def spy_purge(role):
            if role == "tina":
                seen["still_present"] = "tina" in runtime.agents
            calls.append(("purge_role", role))
            return 0
        monkeypatch.setattr(__import__("authz_grants").GRANTS, "purge_role", spy_purge)

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "tina" not in runtime.agents
        assert ("purge_role", "tina") in calls
        assert seen["still_present"] is True

    async def test_reload_agents_specialist_add_invalidates_before_dispatchable(
            self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()
        (agents_dir / "specialists" / "finance").mkdir(parents=True)

        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: MagicMock(role="ellen"))
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())
        monkeypatch.setattr("agent_home.provision_agent_home",
                            lambda *, role, home_root, defaults_root: None)
        new_agent = MagicMock()
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda **kw: new_agent)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": MagicMock()}
        runtime.agents = {"ellen": MagicMock()}
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(
            return_value={"finance": MagicMock()})
        runtime.specialist_registry.load_failures = MagicMock(return_value=[])

        calls = _patch_authz(monkeypatch)
        seen = {}

        def spy_purge(role):
            if role == "finance":
                seen["not_dispatchable_yet"] = "finance" not in runtime.agents
            calls.append(("purge_role", role))
            return 0
        monkeypatch.setattr(__import__("authz_grants").GRANTS, "purge_role", spy_purge)

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert runtime.agents["finance"] is new_agent
        assert ("purge_role", "finance") in calls
        assert seen["not_dispatchable_yet"] is True

    async def test_reload_agents_specialist_evict_invalidates_before_teardown(
            self, tmp_path, monkeypatch):
        from bus import MessageBus
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()
        # No specialists/ dir on disk -> "finance" (previously live) evicted.

        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: MagicMock(role="ellen"))
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        bus = MessageBus()
        bus.register("ellen", MagicMock())
        bus.register("finance", MagicMock())
        runtime.bus = bus
        runtime.role_configs = {"ellen": MagicMock()}
        finance_agent = MagicMock()
        finance_agent.aclose = AsyncMock()
        runtime.agents = {"ellen": MagicMock(), "finance": finance_agent}
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(return_value={})
        runtime.specialist_registry.load_failures = MagicMock(return_value=[])

        calls = _patch_authz(monkeypatch)
        seen = {}

        def spy_purge(role):
            if role == "finance":
                seen["still_present"] = "finance" in runtime.agents
            calls.append(("purge_role", role))
            return 0
        monkeypatch.setattr(__import__("authz_grants").GRANTS, "purge_role", spy_purge)

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "finance" not in runtime.agents
        assert ("purge_role", "finance") in calls
        assert seen["still_present"] is True


# ===========================================================================
# W2: a reload swaps the resident's config, so the AuthzDeps factory — which
# reads cfg.character.name LAZILY at call time — surfaces the NEW display name
# on the next challenge (never a boot-time snapshot).
# ===========================================================================


class TestReloadUpdatesDisplayName:
    async def test_post_reload_challenge_shows_the_new_name(self, tmp_path):
        from types import SimpleNamespace
        from plugin_registry import reload_snapshot
        from plugin_fixtures import entry, mk_artifact, mk_registry
        from test_agent_plugin_binding import _make_agent
        from authz_grants import render_challenge_message

        def _authz_hook(opts):
            for m in (opts.hooks or {}).get("PreToolUse", []):
                for h in getattr(m, "hooks", []):
                    if getattr(h, "_casa_authz_role", None) is not None:
                        return h
            return None

        store = tmp_path / "store"
        e = entry("p", ["resident:assistant"])
        mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}},
                    extra_manifest={"casa": {"protectedTools": ["invoice_reset"]}})
        reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                        store_root=store)
        a = _make_agent(tmp_path, role="assistant")
        a._channel_manager.register(SimpleNamespace(name="telegram"))

        async def build():
            return await a._build_options(
                channel="telegram", channel_key="k", is_fresh=True,
                resume_sid=None, user_text="hi")

        a.config.character.name = "Ellen"
        deps1 = _authz_hook(await build())._casa_authz_deps_factory()
        assert deps1.display_name == "Ellen"

        # Simulate what reload does: the live config's name changes; the NEXT
        # options build (a fresh turn) reads it lazily.
        a.config.character.name = "Ellen-2"
        deps2 = _authz_hook(await build())._casa_authz_deps_factory()
        assert deps2.display_name == "Ellen-2"

        # A post-reload challenge names the new display name.
        text = render_challenge_message(
            tool_name="invoice_reset", enforcement_role="assistant",
            canonical_json="{}", display_name=deps2.display_name)
        assert "Ellen-2 (assistant)" in text


class TestRestartToSwapGuardCascades:
    """Personality Phase A review (whole-branch): a resident whose personality
    identity (role_checksum OR binding_digest) moved is restart-to-swap — the
    policy cascade (scope=policies) and the bulk sweep (scope=agents) must NOT
    hot-swap it onto the new binding. The single-role reload_agent path already
    guards this by raising restart_required; the cascades instead SKIP the one
    role and keep the loop going for every other role."""

    @staticmethod
    def _resident(role, *, checksum, digest, marker):
        from types import SimpleNamespace
        return SimpleNamespace(
            role=role, role_checksum=checksum, binding_digest=digest,
            marker=marker, character=SimpleNamespace(name=role, card=""),
            triggers=[], channels=[],
        )

    async def test_reload_policies_defers_identity_changed_resident(
            self, tmp_path, monkeypatch, caplog):
        """A STAGED persona swap (binding_digest moved) on ``gary`` must NOT be
        hot-swapped by scope=policies; ``tina`` (identity unchanged) still
        reloads to pick up the new policy_lib."""
        import logging
        from reload import dispatch, register_handler, reload_policies
        register_handler("policies", reload_policies)

        agents_dir = tmp_path / "agents"
        (agents_dir / "gary").mkdir(parents=True)
        (agents_dir / "tina").mkdir()

        # On-disk (post-reconcile) cfgs: gary's binding_digest moved OLD->NEW
        # (a staged swap the load committed on disk); tina's is unchanged.
        disk = {
            "gary": self._resident("gary", checksum="RC_G",
                                    digest="NEW", marker="reloaded"),
            "tina": self._resident("tina", checksum="RC_T",
                                    digest="T_OLD", marker="reloaded"),
        }
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda d, **kw: disk["gary"] if "gary" in d else disk["tina"])
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        import reload as reload_mod

        def fake_construct(*, cfg, runtime):
            a = MagicMock()
            a.handle_message = MagicMock()
            a._built_for = cfg
            return a
        monkeypatch.setattr(reload_mod, "_construct_agent", fake_construct)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {
            "gary": self._resident("gary", checksum="RC_G",
                                   digest="OLD", marker="live"),
            "tina": self._resident("tina", checksum="RC_T",
                                   digest="T_OLD", marker="live"),
        }
        old_gary = MagicMock()
        old_gary.aclose = AsyncMock()
        old_tina = MagicMock()
        old_tina.aclose = AsyncMock()
        runtime.agents = {"gary": old_gary, "tina": old_tina}
        runtime.specialist_registry.all_configs = lambda: {}

        with caplog.at_level(logging.WARNING, logger="reload"):
            result = await dispatch("policies", runtime=runtime)
        assert result["status"] == "ok"

        # gary: identity changed -> NOT hot-swapped. Live cfg + agent untouched.
        assert runtime.role_configs["gary"].marker == "live"
        assert runtime.role_configs["gary"].binding_digest == "OLD"
        assert runtime.agents["gary"] is old_gary
        # tina: unchanged identity -> reloaded normally for the new policy_lib.
        assert runtime.role_configs["tina"].marker == "reloaded"
        assert runtime.agents["tina"] is not old_tina
        assert runtime.agents["tina"]._built_for is disk["tina"]
        # A restart-required warning surfaced for gary.
        assert "gary" in caplog.text
        assert "restart" in caplog.text.lower()

    async def test_reload_policies_non_identity_change_still_reloads(
            self, tmp_path, monkeypatch):
        """The guard must NOT block a legitimate policy-only propagation: same
        role_checksum + binding_digest -> the resident still reloads."""
        from reload import dispatch, register_handler, reload_policies
        register_handler("policies", reload_policies)

        agents_dir = tmp_path / "agents"
        (agents_dir / "gary").mkdir(parents=True)

        reloaded_cfg = self._resident("gary", checksum="RC_G",
                                      digest="SAME", marker="reloaded")
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda d, **kw: reloaded_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        import reload as reload_mod

        def fake_construct(*, cfg, runtime):
            a = MagicMock()
            a.handle_message = MagicMock()
            a._built_for = cfg
            return a
        monkeypatch.setattr(reload_mod, "_construct_agent", fake_construct)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {
            "gary": self._resident("gary", checksum="RC_G",
                                   digest="SAME", marker="live"),
        }
        old_gary = MagicMock()
        old_gary.aclose = AsyncMock()
        runtime.agents = {"gary": old_gary}
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("policies", runtime=runtime)
        assert result["status"] == "ok"
        # Identity unchanged -> the policy-only reload propagated normally.
        assert runtime.role_configs["gary"].marker == "reloaded"
        assert runtime.agents["gary"]._built_for is reloaded_cfg

    async def test_reload_agents_defers_identity_changed_resident(
            self, tmp_path, monkeypatch, caplog):
        """scope=agents must never activate a staged personality-identity change
        on a live resident: the live agent + cfg stay on the OLD binding, and the
        sweep still processes the other roles."""
        import logging
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "gary").mkdir()
        (agents_dir / "newcomer").mkdir()   # genuinely-new resident -> add

        # gary is live; its on-disk load reflects a staged swap (binding moved).
        # newcomer is a brand-new resident (no live identity) -> must still add.
        newcomer_cfg = self._resident("newcomer", checksum="RC_N",
                                      digest="N", marker="new")
        gary_disk = self._resident("gary", checksum="RC_G",
                                   digest="NEW", marker="reloaded")

        def fake_load(d, **kw):
            if "newcomer" in d:
                return newcomer_cfg
            return gary_disk
        monkeypatch.setattr("agent_loader.load_agent_from_dir", fake_load)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
        monkeypatch.setattr("agent_home.provision_agent_home",
                            lambda *, role, home_root, defaults_root: None)

        import reload as reload_mod

        def fake_construct(*, cfg, runtime):
            a = MagicMock()
            a.handle_message = MagicMock()
            a._built_for = cfg
            return a
        monkeypatch.setattr(reload_mod, "_construct_agent", fake_construct)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {
            "gary": self._resident("gary", checksum="RC_G",
                                   digest="OLD", marker="live"),
        }
        old_gary = MagicMock()
        old_gary.aclose = AsyncMock()
        runtime.agents = {"gary": old_gary}
        runtime.specialist_registry.all_configs = lambda: {}

        with caplog.at_level(logging.WARNING, logger="reload"):
            result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"

        # gary: staged identity change -> NOT hot-swapped.
        assert runtime.role_configs["gary"].marker == "live"
        assert runtime.role_configs["gary"].binding_digest == "OLD"
        assert runtime.agents["gary"] is old_gary
        # newcomer: no live identity -> added normally (loop continued).
        assert "newcomer" in runtime.role_configs
        assert runtime.agents["newcomer"]._built_for is newcomer_cfg

    async def test_reload_triggers_two_step_laundering_is_blocked(
            self, tmp_path, monkeypatch, caplog):
        """Task 14 (round-3 contract): a trigger-reload on a resident whose
        on-disk personality identity moved REFUSES the whole operation rather
        than half-applying it. Step 1: scope=triggers RAISES restart_required
        (structured error via dispatch); NOTHING mutates — the trigger registry
        is never reregistered, role_configs still holds the OLD digest, and the
        live agent is unchanged. Step 2 (laundering proof): a subsequent
        scope=agent STILL fires restart_required, because the baseline was never
        poisoned NEW-vs-NEW.

        This reworks the round-2 test (which asserted the OLD design: reregister
        NEW triggers but keep the OLD cache). Two reviewers flagged that as a
        half-applied, mixed-state design; refusing outright kills P1 (stale
        cached channels authorizing webhook ingress) and P2 (misreported
        registered list) at the root. Intended semantics change, not an
        assertion weakening."""
        import logging
        from types import SimpleNamespace
        from reload import (dispatch, register_handler, reload_agent,
                            reload_triggers)
        register_handler("triggers", reload_triggers)
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "gary").mkdir(parents=True)

        # On-disk (post-reconcile) cfg: gary's binding_digest moved OLD->NEW (a
        # staged swap load_agent_from_dir committed desired->active on disk), and
        # it carries a NEW trigger the reregister path must still install.
        disk_cfg = SimpleNamespace(
            role="gary", role_checksum="RC_G", binding_digest="NEW",
            marker="reloaded",
            character=SimpleNamespace(name="gary", card=""),
            triggers=[SimpleNamespace(name="probe-new")],
            channels=["telegram"],
        )
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda d, **kw: disk_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        import reload as reload_mod

        def fake_construct(*, cfg, runtime):  # must never be reached for gary
            a = MagicMock()
            a.handle_message = MagicMock()
            a._built_for = cfg
            return a
        monkeypatch.setattr(reload_mod, "_construct_agent", fake_construct)

        live_cfg = SimpleNamespace(
            role="gary", role_checksum="RC_G", binding_digest="OLD",
            marker="live",
            character=SimpleNamespace(name="gary", card=""),
            triggers=[SimpleNamespace(name="boot-trigger")],
            channels=["telegram"],
        )
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"gary": live_cfg}
        old_gary = MagicMock()
        old_gary.aclose = AsyncMock()
        runtime.agents = {"gary": old_gary}
        runtime.trigger_registry.reregister_for = MagicMock()
        runtime.specialist_registry.all_configs = lambda: {}

        # --- Step 1: trigger reload with a STAGED identity change on disk ---
        with caplog.at_level(logging.WARNING, logger="reload"):
            r1 = await dispatch("triggers", runtime=runtime, role="gary")
        # REFUSED OUTRIGHT: structured restart_required error, nothing applied.
        assert r1["status"] == "error"
        assert r1["kind"] == "restart_required"
        # NOTHING mutated: the trigger registry was never reregistered.
        runtime.trigger_registry.reregister_for.assert_not_called()
        # BASELINE PRESERVED: role_configs still carries the OLD identity.
        assert runtime.role_configs["gary"] is live_cfg
        assert runtime.role_configs["gary"].binding_digest == "OLD"
        # The live agent object is unchanged.
        assert runtime.agents["gary"] is old_gary
        assert "gary" in caplog.text
        assert "restart" in caplog.text.lower()

        # --- Step 2 (laundering proof): scope=agent still refuses hot-swap ---
        r2 = await dispatch("agent", runtime=runtime, role="gary")
        assert r2["status"] == "error"
        assert r2["kind"] == "restart_required"
        # The live agent was never swapped.
        assert runtime.agents["gary"] is old_gary

    async def test_reload_triggers_non_identity_change_still_refreshes(
            self, tmp_path, monkeypatch):
        """The guard must NOT break the legitimate Q-1 cache refresh: when the
        resident's identity is UNCHANGED (same checksum + digest), scope=triggers
        still overwrites role_configs[role] with the freshly-loaded cfg so the
        back-compat consumer sees the post-reload trigger list."""
        from types import SimpleNamespace
        from reload import dispatch, register_handler, reload_triggers
        register_handler("triggers", reload_triggers)

        agents_dir = tmp_path / "agents"
        (agents_dir / "gary").mkdir(parents=True)

        new_cfg = SimpleNamespace(
            role="gary", role_checksum="RC_G", binding_digest="SAME",
            marker="reloaded",
            triggers=[SimpleNamespace(name="boot-trigger"),
                      SimpleNamespace(name="probe-q1")],
            channels=["telegram"],
        )
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda d, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {
            "gary": SimpleNamespace(
                role="gary", role_checksum="RC_G", binding_digest="SAME",
                marker="live",
                triggers=[SimpleNamespace(name="boot-trigger")],
                channels=["telegram"]),
        }
        runtime.trigger_registry.reregister_for = MagicMock()

        result = await dispatch("triggers", runtime=runtime, role="gary")
        assert result["status"] == "ok"
        # Identity unchanged -> the Q-1 cache refresh happened normally.
        assert runtime.role_configs["gary"] is new_cfg
        names = [t.name for t in runtime.role_configs["gary"].triggers]
        assert names == ["boot-trigger", "probe-q1"]

    async def test_reload_triggers_identity_change_leaves_no_mixed_state(
            self, tmp_path, monkeypatch, caplog):
        """P1 (Sol) no-mixed-state proof: when the on-disk cfg's personality
        identity moved AND its channels ALSO differ (webhook removed), the
        refused scope=triggers must leave everything the trigger reconciler
        consumes consistently OLD. The reconciler authorizes plugin webhook
        ingress from runtime.role_configs[role].channels; the round-2 design
        would have kept the OLD cache (still listing 'webhook') while
        reregistering the NEW triggers — mixed state. The round-3 refusal keeps
        cache channels AND the trigger registry both untouched-OLD.

        Kept at the reload_triggers seam (no full webhook-ingress e2e)."""
        import logging
        from types import SimpleNamespace
        from reload import dispatch, register_handler, reload_triggers
        register_handler("triggers", reload_triggers)

        agents_dir = tmp_path / "agents"
        (agents_dir / "gary").mkdir(parents=True)

        # On-disk cfg: identity moved OLD->NEW *and* channels dropped 'webhook'.
        disk_cfg = SimpleNamespace(
            role="gary", role_checksum="RC_G", binding_digest="NEW",
            marker="reloaded",
            triggers=[SimpleNamespace(name="probe-new")],
            channels=["telegram"],
        )
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda d, **kw: disk_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        live_cfg = SimpleNamespace(
            role="gary", role_checksum="RC_G", binding_digest="OLD",
            marker="live",
            triggers=[SimpleNamespace(name="boot-trigger")],
            channels=["telegram", "webhook"],
        )
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"gary": live_cfg}
        runtime.trigger_registry.reregister_for = MagicMock()

        with caplog.at_level(logging.WARNING, logger="reload"):
            result = await dispatch("triggers", runtime=runtime, role="gary")
        assert result["status"] == "error"
        assert result["kind"] == "restart_required"
        # NO MIXED STATE: cache channels still the OLD set (webhook still there),
        # so the reconciler's ingress-authorization view is unchanged...
        assert runtime.role_configs["gary"] is live_cfg
        assert runtime.role_configs["gary"].channels == ["telegram", "webhook"]
        # ...and the trigger registry was never touched.
        runtime.trigger_registry.reregister_for.assert_not_called()


class TestReloadIssue327:
    """#327: four correctness gaps in the scoped reload path."""

    def _resident_cfg(self, role, *, triggers=None, channels=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            role=role,
            character=SimpleNamespace(name=role.title(), card=""),
            triggers=list(triggers or []), channels=list(channels or []),
            memory=SimpleNamespace(read_strategy="per_turn"),
        )

    async def test_added_resident_registers_its_triggers(
        self, tmp_path, monkeypatch,
    ):
        """#327(a): a resident added via scope=agents must get its declared
        triggers wired — pre-fix the bulk add path installed config/agent/
        bus but never called the trigger registry, so cron/interval/webhook
        triggers silently never fired until a restart."""
        from types import SimpleNamespace
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "newcomer").mkdir()

        trig = SimpleNamespace(name="daily", type="cron")
        new_cfg = self._resident_cfg(
            "newcomer", triggers=[trig], channels=["telegram"])
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
        monkeypatch.setattr("agent_home.provision_agent_home",
                            lambda **kw: None)
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {}
        runtime.specialist_registry.all_configs = lambda: {}
        runtime.trigger_registry.reregister_for = MagicMock()

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "newcomer" in runtime.agents
        runtime.trigger_registry.reregister_for.assert_any_call(
            "newcomer", [trig], ["telegram"],
        )
        assert any("registered_triggers_newcomer" == a
                   for a in result["actions"])

    async def test_added_resident_trigger_failure_is_surfaced_not_fatal(
        self, tmp_path, monkeypatch,
    ):
        """#327(a): a bad trigger on an added resident must not kill the
        sweep, but the failure must be visible in the action trail."""
        from types import SimpleNamespace
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "newcomer").mkdir()

        trig = SimpleNamespace(name="bad", type="cron")
        new_cfg = self._resident_cfg(
            "newcomer", triggers=[trig], channels=["telegram"])
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
        monkeypatch.setattr("agent_home.provision_agent_home",
                            lambda **kw: None)
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {}
        runtime.specialist_registry.all_configs = lambda: {}
        from trigger_registry import TriggerError
        runtime.trigger_registry.reregister_for = MagicMock(
            side_effect=TriggerError("bad cron"))

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"          # sweep survives
        assert "newcomer" in runtime.agents      # agent still added
        assert any(a.startswith("trigger_register_failed_newcomer")
                   for a in result["actions"])

    async def test_reload_agent_surfaces_reregister_failure(
        self, tmp_path, monkeypatch,
    ):
        """#327(b): reload_agent must surface a reregister failure as
        kind=reregister_failed — pre-fix it logged and returned ok while
        the role was left with no (or partial) triggers."""
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)

        new_cfg = self._resident_cfg(
            "ellen", triggers=[MagicMock()], channels=["telegram"])
        new_agent = MagicMock()
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda **kw: new_agent)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs["ellen"] = self._resident_cfg("ellen")
        from trigger_registry import TriggerError
        runtime.trigger_registry.reregister_for = MagicMock(
            side_effect=TriggerError("bad cron"))

        result = await dispatch("agent", runtime=runtime, role="ellen")
        assert result["status"] == "error"
        assert result["kind"] == "reregister_failed"
        # The swap itself DID land (config + agent) — the error is about
        # the trigger state, and the message must say so.
        assert runtime.agents["ellen"] is new_agent
        assert "trigger" in result["message"].lower()

    async def test_specialist_constructed_with_registry_including_itself(
        self, tmp_path, monkeypatch,
    ):
        """#327(c): the Agent must be constructed AGAINST a registry that
        already contains its own specialist:<role> tier — Agent retains the
        registry object, so a stale registry makes its lazy plugin
        resolution tier-miss to resident:<role> forever."""
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "specialists" / "newspec").mkdir(parents=True)

        new_cfg = self._resident_cfg("newspec", channels=[])
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())

        seen = {}

        import reload as reload_mod

        def spy_construct(**kw):
            seen.update(kw)
            return MagicMock()

        monkeypatch.setattr(reload_mod, "_construct_agent", spy_construct)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {}
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = lambda: {"newspec": new_cfg}

        result = await dispatch("agent", runtime=runtime, role="newspec")
        assert result["status"] == "ok"
        reg = seen.get("agent_registry")
        assert reg is not None, (
            "constructed without an explicit agent_registry — the Agent "
            "would retain the stale pre-reload registry")
        assert reg.tier_for_role("newspec") == "specialist"
        # The runtime's published registry equally knows the specialist
        # (rebuilt from post-load state in the swap window).
        assert runtime.agent_registry.tier_for_role("newspec") == "specialist"

    async def test_backfilled_specialist_constructed_with_fresh_registry(
        self, tmp_path, monkeypatch,
    ):
        """#327(c), agents-sweep arm: the backfill construction must see the
        POST-rescan registry (rebuilt before the backfill loop), so the new
        specialist resolves its own specialist:<role> plugin tier."""
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "specialists" / "newspec").mkdir(parents=True)

        new_cfg = self._resident_cfg("newspec")
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
        monkeypatch.setattr("agent_home.provision_agent_home",
                            lambda **kw: None)

        constructed = []

        import reload as reload_mod

        def spy_construct(**kw):
            constructed.append(kw)
            return MagicMock()

        monkeypatch.setattr(reload_mod, "_construct_agent", spy_construct)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {}
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = lambda: {"newspec": new_cfg}
        runtime.specialist_registry.load_failures = lambda: []

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert constructed, "backfill never constructed the specialist"
        # The registry handed to construction (retained by the Agent for
        # its lazy plugin resolution) must ALREADY know the specialist —
        # the end-of-sweep rebuild is too late for the retained object.
        reg = constructed[0].get("agent_registry")
        assert reg is not None, (
            "backfill constructed without an explicit agent_registry — the "
            "Agent would retain the stale pre-rescan registry")
        assert reg.tier_for_role("newspec") == "specialist"

    async def test_policies_cascade_holds_per_role_agent_lock(
        self, tmp_path, monkeypatch,
    ):
        """#327(d): the policies cascade must serialize each role's swap
        with that role's agent-scope lock — pre-fix an in-flight agent
        reload could suspend, lose to a policies cascade, then install its
        stale-policy agent afterwards while both reported success."""
        import reload as reload_mod
        from reload import _get_lock, dispatch, register_handler, reload_policies
        register_handler("policies", reload_policies)

        cascaded = []

        async def fake_role_reload(runtime, r):
            cascaded.append(r)

        monkeypatch.setattr(
            reload_mod, "_reload_role_after_policies", fake_role_reload)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.role_configs = {"ellen": MagicMock()}
        runtime.specialist_registry.all_configs = lambda: {}

        lock = _get_lock("agent:ellen")
        await lock.acquire()
        try:
            task = asyncio.create_task(dispatch("policies", runtime=runtime))
            await asyncio.sleep(0.05)
            assert cascaded == [], (
                "cascade swapped ellen while the agent:ellen lock was held "
                "by a concurrent agent-scope reload")
        finally:
            lock.release()
        result = await task
        assert result["status"] == "ok"
        assert cascaded == ["ellen"]

    async def test_specialist_construct_failure_mutates_nothing(
        self, tmp_path, monkeypatch,
    ):
        """Sol r1-6 / Terra r4-2: a specialist construction failure must
        leave EVERY live registry untouched — the construction registry is
        an overlay built from the freshly loaded config, so no
        specialist_registry.load happens before construction and no
        registry state is published on failure (main's all-or-nothing
        failure semantics)."""
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "specialists" / "newspec").mkdir(parents=True)

        new_cfg = self._resident_cfg("newspec", channels=[])
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())

        import reload as reload_mod

        def boom(**kw):
            raise RuntimeError("construction failed")

        monkeypatch.setattr(reload_mod, "_construct_agent", boom)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {}
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = lambda: {}
        registry_before = runtime.agent_registry

        result = await dispatch("agent", runtime=runtime, role="newspec")
        assert result["status"] == "error"
        assert result["kind"] == "construct_failed"
        # Nothing mutated: no specialist rescan, no registry publication.
        runtime.specialist_registry.load.assert_not_called()
        assert runtime.agent_registry is registry_before


class TestOverlayConsumptionRetry:
    """#331(d): the roles overlay is built under MATERIALIZE_LOCK but consumed
    (agent load) after release — a concurrent upgrade committing between the
    two makes the loader fail on a role-artifact/binding mismatch. The load
    wrapper rebuilds the overlay once and retries."""

    async def test_specialist_load_retries_once_after_overlay_rebuild(
            self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        import agent_loader
        import reload as reload_mod

        calls = {"resolve": 0, "load": 0}

        async def _fake_roles_dir(runtime):
            calls["resolve"] += 1
            return str(tmp_path / f"overlay-{calls['resolve']}")

        monkeypatch.setattr(reload_mod, "_specialist_roles_dir", _fake_roles_dir)

        def _flaky_load(agent_dir, *, policies, roles_dir):
            calls["load"] += 1
            if calls["load"] == 1:
                raise ValueError("simulated stale-overlay checksum mismatch")
            return SimpleNamespace(role="mtg", loaded_from=roles_dir)

        monkeypatch.setattr(agent_loader, "load_agent_from_dir", _flaky_load)

        cfg, roles_dir = await reload_mod._load_agent_with_overlay_retry(
            SimpleNamespace(), "agents/mtg", policies=None, tier="specialist")

        # The overlay was REBUILT (second resolve) and the returned roles_dir
        # is the one the successful load actually consumed.
        assert calls == {"resolve": 2, "load": 2}
        assert roles_dir.endswith("overlay-2")
        assert cfg.loaded_from.endswith("overlay-2")

    async def test_second_failure_surfaces_the_load_error(
            self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        import agent_loader
        import reload as reload_mod

        async def _fake_roles_dir(runtime):
            return str(tmp_path / "overlay")

        monkeypatch.setattr(reload_mod, "_specialist_roles_dir", _fake_roles_dir)
        calls = {"load": 0}

        def _always_broken(agent_dir, *, policies, roles_dir):
            calls["load"] += 1
            raise ValueError("genuinely broken component")

        monkeypatch.setattr(agent_loader, "load_agent_from_dir", _always_broken)

        with pytest.raises(ValueError, match="genuinely broken"):
            await reload_mod._load_agent_with_overlay_retry(
                SimpleNamespace(), "agents/mtg", policies=None, tier="specialist")
        assert calls["load"] == 2  # exactly one retry, then surfaced

    async def test_resident_load_error_is_not_retried(self, monkeypatch):
        from types import SimpleNamespace
        import agent_loader
        import reload as reload_mod

        calls = {"load": 0}

        def _broken(agent_dir, *, policies, roles_dir):
            calls["load"] += 1
            raise ValueError("resident load error")

        monkeypatch.setattr(agent_loader, "load_agent_from_dir", _broken)
        with pytest.raises(ValueError, match="resident load error"):
            await reload_mod._load_agent_with_overlay_retry(
                SimpleNamespace(), "agents/butler", policies=None, tier="resident")
        assert calls["load"] == 1


class TestPersonalityMapRefresh:
    """GH #356: every role_configs mutation must synchronously rebind the four
    personality maps (role_slots / persona_packs / bindings /
    compiled_prompt_bundles) — before the fix they were boot-built
    MappingProxyType views no reload path ever refreshed, so a hot-added
    resident was dispatchable while `casactl persona inspect/render/diff`
    404d for its role until restart (and an evicted one stayed inspectable)."""

    @staticmethod
    def _personality_cfg(role: str):
        from types import SimpleNamespace
        return SimpleNamespace(
            role=role, role_id=role,
            character=SimpleNamespace(name=role.title(), card=""),
            triggers=[], channels=[],
            memory=SimpleNamespace(read_strategy="per_turn"),
            role_slot=SimpleNamespace(role_id=role),
            persona_pack=SimpleNamespace(persona_id=f"p-{role}", version="1"),
            binding=SimpleNamespace(persona_id=f"p-{role}", persona_version="1"),
            compiled_prompt_bundle=SimpleNamespace(role_id=role),
        )

    async def test_refresh_derives_and_rebinds_read_only_views(self):
        runtime = _make_runtime()
        cfg = self._personality_cfg("ellen")
        runtime.role_configs["ellen"] = cfg
        runtime.refresh_personality_maps()
        assert runtime.role_slots["ellen"] is cfg.role_slot
        assert runtime.persona_packs["p-ellen@1"] is cfg.persona_pack
        assert runtime.bindings["ellen"] is cfg.binding
        assert runtime.compiled_prompt_bundles["ellen"] is cfg.compiled_prompt_bundle
        # Views are read-only (MappingProxyType) — reload replaces, never mutates.
        with pytest.raises(TypeError):
            runtime.role_slots["x"] = object()
        # Evict + refresh drops every trace of the role.
        runtime.role_configs.pop("ellen")
        runtime.refresh_personality_maps()
        assert "ellen" not in runtime.role_slots
        assert "p-ellen@1" not in runtime.persona_packs
        assert "ellen" not in runtime.bindings
        assert "ellen" not in runtime.compiled_prompt_bundles

    async def test_refresh_tolerates_narrow_configs_without_personality_fields(self):
        from types import SimpleNamespace
        runtime = _make_runtime()
        runtime.role_configs["bare"] = SimpleNamespace(triggers=[])
        runtime.refresh_personality_maps()  # must not raise
        assert "bare" not in runtime.role_slots

    async def test_agents_sweep_add_populates_maps_before_return(
        self, tmp_path, monkeypatch,
    ):
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "newcomer").mkdir()

        new_cfg = self._personality_cfg("newcomer")
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())
        monkeypatch.setattr("agent_home.provision_agent_home",
                            lambda **kw: None)
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert runtime.role_slots["newcomer"] is new_cfg.role_slot
        assert runtime.bindings["newcomer"] is new_cfg.binding
        assert runtime.compiled_prompt_bundles["newcomer"] is new_cfg.compiled_prompt_bundle

    async def test_agents_sweep_evict_drops_maps(self, tmp_path, monkeypatch):
        from bus import MessageBus
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()  # tina's dir gone → evict

        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        bus = MessageBus()
        bus.register("tina", MagicMock())
        runtime.bus = bus
        runtime.role_configs = {"tina": self._personality_cfg("tina")}
        tina_agent = MagicMock()
        tina_agent.aclose = AsyncMock()
        runtime.agents = {"tina": tina_agent}
        runtime.specialist_registry.all_configs = lambda: {}
        runtime.refresh_personality_maps()
        assert "tina" in runtime.role_slots  # precondition

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "tina" not in runtime.role_slots
        assert "tina" not in runtime.bindings
        assert "tina" not in runtime.compiled_prompt_bundles
        assert "p-tina@1" not in runtime.persona_packs

    async def test_per_role_swap_rebinds_maps(self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)

        new_cfg = self._personality_cfg("ellen")
        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: new_cfg)
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())
        import reload as reload_mod
        new_agent = MagicMock()
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda *a, **kw: new_agent)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        old_cfg = self._personality_cfg("ellen")
        runtime.role_configs["ellen"] = old_cfg
        old_agent = MagicMock()
        old_agent.aclose = AsyncMock()
        runtime.agents["ellen"] = old_agent
        runtime.refresh_personality_maps()
        assert runtime.compiled_prompt_bundles["ellen"] is old_cfg.compiled_prompt_bundle

        result = await dispatch("agent", runtime=runtime, role="ellen")
        assert result["status"] == "ok"
        # Maps describe the config the now-live agent runs — not the old one.
        assert runtime.compiled_prompt_bundles["ellen"] is new_cfg.compiled_prompt_bundle
        assert runtime.bindings["ellen"] is new_cfg.binding
