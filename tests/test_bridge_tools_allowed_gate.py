"""The internal tools-call bridge enforces the authenticated engagement's grant.

Security fix (v0.166.0): the executor MCP bridge (`/internal/tools/call`, and
the `svc_casa_mcp` HTTP path that forwards into it) used to dispatch ANY tool in
the full map once the engagement token authenticated — it consulted no grant
(the old INV-MCP-001). But an executor's own root shell can read its workspace
`.mcp.json` token and POST the socket directly, bypassing the CLI-side allowlist
that was supposed to be the "before dispatch" enforcement. So a plugin-developer
could call e.g. `plugin_assign(target="executor:plugin-developer")` and raise its
own grant ceiling. The bridge now gates dispatch on the engagement's own grant,
fail-closed when no active engagement is bound.

Design attack: Sol+Terra (2026-08-08) — inverting INV-MCP-001 is intentional;
the socket permission boundary (INV-MCP-002) does not exclude the executor's own
shell, so enforcement must move to dispatch.
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from internal_handlers import _make_internal_tools_call_handler

pytestmark = pytest.mark.unit


async def _ran(_arguments):
    return {"content": [{"type": "text", "text": "ran"}]}


def _record(*, kind: str, tools_allowed, token: str = "tok-e1", status: str = "active"):
    return type("Record", (), {
        "status": status, "kind": kind,
        "tools_allowed": tuple(tools_allowed),
        "auth_token": token,
    })()


def _client_for(record):
    registry = type("Registry", (), {"get": lambda self, _id: record})()
    app = web.Application()
    app.router.add_post(
        "/internal/tools/call",
        _make_internal_tools_call_handler(
            tool_dispatch={"plugin_assign": _ran, "query_engager": _ran,
                           "emit_completion": _ran},
            engagement_registry=registry,
        ),
    )
    return app


async def _call(app, name, *, eng_id="e1", token="tok-e1"):
    body = {"name": name, "arguments": {}}
    if eng_id is not None:
        body["engagement_id"] = eng_id
    if token is not None:
        body["engagement_token"] = token
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/internal/tools/call", json=body)
        return await resp.json()


async def test_granted_tool_dispatches():
    rec = _record(kind="executor",
                  tools_allowed=("mcp__casa-framework__query_engager",))
    payload = await _call(_client_for(rec), "query_engager")
    assert payload.get("content") == [{"type": "text", "text": "ran"}]


async def test_ungranted_tool_is_rejected():
    """The escalation path: a plugin-developer whose grant is only query_engager
    must not reach plugin_assign, even with a valid token."""
    rec = _record(kind="executor",
                  tools_allowed=("mcp__casa-framework__query_engager",))
    payload = await _call(_client_for(rec), "plugin_assign")
    assert "content" not in payload
    assert "not_granted" in payload["error"]["message"]


async def test_specialist_empty_record_still_gets_its_mandatory_grants():
    """Interactive specialists are created with an empty tools_allowed; the
    kind-mandatory casa grants (query_engager, emit_completion) must still pass,
    or delegation breaks."""
    rec = _record(kind="specialist", tools_allowed=())
    payload = await _call(_client_for(rec), "query_engager")
    assert payload.get("content") == [{"type": "text", "text": "ran"}]
    # ...but a specialist still cannot reach an unrelated privileged tool.
    payload = await _call(_client_for(rec), "plugin_assign")
    assert "not_granted" in payload["error"]["message"]


async def test_specialist_definition_granted_framework_tool_passes():
    """A specialist config may grant framework tools beyond the two mandatory
    ones (e.g. search_memory); the record now pins them, so the gate admits
    them. Regression for the review finding that an empty record rejected a
    legitimately-granted specialist tool."""
    rec = _record(kind="specialist", tools_allowed=(
        "mcp__casa-framework__query_engager",
        "mcp__casa-framework__emit_completion",
        "mcp__casa-framework__search_memory",
    ))
    registry = type("R", (), {"get": lambda self, _id: rec})()
    app = web.Application()
    app.router.add_post(
        "/internal/tools/call",
        _make_internal_tools_call_handler(
            tool_dispatch={"search_memory": _ran}, engagement_registry=registry),
    )
    payload = await _call(app, "search_memory")
    assert payload.get("content") == [{"type": "text", "text": "ran"}]


async def test_no_engagement_id_is_fail_closed_except_terminal():
    rec = _record(kind="executor", tools_allowed=())
    app = _client_for(rec)
    # No engagement id at all → unbound → reject a normal tool.
    payload = await _call(app, "query_engager", eng_id=None, token=None)
    assert "not_granted" in payload["error"]["message"]
    # ...but the terminal-binding tool still runs unbound (lifecycle).
    payload = await _call(app, "emit_completion", eng_id=None, token=None)
    assert payload.get("content") == [{"type": "text", "text": "ran"}]


async def test_invalid_token_still_rejects_before_the_grant_check():
    rec = _record(kind="executor",
                  tools_allowed=("mcp__casa-framework__query_engager",),
                  token="tok-e1")
    payload = await _call(_client_for(rec), "query_engager", token="WRONG")
    assert payload["error"]["code"] == -32003  # engagement_auth_failed
