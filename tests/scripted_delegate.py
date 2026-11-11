"""Shared fixtures for tests that run a delegated agent over a scripted SDK
client: a protected tool and its arguments, a script step that runs inside the
response stream (where the SDK runs a protected call's hook), the scripted
client, and builders for the text, tool-call and tool-result messages."""
from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
    ToolUseBlock, UserMessage,
)

from test_delegate_to_agent import _FakeSpecialistClient

TOOL = "mcp__plugin_p_p__invoice_reset"
PROTECTED = {TOOL: {"artifact_id": "artifact-1", "summary": None}}
ARGS = {"amount": 10}
TAIL = "TAIL-A12"


class _Step:
    """A script step run INSIDE the response stream, where the SDK runs the
    protected call's hook, instead of a yielded message."""

    def __init__(self, fn) -> None:
        self.fn = fn


class _Scripted(_FakeSpecialistClient):
    script: list = []

    @classmethod
    def load(cls, *items):
        _FakeSpecialistClient.reset()
        cls.script = list(items)

    async def receive_response(self):
        yield SystemMessage(subtype="init", data={"session_id": "exec-sid"})
        for item in type(self).script:
            if isinstance(item, _Step):
                await item.fn()
                continue
            yield item
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=2, session_id="exec-sid")


def _text(t):
    return AssistantMessage(content=[TextBlock(text=t)], model="sonnet")


def _call(call_id):
    return AssistantMessage(content=[ToolUseBlock(id=call_id, name=TOOL, input=dict(ARGS))],
                            model="sonnet")


def _result(call_id, *, error=True):
    return UserMessage(content=[ToolResultBlock(tool_use_id=call_id,
                                                content="denied" if error else "ok",
                                                is_error=error)])
