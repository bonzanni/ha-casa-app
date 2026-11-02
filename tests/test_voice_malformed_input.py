"""#287: malformed-but-authenticated-shaped voice input fails closed.

Three edge cases each turned a malformed input into a 500 (or a closed
socket) where a typed refusal is intended. All are reachable only by a
caller already holding the webhook secret — the impact is robustness, not
authorization — but a robustness hole on an authenticated surface is still
a hole:

  * a non-ASCII ``X-Webhook-Signature`` made ``hmac.compare_digest`` raise
    ``TypeError`` on the SSE and WS paths (the catalog pre-checked) → 500
    instead of 401;
  * valid JSON with a non-object top level on SSE hit ``payload.get`` on a
    list/str/number → ``AttributeError`` → 500 instead of 400;
  * an unhashable capability entry in a WS registration frame reached
    ``frozenset(requested)`` after the element-type check had already
    failed → ``TypeError`` with no per-frame catch → closed socket instead
    of a refused registration.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from bus import MessageBus
from casa_core_middleware import cid_middleware
from channels.voice.channel import VoiceChannel
from channels.voice.routes import VoiceRouteRegistry, VoiceWsConnection
from voice_auth_helpers import VOICE_TEST_SECRET, voice_signature

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _Cfg:
    def __init__(self) -> None:
        self.role = "butler"
        self.channels = ["ha_voice"]
        self.voice_errors: dict = {}
        self.role_artifact = STUB_ROLE_ARTIFACT
        self.memory = type("M", (), {"token_budget": 800})()

        class _TTS:
            tag_dialect = "square_brackets"
        self.tts = _TTS()


async def _client():
    bus = MessageBus()
    bus.register("butler", AsyncMock(return_value=None))
    channel = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _Cfg()},
        idle_timeout=300,
    )
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


class TestNonAsciiSignature:
    """Case 1: a non-ASCII signature header is a 401, not a 500."""

    async def test_sse_non_ascii_signature_is_401(self):
        client = await _client()
        try:
            resp = await client.post(
                "/api/converse", data=b"{}",
                headers={"X-Webhook-Signature": "café-signature"},
            )
            assert resp.status == 401
        finally:
            await client.close()

    async def test_ws_non_ascii_signature_is_401(self):
        client = await _client()
        try:
            with pytest.raises(WSServerHandshakeError) as err:
                async with client.ws_connect(
                    "/api/converse/ws",
                    headers={"X-Webhook-Signature": "café-signature"},
                ):
                    pass
            assert err.value.status == 401
        finally:
            await client.close()


class TestNonObjectJsonTopLevel:
    """Case 2: valid JSON that is not an object is a 400, not a 500."""

    @pytest.mark.parametrize("body", [b"[1, 2, 3]", b'"a string"', b"42"])
    async def test_sse_non_object_json_is_400(self, body):
        client = await _client()
        try:
            resp = await client.post(
                "/api/converse", data=body,
                headers={"X-Webhook-Signature": voice_signature(body)},
            )
            assert resp.status == 400
        finally:
            await client.close()


class TestUnhashableCapabilities:
    """Case 3: an unhashable capability entry refuses the registration and
    leaves the socket usable — it must not close the connection."""

    async def test_ws_unhashable_capability_refused_socket_survives(self):
        client = await _client()
        try:
            async with client.ws_connect(
                "/api/converse/ws",
                headers={"X-Webhook-Signature": voice_signature(b"")},
            ) as ws:
                await ws.send_json({
                    "type": "voice_route_register",
                    "protocol": 3,
                    "route_id": "route-1",
                    "agent_role": "butler",
                    "capabilities": [{}],
                })
                reply = await asyncio.wait_for(ws.receive_json(), timeout=5)
                assert reply["type"] == "voice_route_registered"
                assert reply["accepted_capabilities"] == []
                # The socket is still alive: a well-formed registration on
                # the SAME connection succeeds afterwards.
                await ws.send_json({
                    "type": "voice_route_register",
                    "protocol": 3,
                    "route_id": "route-1",
                    "agent_role": "butler",
                    "capabilities": [
                        "background_jobs", "endpoint_delivery",
                        "voice_handoff",
                    ],
                })
                reply2 = await asyncio.wait_for(ws.receive_json(), timeout=5)
                assert reply2["type"] == "voice_route_registered"
                assert sorted(reply2["accepted_capabilities"]) == [
                    "background_jobs", "endpoint_delivery", "voice_handoff",
                ]
        finally:
            await client.close()

    async def test_registry_register_unhashable_returns_refusal(self):
        registry = VoiceRouteRegistry(
            secret_present=True, agent_configs={"butler": _Cfg()},
        )

        class _Ws:
            def __init__(self):
                self.sent = []

            async def send_json(self, frame):
                self.sent.append(frame)

        conn = VoiceWsConnection(_Ws())
        bound = await registry.register(conn, {
            "type": "voice_route_register", "protocol": 3,
            "route_id": "r", "agent_role": "butler",
            "capabilities": [{}, "background_jobs"],
        })
        assert bound is None


class TestNonStringFields:
    """#287 round 2 (Sol + Terra): shape-check the fields an authenticated
    caller controls — non-str prompt/agent_role/scope_id/context on SSE and
    non-str utterance_id on WS all raised (500 / closed socket)."""

    async def test_sse_non_str_prompt_is_400(self):
        client = await _client()
        try:
            body = b'{"prompt": [1]}'
            resp = await client.post(
                "/api/converse", data=body,
                headers={"X-Webhook-Signature": voice_signature(body)},
            )
            assert resp.status == 400
        finally:
            await client.close()

    async def test_sse_non_str_agent_role_is_404(self):
        client = await _client()
        try:
            body = b'{"prompt": "hi", "agent_role": []}'
            resp = await client.post(
                "/api/converse", data=body,
                headers={"X-Webhook-Signature": voice_signature(body)},
            )
            assert resp.status == 404
        finally:
            await client.close()

    @pytest.mark.parametrize("payload", [
        b'{"prompt": "hi", "scope_id": [1]}',
        b'{"prompt": "hi", "context": [1]}',
    ])
    async def test_sse_non_str_scope_or_context_never_500s(self, payload):
        client = await _client()
        try:
            resp = await client.post(
                "/api/converse", data=payload,
                headers={"X-Webhook-Signature": voice_signature(payload)},
            )
            assert resp.status != 500
            # Status alone is a weak probe here (the SSE 200 header goes out
            # before the historical crash point): the real pin for the
            # context case is the sanitize_external_context unit test in
            # tests/test_provenance.py, which fails red on the pre-fix code.
        finally:
            await client.close()

    async def test_ws_non_str_cancel_uid_socket_survives(self):
        client = await _client()
        try:
            async with client.ws_connect(
                "/api/converse/ws",
                headers={"X-Webhook-Signature": voice_signature(b"")},
            ) as ws:
                # Non-EMPTY unhashable: a bare [] is falsy and was already
                # skipped; ["x"] reached tasks.get() and raised.
                await ws.send_json({"type": "cancel", "utterance_id": ["x"]})
                # Socket must still be usable afterwards.
                await ws.send_json({
                    "type": "voice_route_register",
                    "protocol": 3,
                    "route_id": "route-1",
                    "agent_role": "butler",
                    "capabilities": [
                        "background_jobs", "endpoint_delivery",
                        "voice_handoff",
                    ],
                })
                reply = await asyncio.wait_for(ws.receive_json(), timeout=5)
                assert reply["type"] == "voice_route_registered"
                assert sorted(reply["accepted_capabilities"]) == [
                    "background_jobs", "endpoint_delivery", "voice_handoff",
                ]
        finally:
            await client.close()
