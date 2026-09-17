"""Failure notices retain the delegated specialist's tool-call evidence."""

from __future__ import annotations

import asyncio
import time

import pytest

from bus import MessageBus, MessageType
from channels import ChannelManager
from config import (
    AgentConfig, CharacterConfig, MemoryConfig, SessionConfig, ToolsConfig,
)
from specialist_registry import DelegationComplete, DelegationRecord, SpecialistRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _specialist_cfg() -> AgentConfig:
    return AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role="finance",
        model="claude-sonnet-4-6",
        system_prompt="You are Finance.",
        character=CharacterConfig(name="Finance"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=0),
        session=SessionConfig(strategy="ephemeral", idle_timeout=0),
    )


def _origin() -> dict:
    return {
        "role": "assistant",
        "execution_role": "assistant",
        "channel": "telegram",
        "chat_id": "room-1",
        "cid": "c1",
        "user_text": "please do X",
    }


def _init_tools(tmp_path, monkeypatch):
    import tools

    registry = SpecialistRegistry(
        str(tmp_path / "specialists"),
        tombstone_path=str(tmp_path / "delegations.json"),
    )
    bus = MessageBus()
    bus.register("assistant", None)
    tools.init_tools(
        ChannelManager(), bus, registry,
        agent_role_map={"finance": _specialist_cfg()},
    )
    return tools, bus


async def _notification(bus: MessageBus) -> DelegationComplete:
    async with asyncio.timeout(5):
        while True:
            if not bus.queues["assistant"].empty():
                _priority, _sequence, message = await bus.queues["assistant"].get()
                if message.type is MessageType.NOTIFICATION:
                    assert isinstance(message.content, DelegationComplete)
                    return message.content
            await asyncio.sleep(0.01)


class _ScriptedClient:
    tool_names: tuple[str, ...] = ()
    abort = True
    wait_for_cancel = False

    def __init__(self, _options):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def query(self, _prompt):
        pass

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

        blocks = [
            ToolUseBlock(id=str(index), name=name, input={})
            for index, name in enumerate(self.tool_names)
        ]
        yield AssistantMessage(content=blocks, model="test")
        if self.wait_for_cancel:
            await asyncio.Event().wait()
        yield ResultMessage(
            subtype="error_max_turns" if self.abort else "success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=self.abort,
            num_turns=1,
            session_id="test",
        )


def _use_scripted_client(tools, monkeypatch) -> None:
    from claude_agent_sdk import ClaudeAgentOptions

    async def _inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(tools, "ClaudeSDKClient", _ScriptedClient)
    monkeypatch.setattr(tools.asyncio, "to_thread", _inline)
    monkeypatch.setattr(
        tools, "_build_specialist_options",
        lambda *_args, **_kwargs: ClaudeAgentOptions(),
    )


async def _settle_failure(tools, bus: MessageBus, monkeypatch) -> DelegationComplete:
    import agent as agent_mod

    async def _persist_failure(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tools, "_fail_delegation_durably", _persist_failure)
    token = agent_mod.origin_var.set(_origin())
    try:
        task = asyncio.create_task(
            tools._run_delegated_agent_bounded(_specialist_cfg(), "x", ""))
    finally:
        agent_mod.origin_var.reset(token)
    tools._attach_completion_callback(task, DelegationRecord(
        id="delegation-1", agent="finance", started_at=time.time(),
        origin=_origin(),
    ))
    notice = await _notification(bus)
    await asyncio.sleep(0)
    return notice


async def test_aborted_run_notice_lists_short_tool_names_and_counts(
    tmp_path, monkeypatch,
):
    tools, bus = _init_tools(tmp_path, monkeypatch)
    _ScriptedClient.tool_names = (
        "mcp__ledger__tag", "mcp__ledger__tag", "mcp__bank__read_rows",
    )
    _ScriptedClient.abort = True
    _ScriptedClient.wait_for_cancel = False
    _use_scripted_client(tools, monkeypatch)

    notice = await _settle_failure(tools, bus, monkeypatch)

    assert notice.message == (
        "Specialist could not complete the delegated task. "
        "Before stopping it called: tag ×2, read_rows ×1"
    )


async def test_ceiling_timeout_notice_lists_tools_called_before_cancellation(
    tmp_path, monkeypatch,
):
    tools, bus = _init_tools(tmp_path, monkeypatch)

    async def _tool_then_wait(
        _cfg, _task, _context, resolution=None, output_format=None,
        tool_counts=None,
    ):
        assert tool_counts is not None
        tool_counts["apply"] = 1
        await asyncio.Event().wait()

    monkeypatch.setattr(tools, "_run_delegated_agent", _tool_then_wait)
    monkeypatch.setattr(tools, "_DELEGATION_CEILING_S", 0.02)
    monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 0.5)

    notice = await _settle_failure(tools, bus, monkeypatch)

    assert notice.kind == "timeout"
    assert "Before stopping it called: apply ×1" in notice.message


async def test_aborted_run_without_tools_keeps_the_existing_notice(tmp_path, monkeypatch):
    tools, bus = _init_tools(tmp_path, monkeypatch)
    _ScriptedClient.tool_names = ()
    _ScriptedClient.abort = True
    _ScriptedClient.wait_for_cancel = False
    _use_scripted_client(tools, monkeypatch)

    notice = await _settle_failure(tools, bus, monkeypatch)

    assert notice.message == "Specialist could not complete the delegated task."
