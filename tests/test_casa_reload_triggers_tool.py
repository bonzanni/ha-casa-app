"""Tests for casa_reload_triggers - in-process soft reload (D.3 shim)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest.fixture
def configurator_origin():
    """Set origin_var so the L26 role guard lets the call through."""
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


def _runtime_with(tmp_path, *, trigger_registry=None):
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        engagement_registry=MagicMock(),
        agent_registry=MagicMock(),
        trigger_registry=trigger_registry,
        mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir=str(tmp_path),
        agents_dir=str(tmp_path / "agents"),
        home_root=str(tmp_path / "home"),
        defaults_root="/opt/casa",
    )


class TestCasaReloadTriggers:
    async def test_no_role_returns_role_required(self, tmp_path, configurator_origin):
        import agent as agent_mod
        from tools import casa_reload_triggers
        agent_mod.active_runtime = _runtime_with(tmp_path, trigger_registry=MagicMock())
        result = await casa_reload_triggers.handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "role_required"

    async def test_runtime_not_bound_returns_not_initialized(self, configurator_origin):
        import agent as agent_mod
        from tools import casa_reload_triggers
        agent_mod.active_runtime = None
        result = await casa_reload_triggers.handler({"role": "any"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_initialized"

    async def test_unknown_role_returns_error(self, tmp_path, configurator_origin):
        import agent as agent_mod
        from tools import casa_reload_triggers
        agent_mod.active_runtime = _runtime_with(tmp_path, trigger_registry=MagicMock())
        result = await casa_reload_triggers.handler({"role": "nope"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown_role"

    async def test_no_trigger_registry_returns_error(self, tmp_path, configurator_origin):
        import agent as agent_mod
        from tools import casa_reload_triggers
        # Create the agent dir so the unknown_role guard doesn't short-circuit;
        # then set trigger_registry=None and assert "not_initialized".
        agents_dir = tmp_path / "agents" / "assistant"
        agents_dir.mkdir(parents=True)
        agent_mod.active_runtime = _runtime_with(tmp_path, trigger_registry=None)
        result = await casa_reload_triggers.handler({"role": "assistant"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_initialized"

    async def test_unprivileged_caller_refused(self, tmp_path):
        """L26: casa_reload_triggers must enforce the same
        _PRIVILEGED_CONFIG_ROLES guard as casa_reload(scope='triggers')."""
        import agent as agent_mod
        from tools import casa_reload_triggers
        agent_mod.active_runtime = _runtime_with(tmp_path, trigger_registry=MagicMock())
        tok = agent_mod.origin_var.set({"role": "assistant"})
        try:
            result = await casa_reload_triggers.handler({"role": "assistant"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "not_authorized"
        assert "configurator" in payload["message"]

        # No context bound at all -> also refused (never a permissive default).
        result = await casa_reload_triggers.handler({"role": "assistant"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"
