"""Spec §3.2, §4.3 — WebSocket transport + stt_start prewarm dedup."""

import asyncio
import gc
import hashlib
import hmac
import json
import logging
import weakref
from unittest.mock import AsyncMock

import pytest

import voice_phrases
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_auth_helpers import SigningVoiceClient, VOICE_TEST_SECRET

from bus import BusMessage, MessageBus, MessageType
from channels.voice.channel import VoiceChannel, VoiceHandoffReservation
from channels.voice.routes import VoiceWsConnection
from error_kinds import VoiceToolLoopError

pytestmark = pytest.mark.unit


class _StreamingAgent:
    def __init__(self, bus, role): self._role = role
    async def handle_message(self, msg: BusMessage):
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("[warm] Hi.")
            await on_token("[warm] Hi. There.")
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="[warm] Hi. There.", reply_to=msg.id,
            channel=msg.channel, context=msg.context,
        )


class _FakeCfg:
    class tts: tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 800})()
    role = "butler"
    voice_errors: dict = {
        "voice_tool_loop": (
            "[apologetic] I couldn't resolve that cleanly. "
            "Try naming the device again?"
        ),
    }
    channels: list[str] = ["ha_voice"]


class _TextOnlyCfg(_FakeCfg):
    channels: list[str] = ["webhook"]


class _DeliverySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict]] = []
        self.called = asyncio.Event()

    async def handle(self, connection, frame):
        self.calls.append((connection, frame))
        self.called.set()

    async def route_connected(self, route):
        self.connected: list = getattr(self, "connected", [])
        self.connected.append(route)

    async def route_disconnected(self, route):
        self.disconnected: list = getattr(self, "disconnected", [])
        self.disconnected.append(route)


class _HandoffJob:
    """Minimal durable job shape delivered by the Task-3 commit seam."""

    id = "job-1"
    handoff_id = "handoff-1"
    specialist_display_name = "Finance"
    # The endpoint modality the acknowledgement is rendered from (#233/#224):
    # a real job always carries it, so the double must too.
    delivery_modality = "audio"


class _RecordingWs:
    """Connection double that proves the write precedes request cancellation."""

    voice_route_id = "route-1"
    voice_route_capabilities = frozenset({
        "background_jobs", "endpoint_delivery", "voice_handoff",
    })
    voice_job_control_id = "route-1"

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.write_completed = asyncio.Event()

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        await asyncio.sleep(0)
        self.write_completed.set()


class _HandoffingBus:
    """Models a Concierge handler committing after Task-3 durability."""

    def __init__(self) -> None:
        self.request_cancelled = asyncio.Event()
        self.specialist_task: asyncio.Task | None = None

    async def request(self, msg: BusMessage, timeout: float) -> None:
        reservation = msg.context["_voice_handoff_reservation"]
        assert reservation.reserve() is True
        # A token arriving while the (normally async) prelaunch path is held
        # must not win the foreground race.
        await msg.context["_on_token"]("This must not be spoken.")
        self.specialist_task = asyncio.create_task(asyncio.Event().wait())
        reservation.commit(_HandoffJob())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert self.specialist_task is not None
            assert not self.specialist_task.done()
            assert self.request_cancelled is not None
            self.request_cancelled.set()
            raise


