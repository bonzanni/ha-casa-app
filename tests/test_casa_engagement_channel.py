"""Tests for the casa-engagement-channel MCP server skeleton (v0.37.2).

These tests cover the channel server's persistent surface: a single ``reply``
tool that POSTs to ``/internal/channel/send_to_topic`` on the casa-main Unix
socket, plus ``declared_capabilities()`` returning the ``claude/channel``
experimental capability that the bootstrap injects into MCP initialization
options.

The D2 contract locked behavior: ``reply`` accepts a ``chat_id`` arg for SDK
compatibility but never forwards it. Routing always uses the engagement_id
captured at module import time from the ``--engagement-id`` CLI flag.

v0.37.2 (C-1) retired the notification-based permission relay (inbound
``permission_request`` + outbound ``permission`` verdict notifications).
Permission gating now flows through the casa-main PreToolUse hook
``engagement_permission_relay`` — see ``test_hooks_engagement_permission_relay.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import uuid

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def channel_server():
    """Force a fresh re-import of the channel server module per test.

    Uses the explicit ``_configure_for_test`` seam to populate
    ``ENGAGEMENT_ID`` directly, avoiding argv-driven magic.
    """
    sys.modules.pop("channels.casa_engagement_channel", None)
    module = importlib.import_module("channels.casa_engagement_channel")
    module._configure_for_test("deadbeef-test")
    assert module.ENGAGEMENT_ID == "deadbeef-test"
    yield module
    sys.modules.pop("channels.casa_engagement_channel", None)


class TestChannelServerSkeleton:
    async def test_reply_tool_registered(self, channel_server):
        tools = await channel_server._list_tools_for_tests()
        names = [t.name for t in tools]
        assert "reply" in names

    async def test_capability_includes_claude_channel(self, channel_server):
        caps = channel_server.declared_capabilities()
        assert isinstance(caps, dict)
        assert "claude/channel" in caps

    async def test_initialization_options_carry_experimental_capabilities(
        self, channel_server,
    ):
        """End-to-end (§A.6.1): the bootstrap injects experimental caps
        into the inner Server's InitializationOptions."""
        low_level = channel_server._resolve_mcp_server()
        assert low_level is not None
        opts = low_level.create_initialization_options(
            experimental_capabilities=channel_server.declared_capabilities(),
        )
        experimental = opts.capabilities.experimental or {}
        assert "claude/channel" in experimental
        # v0.37.2 (C-1): retired capability MUST NOT come back.
        assert "claude/channel/permission" not in experimental

    async def test_reply_forwards_to_internal_socket(
        self, channel_server, monkeypatch,
    ):
        captured: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            captured.append((path, dict(payload)))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        result = await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "ignored", "text": "hello"},
        )

        assert len(captured) == 1
        path, payload = captured[0]
        assert path == "/internal/channel/send_to_topic"
        assert payload.get("engagement_id") == "deadbeef-test"
        assert payload.get("text") == "hello"
        assert result == {"ok": True}

    async def test_reply_d2_ignores_chat_id_arg(
        self, channel_server, monkeypatch,
    ):
        captured: list[dict] = []

        async def fake_post(path, payload):
            captured.append(dict(payload))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "11111", "text": "one"},
        )
        await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "99999", "text": "two"},
        )

        assert len(captured) == 2
        for payload in captured:
            assert "chat_id" not in payload
            assert payload.get("engagement_id") == "deadbeef-test"
        assert captured[0]["text"] == "one"
        assert captured[1]["text"] == "two"


class TestDeclaredCapabilities:
    """v0.37.2 (C-1) positive guard: capability surface is the channel only."""

    async def test_returns_claude_channel_only(self):
        from channels.casa_engagement_channel import declared_capabilities
        caps = declared_capabilities()
        assert caps == {"claude/channel": {}}


# ---------------------------------------------------------------------------
# v0.75.0 (W5, Task 3) — the `ask` tool.
# ---------------------------------------------------------------------------


