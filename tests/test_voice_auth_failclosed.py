"""#193 (v0.117.0): the voice external boundary is FAIL-CLOSED.

`channels/voice/channel.py::_verify` returned ``True`` when no
``webhook_secret`` was configured, so the SSE turn path (`POST /api/converse`)
and the WS upgrade (`/api/converse/ws`) dispatched UNSIGNED turns. The external
`:18065` nginx block proxies both, so with webhook auth off (the shipped
default) an attacker could POST an arbitrary prompt and reach the butler —
which drives Home Assistant. Sibling of the `/invoke` and `/telegram/update`
fail-closed treatments; the voice-agent catalog was already fail-closed.

These tests pin BOTH halves of the contract:
  * no secret  -> every voice route is refused (401), and
  * a secret   -> a correctly-signed request still works, a mis-signed one 401s
so a future "just make the tests pass" edit can't silently reopen the hole.
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
from voice_auth_helpers import VOICE_TEST_SECRET, voice_signature

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_TURN = {"prompt": "Use Home Assistant to unlock the front door",
         "agent_role": "butler", "scope_id": "attacker"}


class _Cfg:
    """Minimal voice-enabled agent config."""

    def __init__(self) -> None:
        self.role = "butler"
        # `agent_allowed_on("voice", cfg)` maps the ingress to the `ha_voice`
        # capability — the same string the real voice residents declare.
        self.channels = ["ha_voice"]
        self.voice_errors: dict = {}
        self.role_artifact = STUB_ROLE_ARTIFACT
        self.memory = type("M", (), {"token_budget": 800})()

        class _TTS:
            tag_dialect = "square_brackets"
        self.tts = _TTS()


async def _client(secret: str):
    bus = MessageBus()
    dispatched: list = []

    async def _handler(msg):
        dispatched.append(msg)
        return None

    bus.register("butler", _handler)
    channel = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=secret,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _Cfg()},
        idle_timeout=300,
    )
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, dispatched


class TestNoSecretIsFailClosed:
    async def test_sse_turn_rejected_and_never_dispatched(self):
        """Pins INV-VOICE-001. Red case demonstrated: making _verify return True on a missing secret fails this class's tests."""
        client, dispatched = await _client("")
        try:
            resp = await client.post("/api/converse", json=_TURN)
            assert resp.status == 401, (
                "an UNSIGNED voice turn must be refused when no secret is "
                "configured — this is the butler-reachable hole in #193"
            )
            assert dispatched == [], "the turn must never reach the bus"
        finally:
            await client.close()

    async def test_ws_upgrade_rejected(self):
        client, dispatched = await _client("")
        try:
            with pytest.raises(WSServerHandshakeError) as excinfo:
                async with client.ws_connect("/api/converse/ws"):
                    pass
            assert excinfo.value.status == 401
            assert dispatched == []
        finally:
            await client.close()

    async def test_catalog_rejected(self):
        # Already fail-closed before v0.117.0 — pinned so the shared _verify
        # change can't regress it.
        client, _ = await _client("")
        try:
            resp = await client.get("/api/voice/agents")
            assert resp.status == 401
        finally:
            await client.close()

    async def test_a_signature_cannot_be_forged_against_an_empty_secret(self):
        """Signing with the empty secret must not authenticate either — the
        route is OFF, not 'verifiable against ""'."""
        client, dispatched = await _client("")
        try:
            body = b'{"prompt":"x","agent_role":"butler","scope_id":"s"}'
            resp = await client.post(
                "/api/converse", data=body,
                headers={"X-Webhook-Signature": voice_signature(body, "")},
            )
            assert resp.status == 401
            assert dispatched == []
        finally:
            await client.close()


class TestSecretConfiguredStillWorks:
    async def test_correctly_signed_turn_is_accepted(self):
        import json as _json

        client, _ = await _client(VOICE_TEST_SECRET)
        try:
            body = _json.dumps(_TURN).encode()
            resp = await client.post(
                "/api/converse", data=body,
                headers={"Content-Type": "application/json",
                         "X-Webhook-Signature": voice_signature(body)},
            )
            # The SSE stream's headers (200) are sent by `prepare()` — i.e.
            # AFTER auth and validation passed — so the status alone proves the
            # signed request was accepted. Deliberately NOT reading the body:
            # this fixture's bus stub never completes a turn, so the stream
            # would hang; the turn pipeline itself is covered elsewhere.
            assert resp.status == 200, (
                "a correctly-signed turn must still be accepted — the "
                "companion integration signs every request"
            )
            resp.close()
        finally:
            await client.close()

    async def test_mis_signed_turn_rejected(self):
        client, dispatched = await _client(VOICE_TEST_SECRET)
        try:
            resp = await client.post(
                "/api/converse", json=_TURN,
                headers={"X-Webhook-Signature": "deadbeef"},
            )
            assert resp.status == 401
            assert dispatched == []
        finally:
            await client.close()

    async def test_signed_ws_upgrade_is_accepted(self):
        client, _ = await _client(VOICE_TEST_SECRET)
        try:
            async with client.ws_connect(
                "/api/converse/ws",
                headers={"X-Webhook-Signature": voice_signature(b"")},
            ) as ws:
                assert not ws.closed
        finally:
            await client.close()