class _FailingHandoffWs(_RecordingWs):
    """A socket that refuses the handoff write and STAYS refusing (#619).

    THE CONTRACT, which is all this double promises: once it has refused one
    ``send_json`` it refuses every later one, and ``frames`` records only
    writes that actually completed while ``attempts`` records every call.

    That models the case the tests here are about — a socket whose
    closing-state guard has begun refusing data frames — and it is
    deliberately NOT a general account of aiohttp's write behaviour. Three
    successive review rounds each found a different over-broad sentence in this
    docstring when it tried to be one (whether a raise proves zero bytes
    reached the wire; whether every later write is refused, given CLOSE frames
    are still permitted; whether a peer-initiated close refuses writes at all).
    The general theory kept being wrong in a new way, so it is gone: if you
    need aiohttp's semantics, measure them against the installed version rather
    than trusting a comment here.

    What this replaced was ineffective in two ways, and both mattered. It
    refused only the frame whose type was ``handoff`` and then HEALED, so the
    last-resort error frame appeared to be delivered on a socket that had just
    refused the frame before it. And it appended to ``frames`` BEFORE raising,
    so the committed assertion could observe a frame that never left the
    process.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[str] = []
        self._refusing = False

    async def send_json(self, frame: dict) -> None:
        self.attempts.append(frame["type"])
        if self._refusing or frame["type"] == "handoff":
            self._refusing = True
            raise ConnectionResetError("Cannot write to closing transport")
        await asyncio.sleep(0)
        self.frames.append(frame)
        self.write_completed.set()


class _BlockingBlockWs(_RecordingWs):
    """Stops a speech write after the channel has selected it."""

    def __init__(self) -> None:
        super().__init__()
        self.block_started = asyncio.Event()
        self.release_block = asyncio.Event()

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        if frame["type"] == "block":
            self.block_started.set()
            await self.release_block.wait()
        self.write_completed.set()


class _HandoffThenErrorBus(_HandoffingBus):
    """A late foreground error must lose to the durable handoff."""

    async def request(self, msg: BusMessage, timeout: float) -> None:
        reservation = msg.context["_voice_handoff_reservation"]
        assert reservation.reserve() is True
        self.specialist_task = asyncio.create_task(asyncio.Event().wait())
        reservation.commit(_HandoffJob())
        await msg.context["_error_sink"]("sdk_error", "not after handoff")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.request_cancelled.set()
            raise


class _ReserveDuringSpeechBus:
    def __init__(self, ws: _BlockingBlockWs) -> None:
        self._ws = ws
        self.reserved_during_write: bool | None = None

    async def request(self, msg: BusMessage, timeout: float) -> BusMessage:
        writing = asyncio.create_task(msg.context["_on_token"]("Spoken."))
        await self._ws.block_started.wait()
        reservation = msg.context["_voice_handoff_reservation"]
        self.reserved_during_write = reservation.reserve()
        if self.reserved_during_write:
            reservation.commit(_HandoffJob())
        self._ws.release_block.set()
        await writing
        return BusMessage(
            type=MessageType.RESPONSE, source="concierge", target="voice",
            content="Spoken.", channel="voice", context=msg.context,
        )


@pytest.fixture
async def ws_app():
    telemetry_clock = iter((20.0, 20.250))
    bus = MessageBus()
    agent = _StreamingAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop = asyncio.create_task(bus.run_agent_loop("butler"))

    memory = AsyncMock()
    memory.ensure_session = AsyncMock(return_value=None)
    memory.get_context = AsyncMock(return_value="")
    memory.add_turn = AsyncMock(return_value=None)
    memory.profile = AsyncMock(return_value="")

    ch = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=memory, idle_timeout=300,
        monotonic=lambda: next(telemetry_clock),
    )
    app = web.Application()
    ch.register_routes(app)

    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client, bus, memory, ch
    loop.cancel()


@pytest.fixture
async def signed_ws_app():
    secret = "route-secret"
    bus = MessageBus()
    for role in ("concierge", "butler"):
        agent = _StreamingAgent(bus, role)
        bus.register(role, agent.handle_message)
    loops = [
        asyncio.create_task(bus.run_agent_loop(role))
        for role in ("concierge", "butler")
    ]
    delivery = _DeliverySpy()
    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=secret,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={
            "concierge": _FakeCfg(),
            "butler": _FakeCfg(),
            "text-only": _TextOnlyCfg(),
        },
        memory=AsyncMock(),
        idle_timeout=300,
        delivery_coordinator=delivery,
    )
    app = web.Application()
    channel.register_routes(app)
    signature = hmac.new(
        secret.encode(), b"", hashlib.sha256,
    ).hexdigest()
    async with TestClient(TestServer(app)) as client:
        yield client, channel, delivery, {
            "X-Webhook-Signature": signature,
        }
    for task in loops:
        task.cancel()


@pytest.fixture
async def unsigned_route_ws_app():
    bus = MessageBus()
    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret="",
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=AsyncMock(),
        idle_timeout=300,
    )
    app = web.Application()
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, channel


class _PartialThenErrorBus:
    """#594: voices a real block, then faults through the PRODUCTION error
    path (``emit_error_line``), which is the only caller of ``_error_sink``
    outside tests. Calling the sink directly would skip the composition
    under test and pass for the wrong reason."""

    def __init__(self, channel, cfg) -> None:
        self._channel = channel
        self._cfg = cfg

    async def request(self, msg: BusMessage, timeout: float) -> None:
        await msg.context["_on_token"]("[confident] The kitchen lights are off.")
        await self._channel.emit_error_line("unknown", msg.context, self._cfg)
        return None


class _FailingDoneWs(_RecordingWs):
    """Everything writes except the terminal `done`, which fails."""

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        if frame["type"] == "done":
            raise ConnectionResetError("closing transport")
        await asyncio.sleep(0)
        self.write_completed.set()


class _TailOnlySpeechBus:
    """Streams text with NO sentence boundary, so the prosodic splitter holds
    it and the whole answer reaches the wire as the FINAL TAIL block — the one
    write that did not mark the turn as having spoken."""

    async def request(self, msg: BusMessage, timeout: float) -> BusMessage:
        await msg.context["_on_token"]("[confident] The kitchen lights are off")
        return BusMessage(
            type=MessageType.RESPONSE, source="butler", target="voice",
            content="[confident] The kitchen lights are off",
            channel="voice", context=msg.context,
        )


@pytest.mark.asyncio
class TestTailBlockCountsAsSpeech:
    """#594 round 3 (Terra, S1): the final tail block puts real speech on the
    wire without setting `speech_block_sent`, so a failure AFTER it — the
    terminal `done` write — took the last-resort path believing nothing had
    been said, and spoke an unretracted error over a delivered answer."""

    async def test_a_fault_after_a_tail_only_answer_is_retracted(self):
        cfg = _FakeCfg()
        channel = VoiceChannel(
            bus=None, default_agent="butler", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"butler": cfg}, memory=AsyncMock(),
            idle_timeout=300,
        )
        channel._bus = _TailOnlySpeechBus()
        ws = _FailingDoneWs()

        await channel._run_ws_utterance(
            ws, {"agent_role": "butler", "text": "are the lights off?"},
            "utterance-1", asyncio.get_running_loop().time() + 20,
        )

        kinds = [f["type"] for f in ws.frames]
        # Setup premise: the answer reached the wire as a FINAL block, and the
        # terminal write then failed. Without both, this case did not run.
        blocks = [f for f in ws.frames if f["type"] == "block"]
        assert blocks and blocks[-1].get("final") is True, ws.frames
        assert "done" in kinds, ws.frames
        errors = [f for f in ws.frames if f["type"] == "error"]
        assert len(errors) == 1, ws.frames
        assert "disregard that" in errors[0]["spoken"].lower(), errors[0]


class _RejectingBlockWs(_RecordingWs):
    """The first speech block is REJECTED by the transport — selected, never
    delivered. The listener heard nothing."""

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        if frame["type"] == "block":
            raise ConnectionResetError("closing transport")
        await asyncio.sleep(0)
        self.write_completed.set()


@pytest.mark.asyncio
class TestRetractionFollowsDeliveryNotSelection:
    """#594 round 3 (Sol, S1): the handlers set `speech_block_sent` BEFORE
    starting the write, on purpose — it answers "was speech selected", which
    is what handoff selection needs. It is not an answer to "did the listener
    hear anything", and using it as one retracts speech nobody received."""

    async def test_a_rejected_block_write_is_not_retracted(self):
        cfg = _FakeCfg()
        channel = VoiceChannel(
            bus=None, default_agent="butler", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"butler": cfg}, memory=AsyncMock(),
            idle_timeout=300,
        )
        channel._bus = _PartialThenErrorBusStreamOnly()
        ws = _RejectingBlockWs()

        await channel._run_ws_utterance(
            ws, {"agent_role": "butler", "text": "are the lights off?"},
            "utterance-1", asyncio.get_running_loop().time() + 20,
        )

        errors = [f for f in ws.frames if f["type"] == "error"]
        assert len(errors) == 1, ws.frames
        assert "disregard" not in errors[0]["spoken"].lower(), (
            "retracted speech the listener never received: %r" % errors[0])


class _PartialThenErrorBusStreamOnly:
    """Streams one complete sentence and returns; the transport decides
    whether it lands."""

    async def request(self, msg: BusMessage, timeout: float) -> BusMessage:
        await msg.context["_on_token"]("[confident] The kitchen lights are off.")
        return BusMessage(
            type=MessageType.RESPONSE, source="butler", target="voice",
            content="[confident] The kitchen lights are off.",
            channel="voice", context=msg.context,
        )


class _PartialThenRaisingBus:
    """Voices a real block, then raises OUT OF `request` — the transport-level
    failure (a 300s turn timeout, a shutdown) that never reaches the agent's
    classified error branch and so lands in the handler's own outer `except`."""

    async def request(self, msg: BusMessage, timeout: float) -> None:
        await msg.context["_on_token"]("[confident] The kitchen lights are off.")
        raise RuntimeError("transport-level failure after partial output")


