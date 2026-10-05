"""#1207 arm 4 under ruling #1252: a resident turn whose SYNCHRONOUS delegate
left an approval waiting shows the operator the keyboard and nothing the
resident wrote after the delegate's result; the words before it still show,
and the delegate result the resident's model receives is unchanged.

Red case specified by **astra** (drive redcase round, MODE: SPECIFY, against
``8e9971ef241e732f4000c937814bfab1f30ffdad``). It drives the REAL
``Agent.handle_message`` with a scripted resident client, the REAL
``delegate_to_agent`` handler and delegated runner with a scripted child
client, and the REAL ``authz_grants.make_resident_authz_hook`` answering the
child's protected call. The handler runs as the SDK runs an in-process MCP
tool: in a task whose context is a copy of the one the client was connected
in (where ``origin_var`` is the pooled client's holder), never in the task
that folds the resident's messages. The resident's ``ToolResultBlock``
carries the handler's own returned content.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ToolResultBlock, ToolUseBlock,
    UserMessage,
)

from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantStore, make_resident_authz_hook,
    _DENY_POSTED,
)
from session_registry import SessionRegistry

from test_authz_hook import _deny_reason
from test_chosen_silence_announcements import (
    _Factory, _Hook, _ScriptedClient, _make_agent, _patch_retry_sleep,
)
from test_delegate_to_agent import _caller_cfg, _specialist_cfg
from test_pending_approval_desk import _Scripted, _Step
from test_pending_approval_output import (
    ARGS, PROTECTED, TOOL, _Channel, _call, _msg, _result, _text,
)

pytestmark = [pytest.mark.asyncio]

DELEGATE = "mcp__casa-framework__delegate_to_agent"
SPECIALIST = "finance"
DELEGATE_ARGS = {"agent": SPECIALIST, "task": "reset invoice", "context": "",
                 "mode": "sync"}
TAIL = "TAIL-A4"


class _ConnectedClient(_ScriptedClient):
    """A scripted resident client that keeps the context it was connected in —
    the context the SDK's read task snapshots, and so the one every
    in-process MCP handler task copies."""

    async def connect(self):
        self.ctx = contextvars.copy_context()
        return None


class _ConnectedFactory(_Factory):
    def __call__(self, options) -> _ConnectedClient:
        script = self._scripts.pop(0) if self._scripts else []
        c = _ConnectedClient(options, script, sid=f"sid-{len(self.clients) + 1}")
        self.clients.append(c)
        return c


def _delegate_call(call_id: str) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(
        id=call_id, name=DELEGATE, input=dict(DELEGATE_ARGS))], model="sonnet")


async def test_sync_delegate_pending_approval_keeps_resident_prefix(tmp_path):
    import tools
    import verdict_broker
    agent = _make_agent(tmp_path)
    channel = _Channel()
    agent._channel_manager.register(channel)
    registry = MagicMock()
    registry.register_delegation = AsyncMock()
    registry.complete_delegation = AsyncMock()
    tools.init_tools(channel_manager=agent._channel_manager, bus=MagicMock(),
                     specialist_registry=registry, mcp_registry=MagicMock())
    cfg = _specialist_cfg(SPECIALIST)
    grants, coord = GrantStore(), ChallengeCoordinator()
    hook = make_resident_authz_hook(
        SPECIALIST, PROTECTED,
        lambda: AuthzDeps(channel=channel, grants=grants, challenges=coord))
    hook_answers: list = []
    delegate_results: list = []

    async def _child_asks():
        hook_answers.append(await hook(
            {"tool_name": TOOL, "tool_input": dict(ARGS)}, "deny-1", {}))

    _Scripted.load(_text("CHILD-BEFORE"), _call("deny-1"), _Step(_child_asks),
                   _result("deny-1"), _text("CHILD-AFTER"))

    # The resident's result block for the delegate call: filled with what the
    # handler actually returned, before the stream yields it.
    delegate_block = ToolResultBlock(tool_use_id="delegate-1", content=None,
                                     is_error=False)
    factory = _ConnectedFactory([[
        _text("BEFORE"),
        _delegate_call("delegate-1"),
        None,  # replaced below by the handler step
        UserMessage(content=[delegate_block]),
        _text(TAIL),
    ]])

    async def _run_handler():
        client = factory.clients[-1]
        task = asyncio.get_running_loop().create_task(
            tools.delegate_to_agent.handler(dict(DELEGATE_ARGS)),
            context=client.ctx.copy())
        result = await task
        assert task is not asyncio.current_task()
        delegate_block.content = result["content"]
        delegate_block.is_error = bool(result.get("is_error", False))
        delegate_results.append(json.loads(result["content"][0]["text"]))

    factory._scripts[0][2] = _Hook(_run_handler)

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(verdict_broker, "BROKER",
                                              verdict_broker.VerdictBroker()))
            stack.enter_context(patch.object(SessionRegistry, "_save_locked",
                                              AsyncMock()))
            stack.enter_context(patch.object(
                tools, "_agent_role_map",
                {"assistant": _caller_cfg(), SPECIALIST: cfg}))
            stack.enter_context(patch.object(
                tools, "_prelaunch",
                AsyncMock(return_value=(SPECIALIST, cfg, None, None, None))))
            stack.enter_context(patch.object(
                tools, "_build_specialist_options",
                return_value=ClaudeAgentOptions(model="sonnet")))
            stack.enter_context(patch.object(tools, "_delegated_resolution",
                                              return_value=None))
            stack.enter_context(patch("tools.ClaudeSDKClient", _Scripted))
            stack.enter_context(patch("sdk_client_pool._default_make_client",
                                      factory))
            stack.enter_context(_patch_retry_sleep())
            await asyncio.wait_for(agent.handle_message(_msg("dm")), 10)
    finally:
        await agent.aclose()

    # The child's protected call was denied POSTED: one keyboard is up.
    assert len(hook_answers) == 1
    assert _deny_reason(hook_answers[0]) == _DENY_POSTED
    assert len(channel.posts) == 1
    # The resident's model received the delegate result unchanged and whole.
    assert len(delegate_results) == 1
    assert delegate_results[0]["status"] == "ok"
    assert delegate_results[0]["text"] == "CHILD-BEFORE\n\nCHILD-AFTER"
    # The operator sees only what the resident wrote before the delegate's
    # result: streamed and final.
    assert channel.tokens == ["BEFORE"]
    assert channel.final_texts() == ["BEFORE"]
