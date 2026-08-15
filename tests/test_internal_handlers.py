# tests/test_internal_handlers.py
"""Unit tests for internal_handlers.py (Plan 4b Phase 3.6)."""
from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ----- Test fixtures: minimal engagement registry + tool dispatch ----------


class _FakeRecord:
    def __init__(self, eng_id: str, status: str = "active") -> None:
        self.id = eng_id
        self.status = status
        # #335: per-engagement secret; the body must present it to bind.
        self.auth_token = f"tok-{eng_id}"
        # v0.166.0: the bridge grant-gate dispatches only granted tools. These
        # plumbing tests use arbitrary tool names, so grant at the server level
        # (any casa tool) — the grant policy itself is pinned in
        # test_bridge_tools_allowed_gate.py.
        self.kind = "executor"
        self.tools_allowed = ("mcp__casa-framework",)


class _FakeRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, _FakeRecord] = {}

    def add(self, rec: _FakeRecord) -> None:
        self._by_id[rec.id] = rec

    def get(self, eng_id: str) -> _FakeRecord | None:
        return self._by_id.get(eng_id)


async def _ok_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Returns args back as a 'content' block, MCP-style."""
    return {"content": [{"type": "text", "text": json.dumps(args)}]}


async def _engagement_aware_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Reads engagement_var to verify ContextVar binding works."""
    from tools import engagement_var
    rec = engagement_var.get(None)
    rec_id = rec.id if rec is not None else None
    return {"content": [{"type": "text", "text": json.dumps({"eng": rec_id})}]}


async def _raising_tool(_args: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("boom")


def _make_app(*, dispatch: dict, registry: _FakeRegistry,
              hook_policies: dict | None = None) -> web.Application:
    """Build a tiny aiohttp app exposing the two internal handlers."""
    from internal_handlers import (
        _make_internal_tools_call_handler,
        _make_internal_hooks_resolve_handler,
    )
    app = web.Application()
    app.router.add_post(
        "/internal/tools/call",
        _make_internal_tools_call_handler(
            tool_dispatch=dispatch, engagement_registry=registry,
        ),
    )
    app.router.add_post(
        "/internal/hooks/resolve",
        _make_internal_hooks_resolve_handler(
            hook_policies=hook_policies or {},
        ),
    )
    return app


async def test_tools_call_known_tool_returns_result() -> None:
    reg = _FakeRegistry()
    rec = _FakeRecord(eng_id="abc-123", status="active")
    reg.add(rec)
    app = _make_app(dispatch={"ok_tool": _ok_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "ok_tool", "arguments": {"x": 1},
                  "engagement_id": "abc-123", "engagement_token": rec.auth_token},
        )
        assert resp.status == 200
        body = await resp.json()
        # Internal handler returns the bare tool result (no JSON-RPC wrapping).
        assert body == {"content": [{"type": "text", "text": '{"x": 1}'}]}


async def test_tools_call_unknown_tool_returns_error_object() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "nonesuch", "arguments": {}, "engagement_id": None},
        )
        assert resp.status == 200  # 200 — protocol-level not transport-level
        body = await resp.json()
        assert body == {"error": {"code": -32602, "message": "Unknown tool: nonesuch"}}


async def test_tools_call_missing_name_returns_error_object() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={"ok_tool": _ok_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"arguments": {}, "engagement_id": None},
        )
        body = await resp.json()
        assert body == {"error": {"code": -32602, "message": "missing name"}}