@pytest.mark.asyncio
class TestWSTransportFaultAfterSpeechIsRetracted:
    async def test_a_transport_level_fault_after_speech_is_retracted(self):
        """#594 round 2 (Sol + Terra, S1): this path composed its own frame
        and skipped the retraction on both transports."""
        cfg = _FakeCfg()
        channel = VoiceChannel(
            bus=None, default_agent="butler", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"butler": cfg}, memory=AsyncMock(),
            idle_timeout=300,
        )
        channel._bus = _PartialThenRaisingBus()
        ws = _RecordingWs()

        await channel._run_ws_utterance(
            ws, {"agent_role": "butler", "text": "are the lights off?"},
            "utterance-1", asyncio.get_running_loop().time() + 20,
        )

        assert "block" in [f["type"] for f in ws.frames], ws.frames
        errors = [f for f in ws.frames if f["type"] == "error"]
        assert len(errors) == 1, ws.frames
        spoken = errors[0]["spoken"].lower()
        assert "disregard that" in spoken, errors[0]
        assert "sorry, something went wrong" in spoken, errors[0]


@pytest.mark.asyncio
class TestWSPartialThenFaultIsRetracted:
    async def test_the_error_frame_retracts_the_speech_already_voiced(self):
        """The WS transport owes the same retraction as SSE — it maintains
        its own ``speech_block_sent``, so the wiring is per-transport."""
        cfg = _FakeCfg()
        channel = VoiceChannel(
            bus=None, default_agent="butler", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"butler": cfg}, memory=AsyncMock(),
            idle_timeout=300,
        )
        channel._bus = _PartialThenErrorBus(channel, cfg)
        ws = _RecordingWs()

        await channel._run_ws_utterance(
            ws, {"agent_role": "butler", "text": "are the lights off?"},
            "utterance-1", asyncio.get_running_loop().time() + 20,
        )

        kinds = [f["type"] for f in ws.frames]
        # Setup premise, asserted: real speech reached the socket first.
        assert "block" in kinds, ws.frames
        errors = [f for f in ws.frames if f["type"] == "error"]
        assert len(errors) == 1, ws.frames
        spoken = errors[0]["spoken"].lower()
        assert "disregard that" in spoken, errors[0]
        assert "sorry, something went wrong" in spoken, errors[0]
        assert spoken.index("disregard that") < spoken.index("sorry, something")


