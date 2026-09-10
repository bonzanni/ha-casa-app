# tests/test_svc_casa_mcp.py
"""Unit tests for svc_casa_mcp.py — the standalone MCP service.

Strategy: import the module's helpers (envelope dispatch, forwarder)
and test each independently. No real Unix socket — we mock the
aiohttp ClientSession to assert request shape and inject responses.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.unit,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="svc_casa_mcp tests use UnixConnector path (Linux only)",
    ),
]


class _DummyTool:
    name = "ok"
    description = "Test"
    input_schema = {"x": int}
    handler = None


def _make_svc_app(tools: list, forward_call) -> web.Application:
    """Build the svc app with a mocked Unix-socket forwarder."""
    from svc_casa_mcp import _build_app
    return _build_app(tools=tools, forward_to_internal=forward_call)


async def test_svc_initialize() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[_DummyTool()], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
        })
        body = await resp.json()
        assert body["result"]["serverInfo"]["name"] == "casa-framework"
        assert body["result"]["protocolVersion"] == "2025-06-18"
        # Initialize doesn't touch the forwarder.
        fwd.assert_not_called()


async def test_svc_tools_list_uses_static_snapshot() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[_DummyTool()], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        })
        body = await resp.json()
        names = [t["name"] for t in body["result"]["tools"]]
        assert names == ["ok"]
        fwd.assert_not_called()


async def test_svc_notifications_initialized_returns_202() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        assert resp.status == 202
        fwd.assert_not_called()


async def test_svc_ping_returns_empty_result() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 99, "method": "ping",
        })
        body = await resp.json()
        assert body == {"jsonrpc": "2.0", "id": 99, "result": {}}


async def test_svc_unknown_method_returns_jsonrpc_error() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 7, "method": "no_such",
        })
        body = await resp.json()
        assert body["error"]["code"] == -32601


async def test_svc_tools_call_forwards_to_internal_with_engagement_id() -> None:
    fwd = AsyncMock(return_value=(
        200, {"content": [{"type": "text", "text": "ok"}]},
    ))
    app = _make_svc_app(tools=[_DummyTool()], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "ok", "arguments": {"x": 1}},
            },
            headers={"X-Casa-Engagement-Id": "eng-9",
                     "X-Casa-Engagement-Token": "tok-eng-9"},
        )
        body = await resp.json()
        assert body["result"] == {"content": [{"type": "text", "text": "ok"}]}
        # forward_call called with internal-shape body (#335: the engagement
        # auth token header rides along as body key engagement_token).
        call_args = fwd.call_args
        assert call_args.kwargs["path"] == "/internal/tools/call"
        assert call_args.kwargs["body"] == {
            "name": "ok",
            "arguments": {"x": 1},
            "engagement_id": "eng-9",
            "engagement_token": "tok-eng-9",
        }


async def test_svc_tools_call_internal_returns_error_object_passes_through_as_jsonrpc_error() -> None:
    fwd = AsyncMock(return_value=(
        200, {"error": {"code": -32001, "message": "Tool 'ok' raised: boom"}},
    ))
    app = _make_svc_app(tools=[_DummyTool()], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "ok", "arguments": {}},
            },
        )
        body = await resp.json()
        assert body["error"]["code"] == -32001
        assert "boom" in body["error"]["message"]


async def test_svc_tools_call_socket_unreachable_returns_casa_unavailable() -> None:
    """ClientConnectorError -> the exact -32000 answer, on the response.

    #903: this asserted `code == -32000` and `"casa" in message.lower()`, which
    survives almost any rewording of the message. The only byte-exact check on
    this literal is a SOURCE-TEXT cross-check in
    `tests/test_mcp_restart_survival.py` — it greps this module and the
    container probe's shell script for the same string so the probe cannot
    drift from the bridge, and it asserts nothing about any response. So the
    answer a caller actually receives was unpinned. It is pinned here, on the
    decoded error object, which is what the model sees.

    The whole object is compared rather than its fields one at a time: an extra
    key appearing in the error is a change to the answer too.
    """
    import aiohttp

    async def _fwd_raises(**_):
        # Simulate Unix socket missing / refused.
        raise aiohttp.ClientConnectorError(
            connection_key=MagicMock(),
            os_error=ConnectionRefusedError("simulated"),
        )
    app = _make_svc_app(tools=[_DummyTool()], forward_call=_fwd_raises)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "ok", "arguments": {}},
            },
        )
        body = await resp.json()
        assert body["error"] == {
            "code": -32000,
            "message": ("casa_temporarily_unavailable: "
                        "casa-main internal socket unreachable"),
        }


async def test_svc_hooks_resolve_forwards_body_to_internal() -> None:
    fwd = AsyncMock(return_value=(
        200, {"hookSpecificOutput": {"permissionDecision": "allow"}},
    ))
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "allow_all", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
        # forward_call received the policy/payload as-is; with no identity
        # headers the credential fields ride along as None (#366, mirroring
        # the tools/call contract).
        call_args = fwd.call_args
        assert call_args.kwargs["path"] == "/internal/hooks/resolve"
        assert call_args.kwargs["body"] == {
            "policy": "allow_all", "payload": {"tool_name": "Bash"},
            "engagement_id": None, "engagement_token": None,
        }


async def test_svc_hooks_resolve_injects_engagement_headers_into_body() -> None:
    """#366: the engagement credential provisioned into the workspace
    .mcp.json arrives as headers (same pair the MCP endpoint uses) and must
    ride to casa-main as body fields — the internal handler authenticates the
    id claim against the record before any identity-consuming callback runs."""
    fwd = AsyncMock(return_value=(200, {}))
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "p", "payload": {"tool_name": "Bash"}},
            headers={"X-Casa-Engagement-Id": "e" * 32,
                     "X-Casa-Engagement-Token": "tok-abc"},
        )
        assert resp.status == 200
        call_args = fwd.call_args
        assert call_args.kwargs["body"] == {
            "policy": "p", "payload": {"tool_name": "Bash"},
            "engagement_id": "e" * 32,
            "engagement_token": "tok-abc",
        }


async def test_svc_hooks_resolve_body_identity_cannot_bypass_headers() -> None:
    """#366: identity comes from the HEADERS this service reads — a body that
    arrives already carrying engagement_id/engagement_token (a forger POSTing
    the internal shape at the public route) is overwritten, never trusted."""
    fwd = AsyncMock(return_value=(200, {}))
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/hooks/resolve",
            json={"policy": "p", "payload": {"tool_name": "Bash"},
                  "engagement_id": "f" * 32,
                  "engagement_token": "forged"},
        )
        call_args = fwd.call_args
        assert call_args.kwargs["body"]["engagement_id"] is None
        assert call_args.kwargs["body"]["engagement_token"] is None


async def test_svc_hooks_resolve_socket_unreachable_fails_closed() -> None:
    """Hook fail-closed on transport error: deny with actionable reason (F-1)."""
    import aiohttp

    async def _fwd_raises(**_):
        raise aiohttp.ClientConnectorError(
            connection_key=MagicMock(),
            os_error=ConnectionRefusedError("simulated"),
        )
    app = _make_svc_app(tools=[], forward_call=_fwd_raises)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "anything", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        reason = body["hookSpecificOutput"]["permissionDecisionReason"]
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        # F-1: reason must signal failure unambiguously so the model
        # doesn't narrate hook errors as success.
        assert "permission relay" in reason.lower()
        assert "tool was not run" in reason.lower()
        assert "casa" in reason.lower()


async def test_svc_hooks_resolve_forwards_with_no_client_timeout() -> None:
    """E-1: /hooks/resolve must defer to casa-main's policy-driven timeout.

    The svc-layer forwarder must call ``forward_to_internal`` with
    ``timeout_s=None`` so a slow operator-in-the-loop relay (e.g.
    ``engagement_permission_relay`` with policy ``timeout: 600``) is not
    truncated by a hardcoded 10s client-side timeout.
    """
    captured: dict = {}

    async def _fwd(**kwargs):
        captured.update(kwargs)
        return 200, {"hookSpecificOutput": {"permissionDecision": "allow"}}
    app = _make_svc_app(tools=[], forward_call=_fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={
                "policy": "engagement_permission_relay",
                "payload": {"tool_name": "Bash"},
            },
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
    # E-1 contract: forwarder timeout disabled in the hooks path.
    assert "timeout_s" in captured, (
        "/hooks/resolve must pass timeout_s explicitly to defer to casa-main"
    )
    assert captured["timeout_s"] is None, (
        f"E-1 regression: expected timeout_s=None, got {captured['timeout_s']!r}"
    )


async def test_svc_hooks_resolve_slow_forwarder_does_not_time_out() -> None:
    """E-1: a forwarder that takes longer than the old 10s default must
    still complete successfully — the policy-driven timeout on casa-main
    is the only effective gate."""
    import asyncio
    started = asyncio.get_event_loop().time()

    async def _slow_fwd(**_):
        # 0.2s — well under any reasonable test timeout, but proves we
        # don't depend on the legacy 10s default. The contract assertion
        # (timeout_s=None) is in the companion test above.
        await asyncio.sleep(0.2)
        return 200, {"hookSpecificOutput": {"permissionDecision": "allow"}}
    app = _make_svc_app(tools=[], forward_call=_slow_fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "x", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
    elapsed = asyncio.get_event_loop().time() - started
    assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert elapsed >= 0.2  # forwarder actually slept; not bypassed


async def test_svc_hooks_resolve_forwarder_error_reason_is_actionable() -> None:
    """F-1: forwarder error (timeout / ClientError) must produce an
    actionable deny reason — the previous empty 'hook forward error: '
    confused the model into narrating success.

    NOTE: with E-1 (timeout_s=None) shipped, the asyncio.TimeoutError
    branch will only fire if casa-main itself returns/closes within its
    own deadline; this test still pins the message format for any
    aiohttp.ClientError that might surface (e.g. payload, response,
    connection-reset).
    """
    import aiohttp

    async def _fwd_raises(**_):
        raise aiohttp.ClientPayloadError("simulated payload error")
    app = _make_svc_app(tools=[], forward_call=_fwd_raises)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "anything", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        reason = body["hookSpecificOutput"]["permissionDecisionReason"]
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "permission relay failed" in reason.lower()
        assert "tool was not run" in reason.lower()
        # Exception class name MUST leak for operator debugging — the old
        # path produced an empty "hook forward error: " on TimeoutError.
        assert "ClientPayloadError" in reason


async def test_svc_tools_call_uses_default_forwarder_timeout() -> None:
    """E-1 scope guard: the tools/call route must NOT inherit the hooks
    path's timeout_s=None. It relies on _forward_to_internal's own
    default instead of an explicit override.

    L72/L21: that default must be generous enough to comfortably exceed
    the slowest legitimate tools/call handler (query_engager's Hindsight
    recall + ClaudeSDKClient one-shot synthesis; emit_completion's
    Telegram teardown + two classify_tier one-shots) — a sub-minute cap
    (the old 10s) produces spurious casa_temporarily_unavailable errors
    while the server-side call is still succeeding."""
    captured: dict = {}

    async def _fwd(**kwargs):
        captured.update(kwargs)
        return 200, {"content": [{"type": "text", "text": "ok"}]}
    app = _make_svc_app(tools=[_DummyTool()], forward_call=_fwd)
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "ok", "arguments": {}},
            },
        )
    # tools/call must NOT explicitly set timeout_s (defer to the
    # forwarder's default).
    assert "timeout_s" not in captured, (
        "tools/call should rely on the default forwarder timeout, "
        f"got explicit timeout_s={captured.get('timeout_s')!r}"
    )

    import inspect

    from svc_casa_mcp import _forward_to_internal
    default = inspect.signature(_forward_to_internal).parameters["timeout_s"].default
    assert default is not None and default >= 60, (
        "the forwarder's default timeout_s must comfortably exceed "
        "worst-case server-side tool latency (SDK one-shot + Hindsight "
        f"recall); got {default!r}"
    )


async def test_svc_get_returns_405() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/mcp/casa-framework")
        assert resp.status == 405


# ---------------------------------------------------------------------------
# /internal/channel/* forwarding — Containment Stage 2 (Task 9)
# ---------------------------------------------------------------------------


async def test_svc_forwards_channel_send_to_topic() -> None:
    fwd = AsyncMock(return_value=(200, {"ok": True}))
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"text": "hi", "engagement_id": "eng-1",
                  "engagement_token": "tok-1"},
        )
        body = await resp.json()
        assert resp.status == 200
        assert body == {"ok": True}
        call_args = fwd.call_args
        assert call_args.kwargs["path"] == "/internal/channel/send_to_topic"
        assert call_args.kwargs["body"] == {
            "text": "hi", "engagement_id": "eng-1",
            "engagement_token": "tok-1",
        }


async def test_svc_forwards_channel_ask_and_ask_cancel() -> None:
    fwd = AsyncMock(return_value=(200, {"ok": True, "outcome": "no_answer"}))
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/ask",
            json={"question": "Q?", "options": ["A", "B"],
                  "engagement_id": "eng-1", "engagement_token": "tok-1"},
        )
        assert resp.status == 200
        resp2 = await client.post(
            "/internal/channel/ask_cancel",
            json={"request_id": "r1", "engagement_id": "eng-1",
                  "engagement_token": "tok-1"},
        )
        assert resp2.status == 200
    paths = [c.kwargs["path"] for c in fwd.call_args_list]
    assert paths == ["/internal/channel/ask", "/internal/channel/ask_cancel"]


async def test_svc_channel_ask_forward_uses_no_client_timeout() -> None:
    """`ask` must not inherit the 180s tools/call default — the operator
    broker can wait up to ~570s for a tap, plus the channel server's own
    client-side pad, so a bounded proxy timeout here would race that wait."""
    captured: dict = {}

    async def _fwd(**kwargs):
        captured.update(kwargs)
        return 200, {"ok": True, "outcome": "no_answer"}
    app = _make_svc_app(tools=[], forward_call=_fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/ask",
            json={"question": "Q?", "options": ["A", "B"]},
        )
        assert resp.status == 200
    assert captured["timeout_s"] is None, (
        f"ask forward must pass timeout_s=None, got {captured['timeout_s']!r}"
    )


async def test_svc_channel_send_to_topic_uses_bounded_default_timeout() -> None:
    """The non-`ask` channel routes are ordinary bounded writes and should
    NOT silently inherit ask's unbounded timeout."""
    captured: dict = {}

    async def _fwd(**kwargs):
        captured.update(kwargs)
        return 200, {"ok": True}
    app = _make_svc_app(tools=[], forward_call=_fwd)
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/internal/channel/send_to_topic", json={"text": "hi"},
        )
    assert captured["timeout_s"] is not None
    assert captured["timeout_s"] >= 60


