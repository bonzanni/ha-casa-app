"""S-1 silent-turn UX edge (block-S live finding 2026-07-15, cid 93f501bb):
a voice turn that produces NO spoken output must not end with a bare
``done`` — the user hears silence with no cue that anything went wrong.

Live repro: a cold session exhausted max_turns on ToolSearch round-trips
and the SSE stream closed with ``event: done`` after zero blocks. The
handler must instead emit a typed ``empty_turn`` error frame with a spoken
persona line (voice_errors-overridable, like every other error kind).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from voice_auth_helpers import SigningVoiceClient, VOICE_TEST_SECRET

from bus import BusMessage, MessageBus, MessageType
from casa_core_middleware import cid_middleware
from channels.voice.channel import VoiceChannel

pytestmark = pytest.mark.unit


class SilentStubAgent:
    """Synthetic agent that completes a turn WITHOUT ever calling on_token
    and returns empty content — the max-turns-exhausted shape."""

    def __init__(self, bus: MessageBus, role: str) -> None:
        self._bus = bus
        self._role = role

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        return BusMessage(
            type=MessageType.RESPONSE,
            source=self._role,
            target=msg.source,
            content="",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )


class _FakeAgentConfig:
    class tts:
        tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 800})()
    role = "butler"
    voice_errors: dict[str, str] = {}
    channels: list[str] = ["ha_voice"]


def _make_channel(bus: MessageBus, cfg=None) -> VoiceChannel:
    return VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": cfg or _FakeAgentConfig()},
        idle_timeout=300,
    )


@pytest.fixture
async def silent_voice_app():
    bus = MessageBus()
    agent = SilentStubAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    channel = _make_channel(bus)
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client, bus
    loop_task.cancel()


async def _parse_sse_events(response) -> list[dict]:
    frames: list[dict] = []
    async for line in response.content:
        s = line.decode("utf-8").rstrip("\r\n")
        if s.startswith("event:"):
            frames.append({"event": s.split(":", 1)[1].strip()})
        elif s.startswith("data:") and frames:
            frames[-1]["data"] = json.loads(s.split(":", 1)[1].strip())
    return frames


@pytest.mark.asyncio
class TestSseSilentTurnFallback:
    async def test_zero_speech_turn_emits_empty_turn_error_not_bare_done(
        self, silent_voice_app,
    ):
        client, _bus = silent_voice_app
        resp = await client.post("/api/converse", json={
            "prompt": "ping", "agent_role": "butler", "scope_id": "s1",
        })
        assert resp.status == 200
        frames = await _parse_sse_events(resp)

        events = [f["event"] for f in frames]
        assert "block" not in events  # premise: the turn really was silent
        errors = [f for f in frames if f["event"] == "error"]
        assert errors, (
            f"a silent turn must emit an error frame, got only {events!r}"
        )
        assert errors[0]["data"]["kind"] == "empty_turn"
        assert errors[0]["data"]["spoken"], (
            "empty_turn must carry a non-empty spoken line by default"
        )
        # Mirrors every other error path: error frame, no done frame.
        assert "done" not in events

    async def test_empty_turn_line_overridable_via_voice_errors(self):
        bus = MessageBus()
        agent = SilentStubAgent(bus, "butler")
        bus.register("butler", agent.handle_message)
        loop_task = asyncio.create_task(bus.run_agent_loop("butler"))
        try:
            cfg = _FakeAgentConfig()
            cfg.voice_errors = {"empty_turn": "[apologetic] Custom empty."}
            channel = _make_channel(bus, cfg)
            app = web.Application(middlewares=[cid_middleware])
            channel.register_routes(app)
            async with TestClient(TestServer(app)) as _raw_client:
                client = SigningVoiceClient(_raw_client)
                resp = await client.post("/api/converse", json={
                    "prompt": "ping", "agent_role": "butler",
                    "scope_id": "s2",
                })
                frames = await _parse_sse_events(resp)
            errors = [f for f in frames if f["event"] == "error"]
            assert errors and errors[0]["data"]["spoken"] == (
                "[apologetic] Custom empty."
            )
        finally:
            loop_task.cancel()

    async def test_turn_with_speech_still_ends_with_done(self):
        """Regression guard: a turn that DID speak keeps the normal
        block(s)+done shape — no spurious empty_turn."""
        bus = MessageBus()

        class SpeakingAgent(SilentStubAgent):
            async def handle_message(self, msg):
                on_token = msg.context.get("_on_token")
                if on_token:
                    await on_token("[confident] All done.")
                return BusMessage(
                    type=MessageType.RESPONSE, source=self._role,
                    target=msg.source, content="[confident] All done.",
                    reply_to=msg.id, channel=msg.channel, context=msg.context,
                )

        agent = SpeakingAgent(bus, "butler")
        bus.register("butler", agent.handle_message)
        loop_task = asyncio.create_task(bus.run_agent_loop("butler"))
        try:
            channel = _make_channel(bus)
            app = web.Application(middlewares=[cid_middleware])
            channel.register_routes(app)
            async with TestClient(TestServer(app)) as _raw_client:
                client = SigningVoiceClient(_raw_client)
                resp = await client.post("/api/converse", json={
                    "prompt": "ping", "agent_role": "butler",
                    "scope_id": "s3",
                })
                frames = await _parse_sse_events(resp)
            events = [f["event"] for f in frames]
            assert "done" in events
            assert not any(
                f["event"] == "error" for f in frames
            )
        finally:
            loop_task.cancel()


@pytest.mark.asyncio
class TestWsSilentTurnFallback:
    async def test_zero_speech_utterance_emits_empty_turn_error(self):
        bus = MessageBus()
        agent = SilentStubAgent(bus, "butler")
        bus.register("butler", agent.handle_message)
        loop_task = asyncio.create_task(bus.run_agent_loop("butler"))
        try:
            channel = _make_channel(bus)
            app = web.Application(middlewares=[cid_middleware])
            channel.register_routes(app)
            async with TestClient(TestServer(app)) as _raw_client:
                client = SigningVoiceClient(_raw_client)
                ws = await client.ws_connect("/api/converse/ws")
                await ws.send_json({
                    "type": "utterance", "utterance_id": "u1",
                    "text": "ping", "agent_role": "butler",
                    "scope_id": "s4",
                })
                got = []
                while True:
                    frame = await asyncio.wait_for(
                        ws.receive_json(), timeout=5)
                    got.append(frame)
                    if frame["type"] in ("done", "error"):
                        break
                await ws.close()
            kinds = [f["type"] for f in got]
            assert "block" not in kinds  # premise: silent
            assert got[-1]["type"] == "error", (
                f"silent WS utterance must end in an error frame, got "
                f"{kinds!r}"
            )
            assert got[-1]["kind"] == "empty_turn"
            assert got[-1]["spoken"]
        finally:
            loop_task.cancel()
