"""Spec §3.1 — SSE transport of VoiceChannel.

Uses aiohttp TestClient + a stub bus request/response pair to drive a
turn end-to-end over HTTP without touching the SDK.
"""

import asyncio
import json
import logging
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from voice_auth_helpers import SigningVoiceClient, VOICE_TEST_SECRET

from bus import BusMessage, MessageBus, MessageType
from casa_core_middleware import cid_middleware
from channels.voice.channel import VoiceChannel
from error_kinds import VoiceToolLoopError

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


class StubAgent:
    """Synthetic agent: streams two tokens via on_token, returns a full text."""

    def __init__(self, bus: MessageBus, role: str) -> None:
        self._bus = bus
        self._role = role

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("[confident] Done.")
            await on_token("[confident] Done. Kitchen lights off.")
        return BusMessage(
            type=MessageType.RESPONSE,
            source=self._role,
            target=msg.source,
            content="[confident] Done. Kitchen lights off.",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )


class _FakeAgentConfig:
    class tts:
        tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 800})()
    role = "butler"
    voice_errors: dict[str, str] = {
        "voice_tool_loop": (
            "[apologetic] I couldn't resolve that cleanly. "
            "Try naming the device again?"
        ),
    }
    channels: list[str] = ["ha_voice"]


class _DummyMemory:
    async def ensure_session(self, *a, **kw): return None
    async def get_context(self, *a, **kw): return ""
    async def add_turn(self, *a, **kw): return None
    async def profile(self, bank: str) -> str: return ""


@pytest.fixture
async def voice_app():
    telemetry_clock = iter((10.0, 10.125))
    bus = MessageBus()
    agent = StubAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeAgentConfig()},
        memory=_DummyMemory(),
        idle_timeout=300,
        monotonic=lambda: next(telemetry_clock),
    )

    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client, bus
    loop_task.cancel()


@pytest.fixture
async def broken_voice_app(monkeypatch):
    """Fixture with monkeypatched bus.request that raises to trigger error handler."""
    bus = MessageBus()
    bus.register("butler", None)

    butler_cfg = _FakeAgentConfig()
    butler_cfg.voice_errors = {"unknown": "[flat] Tina-voice failure."}

    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": butler_cfg},
        memory=_DummyMemory(),
        idle_timeout=300,
    )

    async def fake_request(msg, timeout=300):
        raise RuntimeError("simulated SDK failure")

    # Replace the channel's bus.request method to raise unconditionally
    monkeypatch.setattr(channel._bus, "request", fake_request)

    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client, bus


@pytest.fixture
async def agent_error_voice_app(tmp_path):
    """Wire up a real Agent whose _process raises an UNKNOWN error, + a real
    VoiceChannel. Exercises the natural production path:
    Agent.handle_message catches → error_kind set → emit_error_line called →
    _error_sink fires event: error → SSE skips event: done.
    """
    from agent import Agent
    from config import (
        AgentConfig, CharacterConfig, MemoryConfig, SessionConfig,
        ToolsConfig, TTSConfig,
    )
    from mcp_registry import McpServerRegistry
    from session_registry import SessionRegistry
    from channels import ChannelManager

    bus = MessageBus()

    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role="butler",
        model="claude-haiku-4-5",
        system_prompt="Butler.",
        character=CharacterConfig(name="Tina"),
        tools=ToolsConfig(),
        memory=MemoryConfig(token_budget=800, read_strategy="cached"),
        session=SessionConfig(strategy="pooled", idle_timeout=300),
        tts=TTSConfig(tag_dialect="square_brackets"),
        # RuntimeError classifies as UNKNOWN → this key is used.
        voice_errors={"unknown": "[apologetic] Natural-path Tina voice failure."},
        channels=["ha_voice"],
    )

    channel_manager = ChannelManager()
    voice_channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": cfg},
        memory=_DummyMemory(),
        idle_timeout=300,
    )
    channel_manager.register(voice_channel)

    agent = Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=channel_manager,
    )

    async def _raise(*args, **kwargs):
        raise RuntimeError("SDK-style failure")

    agent._process = _raise  # type: ignore[assignment]

    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    app = web.Application(middlewares=[cid_middleware])
    voice_channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client
    loop_task.cancel()


