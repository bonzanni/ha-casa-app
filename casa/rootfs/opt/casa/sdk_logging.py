"""SDK message-loop logging primitives (Phase 4b — Bug 3 + Bug 4).

One module owns every per-message log shape and the stderr-callback
factory. All consumers (in_casa-driver._deliver_turn,
agent._attempt_sdk_turn, observer._decide_interjection,
tools.delegate_to_agent + _synthesize_answer) call into here so the
log shape is identical and tested in one place.

Loggers:
- "sdk"            : per-message dispatch (assistant_message, tool_use,
                     tool_result, turn_done, system_init).
- "subprocess_cli" : stderr-callback messages (Bug 4) + claude_code
                     log-line relay (G5 — emitted from
                     drivers/claude_code_driver.py at DEBUG).

Per-record `engagement_id` is passed via ``extra={"engagement_id": short}``
so it flows through ``log_cid.JsonFormatter`` and ``HumanFormatter``
(both already merge non-standard LogRecord attrs — verified Task 1 +
test_log_cid.py::TestExtrasFlatten).
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Callable, Iterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = logging.getLogger("sdk")
_stderr_logger = logging.getLogger("subprocess_cli")


# The SDK includes raw CLI stdout in malformed-frame diagnostics and embeds
# the same payload in its message-reader exception. Structured voice output
# may be private, so suppress those SDK-internal records only in the context
# of that one job. Detached SDK tasks inherit ContextVars when spawned; the
# filter therefore follows their lifetime without globally muting concurrent
# resident or non-voice SDK diagnostics.
_structured_voice_sdk_logs_suppressed: ContextVar[bool] = ContextVar(
    "structured_voice_sdk_logs_suppressed", default=False,
)


class _StructuredVoiceSdkPayloadFilter(logging.Filter):
    def filter(self, _record: logging.LogRecord) -> bool:
        return not _structured_voice_sdk_logs_suppressed.get()


_structured_voice_sdk_payload_filter = _StructuredVoiceSdkPayloadFilter()
for _logger_name in (
    "claude_agent_sdk._internal.query",
    "claude_agent_sdk._internal.transport.subprocess_cli",
    "claude_agent_sdk._internal._task_compat",
):
    logging.getLogger(_logger_name).addFilter(
        _structured_voice_sdk_payload_filter,
    )


@contextmanager
def suppress_structured_voice_sdk_payload_logs() -> Iterator[None]:
    """Suppress raw SDK protocol diagnostics for one structured voice job."""
    token = _structured_voice_sdk_logs_suppressed.set(True)
    try:
        yield
    finally:
        _structured_voice_sdk_logs_suppressed.reset(token)


# ---------------------------------------------------------------------------
# Tool-target extraction (§6.2 priority-ordered rules)
# ---------------------------------------------------------------------------


_TARGET_FIELDS = ("file_path", "path", "pattern", "command")
_TARGET_MAX = 80


def extract_tool_target(block: ToolUseBlock) -> str:
    """Render a short label for what a ToolUseBlock targets.

    Priority (first match wins):
      1. block.input["file_path"]
      2. block.input["path"]
      3. block.input["pattern"]
      4. block.input["command"]  (truncated at first newline)
      5. first non-empty string-typed value in block.input
      6. ""

    Result truncated to 80 chars.
    """
    inp = getattr(block, "input", {}) or {}
    if not isinstance(inp, dict):
        return ""
    for field in _TARGET_FIELDS:
        val = inp.get(field)
        if isinstance(val, str) and val:
            if field == "command":
                val = val.split("\n", 1)[0]
            return val[:_TARGET_MAX]
    # Fallback — first string-typed value (skip bool/int/None/list/dict).
    for v in inp.values():
        if isinstance(v, str) and v:
            return v[:_TARGET_MAX]
    return ""


# ---------------------------------------------------------------------------
# Per-message dispatch
# ---------------------------------------------------------------------------


def log_system_init(sdk_msg: SystemMessage) -> None:
    """DEBUG ``system_init model=<m> session_id=<short>``. Subtype-init only."""
    if getattr(sdk_msg, "subtype", None) != "init":
        return
    data = getattr(sdk_msg, "data", {}) or {}
    model = data.get("model", "?")
    sid = data.get("session_id") or ""
    short_sid = sid[:8] if sid else "-"
    logger.debug("system_init model=%s session_id=%s", model, short_sid)


def log_assistant_message(sdk_msg: AssistantMessage, *, idx: int) -> None:
    """INFO ``assistant_message idx=N chars=N tool_uses=N``."""
    chars = 0
    tool_uses = 0
    for block in getattr(sdk_msg, "content", []) or []:
        if isinstance(block, TextBlock):
            chars += len(getattr(block, "text", "") or "")
        elif isinstance(block, ToolUseBlock):
            tool_uses += 1
    logger.info(
        "assistant_message idx=%d chars=%d tool_uses=%d",
        idx, chars, tool_uses,
    )


def log_tool_use(
    block: ToolUseBlock,
    *,
    idx: int,
    started_ms: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """DEBUG ``tool_use idx=N name=<name> ms=N`` from the turn anchor."""
    name = getattr(block, "name", "?")
    elapsed = int(monotonic() * 1000 - started_ms)
    logger.debug("tool_use idx=%d name=%s ms=%d", idx, name, elapsed)


def log_tool_result(
    block: ToolResultBlock, *, idx: int, started_ms: float,
    name: str = "", monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """DEBUG ``tool_result idx=N name=<name> ok=<bool> ms=N``.

    ``ms`` is the monotonic elapsed duration since the turn started
    (started_ms is the per-turn monotonic anchor captured by the caller).
    """
    is_error = bool(getattr(block, "is_error", False))
    now_ms = monotonic() * 1000
    elapsed = int(now_ms - started_ms)
    logger.debug(
        "tool_result idx=%d name=%s ok=%s ms=%d",
        idx, name or "?", not is_error, elapsed,
    )


def log_turn_done(
    sdk_msg: ResultMessage,
    *,
    started_ms: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """INFO ``turn_done turns=N cost_usd=N.NNNN in_tok=N out_tok=N
    cache_read=N cache_write=N ms=N``.

    E2 (observability): the cache token fields were omitted, which made the
    cost line unexplainable — a cached prompt shows ``in_tok=3`` yet costs
    real money, and the v0.56 prompt-cache win was invisible to monitoring.
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` are the
    Anthropic usage keys the SDK forwards."""
    turns = getattr(sdk_msg, "num_turns", 0) or 0
    cost = float(getattr(sdk_msg, "total_cost_usd", 0.0) or 0.0)
    usage = getattr(sdk_msg, "usage", {}) or {}
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
    now_ms = monotonic() * 1000
    elapsed = int(now_ms - started_ms)
    logger.info(
        "turn_done turns=%d cost_usd=%.4f in_tok=%d out_tok=%d "
        "cache_read=%d cache_write=%d ms=%d",
        turns, cost, in_tok, out_tok, cache_read, cache_write, elapsed,
    )


# ---------------------------------------------------------------------------
# Stderr-callback factory + ClaudeAgentOptions wrapper (Bug 4)
# ---------------------------------------------------------------------------


def make_stderr_logger(*, engagement_id: str | None) -> Callable[[str], None]:
    """Return a stderr callback for ``ClaudeAgentOptions.stderr``.

    Each invocation emits one INFO record on the ``subprocess_cli``
    logger. engagement_id (if set, truncated to 8 chars) flows onto
    the record as ``extra={"engagement_id": short}`` so the existing
    JsonFormatter and HumanFormatter render it without changes (Task 1
    verification).

    The SDK swallows callback exceptions (subprocess_cli.py) so we
    don't need to defend here.
    """
    short = engagement_id[:8] if engagement_id else None

    def _cb(line: str) -> None:
        line = (line or "").rstrip()
        if not line:
            return
        if short:
            _stderr_logger.info(
                "stderr %s", line, extra={"engagement_id": short},
            )
        else:
            _stderr_logger.info("stderr %s", line)

    return _cb


def with_stderr_callback(
    options: ClaudeAgentOptions, *, engagement_id: str | None,
) -> ClaudeAgentOptions:
    """Return a copy of options with our stderr callback if not already set.

    Caller provides engagement_id where available (in_casa start /
    resume / observer / query_engager) and None otherwise (assistant DM,
    delegate_to_agent specialist invocation). Caller-provided
    ``stderr=`` callbacks (vanishingly rare in Casa code today) are
    preserved — we never overwrite.

    ``dataclasses.replace`` works because ``ClaudeAgentOptions`` is a
    frozen dataclass per SDK types.py (verified at SDK 0.1.61). The
    same ``replace`` pattern is already used in agent.py to clear
    ``resume``.
    """
    if getattr(options, "stderr", None) is not None:
        return options
    return replace(
        options, stderr=make_stderr_logger(engagement_id=engagement_id),
    )


def install_sdk_task_noise_filter(loop) -> None:
    """Install a loop exception handler that downgrades unretrieved
    ``CLIConnectionError`` from detached SDK tasks to DEBUG (P-4, v0.68.2).

    The SDK's ``Query`` spawns control-request handlers as DETACHED tasks
    (``_spawn_control_request_handler`` → ``spawn_detached``). At engagement
    teardown the handler's response ``transport.write`` races subprocess
    shutdown and raises ``CLIConnectionError`` ("ProcessTransport is not
    ready for writing"); the handler's own error path writes again and
    re-raises, so the task dies unretrieved and asyncio GC logs "Task
    exception was never retrieved" at ERROR on EVERY successful engagement
    close — noise that masks real errors. Nothing casa-side can await those
    SDK-internal tasks, so the loop exception handler is the only
    interception point.

    Scope is deliberately the EXACT type: awaited ``CLIConnectionError``
    paths are handled by turn/engagement error handling (only detached
    SDK-internal tasks reach the loop handler), while the
    ``CLINotFoundError`` subclass (claude binary missing) and every other
    exception stay on the default handler at ERROR.
    """
    def _handler(loop_, context: dict) -> None:
        exc = context.get("exception")
        if type(exc) is CLIConnectionError:
            logger.debug(
                "suppressed detached SDK-task teardown noise: %s: %s",
                type(exc).__name__, exc,
            )
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(_handler)


__all__ = [
    "extract_tool_target",
    "install_sdk_task_noise_filter",
    "log_system_init",
    "log_assistant_message",
    "log_tool_use",
    "log_tool_result",
    "log_turn_done",
    "make_stderr_logger",
    "suppress_structured_voice_sdk_payload_logs",
    "with_stderr_callback",
]