async def test_svc_admin_reload_not_reachable_on_8100() -> None:
    """SECURITY: /admin/reload must never be registered on the 8100
    surface — it stays Unix-socket-only, unauthenticated-reload-reachable
    only from root. A POST here must 404, never reach the forwarder."""
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/admin/reload", json={})
        assert resp.status == 404
        fwd.assert_not_called()


async def test_svc_admin_personality_not_reachable_on_8100() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/admin/personality/inspect", json={})
        assert resp.status == 404
        fwd.assert_not_called()


async def test_svc_channel_forward_socket_unreachable_returns_503() -> None:
    """The third face of the same condition, asserted on its body.

    #903: this checked the status and `ok is False` only, so the error string a
    resumed engagement's channel server reads back could be reworded without a
    test going red — and this face is the one the filing missed entirely. The
    whole decoded body is compared, and the status with it: 503 without the
    named error, or the named error at some other status, is a different answer.
    """
    import aiohttp

    async def _fwd_raises(**_):
        raise aiohttp.ClientConnectorError(
            connection_key=MagicMock(),
            os_error=ConnectionRefusedError("simulated"),
        )
    app = _make_svc_app(tools=[], forward_call=_fwd_raises)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic", json={"text": "hi"},
        )
        assert resp.status == 503
        assert await resp.json() == {
            "ok": False, "error": "casa_temporarily_unavailable",
        }


