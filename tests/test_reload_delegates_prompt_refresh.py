"""#436: a rename must reach agents that are already mid-conversation.

Sourcing the `<delegates>` block from the live role map is only half the fix.
`Agent._build_options` runs on a COLD pool connect; a warm SDK client is reused
without rebuilding its options at all (`SdkClientPool.turn`, pinned by
`tests/test_sdk_client_pool_pool.py::test_warm_reuse_skips_connect_and_build_options`).
Reloading `finance` closes only finance's own pool, so an assistant with a warm
Telegram client would have gone on sending the pre-rename prompt indefinitely.

So the reload paths drop the warm clients of the agents whose block actually
changed — and only those, because a cold reconnect costs a couple of seconds
and a fresh prompt-cache prefix.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import (
    AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig, ToolsConfig,
)

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _restore_tools_role_map():
    import tools
    saved = tools._agent_role_map
    try:
        yield
    finally:
        tools._agent_role_map = saved


def _cfg(role: str, name: str, *, delegates=()) -> AgentConfig:
    return AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        character=CharacterConfig(name=name),
        tools=ToolsConfig(allowed=[], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        system_prompt="base prompt",
        delegates=[DelegateEntry(agent=d, purpose="p", when="w")
                   for d in delegates],
    )


def _live_agent(cfg: AgentConfig) -> MagicMock:
    a = MagicMock(handle_message=MagicMock(), aclose=AsyncMock())
    a.config = cfg
    a.invalidate_tool_surface = AsyncMock()
    return a


def _make_runtime(tmp_path: Path):
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={}, role_configs={},
        specialist_registry=MagicMock(), executor_registry=MagicMock(),
        engagement_registry=MagicMock(), agent_registry=MagicMock(),
        trigger_registry=MagicMock(), mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(), policy_lib=MagicMock(),
        config_dir=str(tmp_path), agents_dir=str(tmp_path / "agents"),
        home_root=str(tmp_path / "home"),
        defaults_root=str(tmp_path / "defaults"),
    )


async def _reload_finance_as(tmp_path, monkeypatch, new_cfg, *, agents):
    """Drive a genuine per-role reload of `finance`, returning the result."""
    import reload as reload_mod
    import tools
    from reload import dispatch, register_handler, reload_agent

    register_handler("agent", reload_agent)
    (tmp_path / "agents" / "finance").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents" / "finance" / "character.yaml").write_text(
        "role: finance\nname: x\narchetype: resident\nprompt: hi\n",
        encoding="utf-8",
    )

    runtime = _make_runtime(tmp_path)
    runtime.role_configs["finance"] = _cfg("finance", "Alex")
    for role, agent in agents.items():
        runtime.agents[role] = agent
        if role != "finance":
            runtime.role_configs[role] = agent.config
    runtime.specialist_registry.all_configs = lambda: {}
    tools.sync_agent_role_map(runtime)

    monkeypatch.setattr(
        "agent_loader.load_agent_from_dir", lambda *a, **kw: new_cfg,
    )
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(
        reload_mod, "_construct_agent",
        lambda **kw: _live_agent(new_cfg),
    )
    result = await dispatch("agent", runtime=runtime, role="finance")
    # The refresh is scheduled, not awaited (see `_schedule_prompt_refresh`),
    # so let the loop run the tasks it created.
    for task in list(reload_mod._PROMPT_REFRESH_TASKS):
        await task
    return result


async def test_rename_drops_warm_clients_of_agents_that_declare_the_role(
    tmp_path, monkeypatch,
):
    assistant = _live_agent(_cfg("assistant", "Ellen", delegates=("finance",)))
    butler = _live_agent(_cfg("butler", "Tina"))          # declares nothing
    finance = _live_agent(_cfg("finance", "Alex"))

    result = await _reload_finance_as(
        tmp_path, monkeypatch, _cfg("finance", "Lex"),
        agents={"assistant": assistant, "butler": butler, "finance": finance},
    )

    assert result["status"] == "ok", result
    assistant.invalidate_tool_surface.assert_awaited_once()
    # Untouched: a cold reconnect is not free, so only the agents whose
    # rendered block actually changed pay for it.
    butler.invalidate_tool_surface.assert_not_awaited()
    assert any(a.startswith("refresh_delegates_prompt") for a in result["actions"])


async def test_a_reload_that_changes_no_name_refreshes_nobody(
    tmp_path, monkeypatch,
):
    """Per-role reloads are routine — a prompt tweak, a tools grant. None of
    those change what any other agent advertises, and cold-reconnecting every
    caller on each one would be a standing tax."""
    assistant = _live_agent(_cfg("assistant", "Ellen", delegates=("finance",)))
    finance = _live_agent(_cfg("finance", "Alex"))

    result = await _reload_finance_as(
        tmp_path, monkeypatch, _cfg("finance", "Alex"),   # same name
        agents={"assistant": assistant, "finance": finance},
    )

    assert result["status"] == "ok", result
    assistant.invalidate_tool_surface.assert_not_awaited()
    assert not any(a.startswith("refresh_delegates_prompt")
                   for a in result["actions"])


async def test_refresh_is_scheduled_not_awaited_by_the_reload(
    tmp_path, monkeypatch,
):
    """The deadlock guard. `casa_reload` runs as a tool INSIDE a warm client's
    turn and `SdkClientPool.invalidate_all` awaits each entry's turn lock — so
    a reload that awaited the invalidation of its own caller would hang on that
    caller's own in-flight turn. Same reasoning as `_schedule_agent_close`."""
    import reload as reload_mod

    started = []
    release = None

    async def _never_returns():
        started.append(True)
        await release

    assistant = _live_agent(_cfg("assistant", "Ellen", delegates=("finance",)))
    assistant.invalidate_tool_surface = MagicMock(
        side_effect=lambda: _never_returns(),
    )
    finance = _live_agent(_cfg("finance", "Alex"))

    import asyncio
    release = asyncio.get_running_loop().create_future()

    import reload as _rm
    register = _rm.register_handler
    from reload import dispatch, reload_agent
    register("agent", reload_agent)

    (tmp_path / "agents" / "finance").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents" / "finance" / "character.yaml").write_text(
        "role: finance\nname: x\narchetype: resident\nprompt: hi\n",
        encoding="utf-8",
    )
    runtime = _make_runtime(tmp_path)
    runtime.role_configs["finance"] = _cfg("finance", "Alex")
    runtime.role_configs["assistant"] = assistant.config
    runtime.agents.update({"assistant": assistant, "finance": finance})
    runtime.specialist_registry.all_configs = lambda: {}
    import tools
    tools.sync_agent_role_map(runtime)

    renamed = _cfg("finance", "Lex")
    monkeypatch.setattr(
        "agent_loader.load_agent_from_dir", lambda *a, **kw: renamed,
    )
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(
        reload_mod, "_construct_agent", lambda **kw: _live_agent(renamed),
    )

    # Would hang if the reload awaited the invalidation.
    result = await asyncio.wait_for(
        dispatch("agent", runtime=runtime, role="finance"), timeout=5.0,
    )
    assert result["status"] == "ok", result
    assert started == [True]
    release.set_result(None)
    for task in list(reload_mod._PROMPT_REFRESH_TASKS):
        await task


