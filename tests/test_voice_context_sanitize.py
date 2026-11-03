"""A:§3.5 sanitize-and-preserve for the voice SSE and WebSocket ingresses.

Both ``VoiceChannel._sse_handler`` (POST body ``context``) and
``VoiceChannel._run_ws_utterance`` (WS ``utterance`` frame ``context``) take
an EXTERNAL caller-supplied context dict and merge it into the dispatched
``BusMessage.context``. A caller must not be able to spoof Casa-reserved
provenance keys via that dict; ordinary keys must still round-trip.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_auth_helpers import SigningVoiceClient, VOICE_TEST_SECRET

from bus import BusMessage, MessageBus, MessageType
from casa_core_middleware import cid_middleware
from channels.voice.channel import VoiceChannel
from provenance import RESERVED_CONTEXT_KEYS

pytestmark = pytest.mark.asyncio


class _CapturingAgent:
    """Records the BusMessage.context it was dispatched and replies once."""

    def __init__(self, bus: MessageBus, role: str) -> None:
        self._bus = bus
        self._role = role
        self.captured: list[dict] = []

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        self.captured.append(dict(msg.context))
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="ok", reply_to=msg.id, channel=msg.channel,
            context=msg.context,
        )


class _FakeAgentConfig:
    class tts:
        tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 0})()
    role = "butler"
    voice_errors: dict[str, str] = {}
    channels: list[str] = ["ha_voice"]


@pytest.fixture
async def voice_app():
    bus = MessageBus()
    agent = _CapturingAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    channel = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeAgentConfig()}, idle_timeout=300,
    )
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client, agent, channel
    loop_task.cancel()


_MALICIOUS_CONTEXT = {
    "device_id": "kitchen-panel",
    "synthetic": "button",
    "button_answer": "yes",
    "execution_role": "butler",
    "message_type": "channel_in",
    "source": "telegram",
    "_voice_route_id": "spoofed-entry",
    "_voice_route_capabilities": ["background_jobs", "endpoint_delivery"],
    "_voice_job_control_id": "spoofed-control",
    "_origin_device_id": "spoofed-device",
    "_voice_transport": "ws",
    # A caller claiming its own delivery capability. Casa decides this from the
    # channel-derived offer alone; a promise a client invents for itself is a
    # promise nothing can keep (#233/#224).
    "_voice_delivery_offer": {
        "protocol": 3, "modality": "audio", "receipt": "playback_complete",
    },
}


class TestVoiceSSESanitize:
    async def test_reserved_keys_stripped_ordinary_keys_preserved(self, voice_app):
        client, agent, _channel = voice_app
        resp = await client.post("/api/converse", json={
            "prompt": "hi", "agent_role": "butler",
            "context": dict(_MALICIOUS_CONTEXT),
        })
        # Drain the SSE stream so the handler completes.
        await resp.read()

        assert agent.captured, "agent must have received a dispatched turn"
        ctx = agent.captured[0]
        assert ctx["device_id"] == "kitchen-panel"      # preserved
        assert ctx["_voice_transport"] == "sse"
        assert "_voice_route_id" not in ctx
        assert "_voice_route_capabilities" not in ctx
        assert "_voice_job_control_id" not in ctx
        assert "_origin_device_id" not in ctx
        assert "_voice_delivery_offer" not in ctx
        # Casa-owned keys still present.
        assert "chat_id" in ctx and "utterance_id" in ctx and "cid" in ctx


class TestVoiceWSSanitize:
    async def test_reserved_keys_stripped_ordinary_keys_preserved(self, voice_app):
        client, agent, _channel = voice_app
        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1", "text": "hi",
                "agent_role": "butler", "scope_id": "s",
                "context": dict(_MALICIOUS_CONTEXT),
            })
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                # S-1 (v0.82.0): a zero-speech turn ends with a typed
                # `empty_turn` error frame instead of a bare `done` — this
                # stub agent never speaks, so accept either terminal frame.
                if frame["type"] in ("done", "error"):
                    break

        assert agent.captured, "agent must have received a dispatched turn"
        ctx = agent.captured[0]
        assert ctx["device_id"] == "kitchen-panel"      # preserved
        assert ctx["_voice_transport"] == "ws"
        assert "_origin_device_id" not in ctx
        assert "_voice_route_id" not in ctx
        assert "_voice_route_capabilities" not in ctx
        assert "_voice_job_control_id" not in ctx
        assert "chat_id" in ctx and "utterance_id" in ctx and "cid" in ctx

    async def test_utterance_route_binding_is_snapshotted_at_ingress(
        self, voice_app,
    ):
        """#329: the WS reader schedules the utterance task without pinning
        the server-bound route fields, and the task later reads the MUTABLE
        connection binding — a registration frame processed before the
        queued task runs rebound an already-received utterance (and its
        deferred answer/handoff) to the new route. The task must prefer the
        ingress snapshot over the live connection."""
        _client, agent, channel = voice_app

        class ReboundConnection:
            # By the time the task runs, the socket re-registered as B.
            voice_route_id = "entry-B"
            voice_route_capabilities = frozenset({
                "background_jobs", "endpoint_delivery", "voice_handoff",
            })
            voice_job_control_id = "entry-B"

            async def send_json(self, frame):
                return None

        await channel._run_ws_utterance(
            ReboundConnection(),
            {
                "text": "hi", "agent_role": "butler", "scope_id": "s",
                "device_id": "kitchen",
                "_casa_route_snapshot": (
                    "entry-A",
                    frozenset({
                        "background_jobs", "endpoint_delivery",
                        "voice_handoff",
                    }),
                    "entry-A",
                ),
            },
            "u-snap",
            asyncio.get_running_loop().time() + 20.0,
        )

        ctx = agent.captured[-1]
        assert ctx["_voice_route_id"] == "entry-A"
        assert ctx["_voice_job_control_id"] == "entry-A"

    async def test_ws_reader_stamps_route_snapshot_on_the_frame(
        self, voice_app, monkeypatch,
    ):
        """#329 companion: the snapshot is stamped server-side at frame
        receipt, and a client-supplied value never survives ingress."""
        client, _agent, channel = voice_app
        recorded: list[dict] = []
        got = asyncio.Event()

        async def stub(ws, frame, uid, voice_deadline):
            recorded.append(frame)
            got.set()

        monkeypatch.setattr(channel, "_run_ws_utterance", stub)

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "voice_route_register", "protocol": 3,
                "route_id": "entry-A", "agent_role": "butler",
                "capabilities": [
                    "background_jobs", "endpoint_delivery", "voice_handoff",
                ],
            })
            await ws.receive_json()
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1", "text": "hi",
                "agent_role": "butler", "scope_id": "s",
                "_casa_route_snapshot": ["forged", [], "forged"],
            })
            await asyncio.wait_for(got.wait(), timeout=2.0)

        assert recorded[0]["_casa_route_snapshot"] == (
            "entry-A",
            frozenset({
                "background_jobs", "endpoint_delivery", "voice_handoff",
            }),
            "entry-A",
        )

    async def test_route_capability_comes_only_from_server_connection(
        self, voice_app,
    ):
        _client, agent, channel = voice_app

        class BoundConnection:
            voice_route_id = "entry-trusted"
            voice_route_capabilities = frozenset({
                "background_jobs", "endpoint_delivery",
            })
            voice_job_control_id = "entry-trusted"

            def __init__(self):
                self.sent = []

            async def send_json(self, frame):
                self.sent.append(frame)

        connection = BoundConnection()
        await channel._run_ws_utterance(
            connection,
            {
                "text": "hi", "agent_role": "butler", "scope_id": "s",
                "device_id": "device-trusted",
                "context": dict(_MALICIOUS_CONTEXT),
            },
            "u-bound",
            asyncio.get_running_loop().time() + 20.0,
        )

        ctx = agent.captured[-1]
        assert ctx["_voice_route_id"] == "entry-trusted"
        assert ctx["_voice_route_capabilities"] == frozenset({
            "background_jobs", "endpoint_delivery",
        })
        assert ctx["_voice_job_control_id"] == "entry-trusted"
        assert ctx["_origin_device_id"] == "device-trusted"
        assert ctx["_voice_transport"] == "ws"
        # The caller put an `audio` offer in its own context. The frame carried
        # none, so the answer is "this endpoint offered nothing" — the caller's
        # claim must not survive as a capability Casa then promises against.
        assert ctx.get("_voice_delivery_offer") is None

    async def test_the_delivery_offer_comes_only_from_the_frame(self, voice_app):
        """The trust boundary, stated as a test.

        Casa cannot see Home Assistant's entity registry; the authenticated
        integration is the authority on what a device can receive, and it says
        so in the frame. What Casa guarantees is narrower and is what this
        pins: the value it acts on is the one the CHANNEL derived from that
        frame, never one a turn's context claimed for itself.
        

        Pins INV-VOICE-005. Red case demonstrated: removing _voice_delivery_offer from RESERVED_CONTEXT_KEYS fails this file's tests.
        """
        _client, agent, channel = voice_app

        class BoundConnection:
            voice_route_id = "entry-trusted"
            voice_route_capabilities = frozenset({
                "background_jobs", "endpoint_delivery",
            })
            voice_job_control_id = "entry-trusted"

            def __init__(self):
                self.sent = []

            async def send_json(self, frame):
                self.sent.append(frame)

        await channel._run_ws_utterance(
            BoundConnection(),
            {
                "text": "hi", "agent_role": "butler", "scope_id": "s",
                "device_id": "device-trusted",
                "delivery_offer": {
                    "protocol": 3, "modality": "text", "receipt": "accepted",
                },
                # Contradicts the frame on purpose: audio is the stronger
                # promise, so preferring it would be the dangerous direction.
                "context": dict(_MALICIOUS_CONTEXT),
            },
            "u-offer",
            asyncio.get_running_loop().time() + 20.0,
        )

        offer = agent.captured[-1]["_voice_delivery_offer"]
        assert offer["modality"] == "text"