@pytest.mark.asyncio
class TestWSTurn:
    async def test_handoff_suppresses_a_late_foreground_error(self):
        bus = _HandoffThenErrorBus()
        channel = VoiceChannel(
            bus=bus, default_agent="concierge", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"concierge": _FakeCfg()}, memory=AsyncMock(),
            idle_timeout=300,
        )
        ws = _RecordingWs()

        await channel._run_ws_utterance(
            ws, {"agent_role": "concierge", "text": "ask finance"},
            "utterance-1", asyncio.get_running_loop().time() + 20,
        )

        assert [frame["type"] for frame in ws.frames] == ["handoff"]
        bus.specialist_task.cancel()
        await asyncio.gather(bus.specialist_task, return_exceptions=True)

    async def test_speech_selection_rejects_handoff_during_block_write(self):
        ws = _BlockingBlockWs()
        bus = _ReserveDuringSpeechBus(ws)
        channel = VoiceChannel(
            bus=bus, default_agent="concierge", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"concierge": _FakeCfg()}, memory=AsyncMock(),
            idle_timeout=300,
        )

        await channel._run_ws_utterance(
            ws, {"agent_role": "concierge", "text": "speak"},
            "utterance-1", asyncio.get_running_loop().time() + 20,
        )

        assert bus.reserved_during_write is False
        assert "handoff" not in [frame["type"] for frame in ws.frames]

    async def test_handoff_ends_only_the_foreground_request_after_its_frame(
        self,
    ):
        """A durable job owns the background task, not the old utterance."""
        bus = _HandoffingBus()
        channel = VoiceChannel(
            bus=bus, default_agent="concierge", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"concierge": _FakeCfg()}, memory=AsyncMock(),
            idle_timeout=300,
        )
        ws = _RecordingWs()

        await channel._run_ws_utterance(
            ws,
            {
                "agent_role": "concierge", "text": "please ask finance",
                "scope_id": "scope-1", "device_id": "kitchen",
            },
            "utterance-1",
            asyncio.get_running_loop().time() + 20,
        )

        # v0.120.0 (#233/#224): the frame now carries `protocol` + `job_id`
        # so the client can produce a receipt Casa will ACCEPT (without them
        # the hand-off stayed PENDING and the answer expired unspoken), and
        # the wording sets the caller's wait expectation.
        assert ws.frames == [{
            "type": "handoff", "protocol": 2, "utterance_id": "utterance-1",
            "handoff_id": "handoff-1", "job_id": "job-1",
            # Wording varies by design (#233); the CONTRACT is what matters:
            # it names the specialist and sets a wait expectation. On an audio
            # endpoint it must never imply a notification.
            "text": voice_phrases.acknowledgement(
                "Finance", "audio", voice_phrases.seed_for("job-1")),
        }]
        assert ws.write_completed.is_set()
        assert bus.request_cancelled.is_set()
        assert bus.specialist_task is not None
        assert not bus.specialist_task.done()
        bus.specialist_task.cancel()
        await asyncio.gather(bus.specialist_task, return_exceptions=True)

    async def test_handoff_reservation_releases_streaming_and_rejects_late_reserve(
        self,
    ):
        reservation = VoiceHandoffReservation()

        assert reservation.reserve() is True
        assert reservation.held is True
        reservation.release()
        assert reservation.held is False
        reservation.mark_speech_sent()
        assert reservation.reserve() is False

    async def test_a_double_that_refused_one_frame_refuses_the_next(self):
        """The double's own contract, checked before anything relies on it
        (#619). The version this replaced healed after refusing the handoff, so
        the committed assertion below could be written against a socket that
        delivered the last-resort frame — which is not what happens when a
        closing-state guard has begun refusing."""
        ws = _FailingHandoffWs()
        with pytest.raises(ConnectionResetError):
            await ws.send_json({"type": "handoff"})
        with pytest.raises(ConnectionResetError):
            await ws.send_json({"type": "error"})
        assert ws.attempts == ["handoff", "error"]
        assert ws.frames == [], ws.frames

    async def test_a_refused_handoff_write_delivers_nothing_and_keeps_the_job_durable(self):
        """#619, and the record of a decision argued twice.

        The channel ATTEMPTS the last-resort error frame after a handoff write
        fails, and on this failure class it cannot arrive: the socket refused
        the handoff via the closing-state guard, which refuses before any byte
        and never unlatches. So the listener hears nothing
        and the durable job is re-offered on reconnect — never a promise
        followed by its own contradiction, which was the worry that opened the
        issue. That worry needs the frame to have been delivered, and a refusal
        proves it was not.

        THE LOSING OPTION, and why it stays lost. Routing this path through
        ``_error_sink`` to suppress the frame emits NOTHING AT ALL, on every
        entrant to that branch — the sink returns early on
        ``handoff.done() or reservation.committed`` and BOTH are already true
        here: the branch only runs inside ``if handoff in done:``, and
        ``commit()`` sets ``_committed`` with no await between.

        It would also silence the branch's ORDINARY entrant, which has a
        healthy socket: on the normal-request path the unused handoff future is
        cancelled before the request is awaited, so a turn that faults in the
        bus arrives at the same ``except`` with ``handoff.done()`` already true
        — and there the frame is the listener's only telling. (A *committed*
        handoff never arrives there on a healthy socket; that branch returns.)
        The ``attempts`` assertion below is what turns the change red.

        Do not re-argue this without a measurement contradicting one of those
        two facts: that the sink's predicate is already satisfied here, and
        that this ``except`` is shared with an entrant whose socket still works.
        """
        bus = _HandoffingBus()
        channel = VoiceChannel(
            bus=bus, default_agent="concierge", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"concierge": _FakeCfg()}, memory=AsyncMock(),
            idle_timeout=300,
        )
        ws = _FailingHandoffWs()

        with pytest.raises(ConnectionResetError):
            await channel._run_ws_utterance(
                ws,
                {
                    "agent_role": "concierge", "text": "please ask finance",
                    "scope_id": "scope-1", "device_id": "kitchen",
                },
                "utterance-1",
                asyncio.get_running_loop().time() + 20,
            )

        # ATTEMPTED both; DELIVERED neither. The old assertion conflated these.
        assert ws.attempts == ["handoff", "error"]
        assert ws.frames == [], ws.frames
        assert bus.request_cancelled.is_set()
        assert bus.specialist_task is not None
        assert not bus.specialist_task.done()
        bus.specialist_task.cancel()
        await asyncio.gather(bus.specialist_task, return_exceptions=True)

    async def test_stt_start_then_utterance(self, ws_app):
        client, _, memory, _ = ws_app
        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "stt_start", "session_key": "voice-s",
                "scope_id": "s", "context": {"device_id": "kitchen"},
            })
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1",
                "text": "hi", "agent_role": "butler", "scope_id": "s",
            })
            got = []
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                got.append(frame["type"])
                if frame["type"] == "done":
                    break
            assert "block" in got
            assert got[-1] == "done"
            # profile() must NOT be called on voice turns — overlay is not
            # pushed at 'friends' clearance; the overlay prewarm was removed.
            assert memory.profile.await_count == 0

    async def test_first_real_block_logs_once_from_ingress_with_fake_clock(
        self, ws_app, caplog,
    ):
        client, _, _, _ = ws_app
        secret = "SECRET_WS_PROMPT"
        with caplog.at_level(logging.INFO, logger="channels.voice.channel"):
            async with client.ws_connect("/api/converse/ws") as ws:
                await ws.send_json({
                    "type": "utterance",
                    "utterance_id": "latency-ws",
                    "text": secret,
                    "agent_role": "butler",
                    "scope_id": "latency-ws",
                })
                async for message in ws:
                    if message.type != WSMsgType.TEXT:
                        break
                    if json.loads(message.data)["type"] == "done":
                        break

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "channels.voice.channel"
            and "voice_first_block" in record.getMessage()
        ]
        assert messages == [
            "voice_first_block role=butler transport=ws ms=250"
        ]
        assert secret not in caplog.text

    async def test_stt_start_is_pool_noop(self, ws_app):
        """v0.80.0 (spec A2): stt_start no longer touches the pool at all —
        the frame carries no agent_role, and VoiceSessionPool.ensure() now
        requires one (role-scoped keying, so two residents on one device
        can't collide on a session_key). Pool registration happens lazily
        on the utterance frame instead, which DOES carry agent_role.
        Sending stt_start twice remains a harmless no-op either way. The
        obsolete overlay prewarm has been removed — no profile() is called."""
        client, _, memory, channel = ws_app

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({"type": "stt_start", "scope_id": "s"})
            await ws.send_json({"type": "stt_start", "scope_id": "s"})
            await asyncio.sleep(0.05)
            assert channel.pool.get("s", role="butler") is None
            # No profile() call — overlay not used for voice.
            assert memory.profile.await_count == 0

    async def test_role_aware_stt_start_ensures_exact_role_scope_only(
        self, signed_ws_app,
    ):
        client, channel, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "stt_start", "scope_id": "device-1",
                "agent_role": "concierge",
            })
            await ws.send_json({
                "type": "stt_start", "scope_id": "device-1",
                "agent_role": "butler",
            })
            await asyncio.sleep(0.05)

        assert channel.pool.get("device-1", role="concierge") is not None
        assert channel.pool.get("device-1", role="butler") is not None

    @pytest.mark.parametrize("frame", [
        {"type": "stt_start", "scope_id": "device-1"},
        {"type": "stt_start", "scope_id": "", "agent_role": "butler"},
        {"type": "stt_start", "scope_id": "device-1", "agent_role": "unknown"},
        {"type": "stt_start", "scope_id": "device-1", "agent_role": "text-only"},
    ])
    async def test_invalid_stt_start_is_a_noop(self, signed_ws_app, frame):
        client, channel, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json(frame)
            await asyncio.sleep(0.02)
        assert channel.pool._sessions == {}

    async def test_job_frame_without_utterance_id_reaches_delivery_first(
        self, signed_ws_app,
    ):
        client, _, delivery, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "job_claimed", "protocol": 2,
                "job_id": "job-1",
                "delivery_attempt_id": "attempt-1",
            })
            await asyncio.wait_for(delivery.called.wait(), timeout=1)
        connection, frame = delivery.calls[-1]
        assert isinstance(connection, VoiceWsConnection)
        assert frame["type"] == "job_claimed"

    async def test_unknown_frame_is_ignored_without_error_or_close(
        self, signed_ws_app,
    ):
        client, _, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({"type": "future_frame", "protocol": 77})
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive_json(), timeout=0.05)
            assert ws.closed is False

    async def test_handoff_received_before_route_registration_is_ignored(
        self, signed_ws_app,
    ):
        client, channel, delivery, headers = signed_ws_app
        dispatch = AsyncMock()
        channel._bus.handlers["butler"] = dispatch

        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "handoff_received",
                "protocol": 2,
                "utterance_id": "u-1",
                "handoff_id": "handoff-1",
                "text": "I will look into that.",
            })
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive_json(), timeout=0.05)

        assert channel.routes.get_connected("entry-1:concierge") is None
        assert delivery.calls == []
        dispatch.assert_not_awaited()

    async def test_non_object_json_frame_is_ignored_without_close(
        self, signed_ws_app,
    ):
        client, _, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json(["old", "integration", "frame"])
            await asyncio.sleep(0.05)
            assert ws.closed is False
            await ws.send_json({"type": "stage", "stage": "stt"})
            await asyncio.sleep(0.01)
            assert ws.closed is False

    async def test_authenticated_registration_binds_route(self, signed_ws_app):
        client, channel, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "voice_route_register", "protocol": 3,
                "route_id": "entry-1", "agent_role": "concierge",
                "capabilities": [
                    "background_jobs", "endpoint_delivery", "voice_handoff",
                ],
            })
            assert await ws.receive_json() == {
                "type": "voice_route_registered", "protocol": 3,
                "accepted_capabilities": [
                    "background_jobs", "endpoint_delivery", "voice_handoff",
                ],
            }
            bound = channel.routes.get_connected("entry-1")
            assert bound is not None
            assert bound.role == "concierge"

    async def test_renamed_registration_notifies_displaced_route(
        self, signed_ws_app,
    ):
        """#304: a socket that re-registers under a NEW route id displaces
        its old binding — the delivery coordinator must hear the same
        route-disconnected notification the socket-teardown path emits, or
        an offered attempt for the old id is never re-offered."""
        client, channel, delivery, headers = signed_ws_app
        register = {
            "type": "voice_route_register", "protocol": 3,
            "route_id": "entry-1", "agent_role": "concierge",
            "capabilities": [
                "background_jobs", "endpoint_delivery", "voice_handoff",
            ],
        }
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json(register)
            await ws.receive_json()
            displaced = channel.routes.get_connected("entry-1")
            await ws.send_json({**register, "route_id": "entry-2"})
            await ws.receive_json()

            assert channel.routes.get_connected("entry-1") is None
            assert channel.routes.get_connected("entry-2") is not None
            assert [
                route.route_id
                for route in getattr(delivery, "disconnected", [])
            ] == [displaced.route_id]

    async def test_failed_registration_ack_still_reports_displaced_route(
        self, signed_ws_app, monkeypatch,
    ):
        """#304 (review round 3): register() mutates the binding BEFORE
        sending the ack. If the socket dies during that ack write, the
        handler never reaches the displaced-route notification and teardown
        reports only the NEW binding — an offer pinned to the displaced one
        stayed classified as valid until reconnect or expiry."""
        client, channel, delivery, headers = signed_ws_app
        fail_ack = {"armed": False}
        orig_send = VoiceWsConnection.send_json

        async def failing_send(self, frame, **kw):
            if (
                fail_ack["armed"]
                and frame.get("type") == "voice_route_registered"
            ):
                raise ConnectionResetError("ack write failed")
            return await orig_send(self, frame, **kw)

        monkeypatch.setattr(VoiceWsConnection, "send_json", failing_send)

        register = {
            "type": "voice_route_register", "protocol": 3,
            "route_id": "entry-1", "agent_role": "concierge",
            "capabilities": [
                "background_jobs", "endpoint_delivery", "voice_handoff",
            ],
        }
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json(register)
            await ws.receive_json()
            fail_ack["armed"] = True
            await ws.send_json({**register, "route_id": "entry-2"})
            # The server aborts the reader on the failed ack; drain close.
            while True:
                msg = await ws.receive()
                if msg.type.name in ("CLOSE", "CLOSED", "CLOSING", "ERROR"):
                    break

        for _ in range(100):
            if any(
                getattr(route, "route_id", None) == "entry-1"
                for route in getattr(delivery, "disconnected", [])
            ):
                break
            await asyncio.sleep(0.01)
        assert [
            route.route_id for route in getattr(delivery, "disconnected", [])
            if route.route_id == "entry-1"
        ] == ["entry-1"]

    async def test_failed_ack_reports_route_displaced_from_another_socket(
        self, signed_ws_app, monkeypatch,
    ):
        """#304 (review round 4): sibling arm — socket B claiming a route
        id held by socket A displaces A's binding inside register(); if
        B's ack write fails, B's `previous` is None, so A's displaced
        binding must be captured separately or its offers stay pinned."""
        client, channel, delivery, headers = signed_ws_app
        fail_ack = {"armed": False}
        orig_send = VoiceWsConnection.send_json

        async def failing_send(self, frame, **kw):
            if (
                fail_ack["armed"]
                and frame.get("type") == "voice_route_registered"
            ):
                raise ConnectionResetError("ack write failed")
            return await orig_send(self, frame, **kw)

        monkeypatch.setattr(VoiceWsConnection, "send_json", failing_send)

        register = {
            "type": "voice_route_register", "protocol": 3,
            "route_id": "entry-1", "agent_role": "concierge",
            "capabilities": [
                "background_jobs", "endpoint_delivery", "voice_handoff",
            ],
        }
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws_a:
            await ws_a.send_json(register)
            await ws_a.receive_json()
            a_binding = channel.routes.get_connected("entry-1")

            fail_ack["armed"] = True
            async with client.ws_connect(
                "/api/converse/ws", headers=headers,
            ) as ws_b:
                await ws_b.send_json(register)
                while True:
                    msg = await ws_b.receive()
                    if msg.type.name in (
                        "CLOSE", "CLOSED", "CLOSING", "ERROR",
                    ):
                        break

            for _ in range(100):
                if a_binding in getattr(delivery, "disconnected", []):
                    break
                await asyncio.sleep(0.01)
            assert a_binding in getattr(delivery, "disconnected", [])

    async def test_invalid_reregistration_keeps_binding_and_stays_offerable(
        self, signed_ws_app,
    ):
        """#304 (channel seam): a malformed re-registration frame must not
        strand the route — the binding survives and no disconnect is
        reported."""
        client, channel, delivery, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "voice_route_register", "protocol": 3,
                "route_id": "entry-1", "agent_role": "concierge",
                "capabilities": [
                    "background_jobs", "endpoint_delivery", "voice_handoff",
                ],
            })
            await ws.receive_json()
            bound = channel.routes.get_connected("entry-1")

            await ws.send_json({
                "type": "voice_route_register", "protocol": 3,
                "route_id": "entry-1", "agent_role": "concierge",
                "capabilities": [{"malformed": True}],
            })
            refusal = await ws.receive_json()

            assert refusal["accepted_capabilities"] == []
            assert channel.routes.get_connected("entry-1") is bound
            assert getattr(delivery, "disconnected", []) == []

    async def test_handler_failure_still_clears_connection_bound_writer(
        self, signed_ws_app,
    ):
        client, channel, delivery, headers = signed_ws_app

        async def fail_handle(_connection, _frame):
            raise RuntimeError("controlled delivery failure")

        delivery.handle = fail_handle
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "voice_route_register", "protocol": 3,
                "route_id": "entry-1", "agent_role": "concierge",
                "capabilities": [
                    "background_jobs", "endpoint_delivery", "voice_handoff",
                ],
            })
            await ws.receive_json()
            assert channel.routes.get_connected("entry-1") is not None
            await ws.send_json({
                "type": "job_claimed", "protocol": 2,
                "job_id": "job-1",
                "delivery_attempt_id": "attempt-1",
            })
            await ws.receive()

        for _ in range(20):
            if channel.routes.get_connected("entry-1") is None:
                break
            await asyncio.sleep(0.01)
        assert channel.routes.get_connected("entry-1") is None

    async def test_empty_secret_never_accepts_background_capability(
        self, unsigned_route_ws_app,
    ):
        """#193 (v0.117.0): with no webhook secret the WS route is fail-CLOSED —
        the upgrade itself is refused (401) before any frame is exchanged, so a
        route can never register, let alone claim background capabilities. This
        supersedes the older guarantee (connection allowed, capabilities
        dropped) with a strictly stronger one."""
        from aiohttp import WSServerHandshakeError

        client, channel = unsigned_route_ws_app
        with pytest.raises(WSServerHandshakeError) as excinfo:
            async with client.ws_connect("/api/converse/ws"):
                pass
        assert excinfo.value.status == 401
        assert channel.routes.get_connected("entry-1") is None

    async def test_cancel_stops_in_flight(self, ws_app):
        client, bus, _, channel = ws_app

        # Replace the handler with one that blocks until cancelled.
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def slow(msg: BusMessage):
            on_token = msg.context.get("_on_token")
            if on_token:
                await on_token("starting")
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        bus.handlers["butler"] = slow

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1",
                "text": "x", "agent_role": "butler", "scope_id": "s",
            })
            await asyncio.wait_for(started.wait(), timeout=2.0)
            await ws.send_json({"type": "cancel", "utterance_id": "u1"})
            await asyncio.wait_for(cancelled.wait(), timeout=3.0)

    async def test_duplicate_utterance_id_cancels_the_orphaned_original(
        self, ws_app,
    ):
        """#303: a retransmitted utterance_id overwrote the task map while
        the first task was still running — cancel frames and teardown then
        reached only the replacement, and the original ran on unseen."""
        client, bus, _, _ = ws_app
        starts = [asyncio.Event(), asyncio.Event()]
        cancels = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def slow(msg: BusMessage):
            nonlocal calls
            index = min(calls, 1)
            calls += 1
            starts[index].set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancels[index].set()
                raise

        bus.handlers["butler"] = slow

        utterance = {
            "type": "utterance", "utterance_id": "u1", "text": "x",
            "agent_role": "butler", "scope_id": "s",
        }
        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json(utterance)
            await asyncio.wait_for(starts[0].wait(), timeout=2.0)
            # The client retransmits the same utterance_id mid-flight.
            await ws.send_json(utterance)
            await asyncio.wait_for(starts[1].wait(), timeout=2.0)
            # The replaced original must be cancelled, not orphaned.
            await asyncio.wait_for(cancels[0].wait(), timeout=2.0)
            # A cancel frame still reaches the replacement.
            await ws.send_json({"type": "cancel", "utterance_id": "u1"})
            await asyncio.wait_for(cancels[1].wait(), timeout=2.0)

    async def test_orphaned_duplicate_task_is_still_owned_by_teardown(
        self, ws_app, monkeypatch,
    ):
        """#303 (review round 2): cancel() is cooperative — a replaced
        original that is slow to unwind must remain reachable by socket
        teardown (re-cancelled and awaited), not dropped from the map and
        abandoned mid-cleanup."""
        client, _, _, channel = ws_app
        started: list[asyncio.Task] = []

        async def stub(ws, frame, uid, voice_deadline):
            first = not started
            started.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if first:
                    # Slow cooperative cleanup: the duplicate's cancel is
                    # absorbed; only a LATER cancel (teardown's) ends it.
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        raise
                raise

        monkeypatch.setattr(channel, "_run_ws_utterance", stub)

        utterance = {
            "type": "utterance", "utterance_id": "u1", "text": "x",
            "agent_role": "butler", "scope_id": "s",
        }
        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json(utterance)
            while not started:
                await asyncio.sleep(0.01)
            await ws.send_json(utterance)
            while len(started) < 2:
                await asyncio.sleep(0.01)

        # Socket closed: teardown must terminate the orphaned original too.
        for _ in range(100):
            if started[0].done():
                break
            await asyncio.sleep(0.01)
        assert started[0].done()
        assert started[1].done()

    async def test_client_context_cannot_clobber_computed_keys(self, ws_app):
        """L59/L8 (WS side): a client-supplied context dict must not
        override the channel-computed chat_id/cid/utterance_id."""
        client, bus, _, _ = ws_app
        captured = {}
        orig_request = bus.request

        async def spy_request(msg, timeout=300):
            captured["msg"] = msg
            return await orig_request(msg, timeout=timeout)

        bus.request = spy_request

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "utterance", "utterance_id": "forged-uid",
                "text": "hi", "agent_role": "butler", "scope_id": "s",
                "context": {
                    "chat_id": "living-room",
                    "cid": "client-forged-cid",
                    "device_id": "kitchen",
                },
            })
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                if frame["type"] == "done":
                    break

        ctx = captured["msg"].context
        assert ctx["chat_id"] == "s"
        assert ctx["cid"] != "client-forged-cid"
        assert ctx["device_id"] == "kitchen"

    async def test_ws_utterance_task_pruned_and_exception_retrieved(
        self, ws_app, caplog, monkeypatch,
    ):
        """L60/L9: finished utterance tasks must be pruned from the
        per-connection tasks dict, and a task that finishes with an
        exception must have that exception retrieved (never logged as
        'never retrieved') and surfaced as a warning."""
        client, _, _, channel = ws_app
        task_refs: list[weakref.ref] = []

        async def stub_ok(ws, frame, uid, voice_deadline):
            task_refs.append(weakref.ref(asyncio.current_task()))
            await ws.send_json({"type": "done", "utterance_id": uid})

        monkeypatch.setattr(channel, "_run_ws_utterance", stub_ok)

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1", "text": "hi",
                "agent_role": "butler", "scope_id": "s",
            })
            msg = await ws.receive_json(timeout=2.0)
            assert msg["type"] == "done"
            await asyncio.sleep(0.05)  # let the done-callback run
            gc.collect()
            # Pruned from the per-connection dict => nothing references the
            # finished task anymore, so the weakref must be dead WHILE the
            # WS is still open.
            assert task_refs and task_refs[0]() is None

            # Exception arm: a failing utterance task must be reaped +
            # logged, never left as an unretrieved-exception task.
            async def stub_fail(ws_, frame, uid, voice_deadline):
                raise ConnectionResetError("Cannot write to closing transport")
            monkeypatch.setattr(channel, "_run_ws_utterance", stub_fail)
            with caplog.at_level("WARNING"):
                await ws.send_json({
                    "type": "utterance", "utterance_id": "u2", "text": "x",
                    "agent_role": "butler", "scope_id": "s",
                })
                await asyncio.sleep(0.1)
            gc.collect()
            assert any("utterance task failed" in r.message for r in caplog.records)
            assert not any("never retrieved" in r.message for r in caplog.records)

    async def test_voice_tool_loop_emits_one_typed_error_without_payload_log(
        self, ws_app, caplog,
    ):
        client, bus, _, _ = ws_app
        secret = "SECRET_VOICE_TOOL_INPUT_WS"

        async def raise_voice_tool_loop(_msg, timeout=300):
            raise VoiceToolLoopError("validation_correction_exhausted")

        bus.request = raise_voice_tool_loop
        with caplog.at_level(logging.DEBUG):
            async with client.ws_connect("/api/converse/ws") as ws:
                await ws.send_json({
                    "type": "utterance",
                    "utterance_id": "guarded-ws",
                    "text": secret,
                    "agent_role": "butler",
                    "scope_id": "guarded-ws",
                })
                frames = [await ws.receive_json(timeout=2.0)]
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.receive_json(), timeout=0.05)

        errors = [frame for frame in frames if frame["type"] == "error"]
        assert len(errors) == 1
        assert errors[0] == {
            "type": "error",
            "utterance_id": "guarded-ws",
            "kind": "voice_tool_loop",
            "spoken": (
                "[apologetic] I couldn't resolve that cleanly. "
                "Try naming the device again?"
            ),
        }
        assert not any(frame["type"] == "done" for frame in frames)
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Rate limiting — per-scope_id (spec 5.2 §8)
# ---------------------------------------------------------------------------


