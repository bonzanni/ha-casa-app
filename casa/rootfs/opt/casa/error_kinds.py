"""Error classification helpers shared by agent and retry modules.

Extracted to a standalone module to avoid the circular-import that arises
when ``agent`` imports ``retry`` *and* ``retry`` imports from ``agent``.
"""

from __future__ import annotations

import asyncio
from enum import Enum


class VoiceToolLoopError(RuntimeError):
    """A direct voice turn exhausted its bounded HA correction loop."""


class ErrorKind(Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SDK_ERROR = "sdk_error"
    MEMORY_ERROR = "memory_error"
    CHANNEL_ERROR = "channel_error"
    VOICE_TOOL_LOOP = "voice_tool_loop"
    REFUSAL = "refusal"
    API_ERROR = "api_error"
    UNKNOWN = "unknown"


_USER_MESSAGES: dict[ErrorKind, str] = {
    ErrorKind.TIMEOUT: "The request timed out. Try again in a moment.",
    ErrorKind.RATE_LIMIT: "Rate limited by the API. Please wait a minute and try again.",
    ErrorKind.SDK_ERROR: "There was an issue communicating with Claude. Please try again.",
    ErrorKind.MEMORY_ERROR: "Memory service is unavailable, but I can still respond without context.",
    ErrorKind.CHANNEL_ERROR: "There was an issue sending the response.",
    ErrorKind.VOICE_TOOL_LOOP: "I couldn't resolve that cleanly. Try naming the device again.",
    ErrorKind.REFUSAL: "That request was declined by Claude's safety system. Rephrasing it usually helps.",
    ErrorKind.API_ERROR: "The Claude API returned an error, so I couldn't finish that. Please try again later.",
    ErrorKind.UNKNOWN: "Sorry, something went wrong while processing your request.",
}


class ApiErrorTurn(RuntimeError):
    """The CLI ended a turn by reporting an API-level fault (#568).

    Carries the already-resolved :class:`ErrorKind` so classification happens
    once, where the evidence is, rather than by matching on message text.
    """

    def __init__(self, kind: ErrorKind, detail: str = "") -> None:
        super().__init__(detail or kind.value)
        self.kind = kind


#: CLI ``error`` values that name a transient fault worth another attempt.
#: Every other value — and there are many, the namespace is open (``model_not_found``
#: was measured on the wire and is not even in the SDK's ``AssistantMessageError``
#: literal) — falls through to the non-retryable :data:`ErrorKind.API_ERROR`, so an
#: unrecognised value fails closed rather than being retried three times.
_API_ERROR_KINDS: dict[str, ErrorKind] = {
    "rate_limit": ErrorKind.RATE_LIMIT,
    "overloaded": ErrorKind.RATE_LIMIT,
    "server_error": ErrorKind.SDK_ERROR,
    "connection_error": ErrorKind.SDK_ERROR,
}


def api_error_kind(sdk_msg: object) -> ErrorKind | None:
    """The :class:`ErrorKind` an ``AssistantMessage`` reports, or ``None``.

    The Claude Code CLI does not raise for an API-level fault — it synthesizes
    an ordinary ``assistant`` message whose single content block holds its own
    user-facing error string, and stamps the envelope with ``error:"<value>"``
    (surfaced by the SDK as ``AssistantMessage.error``). A safety refusal is the
    same construction with ``message.stop_reason:"refusal"`` on top. Both were
    read off the wire against CLI 2.1.233.

    Callers pass an ``AssistantMessage``; the gate is the *truthiness* of
    ``error``, never membership in a known set, because the set of values the
    CLI can emit is open.
    """
    error = getattr(sdk_msg, "error", None)
    if not error:
        return None
    if getattr(sdk_msg, "stop_reason", None) == "refusal":
        return ErrorKind.REFUSAL
    return _API_ERROR_KINDS.get(str(error), ErrorKind.API_ERROR)


def _classify_error(exc: Exception) -> ErrorKind:
    """Classify an exception into an ErrorKind for routing recovery."""
    if isinstance(exc, ApiErrorTurn):
        return exc.kind
    if isinstance(exc, VoiceToolLoopError):
        return ErrorKind.VOICE_TOOL_LOOP
    if isinstance(exc, asyncio.TimeoutError):
        return ErrorKind.TIMEOUT

    msg = str(exc).lower()
    if "rate" in msg and "limit" in msg:
        return ErrorKind.RATE_LIMIT
    if "429" in msg:
        return ErrorKind.RATE_LIMIT
    # Anthropic API overload (HTTP 529 / ``overloaded_error``) — the single
    # most common transient failure. The SDK surfaces it as a ``ProcessError``
    # (type name lacks the CLI/SDK/Connection markers below) whose message
    # carries none of the rate-limit/timeout tokens, so without this rule it
    # fell through to UNKNOWN and was never retried. Treat as RATE_LIMIT: it
    # is retryable, and 529s carry no Retry-After, so the loop uses jittered
    # exponential backoff — exactly right for an overload.
    if "overloaded" in msg or "529" in msg:
        return ErrorKind.RATE_LIMIT
    if "timeout" in msg or "timed out" in msg:
        return ErrorKind.TIMEOUT

    exc_type = type(exc).__name__
    if "CLI" in exc_type or "SDK" in exc_type or "Connection" in exc_type:
        return ErrorKind.SDK_ERROR

    return ErrorKind.UNKNOWN
