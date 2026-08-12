"""#436: the `<delegates>` block must advertise the name delegation ACCEPTS.

`AgentRegistry` is immutable and a live `Agent` keeps the instance it was
constructed with (``reload._construct_agent``) — deliberately, so a later
``runtime.agent_registry`` rebind cannot reach a running agent. Reloading a
single role therefore refreshed `delegate_to_agent`'s role map but left every
OTHER agent rendering its construction-time snapshot. Rename `finance`'s
persona from "Alex" to "Lex", reload only `finance`, and the still-live
assistant kept advertising "Alex" while the resolver knew only "Lex" —
reproducing #433's `delegation_not_declared` for that one name.

The fix derives the rendered name at the point of USE from the same live map
the resolver reads, so the two cannot disagree.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import _live_agent_directory, _render_delegates_block
from agent_registry import AgentRegistry
from config import (
    AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig, ToolsConfig,
)

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


# Every module global `init_tools` rebinds. `--dist loadfile` keeps one FILE
# on one worker, but a worker still runs many files in the same process, so a
# test that calls `init_tools` (the reload e2e below does) leaves all of these
# pointing at its fixtures unless they are put back.
_TOOLS_GLOBALS = (
    "_channel_manager", "_bus", "_specialist_registry", "_mcp_registry",
    "_agent_role_map", "_agent_registry", "_trigger_registry",
    "_engagement_registry", "_executor_registry", "_runtime",
    "_specialist_limiter", "_specialist_telemetry", "_agent_spawn_limiter",
    "_voice_job_route_cap",
)


@pytest.fixture(autouse=True)
def _restore_tools_globals():
    import tools
    saved = {n: getattr(tools, n) for n in _TOOLS_GLOBALS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(tools, name, value)


def test_restore_fixture_covers_every_global_init_tools_rebinds():
    """Guard the guard: a new `init_tools` global that this list misses would
    silently start leaking into other files on the same xdist worker."""
    import inspect

    import tools

    # Parsed, not string-matched: a second `global` statement or a moved
    # `# noqa` must not be able to fail this test without changing behaviour.
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(tools.init_tools)))
    declared = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Global)
        for name in node.names
    }
    assert declared == set(_TOOLS_GLOBALS)


def _cfg(role: str, name: str, *, delegates=None) -> AgentConfig:
    return AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        character=CharacterConfig(name=name),
        tools=ToolsConfig(allowed=[], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        system_prompt="base prompt",
        delegates=list(delegates or []),
    )


def _assistant_cfg() -> AgentConfig:
    return _cfg(
        "assistant", "Ellen",
        delegates=[DelegateEntry(agent="finance", purpose="Money matters.",
                                 when="User asks about money.")],
    )


# --- the renderer ------------------------------------------------------

class TestRenderWithLiveNames:
    def test_live_names_win_over_a_stale_construction_registry(self):
        """The whole of #436 in one assertion: a registry that still says
        "Alex" must not be what the block shows once the live map says "Lex"."""
        assistant = _assistant_cfg()
        stale = AgentRegistry.build(
            residents={"assistant": assistant, "finance": _cfg("finance", "Alex")},
            specialists={},
        )
        block = _render_delegates_block(
            assistant.delegates, stale, live_names={"finance": "Lex"},
        )
        assert "finance (Lex)" in block
        assert "Alex" not in block

    def test_live_names_decide_membership_not_the_stale_registry(self):
        """Membership follows the live map too. A delegate the registry still
        knows but the live map has dropped (disabled/removed since
        construction) is no longer callable, so it must not be advertised."""
        assistant = _assistant_cfg()
        stale = AgentRegistry.build(
            residents={"assistant": assistant, "finance": _cfg("finance", "Alex")},
            specialists={},
        )
        block = _render_delegates_block(
            assistant.delegates, stale, live_names={"assistant": "Ellen"},
        )
        assert block == ""

    def test_live_names_admit_a_delegate_the_stale_registry_lacks(self):
        """The converse: a delegate registered AFTER this agent was built is
        callable now, so it must be advertised now."""
        assistant = _assistant_cfg()
        stale = AgentRegistry.build(
            residents={"assistant": assistant}, specialists={},
        )
        block = _render_delegates_block(
            assistant.delegates, stale,
            live_names={"assistant": "Ellen", "finance": "Lex"},
        )
        assert "finance (Lex)" in block

    def test_role_id_alone_when_the_live_name_is_the_role_id(self):
        """#433's label collapses when there is no distinct persona name; the
        live-map path must collapse it identically, not emit "finance
        (finance)"."""
        assistant = _assistant_cfg()
        block = _render_delegates_block(
            assistant.delegates, None, live_names={"finance": "finance"},
        )
        assert "- finance —" in block
        assert "(finance)" not in block

    def test_no_live_names_falls_back_to_the_registry(self):
        """Back-compat: `live_names=None` means "tools is not initialized",
        and the construction-time registry stays authoritative."""
        assistant = _assistant_cfg()
        reg = AgentRegistry.build(
            residents={"assistant": assistant, "finance": _cfg("finance", "Alex")},
            specialists={},
        )
        block = _render_delegates_block(assistant.delegates, reg)
        assert "finance (Alex)" in block


# --- the live directory ------------------------------------------------

