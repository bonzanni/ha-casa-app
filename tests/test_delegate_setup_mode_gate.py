"""#1015: a turn carrying Casa's ``plugin_setup`` marker can delegate only in
``sync`` mode, so no engagement is ever created from it (INV-PLUG-027).

The gate sits in ``_prelaunch`` before target resolution, engagement creation
or any topic; an error result there is not a non-error delegation, so the
courier row returns to ``pending`` under #1010's bounded budget instead of
resting consumed while the engagement's setup tool is refused for identity.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AgentConfig, DelegateEntry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

import plugin_setup_episodes as pse
from test_agent_process import (
    ScriptedToolClient, _make_agent, _mk_assistant, _mk_init, _mk_result,
    _mk_tool_result, _mk_tool_use, _setup_msg,
)
from test_plugin_setup_episodes import (  # noqa: F401 — the fixture
    _COURIER, _courier_dispatched, wired,
)

pytestmark = [pytest.mark.asyncio]


def _cfg(role: str, delegates: tuple[str, ...] = ()) -> AgentConfig:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    return cfg


def _wire(monkeypatch):
    import tools as tm
    tch = MagicMock()
    tch.open_engagement_topic = AsyncMock(return_value=555)
    cm = MagicMock()
    cm.get.return_value = tch
    reg = MagicMock()
    reg.get.return_value = None
    reg.register_delegation = AsyncMock()
    reg.complete_delegation = AsyncMock()
    reg.fail_delegation = AsyncMock()
    reg.cancel_delegation = AsyncMock()
    eng_reg = MagicMock()
    eng_reg.create = AsyncMock()
    tm.init_tools(
        channel_manager=cm, bus=MagicMock(), specialist_registry=reg,
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=eng_reg,
        agent_role_map={"assistant": _cfg("assistant", delegates=("finance",)),
                        "finance": _cfg("finance")},
    )

    async def _fake_run(cfg, task_text, context_text, resolution=None,
                        output_format=None, tool_counts=None):
        return tm.DelegatedOutput(text="ok", structured_output=None)

    monkeypatch.setattr(tm, "_run_delegated_agent", _fake_run)
    return tm, tch, eng_reg


def _origin(setup: bool) -> dict:
    o = {"role": "assistant", "execution_role": "assistant", "channel": "telegram",
         "chat_id": 42, "user_id": 42, "cid": "t", "user_text": "hi",
         "message_type": "channel_in", "source": "telegram"}
    if setup:
        o.update({"synthetic": "plugin_setup", "plugin_setup_target": "finance"})
    return o


async def _delegate(tm, origin, mode):
    import agent as agent_mod
    token = agent_mod.origin_var.set(origin)
    try:
        res = await tm.delegate_to_agent.handler({
            "agent": "finance", "task": "run the setup tool", "context": "",
            "mode": mode})
    finally:
        agent_mod.origin_var.reset(token)
    return json.loads(res["content"][0]["text"]), res.get("is_error")


@pytest.mark.parametrize("mode", ["interactive", "async"])
async def test_a_non_sync_delegation_on_a_setup_turn_is_an_error_before_any_side_effect(
        monkeypatch, mode):
    tm, tch, eng_reg = _wire(monkeypatch)
    payload, is_error = await _delegate(tm, _origin(setup=True), mode)
    assert payload["status"] == "error" and is_error is True
    assert payload["kind"] == "mode_unsupported_on_setup_turn"
    assert "sync" in payload["message"]
    assert eng_reg.create.await_count == 0                       # zero records
    assert tch.open_engagement_topic.await_count == 0            # zero topics
    assert tm._specialist_registry.register_delegation.await_count == 0


async def test_a_sync_delegation_on_a_setup_turn_proceeds(monkeypatch):
    tm, _tch, _eng_reg = _wire(monkeypatch)
    payload, is_error = await _delegate(tm, _origin(setup=True), "sync")
    assert payload["status"] == "ok" and not is_error


@pytest.mark.parametrize("mode", ["sync", "async", "interactive"])
async def test_an_ordinary_dm_turn_is_unaffected(monkeypatch, mode):
    tm, tch, _eng_reg = _wire(monkeypatch)
    payload, _ = await _delegate(tm, _origin(setup=False), mode)
    assert payload.get("kind") != "mode_unsupported_on_setup_turn"
    if mode == "sync":
        assert payload["status"] == "ok"
    elif mode == "async":
        assert payload["status"] == "pending"
    else:
        # the gate did not fire: the interactive branch reached its topic
        assert tch.open_engagement_topic.await_count == 1


async def test_a_courier_turn_whose_delegation_hit_the_gate_returns_the_row_to_pending(
        tmp_path, wired, monkeypatch):
    """Through the REAL reporter: a courier turn whose only delegation
    returned the gate's error leaves the row pending with one retry counted
    and one save — never consumed (the interactive-courier reproduction,
    inverted)."""
    ep = await _courier_dispatched(wired)
    assert ep["courier_target"] == "finance"
    saves: list[int] = []
    real_save = pse._save
    monkeypatch.setattr(pse, "_save", lambda data: (saves.append(1), real_save(data))[1])
    agent = _make_agent(tmp_path)
    error = json.dumps({"status": "error", "kind": "mode_unsupported_on_setup_turn",
                        "message": "mode='interactive' is not supported on a plugin-setup turn"})
    ScriptedToolClient.reset([
        _mk_init("sid-c", [_COURIER, "Read"]),
        _mk_tool_use("t1", _COURIER, {"agent": "finance", "task": "t", "context": "",
                                      "mode": "interactive"}),
        _mk_tool_result("t1", is_error=True, text=error),
        _mk_assistant("The delegation was refused."),
        _mk_result("sid-c"),
    ])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient):
        await agent._process(_setup_msg("42", episode=ep["id"]))
    row = pse.episodes()[0]
    assert row["status"] == "pending"
    assert row["gate"] == "released"
    assert row["execution_retries"] == 1
    assert row["attempts"] == 0
    assert "finance" in row["last_error"]
    assert saves == [1]