async def test_tools_call_non_object_arguments_returns_error_object() -> None:
    """#380: a truthy non-object ``arguments`` must earn a typed -32602 —
    not be forwarded into the tool to crash there."""
    reg = _FakeRegistry()
    app = _make_app(dispatch={"ok_tool": _ok_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        for bad in ("scalar", 7, ["list"], [], "", 0, False):
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "ok_tool", "arguments": bad,
                      "engagement_id": None},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["error"]["code"] == -32602
            assert "arguments" in body["error"]["message"]


async def test_tools_call_engagement_id_binds_contextvar() -> None:
    """#335: the engagement id binds only together with the record's
    auth token (the id alone is no longer an authenticator)."""
    reg = _FakeRegistry()
    rec = _FakeRecord(eng_id="abc-123", status="active")
    reg.add(rec)
    app = _make_app(dispatch={"eng": _engagement_aware_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "eng", "arguments": {}, "engagement_id": "abc-123",
                  "engagement_token": rec.auth_token},
        )
        body = await resp.json()
        text = json.loads(body["content"][0]["text"])
        assert text == {"eng": "abc-123"}


async def test_tools_call_inactive_engagement_is_rejected_fail_closed() -> None:
    """Defense-in-depth: only UNDERGOING (status=='active') engagements bind.
    A finalized/cancelled record does not bind and the tool never runs.

    #587: the refusal now NAMES that reason (-32006 engagement_not_live)
    instead of borrowing the grant-gate's -32004 tool_not_granted, which
    described a record that may well hold the grant as ungranted."""
    reg = _FakeRegistry()
    rec = _FakeRecord(eng_id="fin-1", status="completed")
    reg.add(rec)
    app = _make_app(dispatch={"eng": _engagement_aware_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "eng", "arguments": {}, "engagement_id": "fin-1",
                  "engagement_token": rec.auth_token},
        )
        body = await resp.json()
        assert body["error"]["code"] == -32006, body
        assert "engagement_not_live" in body["error"]["message"]
        assert "tool_not_granted" not in body["error"]["message"]


async def test_tools_call_handler_exception_returns_error_object() -> None:
    reg = _FakeRegistry()
    rec = _FakeRecord(eng_id="abc-123", status="active")
    reg.add(rec)
    app = _make_app(dispatch={"raises": _raising_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "raises", "arguments": {},
                  "engagement_id": "abc-123", "engagement_token": rec.auth_token},
        )
        body = await resp.json()
        assert body["error"]["code"] == -32001  # tool-level error, distinct from -32000
        assert "boom" in body["error"]["message"]


async def test_tools_call_malformed_json_returns_error_object() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        body = await resp.json()
        assert body == {"error": {"code": -32700, "message": "Parse error"}}


# Append to tests/test_internal_handlers.py


async def _allow_callback(_payload, _ctx, _opts):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }}


async def _deny_callback(_payload, _ctx, _opts):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "blocked",
    }}


async def _none_callback(_payload, _ctx, _opts):
    return None


async def _raising_callback(_payload, _ctx, _opts):
    raise RuntimeError("hook boom")


async def test_hooks_resolve_unknown_policy_denies() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={}, registry=reg, hook_policies={})
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "no_such", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "unknown policy" in body["hookSpecificOutput"]["permissionDecisionReason"]


async def test_hooks_resolve_known_policy_invokes_callback() -> None:
    reg = _FakeRegistry()
    policies = {"deny_all": ("Bash", _deny_callback)}
    app = _make_app(dispatch={}, registry=reg, hook_policies=policies)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "deny_all", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_hooks_resolve_matcher_mismatch_returns_empty_allow() -> None:
    """Defense-in-depth: if the payload's tool_name doesn't fullmatch the
    policy's matcher regex, return empty body (= allow). Mirrors v0.13.1
    behavior."""
    reg = _FakeRegistry()
    policies = {"bash_only": ("Bash", _deny_callback)}
    app = _make_app(dispatch={}, registry=reg, hook_policies=policies)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "bash_only", "payload": {"tool_name": "Read"}},
        )
        body = await resp.json()
        assert body == {}


async def test_hooks_resolve_callback_none_means_allow() -> None:
    """A HookCallback returning None (no decision) -> empty body = allow."""
    reg = _FakeRegistry()
    policies = {"silent": ("Bash", _none_callback)}
    app = _make_app(dispatch={}, registry=reg, hook_policies=policies)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "silent", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body == {}


async def test_hooks_resolve_callback_exception_denies() -> None:
    """Fail-closed: a raising callback denies. Matches v0.13.1 behavior."""
    reg = _FakeRegistry()
    policies = {"buggy": ("Bash", _raising_callback)}
    app = _make_app(dispatch={}, registry=reg, hook_policies=policies)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "buggy", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "raised" in body["hookSpecificOutput"]["permissionDecisionReason"]