class TestAskTool:
    async def test_ask_registered(self, channel_server):
        tools = await channel_server._list_tools_for_tests()
        names = [t.name for t in tools]
        assert "ask" in names

    async def test_invalid_args_are_posted_and_refused_server_side(
        self, channel_server, monkeypatch,
    ):
        """v0.79.0 (§2, r8-1): the ask ingress is now a RAW-DICT pass-through —
        the zero-HTTP fail-fast contract FLIPPED. Bad args are no longer
        short-circuited client-side; the request IS made and casa-main refuses
        it server-side (all validation lives on the server)."""
        calls: list = []

        async def fake_post(path, payload, **kw):
            calls.append((path, payload))
            return {"ok": False, "error": "invalid_args"}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        cases = [
            {"question": "Q?", "options": ["A"]},              # 1 option
            {"question": "Q?", "options": [f"o{i}" for i in range(9)]},  # 9
            {"question": "Q?", "options": ["A", "A"]},          # dup
            {"question": "x" * 1025, "options": ["A", "B"]},    # long q
            {"question": "Q?", "options": ["x" * 49, "B"]},     # long label
            {"question": "", "options": ["A", "B"]},            # empty q
        ]
        for args in cases:
            result = await channel_server._invoke_tool_for_tests("ask", args)
            assert result == {"ok": False, "error": "invalid_args"}, args

        # Every bad-args call DID reach the server (no client short-circuit).
        assert len(calls) == len(cases)
        assert all(c[0] == "/internal/channel/ask" for c in calls)

    async def test_ask_transmits_request_id_and_projection_hash(
        self, channel_server, monkeypatch,
    ):
        """The ask ingress mints a request_id and a projection hash over the
        RAW args (question, options, timeout_s-as-given) and transmits both."""
        captured: list = []

        async def fake_post(path, payload, **kw):
            captured.append(dict(payload))
            return {"ok": True, "outcome": "no_answer"}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)
        await channel_server._invoke_tool_for_tests(
            "ask", {"question": "Q?", "options": ["A", "B"]},
        )
        p = captured[0]
        assert len(p["request_id"]) == 32
        # Hash is over the timeout AS GIVEN (None here) — matches the relay's
        # project_args which reads timeout_s via .get() (absent → None).
        assert p["projection_hash"] == channel_server._ask_projection_hash(
            "Q?", ["A", "B"], None,
        )

    async def test_ask_empty_options_is_transmitted_not_refused(
        self, channel_server, monkeypatch,
    ):
        """options: [] (free-text anchor) is a VALID raw-dict call — it must be
        transmitted, never short-circuited."""
        captured: list = []

        async def fake_post(path, payload, **kw):
            captured.append(dict(payload))
            return {"ok": True, "outcome": "anchored"}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)
        result = await channel_server._invoke_tool_for_tests(
            "ask", {"question": "What's the DB name?", "options": []},
        )
        assert len(captured) == 1
        assert captured[0]["options"] == []
        assert result == {"ok": True, "outcome": "anchored"}

    async def test_reply_transmits_request_id_and_projection_hash(
        self, channel_server, monkeypatch,
    ):
        captured: list = []

        async def fake_post(path, payload, **kw):
            captured.append(dict(payload))
            return {"ok": True, "message_id": 7}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)
        await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "ignored", "text": "hi there"},
        )
        p = captured[0]
        assert len(p["request_id"]) == 32
        assert p["projection_hash"] == channel_server._reply_projection_hash(
            "hi there",
        )
        assert "chat_id" not in p  # D2 still holds

    async def test_ingress_hash_matches_relay_projection_hash(self, channel_server):
        """The channel's INLINE projection hash must be byte-for-byte identical
        to the relay's ``output_sequencer.projection_hash`` of the tool_use
        frame — otherwise a registered intent never binds its content block.
        This is the REAL ingress hash path the §2 reversed-arrival regression
        relies on."""
        from channels.output_sequencer import projection_hash, ASK_TOOL, REPLY_TOOL

        # ask, timeout omitted: frame has no timeout_s → project_args .get()=None.
        assert channel_server._ask_projection_hash("Q?", ["A", "B"], None) == \
            projection_hash(ASK_TOOL, {"question": "Q?", "options": ["A", "B"]})
        # ask, explicit int timeout: no float coercion divergence.
        assert channel_server._ask_projection_hash("Q?", ["A", "B"], 100) == \
            projection_hash(
                ASK_TOOL, {"question": "Q?", "options": ["A", "B"], "timeout_s": 100})
        # reply: frame carries chat_id+text; projection drops chat_id.
        assert channel_server._reply_projection_hash("hello") == \
            projection_hash(REPLY_TOOL, {"chat_id": "x", "text": "hello"})

    async def test_client_timeout_total_matches_timeout_s_plus_15(
        self, channel_server, monkeypatch,
    ):
        captured: dict = {}

        class _FakeResp:
            def raise_for_status(self):
                pass

            async def json(self):
                return {"ok": True, "outcome": "no_answer"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            def __init__(self, *, connector=None, timeout=None):
                captured["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, json=None):
                captured["payload"] = json
                return _FakeResp()

        monkeypatch.setattr(channel_server.aiohttp, "ClientSession", _FakeSession)

        result = await channel_server._invoke_tool_for_tests(
            "ask", {"question": "Proceed?", "options": ["A", "B"], "timeout_s": 100},
        )
        assert captured["timeout"].total == 100 + 15
        assert result == {"ok": True, "outcome": "no_answer"}

    async def test_timeout_s_is_clamped_into_payload(
        self, channel_server, monkeypatch,
    ):
        captured: list = []

        async def fake_post(path, payload, **kw):
            captured.append(dict(payload))
            return {"ok": True, "outcome": "no_answer"}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        await channel_server._invoke_tool_for_tests(
            "ask", {"question": "Q?", "options": ["A", "B"], "timeout_s": 20},
        )
        await channel_server._invoke_tool_for_tests(
            "ask", {"question": "Q?", "options": ["A", "B"], "timeout_s": 600},
        )
        assert captured[0]["timeout_s"] == 30.0
        assert captured[1]["timeout_s"] == 570.0

    async def test_retries_with_same_request_id_on_transport_error(
        self, channel_server, monkeypatch,
    ):
        """A transport-level retry (inside _internal_post) is a reattach --
        the SAME request_id must go out on every attempt, never a fresh
        one. Shrinks the retry backoff schedule (a plain data value, not
        asyncio.sleep) so the test stays fast without touching the shared
        asyncio module."""
        monkeypatch.setattr(
            channel_server, "_RETRY_DELAYS_S", (0.001, 0.001, 0.001),
        )

        import aiohttp

        posted_payloads: list = []
        attempt = {"n": 0}

        class _FakeOkResp:
            def raise_for_status(self):
                pass

            async def json(self):
                return {"ok": True, "outcome": "no_answer"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeFailingResp:
            async def __aenter__(self):
                raise aiohttp.ClientConnectionError("boom")

            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            def __init__(self, *, connector=None, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, json=None):
                posted_payloads.append(dict(json))
                attempt["n"] += 1
                if attempt["n"] < 3:
                    return _FakeFailingResp()
                return _FakeOkResp()

        monkeypatch.setattr(channel_server.aiohttp, "ClientSession", _FakeSession)

        result = await channel_server._invoke_tool_for_tests(
            "ask", {"question": "Proceed?", "options": ["A", "B"]},
        )
        assert len(posted_payloads) == 3
        rids = {p["request_id"] for p in posted_payloads}
        assert len(rids) == 1  # SAME request_id across every retry attempt
        assert result == {"ok": True, "outcome": "no_answer"}

    async def test_finally_posts_ask_cancel_on_genuine_cancellation(
        self, channel_server, monkeypatch,
    ):
        calls: list = []
        started = asyncio.Event()

        async def fake_internal_post(path, payload, **kw):
            calls.append((path, dict(payload)))
            if path == "/internal/channel/ask":
                started.set()
                await asyncio.sleep(10)  # never completes on its own
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_internal_post)

        task = asyncio.create_task(channel_server._invoke_tool_for_tests(
            "ask", {"question": "Proceed?", "options": ["A", "B"]},
        ))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        ask_calls = [c for c in calls if c[0] == "/internal/channel/ask"]
        cancel_calls = [c for c in calls if c[0] == "/internal/channel/ask_cancel"]
        assert len(ask_calls) == 1
        assert len(cancel_calls) == 1
        # SAME request_id across both calls (reattach identity).
        assert ask_calls[0][1]["request_id"] == cancel_calls[0][1]["request_id"]

    async def test_finally_does_not_post_ask_cancel_on_success(
        self, channel_server, monkeypatch,
    ):
        calls: list = []

        async def fake_post(path, payload, **kw):
            calls.append(path)
            return {"ok": True, "outcome": "no_answer"}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        result = await channel_server._invoke_tool_for_tests(
            "ask", {"question": "Proceed?", "options": ["A", "B"]},
        )
        assert result == {"ok": True, "outcome": "no_answer"}
        assert calls == ["/internal/channel/ask"]

    async def test_callback_data_fits_64_bytes_for_max_options(self):
        """W5 design invariant the `ask` tool relies on: v1|engagement_ask|
        <32-hex-rid>|<idx> must fit Telegram's 64-byte callback_data cap for
        the max option count (8) -- the tool generates a full-length
        uuid4().hex request_id."""
        rid = uuid.uuid4().hex
        for i in range(8):
            data = f"v1|engagement_ask|{rid}|{i}"
            assert len(data.encode("utf-8")) <= 64


class TestChannelServerTransportIsTcp8100:
    """Containment Stage 2 (Task 9): the channel server can't reach the
    0600 root Unix socket from a uid-dropped process, so it must talk TCP
    to svc_casa_mcp's token-authed 127.0.0.1:8100 listener instead."""

    async def test_channel_server_posts_to_8100_not_unix_socket(
        self, channel_server, monkeypatch,
    ):
        captured: dict = {}

        class _FakeResp:
            def raise_for_status(self):
                pass

            async def json(self):
                return {"ok": True}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            def __init__(self, *, connector=None, timeout=None):
                captured["connector"] = connector

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, json=None):
                captured["url"] = url
                captured["payload"] = json
                return _FakeResp()

        monkeypatch.setattr(channel_server.aiohttp, "ClientSession", _FakeSession)

        result = await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "x", "text": "hi"},
        )

        assert result == {"ok": True}
        # Must NOT be a UnixConnector — must be a plain TCPConnector.
        assert not hasattr(captured["connector"], "path"), (
            "channel server still builds a UnixConnector — Task 9 requires "
            "a TCPConnector to 127.0.0.1:8100"
        )
        assert captured["url"].startswith("http://127.0.0.1:8100/"), (
            f"expected the channel server to POST to 127.0.0.1:8100, "
            f"got {captured['url']!r}"
        )

    async def test_channel_ask_forward_uses_long_timeout(
        self, channel_server, monkeypatch,
    ):
        """The `ask` client-side timeout must never be truncated to the
        180s tools/call default — it must stay >= the broker's own
        deadline (clamped_timeout + pad, up to 585s)."""
        captured: dict = {}

        class _FakeResp:
            def raise_for_status(self):
                pass

            async def json(self):
                return {"ok": True, "outcome": "no_answer"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            def __init__(self, *, connector=None, timeout=None):
                captured["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, json=None):
                return _FakeResp()

        monkeypatch.setattr(channel_server.aiohttp, "ClientSession", _FakeSession)

        await channel_server._invoke_tool_for_tests(
            "ask", {"question": "Proceed?", "options": ["A", "B"],
                    "timeout_s": 300},
        )
        assert captured["timeout"].total >= 315, (
            f"ask forward timeout {captured['timeout'].total!r} must stay "
            ">= the client-side ask deadline, not the 180s tools/call default"
        )
