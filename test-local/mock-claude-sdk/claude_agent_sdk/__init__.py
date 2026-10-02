"""Deterministic offline shim for claude_agent_sdk.

Records every call to /data/mock_sdk_calls.jsonl (one JSON object per line).
Responds with canned text. Honours the ``resume`` parameter so that
session_id stays stable across a conversation.

Extra knobs (env vars read lazily each call):
- ``MOCK_SDK_LATENCY_SEC``: sleep before returning (for concurrency tests).
- ``MOCK_SDK_REPLY``: override the reply text (default "ok").
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

CALL_LOG = "/data/mock_sdk_calls.jsonl"

# v0.15.1: optional file-driven simulation of an HTTP MCP tool invocation.
# Set MOCK_SDK_TOOL_INVOKE_FILE to a JSON file containing a list of
# {server, tool, args} entries. On each receive_response call the mock
# pops entry 0 and (if options.mcp_servers[server] has a url) makes a
# real JSON-RPC tools/call POST to that URL before yielding messages.
# Used by test-local/e2e/test_ha_delegation.sh to exercise the
# resident-options → SDK → HTTP MCP transport chain without needing a
# live model. See docs/superpowers/plans/2026-04-26-tina-ha-control.md F.1.
TOOL_INVOKE_FILE = "/data/mock_sdk_tool_invoke.json"


def _log(entry: dict[str, Any]) -> None:
    entry = {"ts": time.time(), **entry}
    try:
        with open(CALL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _post_jsonrpc(url: str, headers: dict[str, str], body: bytes) -> None:
    """Synchronous urllib POST. Called via asyncio.to_thread so the
    surrounding event loop (e.g. an aiohttp test server) isn't blocked."""
    import urllib.request, urllib.error  # stdlib; no new deps in the mock
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