async def test_hooks_resolve_malformed_json_denies() -> None:
    """Fail-closed on malformed body too -- matches v0.13.1."""
    reg = _FakeRegistry()
    app = _make_app(dispatch={}, registry=reg, hook_policies={})
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_hooks_resolve_non_dict_body_denies_structurally() -> None:
    """L14: valid JSON that isn't an object must yield the structured
    deny (status 200), not an unhandled-exception 500."""
    reg = _FakeRegistry()
    app = _make_app(dispatch={}, registry=reg, hook_policies={})
    async with TestClient(TestServer(app)) as client:
        for raw in ("[1, 2]", '"hello"', "null", "42"):
            resp = await client.post(
                "/internal/hooks/resolve",
                data=raw,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200, raw
            body = await resp.json()
            assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_hooks_resolve_non_dict_payload_and_policy_deny() -> None:
    """L14: a truthy non-dict payload (would crash at payload.get) and an
    unhashable policy (would crash at hook_policies.get with TypeError)
    must both deny structurally instead of raising."""
    reg = _FakeRegistry()
    policies = {"bash_only": ("Bash", _deny_callback)}
    app = _make_app(dispatch={}, registry=reg, hook_policies=policies)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "bash_only", "payload": "not-a-dict"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"

        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": [1], "payload": {"tool_name": "Bash"}},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_tools_call_terminal_engagement_binds_for_emit_completion() -> None:
    """v0.74.2 (live finding 2026-07-13): a duplicate/racing emit_completion
    delivery landing AFTER the record flips terminal must still bind, so the
    tool's own idempotency check answers already_terminal — the active-only
    gate returned a lying not_in_engagement to the agent. ONLY
    emit_completion gets terminal binding (privileged tools keep the
    defense-in-depth active-only rule).

    Pins INV-TOOL-002 (with the inactive-binds-none sibling). Red case demonstrated: emptying _TERMINAL_BINDING_TOOLS fails this test.
    """
    reg = _FakeRegistry()
    rec = _FakeRecord(eng_id="fin-2", status="completed")
    reg.add(rec)
    app = _make_app(dispatch={"emit_completion": _engagement_aware_tool},
                    registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "emit_completion", "arguments": {},
                  "engagement_id": "fin-2",
                  "engagement_token": rec.auth_token},
        )
        body = await resp.json()
        text = json.loads(body["content"][0]["text"])
        assert text == {"eng": "fin-2"}


async def test_tools_call_unbound_emit_completion_still_dispatches() -> None:
    """#585: with NO engagement claim at all, the grant-gate's terminal
    exemption still dispatches emit_completion — `engagement_casa_grant_names`
    returns an empty set for an unbound caller, so every other tool is refused
    -32004, and only _TERMINAL_BINDING_TOOLS reaches its handler.

    This is the one designed unbound-reachable dispatch on the bridge, and
    `test-local/e2e/test_mcp_restart_survival.sh` depends on it: it is what
    lets that probe prove a full forwarder → casa-main → handler round trip
    across a casa-main bounce without provisioning an engagement. A tightening
    that closed the exemption would otherwise only surface in the nightly
    hardening tier, which is exactly how #585 went unnoticed for a week.

    Red case demonstrated: emptying _TERMINAL_BINDING_TOOLS turns this
    response into -32004 tool_not_granted and fails the test.
    """
    reg = _FakeRegistry()
    app = _make_app(dispatch={"emit_completion": _engagement_aware_tool,
                              "other": _engagement_aware_tool},
                    registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "emit_completion", "arguments": {}},
        )
        body = await resp.json()
        # Dispatched, and bound to NO engagement (the tool's own
        # not_in_engagement branch is what the e2e probe asserts on).
        assert json.loads(body["content"][0]["text"]) == {"eng": None}

        # The sibling half: any non-terminal tool from the same unbound
        # caller is refused before it runs.
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "other", "arguments": {}},
        )
        body = await resp.json()
        assert body["error"]["code"] == -32004


def test_pin_inv_tool_001_result_marks_errors_only_for_error_status():
    """Pins INV-TOOL-001: _result infers the outer MCP error only from
    status=="error" or ok is False; other statuses ride as successes.

    Red case demonstrated: widening the inference to treat "unavailable" as
    an error fails this test.
    """
    from tools import _result

    assert _result({"status": "error"})["is_error"] is True
    assert _result({"ok": False})["is_error"] is True
    for payload in ({"status": "unavailable"}, {"status": "pending"},
                    {"status": "acknowledged"}, {"ok": True}):
        assert _result(payload).get("is_error") is not True