class TestLiveAgentDirectory:
    def test_none_when_the_role_map_is_empty(self):
        """None, not `{}`, so the renderer keeps trusting its own registry
        instead of concluding nobody is dispatchable. This deliberately does
        NOT distinguish "never initialized" from "initialized with no roles":
        boot always registers at least the assistant, so the second state is
        a test shape, not a deployment."""
        import tools
        tools._agent_role_map = {}
        assert _live_agent_directory() is None

    def test_maps_every_dispatchable_role_to_its_persona_name(self):
        import tools
        tools._agent_role_map = {
            "assistant": _cfg("assistant", "Ellen"),
            "finance": _cfg("finance", "Lex"),
        }
        assert _live_agent_directory() == {
            "assistant": "Ellen", "finance": "Lex",
        }

    def test_role_id_stands_in_for_a_missing_persona_name(self):
        import tools
        tools._agent_role_map = {"finance": _cfg("finance", "")}
        assert _live_agent_directory() == {"finance": "finance"}


# --- end to end: a genuine per-role reload -----------------------------

def _make_runtime(tmp_path: Path):
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={},
        role_configs={},
        specialist_registry=MagicMock(),
        executor_registry=MagicMock(),
        engagement_registry=MagicMock(),
        agent_registry=MagicMock(),
        trigger_registry=MagicMock(),
        mcp_registry=MagicMock(),
        session_registry=MagicMock(),
        channel_manager=MagicMock(),
        bus=MagicMock(),
        engagement_driver=MagicMock(),
        claude_code_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir=str(tmp_path),
        agents_dir=str(tmp_path / "agents"),
        home_root=str(tmp_path / "home"),
        defaults_root=str(tmp_path / "defaults"),
    )


@pytest.mark.asyncio
async def test_per_role_reload_refreshes_a_live_agents_delegates_block(
    tmp_path, monkeypatch,
):
    """The #436 reproduction, end to end and through a REAL live `Agent`.

    PR #435's tests rebuild the registry and the role map together, so none of
    them can observe this state; it only exists after a genuine per-role
    reload that reconstructs the renamed agent and nobody else.

    Scope: this drives `_build_options`, i.e. the COLD pool connect. That the
    reload also drops the warm clients which would otherwise skip
    `_build_options` entirely is the other half of the fix, pinned in
    `tests/test_reload_delegates_prompt_refresh.py`.
    """
    import plugin_registry
    import reload as reload_mod
    import tools
    from agent import Agent
    from channels import ChannelManager
    from mcp_registry import McpServerRegistry
    from plugin_fixtures import mk_registry
    from reload import dispatch, register_handler, reload_agent
    from session_registry import SessionRegistry

    plugin_registry.reload_snapshot(
        registry_path=mk_registry(tmp_path, []), store_root=tmp_path / "store",
    )
    register_handler("agent", reload_agent)

    finance_dir = tmp_path / "agents" / "finance"
    finance_dir.mkdir(parents=True)
    (finance_dir / "character.yaml").write_text(
        "role: finance\nname: Alex\narchetype: resident\nprompt: hi\n",
        encoding="utf-8",
    )

    assistant_cfg = _assistant_cfg()
    finance_v1 = _cfg("finance", "Alex")
    finance_v2 = _cfg("finance", "Lex")     # the rename

    runtime = _make_runtime(tmp_path)
    runtime.role_configs["assistant"] = assistant_cfg
    runtime.role_configs["finance"] = finance_v1
    runtime.specialist_registry.all_configs = lambda: {}

    # The assistant is LIVE and keeps the boot registry — the one that still
    # says "Alex". It is deliberately never reconstructed by this reload.
    boot_registry = AgentRegistry.build(
        residents=dict(runtime.role_configs), specialists={},
    )
    sm = AsyncMock()
    sm.profile.return_value = ""
    sm.recall.return_value = ""
    assistant = Agent(
        config=assistant_cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        agent_registry=boot_registry,
        semantic_memory=sm,
    )
    runtime.agents["assistant"] = MagicMock()
    runtime.agents["finance"] = MagicMock(
        handle_message=MagicMock(), aclose=AsyncMock(),
    )
    tools.init_tools(
        channel_manager=MagicMock(), bus=MagicMock(),
        specialist_registry=runtime.specialist_registry,
        mcp_registry=MagicMock(),
        agent_role_map=dict(runtime.role_configs),
        agent_registry=boot_registry,
    )

    # Pre-condition: both sides say "Alex".
    opts = await assistant._build_options(
        channel="telegram", channel_key="k", is_fresh=True,
        resume_sid=None, user_text="hi")
    assert "finance (Alex)" in opts.system_prompt

    # --- rename on disk, reload ONLY finance ---------------------------
    (finance_dir / "character.yaml").write_text(
        "role: finance\nname: Lex\narchetype: resident\nprompt: hi\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_loader.load_agent_from_dir", lambda *a, **kw: finance_v2,
    )
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(
        reload_mod, "_construct_agent",
        lambda **kw: MagicMock(handle_message=MagicMock(), aclose=AsyncMock()),
    )

    result = await dispatch("agent", runtime=runtime, role="finance")
    assert result["status"] == "ok", result
    assert "refresh_role_map" in result["actions"]
    # The assistant was NOT reconstructed — that is the whole premise.
    assert assistant._agent_registry is boot_registry

    # --- the assertion -------------------------------------------------
    opts = await assistant._build_options(
        channel="telegram", channel_key="k", is_fresh=True,
        resume_sid=None, user_text="hi")
    assert "finance (Lex)" in opts.system_prompt
    assert "Alex" not in opts.system_prompt

    # And the resolver agrees, which is the point of sharing one map.
    origin = {"role": "assistant", "execution_role": "assistant"}
    assert tools._canonical_delegate_target("Lex", origin) == "finance"