@pytest.fixture
async def voice_ws_app_with_limiter(request):
    from rate_limit import RateLimiter

    capacity = getattr(request, "param", 2)

    bus = MessageBus()
    roles = ("butler", "concierge")
    for role in roles:
        agent = _StreamingAgent(bus, role)
        bus.register(role, agent.handle_message)
    loop_tasks = [
        asyncio.create_task(bus.run_agent_loop(role)) for role in roles
    ]

    memory = AsyncMock()
    memory.ensure_session = AsyncMock(return_value=None)
    memory.get_context = AsyncMock(return_value="")
    memory.add_turn = AsyncMock(return_value=None)
    memory.profile = AsyncMock(return_value="")

    limiter = RateLimiter(capacity=capacity, window_s=60.0)

    channel = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={role: _FakeCfg() for role in roles},
        memory=memory, idle_timeout=300,
        rate_limiter=limiter,
    )

    app = web.Application()
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client
    for task in loop_tasks:
        task.cancel()


@pytest.mark.asyncio
class TestRateLimit:
    @pytest.mark.parametrize("voice_ws_app_with_limiter", [1], indirect=True)
    async def test_same_scope_has_independent_role_buckets(
        self, voice_ws_app_with_limiter,
    ):
        client = voice_ws_app_with_limiter

        async with client.ws_connect("/api/converse/ws") as ws:
            async def run_turn(uid: str, role: str) -> dict:
                await ws.send_json({
                    "type": "utterance",
                    "utterance_id": uid,
                    "scope_id": "same-scope",
                    "agent_role": role,
                    "text": "hi",
                })
                while True:
                    frame = await ws.receive_json(timeout=2.0)
                    if (
                        frame.get("utterance_id") == uid
                        and frame.get("type") in {"done", "error"}
                    ):
                        return frame

            assert (await run_turn("u-butler-1", "butler"))["type"] == "done"
            assert (
                await run_turn("u-concierge-1", "concierge")
            )["type"] == "done"
            butler_second = await run_turn("u-butler-2", "butler")
            assert butler_second["type"] == "error"
            assert butler_second["kind"] == "rate_limit"

    @pytest.mark.parametrize("voice_ws_app_with_limiter", [1], indirect=True)
    async def test_ws_rate_limit_emits_error_frame(
        self, voice_ws_app_with_limiter,
    ):
        client = voice_ws_app_with_limiter
        async with client.ws_connect("/api/converse/ws") as ws:
            # First utterance admitted.
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1",
                "scope_id": "user-w", "agent_role": "butler",
                "text": "hi",
            })

            got_done_u1 = False
            got_rate_limit_u2 = False
            sent_second = False

            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "done" and data.get("utterance_id") == "u1":
                    got_done_u1 = True
                    # Fire the second utterance — bucket is exhausted.
                    await ws.send_json({
                        "type": "utterance", "utterance_id": "u2",
                        "scope_id": "user-w", "agent_role": "butler",
                        "text": "hello again",
                    })
                    sent_second = True
                elif (
                    data.get("type") == "error"
                    and data.get("utterance_id") == "u2"
                    and data.get("kind") == "rate_limit"
                ):
                    got_rate_limit_u2 = True
                    break

            assert got_done_u1, "first utterance must complete"
            assert sent_second
            assert got_rate_limit_u2, "second utterance must get kind=rate_limit"

    @pytest.mark.parametrize("voice_ws_app_with_limiter", [0], indirect=True)
    async def test_ws_capacity_zero_is_unlimited(
        self, voice_ws_app_with_limiter,
    ):
        client = voice_ws_app_with_limiter
        async with client.ws_connect("/api/converse/ws") as ws:
            for i in range(5):
                uid = f"u{i}"
                await ws.send_json({
                    "type": "utterance", "utterance_id": uid,
                    "scope_id": "u", "agent_role": "butler",
                    "text": f"msg-{i}",
                })
            done_ids: set[str] = set()
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "done":
                    done_ids.add(data["utterance_id"])
                if len(done_ids) == 5:
                    break
            assert done_ids == {f"u{i}" for i in range(5)}


