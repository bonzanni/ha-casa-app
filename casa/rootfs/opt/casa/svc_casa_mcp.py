# casa/rootfs/opt/casa/svc_casa_mcp.py
"""Standalone MCP HTTP service — Phase 3.6 of the Casa engagement runtime.

Listens on 127.0.0.1:8100 and forwards every tool call and hook decision
to casa-main over a Unix domain socket at /run/casa/internal.sock.

Why a separate process: it survives casa-main restarts. An engagement
subprocess's MCP TCP connection to 8100 stays open across casa-main
respawns; mid-restart tool calls return -32000 casa_temporarily_unavailable
(a clean recoverable error the model can retry) instead of a connection
drop (which the CC MCP HTTP client surfaces as a fatal handshake failure
on the next request).

Lifecycle (s6-rc-supervised):
- Bring up HTTP listener on 127.0.0.1:8100 immediately (no wait for casa-main).
- Per request: connect to /run/casa/internal.sock, POST the translated body,
  return the response. Reconnect every call (no pooling) — keeps state
  trivial and the per-call cost is negligible (Unix socket on the same host).
- Cold boot: casa-main and svc-casa-mcp start in parallel. Tool calls that
  arrive before casa-main's socket exists return casa_temporarily_unavailable.
  No retry loop in the data path — the model handles retry semantics.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

# Ensure casa rootfs is on sys.path regardless of cwd.
_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mcp_envelope import (
    PROTOCOL_VERSION,
    VERSION,
    _jsonrpc_error,
    _jsonrpc_ok,
    _tool_schema,
)

logger = logging.getLogger("svc_casa_mcp")

INTERNAL_SOCKET_PATH = "/run/casa/internal.sock"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8100


# ---------------------------------------------------------------------------
# Forwarder — POST a body to the casa-main Unix socket
# ---------------------------------------------------------------------------


ForwardCallable = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]


async def _forward_to_internal(
    *,
    path: str,                  # "/internal/tools/call" or "/internal/hooks/resolve"
    body: dict[str, Any],
    socket_path: str = INTERNAL_SOCKET_PATH,
    timeout_s: float | None = 180.0,
) -> tuple[int, dict[str, Any]]:
    """POST `body` to the casa-main internal handler over Unix socket.

    Returns (http_status, response_body_dict). Raises aiohttp.ClientConnectorError
    if the socket is missing or the connection is refused — the caller maps
    that to the appropriate user-facing error (casa_temporarily_unavailable
    for tools/call, fail-closed deny for hooks/resolve).

    The 180s default bounds model-driven tools/call forwarding — long
    enough for server-side LLM work (query_engager = Hindsight recall +
    ClaudeSDKClient one-shot synthesis; emit_completion = Telegram topic
    teardown + s6 stop + two classify_tier one-shots) yet finite. casa-main
    downness is detected instantly via ClientConnectorError on the
    missing/refused socket, not by this timeout, so restart-resilience is
    unchanged.

    Pass ``timeout_s=None`` to disable the client-side total timeout — the
    /hooks/resolve route does this so casa-main's policy-driven timeout
    (declared in ``hooks.yaml`` per policy, e.g. 600s for
    ``engagement_permission_relay``) is the only effective gate. A hardcoded
    short timeout here truncates human-in-the-loop relays.
    """
    connector = aiohttp.UnixConnector(path=socket_path)
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout,
    ) as sess:
        async with sess.post(f"http://unix{path}", json=body) as resp:
            data = await resp.json()
            return resp.status, data


# ---------------------------------------------------------------------------
# /mcp/casa-framework — JSON-RPC 2.0 handler
# ---------------------------------------------------------------------------


def _build_mcp_handler(
    *,
    tools: list,
    forward_to_internal: ForwardCallable,
):
    """Build the aiohttp POST handler for /mcp/casa-framework.

    `tools` is the iterable of SdkMcpTool-shaped objects (used to build
    the static tools/list response at boot).
    `forward_to_internal` is the Unix-socket forwarder; tests inject a
    mock.
    """
    tool_schemas = [_tool_schema(t) for t in tools]

    async def handler(request: web.Request) -> web.Response:
        try:
            msg = await request.json()
        except Exception:
            return _jsonrpc_error(None, -32700, "Parse error")

        if not isinstance(msg, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request")

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "notifications/initialized":
            return web.Response(status=202)

        if method == "initialize":
            return _jsonrpc_ok(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "casa-framework", "version": VERSION},
            })

        if method == "tools/list":
            return _jsonrpc_ok(req_id, {"tools": tool_schemas})

        if method == "ping":
            return _jsonrpc_ok(req_id, {})

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            eng_id = request.headers.get("X-Casa-Engagement-Id")

            inner_body = {
                "name": name,
                "arguments": arguments,
                "engagement_id": eng_id,
                # #335: the per-engagement secret provisioned into the
                # workspace .mcp.json alongside the id — casa-main verifies
                # it against the record before binding any authority.
                "engagement_token": request.headers.get(
                    "X-Casa-Engagement-Token"),
            }
            try:
                _status, result = await forward_to_internal(
                    path="/internal/tools/call",
                    body=inner_body,
                )
            except aiohttp.ClientConnectorError as exc:
                logger.warning(
                    "svc_casa_mcp: casa-main socket unreachable: %s", exc,
                )
                return _jsonrpc_error(
                    req_id, -32000,
                    "casa_temporarily_unavailable: "
                    "casa-main internal socket unreachable",
                )
            except aiohttp.ClientError as exc:
                logger.warning(
                    "svc_casa_mcp: casa-main forwarding error: %s", exc,
                )
                return _jsonrpc_error(
                    req_id, -32000,
                    f"casa_temporarily_unavailable: {exc}",
                )
            except asyncio.TimeoutError:
                return _jsonrpc_error(
                    req_id, -32000,
                    "casa_temporarily_unavailable: forwarding timeout",
                )

            # Internal handler returns either {"content": [...]} (success) or
            # {"error": {"code": ..., "message": ...}} (handler-side error).
            if isinstance(result, dict) and "error" in result:
                err = result["error"]
                return _jsonrpc_error(req_id, err["code"], err["message"])
            return _jsonrpc_ok(req_id, result)

        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    return handler


# ---------------------------------------------------------------------------
# /hooks/resolve — pass-through forwarder (CC's hook protocol is body-only)
# ---------------------------------------------------------------------------


def _build_hooks_handler(*, forward_to_internal: ForwardCallable):
    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        "svc_casa_mcp /hooks/resolve: malformed JSON",
                }},
            )

        # #366: the engagement credential rides the same header pair the MCP
        # endpoint uses (hook_proxy.sh reads it from the caller's own
        # workspace .mcp.json). The forwarded body is REBUILT here — never
        # passed through — so a caller cannot smuggle identity fields in the
        # body past the header contract.
        if isinstance(body, dict):
            body = {
                "policy": body.get("policy"),
                "payload": body.get("payload"),
                "engagement_id": request.headers.get("X-Casa-Engagement-Id"),
                "engagement_token": request.headers.get(
                    "X-Casa-Engagement-Token"),
            }

        try:
            # timeout_s=None: defer to casa-main's policy-driven timeout
            # (e.g. engagement_permission_relay declares 600s in hooks.yaml).
            # A short forwarder timeout here truncates human-in-the-loop
            # permission relays and lands the engagement in an inconsistent
            # state (verdict arrives later with no waiter).
            _status, result = await forward_to_internal(
                path="/internal/hooks/resolve",
                body=body,
                timeout_s=None,
            )
        except aiohttp.ClientConnectorError as exc:
            logger.warning(
                "svc_casa_mcp /hooks/resolve: casa-main unreachable: %s", exc,
            )
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        "Permission relay unavailable: casa-main internal "
                        "socket is down. The tool was not run. Retry shortly "
                        "or check addon logs.",
                }},
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(
                "svc_casa_mcp /hooks/resolve: forwarding error: %s", exc,
            )
            reason = (
                f"Permission relay failed: forwarder error talking to "
                f"casa-main ({type(exc).__name__}: {exc or 'no detail'}). "
                f"The tool was not run."
            )
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }},
            )

        return web.json_response(result)

    return handler


# ---------------------------------------------------------------------------
# /internal/channel/* — forward the per-engagement channel MCP server's
# outbound calls to casa-main over the Unix socket (Containment Stage 2,
# Task 9).
#
# The channel server (``channels/casa_engagement_channel.py``) runs as a
# child of the uid-dropped CLI subprocess and therefore cannot reach the
# 0600 root-owned Unix socket directly. It instead talks TCP to THIS
# already-token-authed 127.0.0.1:8100 listener, which proxies on its
# behalf. The engagement's own token still authenticates at the far end —
# it rides in the JSON body (not a header, unlike /mcp/casa-framework and
# /hooks/resolve) exactly as it always has, and the ``/internal/channel/*``
# handlers (#335) verify it against the engagement record before acting.
#
# SECURITY: this is an explicit ALLOWLIST of exact paths — never a
# wildcard/prefix match. `/admin/reload` and the personality-admin routes
# are Unix-socket-only and are deliberately never registered here; a POST
# to `/admin/reload` (or any path not in this dict) on 8100 falls through
# to aiohttp's own 404, exactly as if the route never existed.
# ---------------------------------------------------------------------------


# Per-tail forwarder timeout. `ask` must NOT inherit the 180s tools/call
# default: the broker waits up to `_ASK_MAX_TIMEOUT_S` (570s) for an
# operator tap, and the channel server's own aiohttp client timeout for
# `ask` is `clamped_timeout + _ASK_CLIENT_TIMEOUT_PAD_S` (up to 585s) — see
# `channels/casa_engagement_channel.py`. A bounded proxy-side timeout here
# would race that wait exactly as `/hooks/resolve`'s `timeout_s=None`
# comment describes, so `ask` gets the same treatment: no client-side total
# timeout, deferring entirely to the caller's own deadline. The other
# channel calls (send_to_topic, post_inline_keyboard, update_state,
# ask_cancel) are ordinary bounded operator-topic writes, so they keep the
# forwarder's default.
CHANNEL_FORWARD_TIMEOUTS: dict[str, float | None] = {
    "send_to_topic": 180.0,
    "post_inline_keyboard": 180.0,
    "update_state": 180.0,
    "ask": None,
    "ask_cancel": 180.0,
}


def _build_channel_forward_handler(
    *, tail: str, forward_to_internal: ForwardCallable,
):
    """Build the aiohttp POST handler forwarding ``/internal/channel/{tail}``
    to casa-main's Unix socket, unchanged apart from transport."""
    internal_path = f"/internal/channel/{tail}"
    timeout_s = CHANNEL_FORWARD_TIMEOUTS[tail]

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "bad_json"}, status=400,
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"ok": False, "error": "bad_json"}, status=400,
            )

        try:
            status, result = await forward_to_internal(
                path=internal_path, body=body, timeout_s=timeout_s,
            )
        except aiohttp.ClientConnectorError as exc:
            logger.warning(
                "svc_casa_mcp %s: casa-main socket unreachable: %s",
                internal_path, exc,
            )
            return web.json_response(
                {"ok": False, "error": "casa_temporarily_unavailable"},
                status=503,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(
                "svc_casa_mcp %s: forwarding error: %s", internal_path, exc,
            )
            return web.json_response(
                {"ok": False, "error": f"forwarding_error: {exc}"},
                status=502,
            )

        return web.json_response(result, status=status)

    return handler


# ---------------------------------------------------------------------------
# GET /mcp/casa-framework -> 405
# ---------------------------------------------------------------------------


async def _mcp_get_405(_request: web.Request) -> web.Response:
    return web.Response(
        status=405, text="Method Not Allowed\n",
        headers={"Allow": "POST"},
    )


# ---------------------------------------------------------------------------
# App factory + main entry point
# ---------------------------------------------------------------------------


def _build_app(
    *,
    tools: list,
    forward_to_internal: ForwardCallable | None = None,
) -> web.Application:
    """Build the svc-casa-mcp aiohttp app. Tests pass a mock forwarder."""
    fwd = forward_to_internal or _forward_to_internal
    app = web.Application()
    app.router.add_post(
        "/mcp/casa-framework",
        _build_mcp_handler(tools=tools, forward_to_internal=fwd),
    )
    app.router.add_get("/mcp/casa-framework", _mcp_get_405)
    app.router.add_post(
        "/hooks/resolve",
        _build_hooks_handler(forward_to_internal=fwd),
    )
    # Containment Stage 2 (Task 9): explicit allowlist, one route per exact
    # channel-family path. Never a wildcard — `/admin/reload` and
    # personality-admin stay Unix-socket-only and are never registered here.
    for tail in CHANNEL_FORWARD_TIMEOUTS:
        app.router.add_post(
            f"/internal/channel/{tail}",
            _build_channel_forward_handler(tail=tail, forward_to_internal=fwd),
        )
    return app


def _load_static_tools_snapshot() -> list:
    """Import casa's tool registry for the tools/list snapshot.

    Imported lazily so tests can run without the full casa module graph.
    """
    from tools import CASA_TOOLS
    return list(CASA_TOOLS)


async def _main_async() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s svc_casa_mcp %(message)s",
    )
    tools = _load_static_tools_snapshot()
    logger.info(
        "svc_casa_mcp starting — %d tools snapshotted, listening on %s:%d",
        len(tools), LISTEN_HOST, LISTEN_PORT,
    )
    app = _build_app(tools=tools)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT)
    await site.start()
    logger.info("svc_casa_mcp ready")
    # Run until killed.
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> int:
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