@pytest.fixture
async def agent_partial_then_error_voice_app(tmp_path):
    """#594: a real Agent that STREAMS a partial answer and only then faults.

    The listener has already heard the first half — voice writes are
    irreversible — so the error line that follows contradicts speech Casa
    already stood behind. Same wiring as ``agent_error_voice_app``; the only
    difference is that ``_process`` emits real speech before raising.
    """
    from agent import Agent
    from config import (
        AgentConfig, CharacterConfig, MemoryConfig, SessionConfig,
        ToolsConfig, TTSConfig,
    )
    from mcp_registry import McpServerRegistry
    from session_registry import SessionRegistry
    from channels import ChannelManager

    bus = MessageBus()

    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role="butler",
        model="claude-haiku-4-5",
        system_prompt="Butler.",
        character=CharacterConfig(name="Tina"),
        tools=ToolsConfig(),
        memory=MemoryConfig(token_budget=800, read_strategy="cached"),
        session=SessionConfig(strategy="pooled", idle_timeout=300),
        tts=TTSConfig(tag_dialect="square_brackets"),
        voice_errors={"unknown": "[apologetic] Tina could not finish that."},
        channels=["ha_voice"],
    )

    channel_manager = ChannelManager()
    voice_channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": cfg},
        memory=_DummyMemory(),
        idle_timeout=300,
    )
    channel_manager.register(voice_channel)

    agent = Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=channel_manager,
    )

    async def _stream_then_raise(msg, on_token=None, **kwargs):
        if on_token is not None:
            # A complete sentence, so the prosodic splitter flushes a REAL
            # block — which is what puts bytes on the wire.
            await on_token("[confident] The kitchen lights are off.")
        raise RuntimeError("SDK-style failure after partial output")

    agent._process = _stream_then_raise  # type: ignore[assignment]

    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    app = web.Application(middlewares=[cid_middleware])
    voice_channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client
    loop_task.cancel()


async def _sse_frames(resp) -> list[dict]:
    frames: list[dict] = []
    async for line in resp.content:
        s = line.decode("utf-8").rstrip("\r\n")
        if s.startswith("event:"):
            frames.append({"event": s.split(":", 1)[1].strip()})
        elif s.startswith("data:"):
            frames[-1]["data"] = json.loads(s.split(":", 1)[1].strip())
    return frames


class _PartialThenRaisingBus:
    """#594 round 2: voices a real block, then raises OUT OF `_bus.request`.

    This is the transport-level failure — a turn timeout, a shutdown — that
    never reaches `Agent.handle_message`'s classified error branch, so it lands
    in the handler's own outer `except`. Both reviewers reproduced an
    unretracted error frame here.
    """

    async def request(self, msg, timeout):
        await msg.context["_on_token"]("[confident] The kitchen lights are off.")
        raise RuntimeError("transport-level failure after partial output")


class _RetractionCfg:
    """Minimal agent config for the composition itself."""

    class tts:
        tag_dialect = "square_brackets"

    role = "butler"
    channels: list[str] = ["ha_voice"]

    def __init__(self, **voice_errors):
        self.voice_errors = dict(voice_errors)


async def _emit(cfg, *, already_spoke: bool) -> str:
    """Drive the production `emit_error_line` and return the spoken text."""
    from channels.voice.channel import VoiceChannel as _VC

    seen: list = []

    async def _sink(kind, spoken):
        seen.append((kind, spoken))

    channel = _VC(
        bus=None, default_agent="butler", webhook_secret="",
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": cfg}, memory=_DummyMemory(), idle_timeout=300,
    )
    ctx = {"_error_sink": _sink, "_spoken_any": lambda: already_spoke}
    assert await channel.emit_error_line("unknown", ctx, cfg) is True
    return seen[0][1]