# ---------------------------------------------------------------------------
# AR-B prefix-divergence guard + AR-C time-cap (2026-07-11 voice partial-
# streaming design §2 point 3, §6) — WS side.
# ---------------------------------------------------------------------------


class _NonPrefixAgent:
    """Simulates a divergent/retried turn: the two on_token calls do NOT
    form a growing prefix sequence (AR-B)."""

    def __init__(self, bus, role): self._role = role

    async def handle_message(self, msg: BusMessage):
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("Attempt one talking ")       # no sentence mark yet
            await on_token("Attempt two is unrelated.")   # does NOT extend the above
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Attempt two is unrelated.", reply_to=msg.id,
            channel=msg.channel, context=msg.context,
        )


class _ShrinkingAgent:
    """The second cumulative is SHORTER than the first (a canonical
    correction that retracts already-flushed text)."""

    def __init__(self, bus, role): self._role = role

    async def handle_message(self, msg: BusMessage):
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("Hello there my friend.")   # flushes immediately
            await on_token("Hi.")                        # SDK correction: shorter
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Hi.", reply_to=msg.id,
            channel=msg.channel, context=msg.context,
        )


class _StallingAgent:
    """AR-C: emits two deltas with a >1.5s monkeypatched clock gap between
    them, mid-sentence (no natural cut)."""

    def __init__(self, bus, role, clock):
        self._role = role
        self._clock = clock

    async def handle_message(self, msg: BusMessage):
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("word, ")
            self._clock[0] += 2.0  # advance past the 1.5s cap
            await on_token("word, more text")
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="word, more text", reply_to=msg.id,
            channel=msg.channel, context=msg.context,
        )