async def test_policies_cascade_refreshes_the_role_map_it_changed(
    tmp_path, monkeypatch,
):
    """`scope=policies` commits a fresh AgentConfig for every role it swaps,
    so a display name can change with no agent/agents reload in sight. It
    never refreshed the delegation role map, so both the ACL and every
    rendered block stayed on the pre-cascade name indefinitely — and
    `scope=config_sync` inherits it, cascading `agents` (which refreshes)
    BEFORE `policies` (which does the swapping)."""
    import reload as reload_mod
    import tools
    from reload import dispatch, register_handler, reload_policies

    register_handler("policies", reload_policies)
    (tmp_path / "policies").mkdir(parents=True, exist_ok=True)
    for r in ("assistant", "finance"):
        (tmp_path / "agents" / r).mkdir(parents=True, exist_ok=True)

    assistant = _live_agent(_cfg("assistant", "Ellen", delegates=("finance",)))
    runtime = _make_runtime(tmp_path)
    runtime.role_configs["assistant"] = assistant.config
    runtime.role_configs["finance"] = _cfg("finance", "Alex")
    runtime.agents["assistant"] = assistant
    runtime.agents["finance"] = _live_agent(_cfg("finance", "Alex"))
    runtime.specialist_registry.all_configs = lambda: {}
    tools.sync_agent_role_map(runtime)
    assert tools.agent_display_names()["finance"] == "Alex"

    renamed = _cfg("finance", "Lex")

    def _load(agent_dir, **kw):
        return renamed if agent_dir.endswith("finance") else assistant.config

    monkeypatch.setattr("agent_loader.load_agent_from_dir", _load)
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(
        reload_mod, "_construct_agent", lambda **kw: _live_agent(kw["cfg"]),
    )
    monkeypatch.setattr(
        reload_mod, "_resident_identity_changed", lambda *a, **kw: False,
    )

    result = await dispatch("policies", runtime=runtime)
    for task in list(reload_mod._PROMPT_REFRESH_TASKS):
        await task

    assert result["status"] == "ok", result
    assert tools.agent_display_names()["finance"] == "Lex"
    origin = {"role": "assistant", "execution_role": "assistant"}
    assert tools._canonical_delegate_target("Lex", origin) == "finance"


async def test_a_delegate_that_became_reachable_also_refreshes(
    tmp_path, monkeypatch,
):
    """Membership, not only naming: an agent that appears in the live map
    while a caller is mid-conversation must be offered on the caller's next
    turn, which needs the same warm-client drop."""
    import reload as reload_mod
    import tools
    from reload import dispatch, register_handler, reload_agent

    register_handler("agent", reload_agent)
    (tmp_path / "agents" / "finance").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents" / "finance" / "character.yaml").write_text(
        "role: finance\nname: x\narchetype: resident\nprompt: hi\n",
        encoding="utf-8",
    )

    assistant = _live_agent(_cfg("assistant", "Ellen", delegates=("finance",)))
    runtime = _make_runtime(tmp_path)
    runtime.role_configs["assistant"] = assistant.config
    runtime.agents["assistant"] = assistant
    runtime.specialist_registry.all_configs = lambda: {}
    tools.sync_agent_role_map(runtime)          # finance NOT in the map yet

    added = _cfg("finance", "Lex")
    monkeypatch.setattr(
        "agent_loader.load_agent_from_dir", lambda *a, **kw: added,
    )
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(
        reload_mod, "_construct_agent", lambda **kw: _live_agent(added),
    )

    result = await dispatch("agent", runtime=runtime, role="finance")
    for task in list(reload_mod._PROMPT_REFRESH_TASKS):
        await task

    assert result["status"] == "ok", result
    assistant.invalidate_tool_surface.assert_awaited_once()
