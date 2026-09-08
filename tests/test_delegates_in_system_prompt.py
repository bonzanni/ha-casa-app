"""Verify <delegates> block reaches the SDK system prompt at turn time."""

from __future__ import annotations

from agent_registry import AgentRegistry
from config import AgentConfig, CharacterConfig, DelegateEntry, ToolsConfig

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


def _cfg(role: str, name: str, *, delegates=None) -> AgentConfig:
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="x",
        character=CharacterConfig(name=name),
        tools=ToolsConfig(allowed=[], permission_mode="acceptEdits"),
        system_prompt="base prompt",
        delegates=list(delegates or []),
    )


def test_delegates_block_leads_with_the_role_id():
    assistant_cfg = _cfg(
        "assistant", "Ellen",
        delegates=[
            DelegateEntry(
                agent="butler", purpose="Device control.",
                when="User asks to turn things on/off.",
            ),
            DelegateEntry(
                agent="finance", purpose="Money.",
                when="User asks about money.",
            ),
        ],
    )
    butler_cfg = _cfg("butler", "Tina")
    finance_cfg = _cfg("finance", "Alex")

    reg = AgentRegistry.build(
        residents={"assistant": assistant_cfg, "butler": butler_cfg},
        specialists={"finance": finance_cfg},
    )

    from agent import _render_delegates_block
    block = _render_delegates_block(assistant_cfg.delegates, reg)
    assert "<delegates>" in block
    assert "</delegates>" in block
    # #433: the ROLE ID leads, because it is the value `delegate_to_agent`
    # is keyed on. Rendering the persona name first made it the salient
    # token and the model addressed delegates by it. The name is still shown
    # — the model must map "ask Tina to..." onto a role — but as the
    # parenthetical, converging with the specialist-side renderer
    # (`agent_loader._render_delegates_section`), which emits role ids only.
    assert "butler (Tina)" in block
    assert "finance (Alex)" in block
    assert "Device control." in block
    assert "(role:" not in block


def test_delegates_block_omitted_when_no_delegates():
    cfg = _cfg("butler", "Tina")  # no delegates
    reg = AgentRegistry.build(residents={"butler": cfg}, specialists={})
    from agent import _render_delegates_block
    assert _render_delegates_block(cfg.delegates, reg) == ""


def test_executors_block_renders_for_assistant():
    from agent import _render_executors_block
    from config import ExecutorEntry
    cfg = _cfg("assistant", "Ellen")
    cfg.executors = [
        ExecutorEntry(
            executor_type="configurator",
            purpose="Edit configs.",
            when="User wants to change configuration.",
        ),
    ]
    block = _render_executors_block(cfg.executors)
    assert "<executors>" in block
    assert "configurator" in block
    assert "Edit configs." in block
    assert "Engage when: User wants to change configuration." in block


def test_executors_block_omitted_when_empty():
    from agent import _render_executors_block
    assert _render_executors_block([]) == ""
