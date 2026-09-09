# tests/test_engagement_auth_token.py
"""#335: engagement identity must be authenticated, not merely claimed.

The engagement id rides in client-reachable places (workspace .mcp.json,
logs, the shared 127.0.0.1 MCP endpoint), so presenting another active
engagement's id used to be enough to act with that engagement's authority —
including deriving its ROLE for privileged-tool checks. Every internal
surface now verifies the per-engagement secret ``auth_token`` (minted at
record creation, backfilled at load, provisioned only into that
engagement's own workspace) before binding any authority.

Red cases demonstrated: reverting the token check in
``internal_handlers.engagement_auth_ok`` / the tools-call handler, or
dropping the ``auth_token`` mint in ``EngagementRegistry.create``/``load``,
fails these tests.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.unit


class _FakeRecord:
    def __init__(
        self, eng_id: str, status: str = "active",
        auth_token: str = "tok-secret", topic_id: int | None = 77,
    ) -> None:
        self.id = eng_id
        self.status = status
        self.auth_token = auth_token
        self.topic_id = topic_id
        # v0.166.0: the bridge grant-gate dispatches only tools the engagement
        # is granted, so a record used to prove a valid token BINDS must grant
        # the `spy` tool these tests dispatch.
        self.kind = "executor"
        self.tools_allowed = ("mcp__casa-framework__spy",)


class _FakeRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, _FakeRecord] = {}

    def add(self, rec: _FakeRecord) -> None:
        self._by_id[rec.id] = rec

    def get(self, eng_id: str) -> _FakeRecord | None:
        return self._by_id.get(eng_id)


_TOOL_CALLS: list[dict] = []


async def _spy_tool(args: dict[str, Any]) -> dict[str, Any]:
    from tools import engagement_var
    rec = engagement_var.get(None)
    _TOOL_CALLS.append({"args": args, "eng": rec.id if rec else None})
    return {"content": [{"type": "text",
                         "text": json.dumps({"eng": rec.id if rec else None})}]}


def _make_app(registry: _FakeRegistry, tool_name: str = "spy") -> web.Application:
    from internal_handlers import _make_internal_tools_call_handler
    app = web.Application()
    app.router.add_post(
        "/internal/tools/call",
        _make_internal_tools_call_handler(
            tool_dispatch={tool_name: _spy_tool}, engagement_registry=registry,
        ),
    )
    return app


@pytest.fixture(autouse=True)
def _clear_spy():
    _TOOL_CALLS.clear()
    yield
    _TOOL_CALLS.clear()


# ---------------------------------------------------------------------------
# engagement_auth_ok — the verification primitive
# ---------------------------------------------------------------------------


class TestEngagementAuthOk:
    def test_matching_token_passes(self):
        from internal_handlers import engagement_auth_ok
        assert engagement_auth_ok(_FakeRecord("e"), "tok-secret")

    def test_mismatched_token_fails(self):
        from internal_handlers import engagement_auth_ok
        assert not engagement_auth_ok(_FakeRecord("e"), "tok-forged")

    def test_missing_presented_token_fails(self):
        from internal_handlers import engagement_auth_ok
        assert not engagement_auth_ok(_FakeRecord("e"), None)
        assert not engagement_auth_ok(_FakeRecord("e"), "")

    def test_non_string_presented_token_fails(self):
        from internal_handlers import engagement_auth_ok
        assert not engagement_auth_ok(_FakeRecord("e"), 12345)
        assert not engagement_auth_ok(_FakeRecord("e"), ["tok-secret"])

    def test_tokenless_record_matches_nothing(self):
        # Fail-closed: a record without a token confers no authority — even a
        # "matching" empty presentation is refused.
        from internal_handlers import engagement_auth_ok
        rec = _FakeRecord("e", auth_token="")
        assert not engagement_auth_ok(rec, "")
        assert not engagement_auth_ok(rec, "anything")

    def test_record_without_the_attribute_matches_nothing(self):
        from internal_handlers import engagement_auth_ok

        class _Bare:
            pass

        assert not engagement_auth_ok(_Bare(), "anything")


# ---------------------------------------------------------------------------
# /internal/tools/call — the #335 reported surface
# ---------------------------------------------------------------------------


class TestToolsCallRejectsForgedIdentity:
    async def test_forged_id_without_token_is_rejected_and_tool_never_runs(self):
        # THE #335 scenario: a caller presents another active engagement's id.
        reg = _FakeRegistry()
        reg.add(_FakeRecord("victim-engagement"))
        app = _make_app(reg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "spy", "arguments": {},
                      "engagement_id": "victim-engagement"},
            )
            body = await resp.json()
        assert body["error"]["code"] == -32003
        assert "engagement_auth_failed" in body["error"]["message"]
        assert _TOOL_CALLS == []  # the tool was never invoked

    async def test_wrong_token_is_rejected(self):
        reg = _FakeRegistry()
        reg.add(_FakeRecord("victim-engagement"))
        app = _make_app(reg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "spy", "arguments": {},
                      "engagement_id": "victim-engagement",
                      "engagement_token": "tok-forged"},
            )
            body = await resp.json()
        assert body["error"]["code"] == -32003
        assert _TOOL_CALLS == []

    async def test_valid_token_binds_the_engagement(self):
        reg = _FakeRegistry()
        reg.add(_FakeRecord("mine"))
        app = _make_app(reg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "spy", "arguments": {},
                      "engagement_id": "mine",
                      "engagement_token": "tok-secret"},
            )
            body = await resp.json()
        assert json.loads(body["content"][0]["text"]) == {"eng": "mine"}

    async def test_unknown_id_is_unbound_and_rejected_fail_closed(self):
        # v0.166.0: a stale/aged-out id is still not an AUTH failure, but it
        # leaves the call UNBOUND, and the bridge grant-gate now fails closed
        # for an unbound call to a non-terminal tool — an executor's own root
        # shell could otherwise omit the id to dispatch anything. The tool is
        # never invoked.
        reg = _FakeRegistry()
        app = _make_app(reg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "spy", "arguments": {},
                      "engagement_id": "no-such"},
            )
            body = await resp.json()
        assert "tool_not_granted" in body["error"]["message"]
        assert _TOOL_CALLS == []

    async def test_terminal_binding_allowlist_still_requires_the_token(self):
        # The emit_completion terminal-binding exemption (v0.74.2) must not
        # be a token bypass: a forged duplicate-completion replay is refused.
        from internal_handlers import _make_internal_tools_call_handler
        reg = _FakeRegistry()
        reg.add(_FakeRecord("done-1", status="completed"))
        app = web.Application()
        app.router.add_post(
            "/internal/tools/call",
            _make_internal_tools_call_handler(
                tool_dispatch={"emit_completion": _spy_tool},
                engagement_registry=reg,
            ),
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "emit_completion", "arguments": {},
                      "engagement_id": "done-1",
                      "engagement_token": "tok-forged"},
            )
            body = await resp.json()
            assert body["error"]["code"] == -32003
            assert _TOOL_CALLS == []
            # ... while the honest CLI retry (valid token) still reaches the
            # tool with the terminal record bound (idempotency contract).
            resp = await client.post(
                "/internal/tools/call",
                json={"name": "emit_completion", "arguments": {},
                      "engagement_id": "done-1",
                      "engagement_token": "tok-secret"},
            )
            body = await resp.json()
        assert json.loads(body["content"][0]["text"]) == {"eng": "done-1"}


# ---------------------------------------------------------------------------
# Registry — token lifecycle
# ---------------------------------------------------------------------------


class TestRegistryTokenLifecycle:
    async def test_create_mints_a_unique_nonempty_token(self, tmp_path):
        from engagement_registry import EngagementRegistry
        registry = EngagementRegistry(tombstone_path=str(tmp_path / "engagements.json"), bus=None)
        a = await registry.create(
            kind="executor", role_or_type="t", driver="claude_code",
            task="x", origin={}, topic_id=None)
        b = await registry.create(
            kind="executor", role_or_type="t", driver="claude_code",
            task="y", origin={}, topic_id=None)
        assert a.auth_token and b.auth_token
        assert a.auth_token != b.auth_token
        assert len(a.auth_token) >= 32

    async def test_token_round_trips_through_the_tombstone(self, tmp_path):
        from engagement_registry import EngagementRegistry
        path = str(tmp_path / "engagements.json")
        registry = EngagementRegistry(tombstone_path=path, bus=None)
        rec = await registry.create(
            kind="executor", role_or_type="t", driver="claude_code",
            task="x", origin={}, topic_id=None)
        reloaded = EngagementRegistry(tombstone_path=path, bus=None)
        await reloaded.load()
        assert reloaded.get(rec.id).auth_token == rec.auth_token

    async def test_load_backfills_a_tokenless_row_and_persists(self, tmp_path):
        # Pre-upgrade tombstone row: no auth_token key. Every in-memory
        # record must still end up with a token (the surfaces fail closed on
        # token-less records), and the backfill must reach disk so a
        # casa-main-only respawn does not remint (which would invalidate the
        # credential already provisioned into the workspace).
        path = tmp_path / "engagements.json"
        row = {
            "id": "a" * 32, "kind": "executor", "role_or_type": "t",
            "driver": "claude_code", "status": "idle", "topic_id": None,
            "started_at": 1.0, "last_user_turn_ts": 1.0,
            "last_idle_reminder_ts": 0.0, "completed_at": None,
            "sdk_session_id": None, "origin": {}, "task": "x",
        }
        path.write_text(json.dumps([row]), encoding="utf-8")
        from engagement_registry import EngagementRegistry
        registry = EngagementRegistry(tombstone_path=str(path), bus=None)
        await registry.load()
        token = registry.get("a" * 32).auth_token
        assert token
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk[0]["auth_token"] == token
        # A second load keeps the SAME token (no remint).
        registry2 = EngagementRegistry(tombstone_path=str(path), bus=None)
        await registry2.load()
        assert registry2.get("a" * 32).auth_token == token


# ---------------------------------------------------------------------------
# Workspace credential rendering
# ---------------------------------------------------------------------------


class TestWorkspaceCredential:
    def test_mcp_json_carries_id_and_token_on_both_servers(self, tmp_path):
        from drivers.workspace import write_workspace_mcp_json
        write_workspace_mcp_json(
            str(tmp_path), engagement_id="e" * 32,
            engagement_auth_token="tok-ws",
            casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework",
        )
        cfg = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
        fw = cfg["mcpServers"]["casa-framework"]
        assert fw["headers"] == {
            "X-Casa-Engagement-Id": "e" * 32,
            "X-Casa-Engagement-Token": "tok-ws",
        }
        ch = cfg["mcpServers"]["casa-engagement-channel"]
        assert ch["env"]["CASA_ENGAGEMENT_TOKEN"] == "tok-ws"
        assert ch["env"]["CASA_INTERNAL_SOCKET"] == "/run/casa/internal.sock"

    def test_credential_files_are_not_world_readable(self, tmp_path):
        # Terra r1: both files that carry a token were written 0644. Defense
        # in depth (engagements run as root and can still read a sibling's
        # file) — but a secret should not be world-readable regardless.
        import os
        import stat
        from drivers.workspace import write_workspace_mcp_json
        write_workspace_mcp_json(
            str(tmp_path), engagement_id="e" * 32,
            engagement_auth_token="tok-ws",
            casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework",
        )
        mode = stat.S_IMODE(os.stat(tmp_path / ".mcp.json").st_mode)
        assert mode == 0o600, f".mcp.json is {oct(mode)}"

    async def test_tombstone_is_not_world_readable(self, tmp_path):
        import os
        import stat
        from engagement_registry import EngagementRegistry
        path = str(tmp_path / "engagements.json")
        registry = EngagementRegistry(tombstone_path=path, bus=None)
        await registry.create(
            kind="executor", role_or_type="t", driver="claude_code",
            task="x", origin={}, topic_id=None)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_reads_back_the_token_it_wrote(self, tmp_path):
        from drivers.workspace import (
            workspace_mcp_token, write_workspace_mcp_json,
        )
        assert workspace_mcp_token(str(tmp_path)) is None  # no file yet
        write_workspace_mcp_json(
            str(tmp_path), engagement_id="e" * 32,
            engagement_auth_token="tok-ws",
            casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework",
        )
        assert workspace_mcp_token(str(tmp_path)) == "tok-ws"

    def test_pre_335_workspace_reports_no_token(self, tmp_path):
        # The migration trigger: an old workspace has the id header only, so
        # boot replay must see a MISMATCH and refresh + cycle the service.
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "casa-framework": {"headers": {"X-Casa-Engagement-Id": "e" * 32}},
        }}), encoding="utf-8")
        from drivers.workspace import workspace_mcp_token
        assert workspace_mcp_token(str(tmp_path)) is None

    def test_malformed_workspace_config_reports_no_token(self, tmp_path):
        (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
        from drivers.workspace import workspace_mcp_token
        assert workspace_mcp_token(str(tmp_path)) is None


class TestCredentialIsNotReadableThroughTheToolSurface:
    """Sol r1: the token boundary is decorative if any caller can simply READ
    a victim's .mcp.json through the workspace-inspection tool — it needs no
    engagement identity at all, since an unbound tools/call dispatches
    normally."""

    async def test_peek_refuses_to_return_mcp_json_contents(self, tmp_path, monkeypatch):
        import tools as tools_mod
        eng = "f" * 32
        ws = tmp_path / eng
        ws.mkdir()
        from drivers.workspace import write_workspace_mcp_json
        write_workspace_mcp_json(
            str(ws), engagement_id=eng, engagement_auth_token="tok-victim",
            casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework",
        )
        monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path))
        out = await tools_mod.peek_engagement_workspace.handler(
            {"engagement_id": eng, "path": ".mcp.json"})
        text = out["content"][0]["text"]
        assert "tok-victim" not in text
        assert "credential_file" in text

    async def test_peek_still_reads_ordinary_workspace_files(self, tmp_path, monkeypatch):
        import tools as tools_mod
        eng = "f" * 32
        ws = tmp_path / eng
        ws.mkdir()
        (ws / "NOTES.md").write_text("ordinary content", encoding="utf-8")
        monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path))
        out = await tools_mod.peek_engagement_workspace.handler(
            {"engagement_id": eng, "path": "NOTES.md"})
        assert "ordinary content" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# /internal/channel/* — sibling arms of the same id-claim authority
# ---------------------------------------------------------------------------


class _FakeTelegramChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.state_updates: list[tuple[str, str]] = []

    async def send_response_to_topic(self, topic_id: int, text: str) -> int:
        self.sent.append((topic_id, text))
        return 1

    async def send_to_topic(self, topic_id: int, text: str, **_kw) -> int:
        self.sent.append((topic_id, text))
        return 1

    async def update_topic_state(self, *, engagement_id: str, new_state: str):
        self.state_updates.append((engagement_id, new_state))


def _channel_app() -> tuple[web.Application, _FakeTelegramChannel, _FakeRegistry]:
    from channels.channel_handlers import _make_channel_handlers
    reg = _FakeRegistry()
    reg.add(_FakeRecord("victim-engagement"))
    tg = _FakeTelegramChannel()
    app = web.Application()
    for path, handler in _make_channel_handlers(
        telegram_channel=tg, engagement_registry=reg,
    ).items():
        app.router.add_post(path, handler)
    return app, tg, reg


class TestChannelRoutesRejectForgedIdentity:
    async def test_send_to_topic_forged_id_is_rejected(self):
        app, tg, _ = _channel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/channel/send_to_topic",
                json={"engagement_id": "victim-engagement", "text": "spoof",
                      "engagement_token": "tok-forged"},
            )
            body = await resp.json()
        assert body == {"ok": False, "error": "engagement_auth_failed"}
        assert tg.sent == []

    async def test_post_inline_keyboard_forged_id_is_rejected(self):
        app, tg, _ = _channel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/channel/post_inline_keyboard",
                json={"engagement_id": "victim-engagement", "text": "q",
                      "buttons": [[{"text": "Yes", "callback_data": "y"}]]},
            )
            body = await resp.json()
        assert body == {"ok": False, "error": "engagement_auth_failed"}
        assert tg.sent == []

    async def test_ask_forged_id_is_rejected_before_any_broker_work(self):
        app, _, _ = _channel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/channel/ask",
                json={"engagement_id": "victim-engagement",
                      "request_id": "r-1", "question": "phish?",
                      "options": ["yes", "no"],
                      "engagement_token": "tok-forged"},
            )
            body = await resp.json()
        assert body == {"ok": False, "error": "engagement_auth_failed"}

    async def test_ask_cancel_forged_id_is_rejected(self):
        app, _, _ = _channel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/channel/ask_cancel",
                json={"engagement_id": "victim-engagement",
                      "request_id": "r-1"},
            )
            body = await resp.json()
        assert body == {"ok": False, "error": "engagement_auth_failed"}

    async def test_permission_verdict_route_removed(self):
        # #469: the approval-forgery surface is gone entirely — even a
        # correctly-authenticated engagement token gets a 404, because a
        # verdict must come from the operator's tap, not from any holder of
        # an engagement credential.
        app, _, _ = _channel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/channel/permission_verdict",
                json={"engagement_id": "victim-engagement",
                      "request_id": "r-1", "verdict": "allow",
                      "engagement_token": "tok-forged"},
            )
        assert resp.status == 404

    async def test_update_state_forged_id_is_rejected(self):
        app, tg, _ = _channel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/channel/update_state",
                json={"engagement_id": "victim-engagement",
                      "new_state": "awaiting"},
            )
            body = await resp.json()
        assert body == {"ok": False, "error": "engagement_auth_failed"}
        assert tg.state_updates == []

    async def test_send_to_topic_valid_token_still_posts(self):
        app, tg, _ = _channel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/channel/send_to_topic",
                json={"engagement_id": "victim-engagement", "text": "hello",
                      "engagement_token": "tok-secret"},
            )
            body = await resp.json()
        assert body.get("ok") is True
        assert tg.sent == [(77, "hello")]