# #908 / INV-MCP-011. `scripts/hook_proxy.sh` calls this route with `curl -sf`
# and converts ANY non-2xx into `{"decision": "allow"}` — a deliberate
# fail-open, so a hook cannot block an engagement when Casa is unreachable.
# Every refusal the route produces itself therefore has to ride on an HTTP 200,
# or it stops being a refusal. Nothing asserted that status before: a change
# answering `400` here would leave the deny sitting in the handler, correct
# looking, while the shim turned it into a permission GRANT.
#
# Each row is (label, request body, forwarder, expected reason, expected
# (call_count, await_count)). The counts are asserted rather than a "was it
# called" boolean because the malformed arm's whole point is that the body
# reaches casa-main NEVER.
def _hook_refusal_cases():
    import asyncio as _asyncio

    import aiohttp as _aiohttp

    valid = {"policy": "anything", "payload": {"tool_name": "Bash"}}
    return [
        (
            "malformed",
            b"{",
            AsyncMock(),
            "svc_casa_mcp /hooks/resolve: malformed JSON",
            (0, 0),
        ),
        (
            "socket-unreachable",
            json.dumps(valid).encode(),
            AsyncMock(side_effect=_aiohttp.ClientConnectorError(
                connection_key=MagicMock(),
                os_error=ConnectionRefusedError("simulated"),
            )),
            "Permission relay unavailable: casa-main internal socket is down. "
            "The tool was not run. Retry shortly or check addon logs.",
            (1, 1),
        ),
        (
            "client-error",
            json.dumps(valid).encode(),
            AsyncMock(side_effect=_aiohttp.ClientPayloadError(
                "simulated payload error")),
            "Permission relay failed: forwarder error talking to casa-main "
            "(ClientPayloadError: simulated payload error). The tool was not "
            "run.",
            (1, 1),
        ),
        (
            "timeout",
            json.dumps(valid).encode(),
            AsyncMock(side_effect=_asyncio.TimeoutError()),
            "Permission relay failed: forwarder error talking to casa-main "
            "(TimeoutError: ). The tool was not run.",
            (1, 1),
        ),
    ]