@pytest.fixture
async def sse_transport_error_after_speech_app(tmp_path):
    """A real SSE route whose bus raises after voicing a block."""
    from config import (
        AgentConfig, CharacterConfig, MemoryConfig, SessionConfig,
        ToolsConfig, TTSConfig,
    )

    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role="butler", model="claude-haiku-4-5", system_prompt="Butler.",
        character=CharacterConfig(name="Tina"), tools=ToolsConfig(),
        memory=MemoryConfig(token_budget=800, read_strategy="cached"),
        session=SessionConfig(strategy="pooled", idle_timeout=300),
        tts=TTSConfig(tag_dialect="square_brackets"),
        voice_errors={"unknown": "[apologetic] Tina could not finish that."},
        channels=["ha_voice"],
    )
    channel = VoiceChannel(
        bus=MessageBus(), default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": cfg}, memory=_DummyMemory(), idle_timeout=300,
    )
    channel._bus = _PartialThenRaisingBus()
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        yield SigningVoiceClient(_raw_client)


@pytest.mark.asyncio
class TestTransportFaultAfterSpeechIsRetracted:
    """#594 round 2 (Sol + Terra, both S1): a failure that never reaches the
    agent's classified error branch — the 300s bus timeout is the reachable
    one — took the handler's own error path and skipped the retraction."""

    async def test_a_transport_level_fault_after_speech_is_retracted(
        self, sse_transport_error_after_speech_app,
    ):
        client = sse_transport_error_after_speech_app
        resp = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "s"},
        )
        assert resp.status == 200
        frames = await _sse_frames(resp)
        assert [f for f in frames if f["event"] == "block"], frames
        errors = [f for f in frames if f["event"] == "error"]
        assert len(errors) == 1, frames
        assert "disregard" in errors[0]["data"]["spoken"].lower(), errors[0]


class _TailOnlySpeechBus:
    """Streams text with NO sentence boundary, so the splitter holds all of it
    and the answer reaches the wire only as the FINAL TAIL block."""

    async def request(self, msg, timeout):
        await msg.context["_on_token"]("[confident] The kitchen lights are off")
        return None


@pytest.mark.asyncio
class TestSSETailBlockCountsAsSpeech:
    """#594 round 3 — the SSE twin of the socket's tail case. Found by
    mutating each delivery-recording site separately: this one killed no test,
    so the socket's coverage was standing in for it."""

    async def test_a_fault_after_a_tail_only_answer_is_retracted(
        self, monkeypatch,
    ):
        from channels.voice import channel as channel_mod
        from config import (
            AgentConfig, CharacterConfig, MemoryConfig, SessionConfig,
            ToolsConfig, TTSConfig,
        )

        writes: list[dict] = []
        real_write = channel_mod._write_sse

        async def _write_then_fail_on_done(response, event, data):
            writes.append({"event": event, "data": data})
            if event == "done":
                raise ConnectionResetError("closing transport")
            await real_write(response, event, data)

        monkeypatch.setattr(channel_mod, "_write_sse", _write_then_fail_on_done)

        cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
            role="butler", model="claude-haiku-4-5", system_prompt="Butler.",
            character=CharacterConfig(name="Tina"), tools=ToolsConfig(),
            memory=MemoryConfig(token_budget=800, read_strategy="cached"),
            session=SessionConfig(strategy="pooled", idle_timeout=300),
            tts=TTSConfig(tag_dialect="square_brackets"),
            voice_errors={"sdk_error": "[flat] Tina could not finish that."},
            channels=["ha_voice"],
        )
        channel = VoiceChannel(
            bus=MessageBus(), default_agent="butler",
            webhook_secret=VOICE_TEST_SECRET,
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"butler": cfg}, memory=_DummyMemory(),
            idle_timeout=300,
        )
        channel._bus = _TailOnlySpeechBus()
        app = web.Application(middlewares=[cid_middleware])
        channel.register_routes(app)
        async with TestClient(TestServer(app)) as raw:
            client = SigningVoiceClient(raw)
            resp = await client.post(
                "/api/converse",
                json={"prompt": "hi", "agent_role": "butler", "scope_id": "s"},
            )
            await resp.read()

        blocks = [w for w in writes if w["event"] == "block"]
        assert blocks and blocks[-1]["data"].get("final") is True, writes
        assert any(w["event"] == "done" for w in writes), writes
        errors = [w for w in writes if w["event"] == "error"]
        assert len(errors) == 1, writes
        assert "disregard" in errors[0]["data"]["spoken"].lower(), errors[0]


