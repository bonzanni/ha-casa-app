"""Old Agent instances must have aclose() scheduled at every swap/evict
site (reload.py) — F12: a pooled client would otherwise leak one
subprocess per warm conversation per reload."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import reload as reload_mod

pytestmark = pytest.mark.asyncio


def _make_runtime():
    """Copied from tests/test_reload.py:14 (`_make_runtime`).

    `from tests.test_reload import _make_runtime` does NOT work under this
    repo's conftest sys.path setup: conftest.py only inserts the
    casa code root onto sys.path, `tests/` has no `__init__.py`, and
    plain `pytest tests/...` invocation does not add the repo root to
    sys.path either — so `import tests` raises ModuleNotFoundError
    (verified empirically before writing this file). Per the task-9
    brief's documented fallback, the function body is duplicated here
    instead.
    """
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


async def test_schedule_agent_close_awaits_aclose():
    agent = MagicMock()
    agent.aclose = AsyncMock()
    reload_mod._schedule_agent_close(agent)
    await asyncio.sleep(0.01)
    agent.aclose.assert_awaited_once()


async def test_schedule_agent_close_tolerates_missing_aclose():
    reload_mod._schedule_agent_close(object())      # must not raise
    reload_mod._schedule_agent_close(None)


async def test_close_tina_facade_awaits_aclose():
    from casa_core import _close_butler_ha_facade

    class Facade:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    facade = Facade()
    await _close_butler_ha_facade(facade)
    assert facade.closed


async def test_close_tina_facade_failure_is_sanitized(caplog):
    from casa_core import _close_butler_ha_facade

    class Facade:
        async def aclose(self):
            raise RuntimeError("private-token at http://private-ha")

    with caplog.at_level(logging.WARNING):
        await _close_butler_ha_facade(Facade())

    assert [
        record.getMessage() for record in caplog.records
        if "ha_facade" in record.getMessage()
    ] == ["ha_facade_close_failed"]
    assert "private-token" not in caplog.text
    assert "private-ha" not in caplog.text


async def test_close_tina_facade_hanging_close_is_bounded(caplog):
    from casa_core import _close_butler_ha_facade

    close_started = asyncio.Event()
    close_cancelled = asyncio.Event()
    never_close = asyncio.Event()

    class Facade:
        async def aclose(self):
            close_started.set()
            try:
                await never_close.wait()
            finally:
                close_cancelled.set()

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(
            _close_butler_ha_facade(Facade(), timeout=0.01),
            timeout=1.0,
        )

    assert close_started.is_set()
    assert close_cancelled.is_set()
    assert [
        record.getMessage() for record in caplog.records
        if "ha_facade" in record.getMessage()
    ] == ["ha_facade_close_failed"]


async def test_reload_agent_closes_replaced_instance(monkeypatch, tmp_path):
    """Run reload_agent against the stub runtime (mirrors the end-to-end
    pattern in tests/test_reload.py::TestReloadAgent.test_resident_atomic_swap,
    :241-286) and assert the OLD agent object got scheduled for close.
    """
    agents_dir = tmp_path / "agents"
    (agents_dir / "assistant").mkdir(parents=True)

    new_cfg = SimpleNamespace(
        role="assistant",
        character=SimpleNamespace(name="Assistant-2", card=""),
        triggers=[], channels=[],
    )
    monkeypatch.setattr(
        "agent_loader.load_agent_from_dir", lambda *a, **kw: new_cfg,
    )
    monkeypatch.setattr(
        "policies.load_policies", lambda *a, **kw: MagicMock(),
    )

    runtime = _make_runtime()
    runtime.config_dir = str(tmp_path)
    runtime.agents_dir = str(agents_dir)
    runtime.role_configs["assistant"] = SimpleNamespace(
        role="assistant",
        character=SimpleNamespace(name="Assistant", card=""),
    )
    sentinel_old = object()
    runtime.agents["assistant"] = sentinel_old
    # A real Agent has .handle_message (reload_agent binds it to the bus);
    # a MagicMock satisfies that without exercising the real Agent class.
    new_agent = MagicMock()
    monkeypatch.setattr(reload_mod, "_construct_agent",
                        lambda *a, **kw: new_agent)
    closed = []
    monkeypatch.setattr(reload_mod, "_schedule_agent_close",
                        lambda a, **kw: closed.append(a))

    await reload_mod.reload_agent(runtime, role="assistant")

    assert closed == [sentinel_old]
    assert runtime.agents["assistant"] is new_agent