@pytest.mark.parametrize(
    "label,payload,fwd,reason,counts",
    _hook_refusal_cases(),
    ids=[case[0] for case in _hook_refusal_cases()],
)
async def test_svc_hooks_resolve_own_refusals_are_http_200(
    label, payload, fwd, reason, counts,
) -> None:
    """INV-MCP-011: pin bridge-owned refusal status, body and forward counts.

    Resolve the registered POST route without a TCP listener. Request JSON
    parsing and response construction are real; only the forwarder is mocked.

    This pins existing behavior; its behavioral red is a mutation.
    The route's fourth arm — any other exception escaping the forwarder, #912 —
    is pinned by
    ``test_svc_hooks_resolve_unexpected_forwarder_exception_denies`` below,
    which asserts an ERROR record and a traceback this table does not read.
    """
    import asyncio

    import aiohttp
    from aiohttp.test_utils import make_mocked_request

    app = _make_svc_app(tools=[], forward_call=fwd)
    stream = aiohttp.streams.StreamReader(
        MagicMock(), 2 ** 16, loop=asyncio.get_running_loop(),
    )
    stream.feed_data(payload)
    stream.feed_eof()
    request = make_mocked_request(
        "POST",
        "/hooks/resolve",
        payload=stream,
        app=app,
        headers={"Content-Type": "application/json"},
    )

    match = await app.router.resolve(request)
    resp = await match.handler(request)

    assert resp.status == 200
    assert json.loads(resp.body) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    assert (fwd.call_count, fwd.await_count) == counts


