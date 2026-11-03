"""The writer's model port: one prompt in, one reply out.

Every model call the memory package makes goes through a :class:`ModelPort`.
Production uses :class:`SdkModelPort`, a one-shot ``query`` of the Agent SDK
on Opus 5.5 at the effort Casa passes for it, with no tools, no settings
source and no session persistence (the utility one-shot settings, so the
CLI's own transcript cleanup is never enabled, INV-MEM-021), bounded by
:data:`TIMEOUT_S`. Tests pass a deterministic double; no test calls a model.

The SDK and Casa's CLI helpers are imported only when a call is made, so the
package imports nothing of the running Casa process.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from config import effort_for, resolve_model

#: Seconds one writer model call may take before it counts as failed.
TIMEOUT_S = 600.0
#: A runaway backstop, never an efficiency device: the writer answers in one
#: turn, but turn accounting is the CLI's (see ``tier_classifier``).
MAX_TURNS = 8


@dataclass(frozen=True)
class ModelReply:
    text: str
    #: The SDK result's usage and cost, for the writer's cost line.
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None


class ModelPort(Protocol):
    async def complete(self, *, system: str, prompt: str) -> ModelReply:
        """The model's reply to *prompt* under *system*; raises on failure."""


def writer_options(system: str):
    """The SDK options of one writer call."""
    import claude_agent_sdk as sdk
    from claude_runtime import (
        CLAUDE_CLI_PATH,
        SDK_MAX_BUFFER_SIZE,
        cli_session_settings,
        ephemeral_extra_args,
        ephemeral_setting_sources,
        with_cross_session_tools_denied,
    )
    model = resolve_model("opus")
    return sdk.ClaudeAgentOptions(
        model=model,
        effort=effort_for(model),
        cli_path=CLAUDE_CLI_PATH,
        max_buffer_size=SDK_MAX_BUFFER_SIZE,
        system_prompt=system,
        max_turns=MAX_TURNS,
        tools=[], allowed_tools=[],
        disallowed_tools=with_cross_session_tools_denied(
            ["Bash", "Task", "Agent"]),
        settings=cli_session_settings(),
        setting_sources=ephemeral_setting_sources(),
        extra_args=ephemeral_extra_args(),
        # Not bypassPermissions: the CLI refuses that as root. With no tools
        # there is nothing to approve.
        permission_mode="acceptEdits",
    )


class SdkModelPort:
    """The production port: one Agent SDK query per call."""

    def __init__(self, timeout_s: float = TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    async def complete(self, *, system: str, prompt: str) -> ModelReply:
        return await asyncio.wait_for(self._query(system, prompt),
                                      self.timeout_s)

    async def _query(self, system: str, prompt: str) -> ModelReply:
        import claude_agent_sdk as sdk
        options = writer_options(system)
        text: list[str] = []
        result = None
        async for msg in sdk.query(prompt=prompt, options=options):
            if isinstance(msg, sdk.AssistantMessage):
                for block in getattr(msg, "content", []) or []:
                    piece = getattr(block, "text", None)
                    if isinstance(piece, str):
                        text.append(piece)
            elif isinstance(msg, sdk.ResultMessage):
                result = msg
        if result is not None and result.is_error:
            raise RuntimeError(
                f"writer model call ended in error ({result.subtype})")
        return ModelReply(
            text="".join(text),
            usage=getattr(result, "usage", None),
            cost_usd=getattr(result, "total_cost_usd", None))