@pytest.mark.asyncio
class TestRetractionComposition:
    """A retraction with no reason after it is one of the outcomes #594
    exists to prevent — it must never be produced by the fix itself."""

    async def test_an_error_line_with_no_speakable_content_is_not_retracted(self):
        """Sol reproduced this: a schema-valid line that renders to nothing
        left the listener hearing only 'Disregard that —'."""
        spoken = await _emit(_RetractionCfg(unknown=" "), already_spoke=True)
        assert "disregard" not in spoken.lower(), spoken

    async def test_a_persona_can_switch_the_retraction_off(self):
        """`voice_errors: {retraction: ""}` is an explicit decision, not an
        absent key — documented in voice.md, so it has to be true."""
        cfg = _RetractionCfg(retraction="", unknown="[flat] reason")
        spoken = await _emit(cfg, already_spoke=True)
        assert "disregard" not in spoken.lower(), spoken
        assert "reason" in spoken

    async def test_a_tag_only_error_line_is_not_retracted(self):
        """Sol re-review, S1: `"[flat]"` is a delivery instruction with no
        sentence after it. Under `square_brackets` it renders to a non-empty
        string, so an emptiness check on the RENDERED text passes it through
        and the listener hears a retraction with no reason."""
        spoken = await _emit(_RetractionCfg(unknown="[flat]"), already_spoke=True)
        assert "disregard" not in spoken.lower(), spoken

    async def test_a_tag_only_retraction_is_not_prefixed(self):
        """The mirror: a retraction that says nothing aloud must not be
        counted as having retracted anything."""
        cfg = _RetractionCfg(retraction="[flat]", unknown="[flat] reason")
        spoken = await _emit(cfg, already_spoke=True)
        assert "reason" in spoken
        assert spoken.strip().startswith("[flat] reason"), spoken

    async def test_a_persona_can_reword_the_retraction(self):
        cfg = _RetractionCfg(retraction="[flat] Scratch that —",
                             unknown="[flat] reason")
        spoken = await _emit(cfg, already_spoke=True)
        assert "scratch that" in spoken.lower(), spoken
        assert spoken.lower().index("scratch") < spoken.lower().index("reason")


@pytest.mark.asyncio
class TestPartialThenFaultIsRetracted:
    """#594 — a half-spoken answer followed by an error line, with nothing
    marking the first half as void, leaves the listener holding a statement
    Casa did not stand behind."""

    async def test_the_error_frame_retracts_the_speech_already_voiced(
        self, agent_partial_then_error_voice_app,
    ):
        client = agent_partial_then_error_voice_app
        resp = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "s"},
        )
        assert resp.status == 200
        frames = await _sse_frames(resp)

        blocks = [f for f in frames if f["event"] == "block"]
        errors = [f for f in frames if f["event"] == "error"]
        # Premise of the test, asserted as SETUP: real speech reached the
        # wire before the fault. Without this the case under test never ran.
        assert blocks, f"no speech was voiced; frames={frames}"
        assert "kitchen lights are off" in blocks[0]["data"]["text"].lower()

        # ONE error frame carries both sentences — a retraction and its
        # reason must not be two writes that can split.
        assert len(errors) == 1, f"expected one error frame, got {errors}"
        spoken = errors[0]["data"]["spoken"]
        assert "could not finish" in spoken.lower(), spoken
        low = spoken.lower()
        assert "disregard" in low or "ignore" in low, (
            f"nothing retracts the speech already voiced: {spoken!r}")
        # The retraction comes FIRST — it is a correction of what was said.
        assert low.index("disregard" if "disregard" in low else "ignore") \
            < low.index("could not finish"), spoken

    async def test_a_turn_that_voiced_nothing_is_not_retracted(
        self, agent_error_voice_app,
    ):
        """Nothing was heard, so there is nothing to take back — the error
        line must stay exactly as it is."""
        client = agent_error_voice_app
        resp = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "s"},
        )
        frames = await _sse_frames(resp)
        errors = [f for f in frames if f["event"] == "error"]
        assert len(errors) == 1
        spoken = errors[0]["data"]["spoken"]
        assert "Natural-path Tina voice failure" in spoken
        low = spoken.lower()
        assert "disregard" not in low and "ignore" not in low, spoken


