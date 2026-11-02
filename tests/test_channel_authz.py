"""Tests for channel_authz.py — fail-closed channel-capability authorization
at direct-ingress seams (spec A3).

A resident is reachable on an ingress only if its ``channels:`` list
declares the matching capability token: voice requires ``ha_voice``,
webhook requires ``webhook``. Unknown ingress names fail closed. These
tests cover the pure ``agent_allowed_on`` predicate plus the three
integration seams that gate on it: the voice SSE handler, the voice WS
utterance handler, and the ``/invoke/{agent}`` handler.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from voice_auth_helpers import SigningVoiceClient, VOICE_TEST_SECRET

from bus import MessageBus
from casa_core_middleware import cid_middleware
from channel_authz import CHANNEL_CAPABILITY, agent_allowed_on
from channels.voice.channel import VoiceChannel

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# agent_allowed_on — pure unit tests
# ---------------------------------------------------------------------------


class _Cfg:
    def __init__(self, channels):
        self.channels = channels


class TestAgentAllowedOn:
    def test_capability_map(self):
        assert CHANNEL_CAPABILITY == {"voice": "ha_voice", "webhook": "webhook"}

    def test_voice_requires_ha_voice(self):
        assert agent_allowed_on("voice", _Cfg(["ha_voice"])) is True
        assert agent_allowed_on("voice", _Cfg(["telegram"])) is False
        assert agent_allowed_on("voice", _Cfg(["webhook"])) is False

    def test_webhook_requires_webhook(self):
        assert agent_allowed_on("webhook", _Cfg(["webhook"])) is True
        assert agent_allowed_on("webhook", _Cfg(["telegram"])) is False
        assert agent_allowed_on("webhook", _Cfg(["ha_voice"])) is False

    def test_unknown_ingress_is_false(self):
        assert agent_allowed_on("telegram", _Cfg(["telegram"])) is False
        assert agent_allowed_on("", _Cfg(["ha_voice", "webhook"])) is False

    def test_empty_channels_denied_everywhere(self):
        assert agent_allowed_on("voice", _Cfg([])) is False
        assert agent_allowed_on("webhook", _Cfg([])) is False

    def test_missing_channels_attr_denied(self):
        class _NoChannels:
            pass

        assert agent_allowed_on("voice", _NoChannels()) is False
        assert agent_allowed_on("webhook", _NoChannels()) is False

    def test_none_channels_denied(self):
        assert agent_allowed_on("voice", _Cfg(None)) is False


# ---------------------------------------------------------------------------
# SSE integration: unauthorized resident gets the SAME 404 body as an
# unknown agent_role (no existence oracle).
# ---------------------------------------------------------------------------


class _FakeAgentConfig:
    class tts:
        tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 800})()
    role = "assistant"
    voice_errors: dict = {}
    channels: list[str] = ["telegram"]  # NOT ha_voice — deliberately unauthorized


@pytest.fixture
async def voice_app_unauthorized_resident():
    """A resident ('assistant') is registered on the voice config map but
    does NOT declare ha_voice — must be treated identically to an
    unregistered agent_role. ``bus.request`` is an AsyncMock so a
    regression that DID dispatch is caught by ``assert_not_awaited`` (and
    fails fast) rather than hanging on the real 300s bus timeout."""
    bus = MessageBus()
    bus.request = AsyncMock(name="bus.request")

    channel = VoiceChannel(
        bus=bus,
        default_agent="assistant",
        # #193 (v0.117.0): voice is fail-closed, so this authz test must get
        # PAST auth to reach the capability gate it actually exercises — the
        # signing client supplies a valid signature for this secret.
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"assistant": _FakeAgentConfig()},
        idle_timeout=300,
    )
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client, bus, channel


@pytest.mark.asyncio
class TestSSEChannelAuthz:
    async def test_unauthorized_resident_gets_same_404_as_unknown_role(
        self, voice_app_unauthorized_resident,
    ):
        client, bus, channel = voice_app_unauthorized_resident

        resp_unauthorized = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "assistant", "scope_id": "s"},
        )
        body_unauthorized = await resp_unauthorized.read()

        resp_unknown = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "ghost", "scope_id": "s"},
        )
        body_unknown = await resp_unknown.read()

        assert resp_unauthorized.status == 404
        assert resp_unknown.status == 404
        # Byte-for-byte identical bodies — no existence oracle distinguishing
        # "role doesn't exist" from "role exists but isn't voice-reachable".
        assert body_unauthorized == body_unknown
        assert json.loads(body_unauthorized) == {"error": "unknown agent_role"}

        # The gate fires before any bus dispatch — a regression that let the
        # turn through would fail here instead of hanging on a real timeout.
        bus.request.assert_not_awaited()
        # And no voice-pool/session entry was created for the denied scope.
        assert channel.pool.get("s", role="assistant") is None
        assert channel.pool.get("s", role="ghost") is None


# ---------------------------------------------------------------------------
# WS integration: unauthorized resident gets an `unknown_agent` error frame,
# and the bus is never dispatched.
# ---------------------------------------------------------------------------


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data):
        self.sent.append(data)


@pytest.mark.asyncio
class TestWSChannelAuthz:
    async def test_unauthorized_and_unknown_roles_are_indistinguishable(self):
        """Both an unregistered role AND a registered-but-voice-unauthorized
        role must yield IDENTICAL error frames (no existence oracle), never
        dispatch to the bus, and never mutate the voice pool. ``bus.request``
        is an AsyncMock so a regression that dispatched fails ``assert_not_
        awaited`` rather than hanging on the real 300s bus timeout."""
        bus = MessageBus()
        bus.request = AsyncMock(name="bus.request")

        channel = VoiceChannel(
            bus=bus,
            default_agent="assistant",
            webhook_secret="",
            sse_path="/api/converse",
            ws_path="/api/converse/ws",
            agent_configs={"assistant": _FakeAgentConfig()},  # channels=["telegram"]
            idle_timeout=300,
        )

        # Same UID on both frames so the only variable is the role — the
        # emitted frames must then be byte-identical.
        ws_unauthorized = _FakeWS()
        await channel._run_ws_utterance(
            ws_unauthorized,
            {"text": "hi", "agent_role": "assistant", "scope_id": "s"},
            "u1",
            0.0,
        )
        ws_unknown = _FakeWS()
        await channel._run_ws_utterance(
            ws_unknown,
            {"text": "hi", "agent_role": "ghost", "scope_id": "s"},
            "u1",
            0.0,
        )

        assert ws_unauthorized.sent == ws_unknown.sent, (
            "unauthorized and unknown roles must be indistinguishable"
        )
        assert len(ws_unauthorized.sent) == 1
        frame = ws_unauthorized.sent[0]
        assert frame["type"] == "error"
        assert frame["kind"] == "unknown_agent"

        # Never dispatched, never mutated the pool for either role.
        bus.request.assert_not_awaited()
        assert channel.pool.get("s", role="assistant") is None
        assert channel.pool.get("s", role="ghost") is None


# ---------------------------------------------------------------------------
# /invoke/{agent} integration: voice-only resident 404s; webhook resident
# passes through to the bus.
# ---------------------------------------------------------------------------


class _StubResult:
    content = "ok"


class _StubBus:
    def __init__(self):
        self.last_msg = None
        self.dispatched = False

    async def request(self, msg, timeout=300):
        self.dispatched = True
        self.last_msg = msg
        return _StubResult()


_INVOKE_SECRET = "chan-authz-secret"


def _make_invoke_app(bus, role_configs):
    from casa_core import _make_invoke_handler
    from rate_limit import RateLimiter

    handler = _make_invoke_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),  # 0 = disabled
        webhook_secret=_INVOKE_SECRET,  # Release A: /invoke is fail-closed
        bus=bus,
        assistant_role="assistant",
        role_configs=role_configs,
    )
    app = web.Application(middlewares=[cid_middleware])
    app.router.add_post("/invoke/{agent}", handler)
    return app


async def _post_invoke(client, path, obj):
    import hashlib
    import hmac
    body = json.dumps(obj).encode()
    sig = hmac.new(_INVOKE_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return await client.post(
        path, data=body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": sig},
    )


@pytest.mark.asyncio
class TestInvokeChannelAuthz:
    async def test_voice_only_resident_404s(self):
        bus = _StubBus()
        role_configs = {"butler": _Cfg(["ha_voice"])}
        app = _make_invoke_app(bus, role_configs)
        async with TestClient(TestServer(app)) as client:
            resp = await _post_invoke(client, "/invoke/butler", {"prompt": "hi"})
            assert resp.status == 404
            assert (await resp.json()) == {"error": "unknown agent"}
            assert bus.dispatched is False

    async def test_webhook_resident_passes_through(self):
        bus = _StubBus()
        role_configs = {"assistant": _Cfg(["telegram", "webhook"])}
        app = _make_invoke_app(bus, role_configs)
        async with TestClient(TestServer(app)) as client:
            resp = await _post_invoke(client, "/invoke/assistant", {"prompt": "hi"})
            assert resp.status == 200
            assert (await resp.json()) == {"response": "ok"}
            assert bus.dispatched is True