@pytest.fixture
async def ws_app_nonprefix():
    bus = MessageBus()
    agent = _NonPrefixAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop = asyncio.create_task(bus.run_agent_loop("butler"))

    ch = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=AsyncMock(), idle_timeout=300,
    )
    app = web.Application()
    ch.register_routes(app)

    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client
    loop.cancel()


@pytest.fixture
async def ws_app_shrinking():
    bus = MessageBus()
    agent = _ShrinkingAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop = asyncio.create_task(bus.run_agent_loop("butler"))

    ch = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=AsyncMock(), idle_timeout=300,
    )
    app = web.Application()
    ch.register_routes(app)

    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client
    loop.cancel()


@pytest.fixture
async def ws_app_stalling(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "channels.voice.prosodic.time.monotonic", lambda: clock[0],
    )
    bus = MessageBus()
    agent = _StallingAgent(bus, "butler", clock)
    bus.register("butler", agent.handle_message)
    loop = asyncio.create_task(bus.run_agent_loop("butler"))

    ch = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=AsyncMock(), idle_timeout=300,
    )
    app = web.Application()
    ch.register_routes(app)

    async with TestClient(TestServer(app)) as _raw_client:
        client = SigningVoiceClient(_raw_client)
        yield client
    loop.cancel()


async def _collect_ws_frames(client, *, scope_id: str, uid: str) -> list[dict]:
    frames: list[dict] = []
    async with client.ws_connect("/api/converse/ws") as ws:
        await ws.send_json({
            "type": "utterance", "utterance_id": uid,
            "text": "hi", "agent_role": "butler", "scope_id": scope_id,
        })
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            frames.append(frame)
            if frame["type"] == "done":
                break
    return frames