# ---------------------------------------------------------------------------
# #912 — an unexpected exception escaping the forwarder must not become a
# permission GRANT. Specified by **astra** in the drive red-case round.
# ---------------------------------------------------------------------------


class _BridgeFault(Exception):
    """A direct Exception subclass with no aiohttp ancestry.

    Structural fault injection, NOT a claim that any shipped path raises it:
    it stands for the class of programming errors a future edit can introduce
    between the forwarder call and its return.
    """


async def _real_aiohttp_decode_error(body: bytes):
    """Return the exception aiohttp 3.x's OWN decoder raises for `body`.

    The far end's bytes are decoded by ``ClientResponse.json``, whose final
    step is ``loads(stripped.decode(encoding))`` — so a body advertised as
    ``application/json`` raises ``UnicodeDecodeError`` from ``.decode()`` or
    ``json.JSONDecodeError`` from ``loads()``. Neither is an
    ``aiohttp.ClientError``.

    aiohttp's real implementation is executed here against a minimal response
    stand-in rather than re-derived, so the exception instance handed to the
    route is the one the shipped decode path at ``svc_casa_mcp.py``'s
    ``await resp.json()`` would produce. No socket, no session, no listener.
    """
    from types import SimpleNamespace

    from aiohttp import ClientResponse

    async def _read():
        raise AssertionError("body is preloaded; read() must not be reached")

    resp = SimpleNamespace(
        _body=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        status=200,
        history=(),
        request_info=None,
        get_encoding=lambda: "utf-8",
        read=_read,
    )
    try:
        await ClientResponse.json(resp)
    except Exception as exc:      # noqa: BLE001 — the exception IS the fixture
        return exc
    raise AssertionError(f"aiohttp decoded {body!r} without raising")