@pytest.mark.asyncio
class TestSSE:
    async def test_full_turn_emits_blocks_then_done(self, voice_app):
        client, _ = voice_app
        resp = await client.post(
            "/api/converse",
            json={
                "prompt": "turn off kitchen lights",
                "agent_role": "butler",
                "scope_id": "user-xyz",
                "channel": "voice",
                "context": {"device_id": "kitchen", "language": "en"},
            },
        )
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")

        frames = []
        async for line in resp.content:
            s = line.decode("utf-8").rstrip("\r\n")
            if s.startswith("event:"):
                evt = s.split(":", 1)[1].strip()
                frames.append({"event": evt})
            elif s.startswith("data:"):
                frames[-1]["data"] = json.loads(s.split(":", 1)[1].strip())

        events = [f["event"] for f in frames]
        assert "block" in events
        assert events[-1] == "done"

    async def test_first_real_block_logs_once_from_ingress_with_fake_clock(
        self, voice_app, caplog,
    ):
        client, _ = voice_app
        secret = "SECRET_SSE_PROMPT"
        with caplog.at_level(logging.INFO, logger="channels.voice.channel"):
            resp = await client.post(
                "/api/converse",
                json={
                    "prompt": secret,
                    "agent_role": "butler",
                    "scope_id": "latency-sse",
                },
            )
            await resp.read()

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "channels.voice.channel"
            and "voice_first_block" in record.getMessage()
        ]
        assert messages == [
            "voice_first_block role=butler transport=sse ms=125"
        ]
        assert secret not in caplog.text

    async def test_unknown_agent_role_404(self, voice_app):
        client, _ = voice_app
        resp = await client.post(
            "/api/converse",
            json={"prompt": "x", "agent_role": "ghost", "scope_id": "s"},
        )
        assert resp.status == 404

    async def test_missing_prompt_400(self, voice_app):
        client, _ = voice_app
        resp = await client.post("/api/converse", json={"scope_id": "s"})
        assert resp.status == 400

    async def test_error_frame_carries_persona_line(self, broken_voice_app):
        client, _ = broken_voice_app
        resp = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "s"},
        )
        assert resp.status == 200
        body = await resp.read()
        text = body.decode("utf-8")
        assert "event: error" in text
        assert "Tina-voice failure" in text

    async def test_agent_sdk_error_routes_through_emit_error_line(
        self, agent_error_voice_app,
    ):
        """Spec §7 end-to-end: agent catches SDK error, VoiceChannel emits
        event: error with persona line, no event: done is sent."""
        client = agent_error_voice_app
        resp = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "s"},
        )
        assert resp.status == 200
        body = (await resp.read()).decode("utf-8")
        assert "event: error" in body
        assert "Natural-path Tina voice failure" in body
        # Crucially: done MUST NOT be emitted after error.
        assert "event: done" not in body

    async def test_voice_tool_loop_emits_one_typed_error_without_payload_log(
        self, voice_app, caplog,
    ):
        client, bus = voice_app
        secret = "SECRET_VOICE_TOOL_INPUT_SSE"

        async def raise_voice_tool_loop(_msg, timeout=300):
            raise VoiceToolLoopError("validation_correction_exhausted")

        bus.request = raise_voice_tool_loop
        with caplog.at_level(logging.DEBUG):
            resp = await client.post(
                "/api/converse",
                json={
                    "prompt": secret,
                    "agent_role": "butler",
                    "scope_id": "guarded-sse",
                },
            )
            frames = await _parse_sse_events(resp)

        errors = [frame for frame in frames if frame["event"] == "error"]
        assert len(errors) == 1
        assert errors[0]["data"] == {
            "kind": "voice_tool_loop",
            "spoken": (
                "[apologetic] I couldn't resolve that cleanly. "
                "Try naming the device again?"
            ),
        }
        assert not any(frame["event"] == "done" for frame in frames)
        assert secret not in caplog.text

    async def test_client_context_cannot_clobber_computed_keys(self, voice_app):
        """L59/L8: a client-supplied context dict must not override the
        channel-computed chat_id/cid/utterance_id — those key the SDK
        session, rate limiter, and log correlation. Benign passthrough
        keys (e.g. device_id) must still survive."""
        client, bus = voice_app
        captured = {}
        orig_request = bus.request

        async def spy_request(msg, timeout=300):
            captured["msg"] = msg
            return await orig_request(msg, timeout=timeout)

        bus.request = spy_request

        resp = await client.post(
            "/api/converse",
            json={
                "prompt": "hi",
                "agent_role": "butler",
                "scope_id": "kitchen-satellite",
                "context": {
                    "chat_id": "living-room",       # must NOT win
                    "cid": "client-forged-cid",     # must NOT win
                    "utterance_id": "forged-uid",   # must NOT win
                    "device_id": "kitchen",          # passthrough key, must survive
                },
            },
        )
        assert resp.status == 200
        await resp.read()  # drain the SSE stream

        ctx = captured["msg"].context
        assert ctx["chat_id"] == "kitchen-satellite"      # channel-computed scope wins
        assert ctx["cid"] != "client-forged-cid"          # middleware cid wins
        assert ctx["utterance_id"] != "forged-uid"        # server-generated uuid wins
        assert ctx["device_id"] == "kitchen"              # benign client keys pass through
        assert callable(ctx["_on_token"]) and callable(ctx["_error_sink"])


