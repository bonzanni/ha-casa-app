"""The tool result envelopes: a JSON payload or plain text as a tool's MCP
content, marked as an error where it is one."""

from __future__ import annotations

import json
from typing import Any


def _text_result(text: str, *, is_error: bool = False) -> dict:
    """One plain-text tool result, marked as an error where it is one.

    ``is_error`` is what turns a failure into a FAILURE in turn telemetry: the
    SDK's MCP adapter reads it and the wire field follows, so without it
    ``sdk_logging.log_tool_result`` emits ``ok=True`` for a message that
    reached nobody (the same F-7/O-1 reasoning as :func:`_result`, which takes
    a JSON payload rather than plain text).
    """
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


def _result(payload: dict, *, is_error: bool | None = None) -> dict:
    """Wrap a JSON-serializable payload as the tool's MCP content.

    F-7 (v0.32.0): when ``payload["status"] == "error"`` (or the caller
    explicitly passes ``is_error=True``), set ``is_error: True`` on the
    envelope. Without this flag the SDK's ``ToolResultBlock`` defaults
    ``is_error=False`` and ``sdk_logging.log_tool_result`` emits
    ``ok=True`` even for failures — operators reading turn telemetry
    would think a registry-rejected launch actually spawned.
    Auto-detection via ``payload["status"]`` keeps the existing call sites
    untouched while making every error path consistently observable.

    O-1 (v0.37.9): also recognise ``payload["ok"] is False`` so the
    install/uninstall plugin envelopes (which use ``{"ok": False,
    "error": ...}`` instead of ``{"status": "error", ...}``) surface as
    MCP errors too. Live evidence (2026-05-14 P29.1 cid ``52240634``):
    ``tool_result name=install_casa_plugin ok=True ms=12594`` for a
    ``plugin_not_in_marketplace`` failure — telemetry reported the
    failure as success, contradicting F-7's intent.

    The dict key MUST be ``is_error`` (snake_case): the Anthropic Agent
    SDK's MCP server adapter reads
    ``result.get("is_error", False)`` (see
    ``claude_agent_sdk/__init__.py:512``) and converts to the MCP wire
    field ``isError`` itself. Passing ``isError`` here gets silently
    dropped on the way to the model.
    """
    if is_error is None:
        is_error = (
            payload.get("status") == "error"
            or payload.get("ok") is False
        )
    envelope: dict = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if is_error:
        envelope["is_error"] = True
    return envelope
