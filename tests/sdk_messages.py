"""Builders for the Claude SDK messages the scripted-client tests feed an agent.

Tolerant of the SDK's constructor shape: a message the constructor refuses is
built by hand with the same fields.
"""

from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)


def _mk_text_block(text: str) -> _SDKTextBlock:
    """Instantiate whatever TextBlock shape the installed SDK uses."""
    try:
        return _SDKTextBlock(text=text)
    except TypeError:
        return _SDKTextBlock(text)  # type: ignore[call-arg]


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    block = _mk_text_block(text)
    try:
        return _SDKAssistantMessage(content=[block])
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_result(sid, usage=None, *, is_error=False, result=""):
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid
    m.is_error = is_error
    m.result = result
    if usage is not None:
        m.usage = usage
    return m