# ---------------------------------------------------------------------------
# Rate limiting — per-scope_id token bucket (spec 5.2 §8)
# ---------------------------------------------------------------------------


@pytest.fixture
async def voice_app_with_limiter(request):
    """Factory fixture: parametrize capacity via request.param."""
    from rate_limit import RateLimiter

    capacity = getattr(request, "param", 2)

    bus = MessageBus()
    roles = ("butler", "concierge")
    for role in roles:
        agent = StubAgent(bus, role)
        bus.register(role, agent.handle_message)
    loop_tasks = [
        asyncio.create_task(bus.run_agent_loop(role)) for role in roles
    ]

    limiter = RateLimiter(capacity=capacity, window_s=60.0)

    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={role: _FakeAgentConfig() for role in roles},
        memory=_DummyMemory(),
        idle_timeout=300,
        rate_limiter=limiter,
    )

    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client, limiter
    for task in loop_tasks:
        task.cancel()


async def _parse_sse_events(response) -> list[dict]:
    """Read the full SSE stream into a list of {"event": ..., "data": ...}."""
    frames: list[dict] = []
    async for line in response.content:
        s = line.decode("utf-8").rstrip("\r\n")
        if s.startswith("event:"):
            frames.append({"event": s.split(":", 1)[1].strip()})
        elif s.startswith("data:") and frames:
            frames[-1]["data"] = json.loads(s.split(":", 1)[1].strip())
    return frames