async def _unexpected_forwarder_cases():
    """(label, exception instance) for every class that escapes at the base."""
    return [
        ("json-decode", await _real_aiohttp_decode_error(b"{")),
        ("unicode-decode", await _real_aiohttp_decode_error(b'"\xff"')),
        ("runtime-error", RuntimeError("boom")),
        ("bridge-fault", _BridgeFault("boom")),
    ]


def _json_post_request(app, path: str, body: dict):
    """A JSON POST to `path`, resolved through the app's OWN router.

    No listening socket is opened — the sandbox the reviewers run in denies
    one, and a status read back through a listener is not available here by
    construction.
    """
    import asyncio

    import aiohttp
    from aiohttp.test_utils import make_mocked_request

    stream = aiohttp.streams.StreamReader(
        MagicMock(), 2 ** 16, loop=asyncio.get_running_loop(),
    )
    stream.feed_data(json.dumps(body).encode())
    stream.feed_eof()
    return make_mocked_request(
        "POST",
        path,
        payload=stream,
        app=app,
        headers={"Content-Type": "application/json"},
    )


def _hooks_request(app, body: dict):
    """A /hooks/resolve request, resolved in-process. See above."""
    return _json_post_request(app, "/hooks/resolve", body)


_HOOK_REQUEST_BODY = {
    "policy": "engagement_permission_relay",
    "payload": {"tool_name": "Bash", "tool_input": {"command": "true"}},
}