async def _maybe_invoke_mcp_tool(options: "ClaudeAgentOptions") -> None:
    """If MOCK_SDK_TOOL_INVOKE_FILE points at a non-empty list, pop entry 0
    and POST a JSON-RPC tools/call to that server's HTTP URL. Errors are
    swallowed (and logged) so a misconfigured test can't break unrelated
    code paths."""
    path = os.environ.get("MOCK_SDK_TOOL_INVOKE_FILE", TOOL_INVOKE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            entries = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(entries, list) or not entries:
        return

    entry = entries.pop(0)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
    except OSError:
        pass

    server = entry.get("server")
    tool = entry.get("tool")
    args = entry.get("args") or {}
    cfg = (options.mcp_servers or {}).get(server) or {}
    url = cfg.get("url") if isinstance(cfg, dict) else None
    if not url:
        _log({"event": "mcp_invoke_skipped",
              "server": server, "tool": tool,
              "reason": "no url in options.mcp_servers"})
        return

    headers = cfg.get("headers") or {}
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode("utf-8")

    try:
        await asyncio.to_thread(_post_jsonrpc, url, headers, body)
        _log({"event": "mcp_invoke", "server": server, "tool": tool,
              "url": url, "status": "ok"})
    except Exception as exc:  # noqa: BLE001 — mock must not break callers
        _log({"event": "mcp_invoke", "server": server, "tool": tool,
              "url": url, "status": "error", "exc": str(exc)})


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    """Phase 4b — Bug 3 dispatch surface. Real SDK shape: name, input dict,
    optional id used to correlate with a ToolResultBlock."""

    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class ToolResultBlock:
    """Phase 4b — Bug 3 dispatch surface. Mirrors the real SDK's
    ToolResultBlock fields used by sdk_logging.log_tool_result."""

    tool_use_id: str = ""
    content: Any = None
    is_error: bool | None = None


@dataclass
class AssistantMessage:
    content: list[Any] = field(default_factory=list)


@dataclass
class UserMessage:
    """Phase 4b — Bug 3 dispatch surface. Real SDK shape carries a list of
    ToolResultBlocks when the assistant's tools have run; sdk_logging
    iterates ``content`` looking for ToolResultBlock instances."""

    content: list[Any] = field(default_factory=list)


@dataclass
class ResultMessage:
    session_id: str
    subtype: str = "result"
    # Optional spec 5.2 §5 usage fields. Default empty so historical
    # E2E scenarios (no MOCK_SDK_USAGE_* env) keep working.
    usage: dict[str, int] = field(default_factory=dict)
    # Phase 4b — sdk_logging.log_turn_done reads num_turns / total_cost_usd.
    num_turns: int = 0
    total_cost_usd: float = 0.0


@dataclass
class SystemMessage:
    subtype: str = "init"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationResetMessage:
    """INV-BGJOB-005 — the in_casa driver imports this at module scope to
    confirm a fresh job's /clear. Field shape mirrors the real SDK's
    message_parser; the mock client never emits it."""

    new_conversation_id: str = ""
    uuid: str = ""
    session_id: str = ""


@dataclass
class StreamEvent:
    """v0.67.0 voice partial streaming — agent.py imports this at module
    scope, so the mock must export it even though the mock client never
    emits partials (include_partial_messages is accepted and ignored).
    Field shape mirrors the real SDK's message_parser (uuid, session_id,
    event payload dict, parent_tool_use_id)."""

    uuid: str = ""
    session_id: str = ""
    event: dict[str, Any] = field(default_factory=dict)
    parent_tool_use_id: str | None = None


@dataclass
class PermissionResultDeny:
    """v0.68.0 fail-closed in-casa permissions — plugin_grants.py imports
    this at module scope and uses it in the fail_closed can_use_tool callback
    to deny tools not in allowed_tools (residents/specialists have no way to
    answer an interactive prompt). The mock accepts and ignores the return
    value; permission decisions in the offline mock are pre-made by
    ClaudeAgentOptions. Field order and defaults mirror the real SDK:
    PermissionResultDeny(behavior: Literal['deny']='deny', message: str='',
    interrupt: bool=False) — behavior is the discriminant (plain str here;
    the mock avoids importing Literal)."""

    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False


@dataclass
class HookMatcher:
    matcher: str
    hooks: list[Any] = field(default_factory=list)


@dataclass
class ClaudeAgentOptions:
    model: str = ""
    system_prompt: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = ""
    max_turns: int = 0
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    hooks: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    resume: str | None = None
    setting_sources: list[str] = field(default_factory=list)
    # (f) v0.69.9: all three option builders set skills="all" (replaces the
    # deprecated bare "Skill" in allowed_tools). Mock accepts + ignores it.
    skills: Any = None
    # Plan 4b (commit 28b8748): casa_core wires plugins=build_sdk_plugins(...)
    # into resident SDK construction. Without this field the dataclass init
    # raised TypeError, every turn failed, and /data/sessions.json stayed
    # empty — masking the bug under unit tests (which use the host's real
    # SDK). See reference_mock_sdk_drift in memory for the v0.5.9 precedent.
    plugins: list[Any] = field(default_factory=list)
    # v0.154.0 (#430): agent._build_options passes
    # env=sanitized_env_for_resolution(...) on EVERY turn, so without this
    # field the dataclass init raised TypeError and every turn failed —
    # the same shape as `plugins` above, and it recurred for the same reason
    # (unit tests use the host's REAL sdk, which has the field). The mock
    # accepts and ignores it: it spawns no subprocess to hand an environment
    # to. This was found four releases late, with the whole e2e tier failing
    # at its first turn.
    env: dict[str, Any] = field(default_factory=dict)
    # Phase 4b — Bug 4 stderr callback field. sdk_logging.with_stderr_callback
    # wraps the options struct via dataclasses.replace(options, stderr=cb);
    # the mock's frozen=False default still requires the field to exist.
    stderr: Any = None
    # v0.67.0 voice partial streaming: agent._build_options passes this for
    # every channel (True only for voice). The mock accepts and ignores it —
    # its client never emits StreamEvents, which the agent handles fine
    # (partials are an optimization, not a contract).
    include_partial_messages: bool = False
    # v0.68.0 fail-closed in-casa permissions: agent._build_options passes
    # a can_use_tool callback for residents/specialists. The mock accepts
    # and ignores it; offline permission decisions are pre-made by options.
    can_use_tool: Any = None
    # v0.84.0 effective CLI pin: every Casa option builder selects the
    # boot-verified absolute executable. The mock accepts and ignores it.
    cli_path: str | None = None
    # v0.332.16 (#1111): every Casa option builder raises the per-line stream
    # limit to claude_runtime.SDK_MAX_BUFFER_SIZE. The mock reads no CLI
    # stream, so it accepts and ignores it.
    max_buffer_size: int | None = None
    # v0.97.0 (Release A) restricted webhook runtime: the untrusted-webhook
    # option builder (build_restricted_webhook_options) sets these to lock the
    # runtime down — tools=[] strips built-ins, strict_mcp_config excludes
    # ambient .mcp.json, settings carries {"disableAllHooks": true}. The mock
    # accepts and ignores them (offline builds assert them directly).
    tools: Any = None
    strict_mcp_config: bool = False
    settings: str | None = None
    # v0.127.0: tools._build_specialist_options passes output_format (plus the
    # matching structured-stderr callback) on EVERY in-casa specialist build.
    # Missing here since it was introduced — found in v0.159.0 by
    # tests/test_mock_sdk_parity.py, the static sweep added for #447, not by
    # any e2e run. FOURTH instance of this drift; see `env` and `plugins`
    # above. The mock accepts and ignores it: it spawns no CLI to format.
    output_format: str | None = None
    # #1168 / #1181: the utility one-shots pass
    # extra_args={"no-session-persistence": None} (and setting_sources=[]),
    # and the delegation runner sets a Casa-chosen session_id through
    # dataclasses.replace. The mock accepts both; its client adopts the
    # chosen id below, as the real CLI does.
    extra_args: dict[str, str | None] = field(default_factory=dict)
    session_id: str | None = None


class ClaudeSDKClient:
    """Async-context-manager mock. Yields a fixed response stream."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self._last_prompt: str = ""
        # Reuse session id on resume so registry round-tripping is observable.
        # A caller-chosen session id (--session-id) names the new session.
        self._session_id: str = (options.resume or options.session_id
                                 or f"mock-{uuid.uuid4().hex[:12]}")
        # Public attribute mirroring the real SDK's ClaudeSDKClient.session_id.
        self.session_id: str = self._session_id
        _log({
            "event": "client_init",
            "model": options.model,
            "resume": options.resume,
            "assigned_session_id": self._session_id,
        })

    async def __aenter__(self) -> "ClaudeSDKClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _log({"event": "client_exit", "session_id": self._session_id})

    async def query(self, prompt: str) -> None:
        self._last_prompt = prompt
        _log({
            "event": "query",
            "session_id": self._session_id,
            "prompt_len": len(prompt),
        })

    async def receive_response(self):
        latency = float(os.environ.get("MOCK_SDK_LATENCY_SEC", "0") or "0")
        if latency > 0:
            await asyncio.sleep(latency)

        # Phase 3.1: finer-grained knob for delegate_to_agent degradation
        # tests — milliseconds, stacks with MOCK_SDK_LATENCY_SEC.
        delay_ms = float(os.environ.get("MOCK_SDK_DELAY_MS", "0") or "0")
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

        # v0.15.1: optionally simulate an HTTP MCP tool invocation before
        # yielding the canned reply. See _maybe_invoke_mcp_tool above.
        await _maybe_invoke_mcp_tool(self.options)

        reply = os.environ.get("MOCK_SDK_REPLY", "ok")

        yield SystemMessage(subtype="init", data={"session_id": self._session_id})
        yield AssistantMessage(content=[TextBlock(text=reply)])
        yield ResultMessage(
            session_id=self._session_id,
            usage={
                "input_tokens": int(os.environ.get("MOCK_SDK_USAGE_INPUT", "0") or "0"),
                "output_tokens": int(os.environ.get("MOCK_SDK_USAGE_OUTPUT", "0") or "0"),
                "cache_read_input_tokens": int(
                    os.environ.get("MOCK_SDK_USAGE_CACHE_READ", "0") or "0"
                ),
                "cache_creation_input_tokens": int(
                    os.environ.get("MOCK_SDK_USAGE_CACHE_WRITE", "0") or "0"
                ),
            },
        )


class SdkMcpTool:
    """Mock of claude_agent_sdk.SdkMcpTool.

    Real SDK's @tool decorator wraps an async function into a dataclass with
    ``name``, ``description``, ``input_schema``, ``handler`` attributes.
    v0.13.1's mcp_bridge._build_tool_dispatch iterates CASA_TOOLS expecting
    each entry to have ``.name`` and ``.handler`` — without this shim the
    mock dropped the decorator silently and callers got raw function objects.
    """

    def __init__(self, name: str, description: str, input_schema, handler):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler

    def __call__(self, *args, **kwargs):
        # Some call sites invoke the decorated function directly (SDK path).
        return self.handler(*args, **kwargs)


def tool(name: str, description: str, params: dict[str, Any]) -> Any:
    """Mock decorator for @tool. Wraps the async function into an SdkMcpTool-shaped
    object so duck-typing on ``.name``/``.description``/``.input_schema``/``.handler``
    works across the SDK and HTTP bridge paths."""
    def decorator(func):
        return SdkMcpTool(
            name=name, description=description,
            input_schema=params, handler=func,
        )
    return decorator


def create_sdk_mcp_server(name: str = "", tools: list[Any] | None = None) -> dict[str, Any]:
    """Mock MCP server factory. Returns a minimal config dict."""
    return {
        "type": "stdio",
        "command": "echo",
        "args": ["mock"],
    }


class CLIConnectionError(Exception):
    """Mock of claude_agent_sdk.CLIConnectionError (transport not writable /
    not connected). sdk_logging's loop noise filter matches this EXACT type;
    the mock client never raises it, so the filter is inert offline."""


class ProcessError(Exception):
    """Mock of claude_agent_sdk.ProcessError. Raised when the CLI process fails.

    Matches the real SDK's 3-arg signature so tests that construct it with
    ``ProcessError("msg", exit_code=1)`` or ``exit_code=1, stderr="..."``
    work unchanged.
    """

    def __init__(
        self,
        message: str,
        exit_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        if exit_code is not None:
            message = f"{message} (exit code: {exit_code})"
        if stderr:
            message = f"{message}\nError output: {stderr}"
        super().__init__(message)


# --- Session API (0.2.x) -------------------------------------------------
# The long-term-save path imports these at module top-level
# (session_saver.get_session_messages, session_sweeper.delete_session). The
# offline harness has no persisted transcript, so they are inert: read returns
# an empty transcript (the save path then retains nothing) and delete is a
# recorded no-op. Mirrors the real 0.2.87 surface so casa_core imports cleanly.
@dataclass
class SessionMessage:
    """Mock of claude_agent_sdk.SessionMessage. The save path reads ``.type``
    ('user'|'assistant') and ``.message`` (Any)."""

    type: str = "user"
    message: Any = None
    uuid: str = ""
    session_id: str = ""
    parent_tool_use_id: str | None = None


def get_session_messages(
    session_id: str, directory: str | None = None,
    limit: int | None = None, offset: int = 0,
) -> list["SessionMessage"]:
    """Mock: no persisted transcript offline → empty list (callers handle it)."""
    _log({"event": "get_session_messages",
          "session_id": session_id, "directory": directory})
    return []


def delete_session(session_id: str, directory: str | None = None) -> None:
    """Mock: no real transcript file to delete — record the call and no-op."""
    _log({"event": "delete_session",
          "session_id": session_id, "directory": directory})
    return None


__all__ = [
    "AssistantMessage",
    "CLIConnectionError",
    "ClaudeAgentOptions",
    "ClaudeSDKClient",
    "ConversationResetMessage",
    "HookMatcher",
    "PermissionResultDeny",
    "ProcessError",
    "ResultMessage",
    "SdkMcpTool",
    "SessionMessage",
    "StreamEvent",
    "SystemMessage",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
    "tool",
    "create_sdk_mcp_server",
    "delete_session",
    "get_session_messages",
]