@pytest.mark.asyncio
class TestRateLimit:
    @pytest.mark.parametrize("voice_app_with_limiter", [1], indirect=True)
    async def test_same_scope_has_independent_role_buckets(
        self, voice_app_with_limiter,
    ):
        client, _ = voice_app_with_limiter

        async def run_turn(role: str) -> list[dict]:
            response = await client.post(
                "/api/converse",
                json={
                    "prompt": "hi",
                    "agent_role": role,
                    "scope_id": "same-scope",
                },
            )
            assert response.status == 200
            return await _parse_sse_events(response)

        butler_first = await run_turn("butler")
        assert any(frame["event"] == "done" for frame in butler_first)

        concierge_first = await run_turn("concierge")
        assert any(frame["event"] == "done" for frame in concierge_first)

        butler_second = await run_turn("butler")
        assert any(
            frame["event"] == "error"
            and frame.get("data", {}).get("kind") == "rate_limit"
            for frame in butler_second
        )
        assert not any(frame["event"] == "done" for frame in butler_second)

    @pytest.mark.parametrize("voice_app_with_limiter", [2], indirect=True)
    async def test_over_limit_emits_rate_limit_error_frame(
        self, voice_app_with_limiter,
    ):
        client, _limiter = voice_app_with_limiter
        payload = {
            "prompt": "do stuff", "agent_role": "butler",
            "scope_id": "user-rate", "channel": "voice",
        }
        # Exhaust the capacity-2 bucket.
        for _ in range(2):
            r = await client.post("/api/converse", json=payload)
            assert r.status == 200
            frames = await _parse_sse_events(r)
            assert any(f["event"] == "done" for f in frames)

        # 3rd request: must emit event: error kind=rate_limit and NOT done.
        r = await client.post("/api/converse", json=payload)
        assert r.status == 200
        frames = await _parse_sse_events(r)
        kinds = [
            f.get("data", {}).get("kind") for f in frames
            if f["event"] == "error"
        ]
        assert "rate_limit" in kinds
        assert not any(f["event"] == "done" for f in frames)

    @pytest.mark.parametrize("voice_app_with_limiter", [0], indirect=True)
    async def test_capacity_zero_admits_unlimited_turns(
        self, voice_app_with_limiter,
    ):
        client, _ = voice_app_with_limiter
        for i in range(15):
            r = await client.post(
                "/api/converse",
                json={
                    "prompt": f"turn-{i}", "agent_role": "butler",
                    "scope_id": "user-x",
                },
            )
            assert r.status == 200
            frames = await _parse_sse_events(r)
            assert any(f["event"] == "done" for f in frames), (
                f"turn {i} did not complete"
            )

    @pytest.mark.parametrize("voice_app_with_limiter", [1], indirect=True)
    async def test_bucket_is_per_scope_id(self, voice_app_with_limiter):
        client, _ = voice_app_with_limiter

        # scope-A exhausts its 1-token bucket.
        r = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "A"},
        )
        assert r.status == 200
        assert any(f["event"] == "done" for f in await _parse_sse_events(r))

        # scope-B is admitted — fresh bucket.
        r = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "B"},
        )
        assert r.status == 200
        assert any(f["event"] == "done" for f in await _parse_sse_events(r))

        # scope-A's second request is rejected with rate_limit.
        r = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "A"},
        )
        frames = await _parse_sse_events(r)
        kinds = [
            f.get("data", {}).get("kind") for f in frames
            if f["event"] == "error"
        ]
        assert "rate_limit" in kinds


# ---------------------------------------------------------------------------
# AR-B prefix-divergence guard + AR-C time-cap (2026-07-11 voice partial-
# streaming design §2 point 3, §6) — SSE side.
# ---------------------------------------------------------------------------


class _NonPrefixAgent:
    """Simulates a divergent/retried turn: the two on_token calls do NOT
    form a growing prefix sequence — e.g. a mid-turn SDK retry restarting
    against unrelated content, or a canonical correction that diverges
    from the accumulated partials (AR-B covers both uniformly)."""

    def __init__(self, bus: MessageBus, role: str) -> None:
        self._role = role

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("Attempt one talking ")       # no sentence mark yet
            await on_token("Attempt two is unrelated.")   # does NOT extend the above
        return BusMessage(
            type=MessageType.RESPONSE,
            source=self._role,
            target=msg.source,
            content="Attempt two is unrelated.",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )


class _ShrinkingAgent:
    """The second cumulative is SHORTER than the first (a canonical
    correction that retracts already-flushed text)."""

    def __init__(self, bus: MessageBus, role: str) -> None:
        self._role = role

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("Hello there my friend.")   # flushes immediately (sentence mark)
            await on_token("Hi.")                        # SDK correction: much shorter
        return BusMessage(
            type=MessageType.RESPONSE,
            source=self._role,
            target=msg.source,
            content="Hi.",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )


class _StallingAgent:
    """AR-C: emits two deltas with a >1.5s monkeypatched clock gap between
    them, mid-sentence (no natural cut) — exercises the splitter's time-cap
    path via real per-delta channel feeding (previously dormant: a whole
    message arrived in one feed() call, per the design doc §3)."""

    def __init__(self, bus: MessageBus, role: str, clock: list[float]) -> None:
        self._role = role
        self._clock = clock

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("word, ")
            self._clock[0] += 2.0  # advance past the 1.5s cap
            await on_token("word, more text")
        return BusMessage(
            type=MessageType.RESPONSE,
            source=self._role,
            target=msg.source,
            content="word, more text",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )


