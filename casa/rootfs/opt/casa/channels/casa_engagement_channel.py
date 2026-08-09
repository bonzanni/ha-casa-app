"""casa-engagement-channel: per-engagement stdio MCP server (v0.37.2).

Launched by the ``claude_code`` driver via ``--channels server:<name>`` on the
``claude`` CLI subprocess. Supplies operator-facing tools that route through svc_casa_mcp's token-authed
``127.0.0.1:8100`` listener at ``/internal/channel/send_to_topic`` (Containment
Stage 2, Task 9), which forwards on to casa-main over the root-only Unix
socket and fans out to Telegram (or any future channel) by topic.

Surface (v0.37.2):
- ``reply(chat_id, text)`` — append a message to the engagement's operator topic.
  ``chat_id`` is accepted for SDK compatibility but NEVER forwarded (D2 locked).
- ``declared_capabilities()`` — returns ``{"claude/channel": {}}``.

Permission relay is handled by the casa-main PreToolUse hook
``engagement_permission_relay`` (v0.37.2; C-1). Earlier versions
attempted to relay via ``notifications/claude/channel/permission_request``
from the CLI — that path was retired in v0.37.2 because CC CLI 2.1.x
does not emit that notification under any tested permission_mode.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import uuid
from contextlib import AsyncExitStack
from typing import Any

import aiohttp
import anyio
from aiohttp import TCPConnector
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level state populated from argv / env at import or main().
# ---------------------------------------------------------------------------

ENGAGEMENT_ID: str | None = None
INTERNAL_SOCKET: str = os.environ.get("CASA_INTERNAL_SOCKET", "/run/casa/internal.sock")
# Containment Stage 2 (Task 9): the channel server runs as a child of the
# uid-dropped CLI, which cannot reach the 0600 root-owned unix socket above
# (``INTERNAL_SOCKET`` is retained only for any legacy/env-driven callers —
# ``_internal_post`` no longer reads it). Instead it talks to svc_casa_mcp's
# token-authed TCP listener, which forwards ONLY the ``/internal/channel/*``
# family to the unix socket on this process's behalf (never `/admin/reload`
# or personality-admin — see svc_casa_mcp.py's explicit route allowlist).
CHANNEL_HTTP_HOST: str = "127.0.0.1"
CHANNEL_HTTP_PORT: int = 8100
# #335: per-engagement secret, injected via the workspace .mcp.json "env"
# block. Sent alongside engagement_id in every internal POST — casa-main
# verifies it against the record before acting with the engagement's
# authority. Absent (None) only in pre-upgrade workspaces, whose bodies the
# handlers then reject fail-closed.
ENGAGEMENT_TOKEN: str | None = os.environ.get("CASA_ENGAGEMENT_TOKEN")

INSTRUCTIONS = (
    "Casa engagement channel (Phase 1 skeleton). The full operator-facing "
    "instructions prompt lands in Phase 3 (Task 29)."
)

server: FastMCP = FastMCP("casa-engagement-channel", instructions=INSTRUCTIONS)

# v0.79.0 (§2, r8-1): ask/reply are RAW-DICT ingresses — NO client-side
# validation. The subprocess transmits the raw args, the per-logical-call
# request_id, and the pinned projection hash; casa-main validates server-side
# and refuses bad args (the zero-HTTP fail-fast contract flipped in T3). Timeout
# is still CLAMPED into the payload here (the broker needs a bounded wait), but
# the projection hash is computed over the timeout AS GIVEN so it matches the
# relay's hash of the tool_use frame.
_ASK_MAX_TIMEOUT_S = 570.0
_ASK_MIN_TIMEOUT_S = 30.0
_ASK_DEFAULT_TIMEOUT_S = 300.0
# The broker itself waits up to _ASK_MAX_TIMEOUT_S for an operator tap; a
# same-length (or shorter) aiohttp ClientTimeout would race that wait and
# abort the HTTP call out from under a still-legitimately-pending ask. Pad
# by 15s so the transport always outlives the broker's own deadline.
_ASK_CLIENT_TIMEOUT_PAD_S = 15.0


# ---------------------------------------------------------------------------
# Pinned projection → hash (§2 "Hash identity"). Computed AT THE INGRESS
# BOUNDARY over RAW args, INLINE (this subprocess cannot import casa modules —
# only its own directory is on sys.path). Byte-for-byte identical to
# ``authz_grants.canonical_args_hash`` so casa-main's relay, which recomputes
# the hash from the tool_use frame via ``channels.output_sequencer``, agrees.
# ---------------------------------------------------------------------------

def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _ask_projection_hash(
    question: Any, options: Any, timeout_s: Any, multi: Any = False,
) -> str:
    """ask → ``{question, options, timeout_s-as-given, multi-as-given}``.

    A5 · F-MULTI (v0.83.0): ``multi`` joins the pinned projection. The relay's
    ``output_sequencer.project_args`` mirrors this EXACTLY (same four keys), so
    an intent's transmitted hash and the ask block's computed hash agree — the
    pinned hash-identity contract."""
    return _canonical_hash(
        {"question": question, "options": options,
         "timeout_s": timeout_s, "multi": multi},
    )


def _reply_projection_hash(text: Any) -> str:
    """reply → ``{text}`` (drops the SDK-compat ``chat_id``)."""
    return _canonical_hash({"text": text})


def declared_capabilities() -> dict[str, dict]:
    """Experimental capabilities advertised in MCP InitializationOptions.

    v0.37.2: declares only ``claude/channel`` (outbound reply tool +
    topic-state notifications). The ``claude/channel/permission``
    capability was retired in v0.37.2 (C-1) — permission relay now flows
    through the casa-main PreToolUse hook ``engagement_permission_relay``
    (see ``docs/superpowers/specs/2026-05-13-c1-permission-relay-fix.md``).
    """
    return {"claude/channel": {}}


# ---------------------------------------------------------------------------
# Internal HTTP-over-Unix-socket client.
# ---------------------------------------------------------------------------

# Exponential backoff schedule shared by every _internal_post retry loop.
# Module-level so a test can shrink it (rather than patching asyncio.sleep,
# which mutates the process-wide asyncio module — see CLAUDE.md's memory
# cage note) to make a retry-path test run fast.
_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.0, 2.0)


async def _internal_post(
    path: str, payload: dict[str, Any], *, timeout_s: float | None = None,
) -> dict[str, Any]:
    """POST ``payload`` to ``path`` on svc_casa_mcp's token-authed TCP
    listener (``http://127.0.0.1:8100``), which forwards the
    ``/internal/channel/*`` family on to casa-main's Unix socket.

    Containment Stage 2 (Task 9): a uid-dropped engagement subprocess cannot
    reach the 0600 root-owned Unix socket, so this client talks TCP to the
    already-token-authed loopback listener instead. The engagement identity
    still authenticates at the far end — ``engagement_id``/``engagement_token``
    ride in the JSON body exactly as before, verified by the
    ``/internal/channel/*`` handlers (#335) against the engagement record.

    Auto-merges ``engagement_id=ENGAGEMENT_ID`` if not already present in
    ``payload``. Retries with exponential backoff (``_RETRY_DELAYS_S``),
    reusing the SAME ``payload`` (and therefore the same ``request_id``,
    when the caller included one) across every attempt — a transport-level
    retry is a reattach, never a fresh request. Returns the parsed JSON
    response body on success; raises the last ``aiohttp`` exception if all
    attempts fail.

    ``timeout_s``: optional total ``aiohttp.ClientTimeout`` override. Left
    ``None`` (aiohttp's own ~300s default) for every pre-W5 caller; the `ask`
    tool passes an explicit, longer budget (see ``_ASK_CLIENT_TIMEOUT_PAD_S``)
    so the transport timeout can never race the broker's own wait.
    """
    body = dict(payload)
    if "engagement_id" not in body and ENGAGEMENT_ID is not None:
        body["engagement_id"] = ENGAGEMENT_ID
    # #335: the id claim is only honored with the matching workspace secret.
    if "engagement_token" not in body and ENGAGEMENT_TOKEN is not None:
        body["engagement_token"] = ENGAGEMENT_TOKEN

    connector = TCPConnector()
    session_kwargs: dict[str, Any] = {"connector": connector}
    if timeout_s is not None:
        session_kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout_s)
    last_exc: BaseException | None = None
    async with aiohttp.ClientSession(**session_kwargs) as session:
        for attempt, delay in enumerate(_RETRY_DELAYS_S):
            try:
                async with session.post(
                    f"http://{CHANNEL_HTTP_HOST}:{CHANNEL_HTTP_PORT}{path}",
                    json=body,
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, OSError) as exc:
                last_exc = exc
                if attempt < len(_RETRY_DELAYS_S) - 1:
                    await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Tools.
# ---------------------------------------------------------------------------

@server.tool()
async def reply(chat_id: str, text: str) -> dict[str, Any]:
    """Append ``text`` to this engagement's operator topic.

    D2 locked: ``chat_id`` is accepted for CLI/SDK schema compatibility but is
    NEVER forwarded. The casa-main handler resolves the target chat/topic from
    ``engagement_id``, which we pass explicitly so the outbound payload is
    self-describing (auditable in logs / test capture-fakes).

    v0.79.0 (§2): mints a per-logical-call ``request_id`` (reused by transport
    retries inside ``_internal_post``) and transmits the pinned ``{text}``
    projection hash so casa-main can register a discrete-send intent and a
    post-loss retry reattaches idempotently (no double post).
    """
    del chat_id  # D2: explicitly discarded.
    request_id = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "text": text,
        "request_id": request_id,
        "projection_hash": _reply_projection_hash(text),
    }
    if ENGAGEMENT_ID is not None:
        payload["engagement_id"] = ENGAGEMENT_ID
    return await _internal_post("/internal/channel/send_to_topic", payload)


@server.tool()
async def ask(
    question: str, options: list, timeout_s: Any = None, multi: bool = False,
) -> dict[str, Any]:
    """Ask the operator a question. With 2-8 ``options`` it renders tappable
    buttons; with ``options: []`` it posts a numbered free-text question anchor.

    Each ``options`` entry is either a plain string OR a ``{"label": str,
    "short": str}`` dict: ``label`` is the full choice shown VERBATIM in the
    message body and returned on selection. **Supply ``short`` for every
    option that isn't already a couple of words** — it is the button
    caption, so it is the difference between a readable keyboard and a wall
    of truncated text. Without a usable ``short`` (missing, blank, duplicated
    across options, or too long once decorated), Casa falls back to a plain
    numbered floor (``"Option 1"``, ``"Option 2"``, …) for the WHOLE set —
    that floor is a safety net, not something to write toward. Returns the
    selected FULL label.

    Enumerable answers MUST go in ``options`` — never as prose inside
    ``question``, not even a compact inline list like ``"(a) rename the repo
    (b) keep the current name (c) ask again later"``. That reads as
    enumerated to a human, but the operator still can't tap it. If the
    possible answers are countable, put them in ``options`` and keep
    ``question`` a plain sentence.

    Pass ``multi: true`` (requires ≥2 ``options``) when SEVERAL choices may
    apply — the keyboard becomes toggle checkboxes plus a ``✅ Submit`` row,
    and the response carries ``options``/``option_indices`` (the full
    selection) alongside the first-selection ``option``/``option_index``.

    Never pre-number or pre-letter your own options (``A — …``, ``1. …``) or
    your own question (``Q7: …``) — Casa numbers both, prepending its own
    ``Q<n>:`` to the question and numbering every option button. Your text
    posts VERBATIM, so a self-added number or letter just sits there
    duplicated next to Casa's own numbering; it is not stripped or merged.

    THE CONTRACT: `ask` is the ONLY channel for an operator decision — never
    embed a question in a `reply`. After it posts (button OR anchor), END YOUR
    TURN and wait; the operator's answer starts your next turn. Ask ONE question
    at a time (a second while one is live is refused `question_pending`).

    Returns the selected FULL label. On timeout it returns
    ``{"outcome": "no_answer", "engagement_paused": true}`` — the operator is
    away and the engagement is now PAUSED: END YOUR TURN silently (no sign-off)
    and wait. Do NOT re-ask; your question stays on record. A further `ask`
    while paused is refused `operator_away` — that too means end your turn.
    NOT an authorization mechanism.

    v0.79.0 (§2, r8-1): RAW-DICT ingress — no client-side validation. The raw
    args, the per-logical-call ``request_id`` (minted BEFORE attempt 1, reused
    by retries) and the pinned projection hash (over the timeout AS GIVEN) are
    transmitted; casa-main validates + refuses server-side.
    """
    # timeout is clamped for the broker payload; the hash is over the RAW value.
    try:
        raw_t = (
            float(timeout_s) if timeout_s is not None else _ASK_DEFAULT_TIMEOUT_S
        )
    except (TypeError, ValueError):
        raw_t = _ASK_DEFAULT_TIMEOUT_S
    clamped_timeout = min(max(raw_t, _ASK_MIN_TIMEOUT_S), _ASK_MAX_TIMEOUT_S)
    # Generated ONCE: every retry attempt (transport-level, inside
    # _internal_post) and any explicit ask_cancel reuse this SAME id so a
    # retry reattaches to the one live broker request instead of posting a
    # second keyboard.
    request_id = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "request_id": request_id,
        "projection_hash": _ask_projection_hash(
            question, options, timeout_s, multi),
        "question": question,
        "options": options,
        "timeout_s": clamped_timeout,
        "multi": multi,
    }
    if ENGAGEMENT_ID is not None:
        payload["engagement_id"] = ENGAGEMENT_ID

    completed = False
    try:
        result = await _internal_post(
            "/internal/channel/ask", payload,
            timeout_s=clamped_timeout + _ASK_CLIENT_TIMEOUT_PAD_S,
        )
        completed = True
        return result
    finally:
        # Genuine caller cancellation (or any other early exit that never
        # reached a response) — tell casa-main to stop waiting, so a later
        # same-id reattach can't resurrect a stale tap. Best-effort: never
        # let a failure here mask the real cancellation/exception.
        if not completed:
            try:
                await _internal_post(
                    "/internal/channel/ask_cancel", {"request_id": request_id},
                )
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Test helpers (public API used by tests/test_casa_engagement_channel.py).
# ---------------------------------------------------------------------------

async def _list_tools_for_tests() -> list:
    """Test-only helper — DO NOT call from runtime tool-dispatch flow.

    Return the list of registered tools (each has a ``.name`` attribute).
    """
    return await server.list_tools()


async def _invoke_tool_for_tests(name: str, args: dict[str, Any]) -> Any:
    """Test-only helper — DO NOT call from runtime tool-dispatch flow.

    Invoke the registered tool function directly with ``**args``. Bypasses
    FastMCP's wire-format wrapping so tests can assert on the raw Python
    return value. Uses the ``_tool_manager`` accessor; falls back to
    ``call_tool`` only when the ``_tool_manager`` attr is missing entirely
    (a future mcp rename). When ``_tool_manager`` exists but the requested
    tool is not registered, raises ``KeyError`` rather than silently routing
    through ``call_tool``.
    """
    tm = getattr(server, "_tool_manager", None)
    if tm is not None:
        tool = tm.get_tool(name)
        if tool is None:
            raise KeyError(f"tool {name!r} not registered")
        fn = tool.fn
        result = fn(**args)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    return await server.call_tool(name, args)


# ---------------------------------------------------------------------------
# Stdio bootstrap with injected experimental capabilities (§A.6.1).
# ---------------------------------------------------------------------------

def _resolve_mcp_server():
    """Return the underlying low-level mcp Server, tolerating a future rename."""
    return getattr(server, "_mcp_server", None) or getattr(server, "server", None)


async def _run_with_channel_capabilities() -> None:
    """Run the FastMCP server over stdio with channel experimental capabilities.

    Replaces ``FastMCP.run_stdio_async`` because the upstream coroutine
    hard-codes a no-arg ``create_initialization_options()`` call and we need
    to inject ``experimental_capabilities`` so the CLI sees ``claude/channel``.
    """
    low_level = _resolve_mcp_server()
    if low_level is None:
        raise RuntimeError(
            "FastMCP did not expose an underlying mcp Server "
            "(neither _mcp_server nor server attribute present)",
        )
    async with stdio_server() as (read_stream, write_stream):
        await low_level.run(
            read_stream,
            write_stream,
            low_level.create_initialization_options(
                experimental_capabilities=declared_capabilities(),
            ),
        )


# ---------------------------------------------------------------------------
# CLI entry.
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="casa-engagement-channel")
    parser.add_argument(
        "--engagement-id",
        required=True,
        help="Engagement id used to route /internal/channel/* calls.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    global ENGAGEMENT_ID
    args = _build_arg_parser().parse_args(argv)
    ENGAGEMENT_ID = args.engagement_id
    anyio.run(_run_with_channel_capabilities)


# ---------------------------------------------------------------------------
# Test-only seam: populate module state directly without entering run loop.
# ---------------------------------------------------------------------------

def _configure_for_test(
    engagement_id: str, *, internal_socket: str | None = None,
    engagement_token: str | None = None,
) -> None:
    """Test-only seam — populate module state directly without entering run-loop.

    Production code never calls this; tests use it after re-importing the module
    with a fresh argv patch.
    """
    global ENGAGEMENT_ID, INTERNAL_SOCKET, ENGAGEMENT_TOKEN
    ENGAGEMENT_ID = engagement_id
    if internal_socket is not None:
        INTERNAL_SOCKET = internal_socket
    if engagement_token is not None:
        ENGAGEMENT_TOKEN = engagement_token


if __name__ == "__main__":  # pragma: no cover
    main()