async def test_svc_hooks_resolve_unexpected_forwarder_exception_denies(
    caplog,
) -> None:
    """#912: an ordinary exception escaping the forwarder is answered as a
    200 `deny`, never allowed to escape.

    At the base the route catches only ``aiohttp.ClientConnectorError`` and
    ``(aiohttp.ClientError, asyncio.TimeoutError)``. Anything else escapes the
    coroutine; aiohttp renders it as its own HTTP 500; and
    ``scripts/hook_proxy.sh``'s ``curl -sf ... || { echo allow; }`` converts
    every non-2xx into the byte-identical fail-open ALLOW it emits when Casa
    is unreachable. A bridge defect therefore becomes a permission GRANT.

    Pre-fix red, measured: 4 escapes / 0 responses — the four exceptions leave
    ``match.handler(request)``. The 500 itself is NOT observed here and cannot
    be: it is rendered one layer out by aiohttp, and a red case may not open a
    listening socket. The escape is the assertion.

    Two of the four instances come from aiohttp's own ``ClientResponse.json``
    executed on real far-end bytes; the other two are structural fault
    injection standing for programming errors, not claims about shipped paths.

    Specified by **astra** in the drive red-case round.
    """
    import logging

    escaped: list[BaseException] = []
    responses: list = []
    calls: list[tuple[int, int]] = []
    logged: list[tuple] = []

    cases = await _unexpected_forwarder_cases()
    for label, exc in cases:
        fwd = AsyncMock(side_effect=exc)
        app = _make_svc_app(tools=[], forward_call=fwd)
        request = _hooks_request(app, _HOOK_REQUEST_BODY)
        match = await app.router.resolve(request)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="svc_casa_mcp"):
            try:
                responses.append((label, await match.handler(request)))
            except BaseException as raised:      # noqa: BLE001
                escaped.append(raised)
        calls.append((fwd.call_count, fwd.await_count))
        tracebacks = [
            rec for rec in caplog.records
            if rec.levelno == logging.ERROR and rec.exc_info is not None
        ]
        logged.append(
            (len(tracebacks), tracebacks[0].name if tracebacks else None))

    # Counts, not statuses: the forwarder ran exactly once per case (so this
    # is the post-forward arm, not the malformed-body arm), nothing escaped,
    # and each case produced exactly one ERROR record carrying a traceback.
    assert (len(escaped), len(responses), calls) == (
        0, 4, [(1, 1), (1, 1), (1, 1), (1, 1)],
    ), [type(e).__name__ for e in escaped]
    assert logged == [(1, "svc_casa_mcp")] * 4, logged

    for (label, exc), (rlabel, resp) in zip(cases, responses):
        assert rlabel == label
        assert resp.status == 200
        assert json.loads(resp.body) == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Permission relay failed: unexpected bridge error "
                    f"({type(exc).__name__}). The tool was not run. "
                    "Check addon logs."
                ),
            },
        }, label


async def test_svc_hooks_resolve_unexpected_arm_forwards_header_identity_only(
) -> None:
    """#912's arm must not disturb what reaches casa-main (#366, E-1).

    The forwarded call is still one call to ``/internal/hooks/resolve`` with a
    body REBUILT from request headers — the caller's own body credentials are
    dropped — and still carries ``timeout_s=None`` so a human-in-the-loop
    relay is not truncated.
    """
    fwd = AsyncMock(side_effect=RuntimeError("boom"))
    app = _make_svc_app(tools=[], forward_call=fwd)
    request = _hooks_request(app, {
        **_HOOK_REQUEST_BODY,
        "engagement_id": "SPOOFED",
        "engagement_token": "SPOOFED",
    })
    match = await app.router.resolve(request)
    resp = await match.handler(request)

    assert resp.status == 200
    assert (fwd.call_count, fwd.await_count, len(fwd.call_args.args)) == (1, 1, 0)
    assert fwd.call_args.kwargs == {
        "path": "/internal/hooks/resolve",
        "body": {
            "policy": "engagement_permission_relay",
            "payload": {"tool_name": "Bash", "tool_input": {"command": "true"}},
            "engagement_id": None,
            "engagement_token": None,
        },
        "timeout_s": None,
    }


async def test_svc_hooks_resolve_cancellation_still_propagates(caplog) -> None:
    """#912's arm catches ``Exception``, never ``BaseException``.

    A cancelled request delivers nothing to any shim, so converting it into a
    deny would invent a verdict nobody reads. The identical instance must
    escape, with no response and no ERROR record.
    """
    import asyncio
    import logging

    sentinel = asyncio.CancelledError("gone")
    fwd = AsyncMock(side_effect=sentinel)
    app = _make_svc_app(tools=[], forward_call=fwd)
    request = _hooks_request(app, _HOOK_REQUEST_BODY)
    match = await app.router.resolve(request)

    escaped: list[BaseException] = []
    responses: list = []
    with caplog.at_level(logging.ERROR, logger="svc_casa_mcp"):
        try:
            responses.append(await match.handler(request))
        except BaseException as raised:      # noqa: BLE001
            escaped.append(raised)

    assert (len(escaped), len(responses), len(caplog.records)) == (1, 0, 0)
    assert escaped[0] is sentinel
    assert (fwd.call_count, fwd.await_count) == (1, 1)