async def _run_and_parse(client, scope_id: str) -> tuple[int, str, list[dict]]:
    resp = await client.post(
        "/api/converse",
        json={"prompt": "hi", "agent_role": "butler", "scope_id": scope_id},
    )
    frames: list[dict] = []
    async for line in resp.content:
        s = line.decode("utf-8").rstrip("\r\n")
        if s.startswith("event:"):
            frames.append({"event": s.split(":", 1)[1].strip()})
        elif s.startswith("data:"):
            frames[-1]["data"] = json.loads(s.split(":", 1)[1].strip())
    return resp.status, "", frames


@pytest.fixture
async def voice_app_nonprefix():
    bus = MessageBus()
    agent = _NonPrefixAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeAgentConfig()},
        memory=_DummyMemory(),
        idle_timeout=300,
    )
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client
    loop_task.cancel()


@pytest.fixture
async def voice_app_shrinking():
    bus = MessageBus()
    agent = _ShrinkingAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeAgentConfig()},
        memory=_DummyMemory(),
        idle_timeout=300,
    )
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client
    loop_task.cancel()


@pytest.fixture
async def voice_app_stalling(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "channels.voice.prosodic.time.monotonic", lambda: clock[0],
    )
    bus = MessageBus()
    agent = _StallingAgent(bus, "butler", clock)
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeAgentConfig()},
        memory=_DummyMemory(),
        idle_timeout=300,
    )
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client
    loop_task.cancel()


@pytest.mark.asyncio
class TestARBGuardSSE:
    async def test_nonprefix_cumulative_resets_splitter_and_logs_debug(
        self, voice_app_nonprefix, caplog,
    ):
        import logging

        with caplog.at_level(logging.DEBUG, logger="channels.voice.channel"):
            status, _, frames = await _run_and_parse(
                voice_app_nonprefix, "s-ar-b-sse",
            )

        assert status == 200
        events = [f["event"] for f in frames]
        assert "done" in events

        block_texts = [
            f["data"]["text"] for f in frames if f["event"] == "block"
        ]
        # The pre-reset buffered text ("Attempt one talking ", no sentence
        # mark, never flushed) is discarded on reset; the fresh splitter
        # renders attempt two's text cleanly — no garbled concatenation.
        assert block_texts == ["Attempt two is unrelated."], block_texts

        assert any(
            "non-prefix cumulative" in r.getMessage()
            for r in caplog.records
            if r.name == "channels.voice.channel"
        ), [r.getMessage() for r in caplog.records]

    async def test_shrinking_cumulative_does_not_throw(
        self, voice_app_shrinking,
    ):
        status, _, frames = await _run_and_parse(
            voice_app_shrinking, "s-ar-b-shrink-sse",
        )

        assert status == 200
        events = [f["event"] for f in frames]
        assert "done" in events
        assert "error" not in events

        block_texts = [
            f["data"]["text"] for f in frames if f["event"] == "block"
        ]
        # The first (already-flushed) block survives; the shrink is
        # rendered as a fresh, clean block of its own — no crash, no
        # garbage/empty-string block.
        assert block_texts == ["Hello there my friend.", "Hi."], block_texts


@pytest.mark.asyncio
class TestARCTimeCapSSE:
    async def test_stall_mid_sentence_forces_clause_preferring_block(
        self, voice_app_stalling,
    ):
        status, _, frames = await _run_and_parse(
            voice_app_stalling, "s-ar-c-sse",
        )

        assert status == 200
        block_texts = [
            f["data"]["text"] for f in frames if f["event"] == "block"
        ]
        # The >1.5s stall forces a cap block on the rightmost clause mark
        # (the comma) rather than waiting for a sentence mark or hard-
        # cutting mid-word; the remainder is flushed at turn end.
        assert block_texts, "expected the time-cap to force a block mid-turn"
        assert block_texts[0].rstrip() == "word,", block_texts
        assert "more text" in "".join(block_texts)