@pytest.mark.asyncio
class TestARBGuardWS:
    async def test_nonprefix_cumulative_resets_splitter_and_logs_debug(
        self, ws_app_nonprefix, caplog,
    ):
        with caplog.at_level(logging.DEBUG, logger="channels.voice.channel"):
            frames = await _collect_ws_frames(
                ws_app_nonprefix, scope_id="s-ar-b-ws", uid="u-ar-b",
            )

        assert any(f["type"] == "done" for f in frames)
        block_texts = [f["text"] for f in frames if f["type"] == "block"]
        # The pre-reset buffered text ("Attempt one talking ", no sentence
        # mark, never flushed) is discarded on reset; the fresh splitter
        # renders attempt two's text cleanly — no garbled concatenation.
        assert block_texts == ["Attempt two is unrelated."], block_texts
        assert any(
            "non-prefix cumulative" in r.getMessage()
            for r in caplog.records
            if r.name == "channels.voice.channel"
        ), [r.getMessage() for r in caplog.records]

    async def test_shrinking_cumulative_does_not_throw(self, ws_app_shrinking):
        frames = await _collect_ws_frames(
            ws_app_shrinking, scope_id="s-ar-b-shrink-ws", uid="u-shrink",
        )

        types = [f["type"] for f in frames]
        assert "done" in types
        assert "error" not in types
        block_texts = [f["text"] for f in frames if f["type"] == "block"]
        # The first (already-flushed) block survives; the shrink is
        # rendered as a fresh, clean block of its own — no crash, no
        # garbage/empty-string block.
        assert block_texts == ["Hello there my friend.", "Hi."], block_texts


@pytest.mark.asyncio
class TestARCTimeCapWS:
    async def test_stall_mid_sentence_forces_clause_preferring_block(
        self, ws_app_stalling,
    ):
        frames = await _collect_ws_frames(
            ws_app_stalling, scope_id="s-ar-c-ws", uid="u-arc",
        )

        block_texts = [f["text"] for f in frames if f["type"] == "block"]
        # The >1.5s stall forces a cap block on the rightmost clause mark
        # (the comma) rather than waiting for a sentence mark or hard-
        # cutting mid-word; the remainder is flushed at turn end.
        assert block_texts, "expected the time-cap to force a block mid-turn"
        assert block_texts[0].rstrip() == "word,", block_texts
        assert "more text" in "".join(block_texts)


async def test_pin_inv_voice_002_text_only_agent_refused_on_every_voice_path(
    signed_ws_app,
):
    """Pins INV-VOICE-002: an agent without the voice capability is
    unreachable over voice — SSE dispatch, WS route registration, and WS
    utterance dispatch all refuse it.

    Red case demonstrated: dropping the `agent_allowed_on("voice", cfg)` half
    of _sse_handler's gate lets the SSE turn through and fails the 404
    assertion.
    """
    client, channel, _, headers = signed_ws_app
    body = json.dumps({
        "prompt": "hi", "agent_role": "text-only", "scope_id": "s",
    }).encode()
    sse_headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": hmac.new(
            channel._webhook_secret.encode(), body, hashlib.sha256,
        ).hexdigest(),
    }
    response = await client.post("/api/converse", data=body, headers=sse_headers)
    assert response.status == 404
    response.close()

    async with client.ws_connect("/api/converse/ws", headers=headers) as ws:
        await ws.send_json({
            "type": "voice_route_register", "protocol": 2,
            "route_id": "entry-text", "agent_role": "text-only",
            "capabilities": [
                "background_jobs", "endpoint_delivery", "voice_handoff",
            ],
        })
        assert (await ws.receive_json())["accepted_capabilities"] == []
        assert channel.routes.get_connected("entry-text") is None

        await ws.send_json({
            "type": "utterance", "utterance_id": "u-text",
            "text": "hi", "agent_role": "text-only", "scope_id": "s",
        })
        reply = await ws.receive_json()
        assert reply["kind"] == "unknown_agent"