async def test_svc_hooks_resolve_adjacent_answers_are_unchanged() -> None:
    """#912's arm must not touch what the route already answers.

    Two adjacent behaviours, pinned so a catch cannot alter them silently:
    a far-end verdict (including an ALLOW) comes back as the body under the
    bridge's own 200, and a far end answering an EMPTY body — for which
    aiohttp's decoder returns ``None`` rather than raising — still yields a
    200 with body ``null``. The second is deliberately preserved, not fixed:
    it is a different mechanism and is not #912.
    """
    allow = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
    }}
    seen = []
    for far_end in (allow, None):
        fwd = AsyncMock(return_value=(200, far_end))
        app = _make_svc_app(tools=[], forward_call=fwd)
        request = _hooks_request(app, _HOOK_REQUEST_BODY)
        match = await app.router.resolve(request)
        resp = await match.handler(request)
        seen.append((resp.status, json.loads(resp.body),
                     fwd.call_count, fwd.await_count))

    assert seen == [(200, allow, 1, 1), (200, None, 1, 1)]


# ---------------------------------------------------------------------------
# #880 — the boot window's two assistant-facing faces must tell one story.
# Specified by **astra** in the drive red-case round.
async def test_socket_unreachable_hook_reason_matches_tools_call() -> None:
    """One condition, one wording: the operator's decision on #880 (2026-09-10).

    Casa resumes mid-flight engagements before it opens the internal socket
    those engagements reach it through, so a resumed engagement's first move can
    land while ``_forward_to_internal`` still raises
    ``aiohttp.ClientConnectorError``. The bridge answers that ONE condition on
    two faces, and at the base they word it differently: ``tools/call`` says
    ``casa_temporarily_unavailable: casa-main internal socket unreachable``
    while ``/hooks/resolve`` said ``Permission relay unavailable: casa-main
    internal socket is down. …``. Option (a) of the ruling keeps the hook answer
    a refusal — HTTP 200 carrying a ``deny``, INV-MCP-011 untouched — and gives
    it exactly the wording the tool-call face already uses.

    The comparison is DERIVED from both handlers in the same run rather than
    written against a literal here: a drift on either face alone breaks it, and
    a copy of the string in this file would only pin the face it was copied
    from. Both requests are resolved through the app's own router, so no
    listening socket is opened.
    """
    import aiohttp

    fwd = AsyncMock(side_effect=aiohttp.ClientConnectorError(
        connection_key=MagicMock(),
        os_error=ConnectionRefusedError("simulated"),
    ))
    app = _make_svc_app(tools=[_DummyTool()], forward_call=fwd)

    call_request = _json_post_request(app, "/mcp/casa-framework", {
        "jsonrpc": "2.0", "id": 8,
        "method": "tools/call",
        "params": {"name": "ok", "arguments": {}},
    })
    match = await app.router.resolve(call_request)
    call_resp = await match.handler(call_request)

    hook_request = _json_post_request(
        app, "/hooks/resolve", _HOOK_REQUEST_BODY)
    match = await app.router.resolve(hook_request)
    hook_resp = await match.handler(hook_request)

    # Both faces reached the forwarder, once each, on their own internal path.
    assert (fwd.call_count, fwd.await_count) == (2, 2)
    assert [c.kwargs["path"] for c in fwd.call_args_list] == [
        "/internal/tools/call", "/internal/hooks/resolve",
    ]
    error = json.loads(call_resp.body)["error"]
    assert error["code"] == -32000

    output = json.loads(hook_resp.body)["hookSpecificOutput"]
    assert hook_resp.status == 200
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"

    # The declaration of #880: for THIS condition the two faces carry the same
    # bytes. Nothing here is asserted about the route's other refusal arms.
    assert (output["permissionDecisionReason"].encode("utf-8")
            == error["message"].encode("utf-8"))