class TestNotLiveIsDistinctFromNotGranted:
    """#587 — the bridge answered two different refusals with one message.

    A caller that authenticates correctly against a known record which is not
    BINDABLE got `-32004 tool_not_granted`, describing a record that may hold
    the grant as ungranted. Binding requires `status == "active"`, so this is
    reachable whenever a non-terminal call arrives for a record that has gone
    terminal — a cancellation or completion landing while the engagement's CLI
    still has a turn in flight. The refusal itself was always correct and
    fail-closed; only its attribution was wrong, and that attribution sent the
    #585 investigation down the grant path twice.
    """

    @staticmethod
    def _granted_record(eng_id, status):
        rec = _FakeRecord(eng_id=eng_id, status=status)
        # Explicitly HOLDS the grant, so a `tool_not_granted` answer would be
        # a statement the record itself contradicts.
        rec.tools_allowed = ("mcp__casa-framework__eng",)
        return rec

    @pytest.mark.parametrize("status", ["completed", "cancelled", "error",
                                        "idle"])
    async def test_authenticated_but_unbindable_says_not_live(self, status):
        reg = _FakeRegistry()
        rec = self._granted_record("held-1", status)
        reg.add(rec)
        app = _make_app(dispatch={"eng": _engagement_aware_tool}, registry=reg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "eng", "arguments": {},
                      "engagement_id": "held-1",
                      "engagement_token": rec.auth_token},
            )
            body = await resp.json()
        assert body["error"]["code"] == -32006, body
        assert "engagement_not_live" in body["error"]["message"]
        assert status in body["error"]["message"], body
        assert "tool_not_granted" not in body["error"]["message"]

    async def test_bound_but_ungranted_still_says_not_granted(self):
        """The other half of the split, unchanged: a LIVE record that genuinely
        lacks the grant keeps -32004, because there the message is true."""
        reg = _FakeRegistry()
        rec = _FakeRecord(eng_id="live-1", status="active")
        rec.tools_allowed = ()          # holds nothing
        reg.add(rec)
        app = _make_app(dispatch={"eng": _engagement_aware_tool}, registry=reg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "eng", "arguments": {},
                      "engagement_id": "live-1",
                      "engagement_token": rec.auth_token},
            )
            body = await resp.json()
        assert body["error"]["code"] == -32004, body
        assert "tool_not_granted" in body["error"]["message"]

    async def test_unbound_caller_still_says_not_granted(self):
        """An id the registry cannot resolve has no record to describe, so the
        unbound fail-closed refusal INV-MCP-001 states is unchanged — and the
        restart-survival probe still asserts exactly that."""
        reg = _FakeRegistry()
        app = _make_app(dispatch={"eng": _engagement_aware_tool}, registry=reg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "eng", "arguments": {},
                      "engagement_id": "no-such"},
            )
            body = await resp.json()
        assert body["error"]["code"] == -32004, body
        assert "tool_not_granted" in body["error"]["message"]

    async def test_terminal_binding_tool_is_not_refused_as_not_live(self):
        """emit_completion binds even on a terminal record (v0.74.2), so a
        completion retry must reach the tool's own idempotency check rather
        than the new refusal."""
        reg = _FakeRegistry()
        rec = self._granted_record("done-1", "completed")
        reg.add(rec)
        app = _make_app(
            dispatch={"emit_completion": _engagement_aware_tool}, registry=reg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "emit_completion", "arguments": {},
                      "engagement_id": "done-1",
                      "engagement_token": rec.auth_token},
            )
            body = await resp.json()
        assert "error" not in body, body
        assert json.loads(body["content"][0]["text"]) == {"eng": "done-1"}

    async def test_a_bad_token_still_fails_authentication_first(self):
        """The new refusal must not become an oracle: it is reached only AFTER
        the token check, so a forged id never learns a record's liveness."""
        reg = _FakeRegistry()
        reg.add(self._granted_record("held-2", "cancelled"))
        app = _make_app(dispatch={"eng": _engagement_aware_tool}, registry=reg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "eng", "arguments": {},
                      "engagement_id": "held-2",
                      "engagement_token": "tok-forged"},
            )
            body = await resp.json()
        assert body["error"]["code"] == -32003, body
        assert "engagement_auth_failed" in body["error"]["message"]
