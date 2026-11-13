"""A turn the operator stopped: what it wrote is delivered as any reply is,
and a stop that left nothing written is silence — not an error line from
Casa, and no verdict on the session it resumed.

The SDK boundary is scripted through ``sdk_client_pool._default_make_client``
with a client whose ``interrupt()`` ends the turn in flight the way the SDK
does: one assistant message with the text so far, then an error result.

A streamed turn's draft is ended whenever the turn stops running the model
— it returned, it raised, or it was cancelled — and its reply then goes out
as an ordinary message.

A turn stopped while it waits on a synchronous hand-off takes the hand-off
down with it: the agent it handed the work to runs on an SDK client of its
own, which no stop reaches, and ends because the stopped turn's tool handler
is cancelled.

The last test runs the whole path of a press with nothing set by hand: the
bus dispatches the turn, the real Telegram channel writes its draft on a fake
bot, and the stop update goes through the channel's own handler to the pool.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import types
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    StreamEvent as _SDKStreamEvent,
    TextBlock as _SDKTextBlock,
)

from bus import MessageBus, MessageType
from channels import ChannelManager
from channels.telegram_draft import DraftStream
from channels.tg_address import CASA
from error_kinds import ErrorKind
from log_cid import cid_var
from sdk_client_pool import interrupt_turns
from session_registry import SessionRegistry, build_scoped_session_key
from specialist_registry import SpecialistRegistry
from telegram_channel_double import _mk_channel_with_fake_bot, make_transport
from text_util import utf16_len

try:
    from tests.test_agent_process import (
        _make_agent_with_registry,
        patch_retry_sleep,
    )
    from tests.test_delegate_to_agent import _caller_cfg, _seed_specialist_dir
    from tests.test_specialist_registry import _use_synthetic_roles_dir
    from tests.test_resume_fault_streak import (
        _align_seeded_surface,
        _seeded_registry,
        _trusted_msg,
    )
except ImportError:
    from test_agent_process import (
        _make_agent_with_registry,
        patch_retry_sleep,
    )
    from test_delegate_to_agent import _caller_cfg, _seed_specialist_dir
    from test_specialist_registry import _use_synthetic_roles_dir
    from test_resume_fault_streak import (
        _align_seeded_surface,
        _seeded_registry,
        _trusted_msg,
    )

_CID = "stop-cid"
_KEY = build_scoped_session_key("telegram", "butler", "fault-scope")
_CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    try:
        block = _SDKTextBlock(text=text)
    except TypeError:
        block = _SDKTextBlock(text)  # type: ignore[call-arg]
    try:
        return _SDKAssistantMessage(content=[block])
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_result(sid: str, *, is_error: bool = False,
               subtype: str = "success") -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    m.is_error = is_error  # type: ignore[attr-defined]
    m.result = ""  # type: ignore[attr-defined]
    m.subtype = subtype  # type: ignore[attr-defined]
    return m


def _mk_partial(text: str) -> _SDKStreamEvent:
    """A partial message: one piece of the reply's text, as it is written."""
    return _SDKStreamEvent(
        uuid="ev", session_id="sid-partial",
        event={"type": "content_block_delta",
               "delta": {"type": "text_delta", "text": text}})


class StoppableClient:
    """One attempt of a turn.

    ``fault`` is raised from ``query()`` — an attempt that fails before it
    runs. ``answer`` is a turn that completes on its own. Otherwise the turn
    stays in flight, as a long answer does, until ``interrupt()`` ends it:
    one assistant message with ``written`` (when anything was written), then
    the error result the SDK closes an interrupted turn with. ``partials``
    are the pieces of text such a turn streams before it waits."""

    def __init__(self, options, in_flight: asyncio.Event, *, sid: str,
                 written: str = "", answer: str | None = None,
                 fault: Exception | None = None,
                 partials: tuple[str, ...] = ()) -> None:
        self.options = options
        self.sid = sid
        self.interrupts = 0
        self._in_flight = in_flight
        self._written = written
        self._partials = partials
        self._answer = answer
        self._fault = fault
        self._stop = asyncio.Event()

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt, session_id="default"):
        if self._fault is not None:
            raise self._fault

    async def interrupt(self):
        self.interrupts += 1
        self._stop.set()

    async def receive_response(self):
        if self._answer is not None:
            yield _mk_assistant(self._answer)
            yield _mk_result(self.sid)
            return
        for piece in self._partials:
            yield _mk_partial(piece)
        self._in_flight.set()
        await self._stop.wait()
        if self._written:
            yield _mk_assistant(self._written)
        yield _mk_result(
            self.sid, is_error=True, subtype="error_during_execution")


class Attempts:
    """``make_client`` factory: one client per connect, in the order given.
    ``in_flight`` is set once an attempt is waiting to be stopped."""

    def __init__(self, *attempts: dict) -> None:
        self._attempts = list(attempts)
        self.clients: list[StoppableClient] = []
        self.in_flight = asyncio.Event()

    def __call__(self, options) -> StoppableClient:
        client = StoppableClient(
            options, self.in_flight,
            sid=f"sid-attempt-{len(self.clients) + 1}",
            **self._attempts.pop(0),
        )
        self.clients.append(client)
        return client


class RecordingChannel:
    """Channel double with no token callback: the turn is not streamed, so
    whatever it delivers arrives in one final call."""

    name = "telegram"

    def __init__(self) -> None:
        self.responses: list[str] = []
        self.sends: list[str] = []
        self.finished = 0

    async def send_response(self, text, context) -> None:
        self.responses.append(str(text))

    async def send(self, text, context) -> None:
        self.sends.append(str(text))

    async def turn_finished(self, context) -> None:
        self.finished += 1


class StreamingChannel(RecordingChannel):
    """Channel double whose turns are streamed: the token callback records
    what it is handed and its ``end`` counts the calls — and raises
    ``end_error`` when one is set. ``events`` keeps the order in which the
    stream was ended and the reply delivered."""

    def __init__(self) -> None:
        super().__init__()
        self.tokens: list[str] = []
        self.ended = 0
        self.events: list[str] = []
        self.end_error: Exception | None = None

    def create_on_token(self, context):
        async def on_token(text) -> None:
            self.tokens.append(str(text))

        def end() -> None:
            self.ended += 1
            self.events.append("end")
            if self.end_error is not None:
                raise self.end_error

        on_token.end = end
        return on_token

    async def send_response(self, text, context) -> None:
        self.events.append("send_response")
        await super().send_response(text, context)


class SteppedTime:
    """Clock and sleep for a draft stream inside a real turn. A sleep returns
    only when the test grants it, so the stream never runs ahead while the
    turn waits on something else."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeping = False
        self._grant = asyncio.Semaphore(0)

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeping = True
        await self._grant.acquire()
        self.t += seconds

    async def _asleep(self) -> None:
        for _ in range(10_000):
            if self.sleeping:
                return
            await asyncio.sleep(0)
        raise AssertionError("the draft stream never went to sleep")

    async def rounds(self, n: int) -> None:
        """Let the stream run *n* rounds; return once it sleeps again."""
        for _ in range(n):
            await self._asleep()
            self.sleeping = False
            self._grant.release()
        await self._asleep()


class WritingClient:
    """A turn that writes its reply as several messages, pausing after each
    one for as long as ``pause`` takes."""

    def __init__(self, options, parts, pause) -> None:
        self.options = options
        self._parts = parts
        self._pause = pause

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt, session_id="default"):
        return None

    async def receive_response(self):
        for part in self._parts:
            yield _mk_assistant(part)
            await self._pause()
        yield _mk_result("sid-writing")


class HandOffClient:
    """A turn waiting on a synchronous hand-off. Its stream runs the
    ``delegate_to_agent`` handler in a task of its own, as the SDK runs a
    tool's handler, and ``interrupt()`` cancels that task — what the SDK does
    to a handler in flight — before the turn closes with its error result."""

    def __init__(self, options) -> None:
        self.options = options
        self.handler: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt, session_id="default"):
        return None

    async def interrupt(self):
        self.handler.cancel()
        self._stop.set()

    async def receive_response(self):
        from tools import delegate_to_agent

        self.handler = asyncio.create_task(delegate_to_agent.handler({
            "agent": "finance", "task": "x", "context": "", "mode": "sync"}))
        await self._stop.wait()
        await asyncio.wait({self.handler})
        yield _mk_result(
            "sid-hand-off", is_error=True, subtype="error_during_execution")


class WaitingChild:
    """The agent a hand-off runs, on an SDK client of its own — never a
    pooled one. Its run stays in flight until it is cancelled, and *state*
    is told of both."""

    def __init__(self, options, state) -> None:
        self._state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def query(self, text):
        return None

    async def receive_response(self):
        self._state.in_flight.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._state.cancelled = True
            raise
        yield  # never reached: this makes the method an async generator


async def _stop_once_in_flight(start, attempts: Attempts):
    """Start a turn under a correlation id, wait until an attempt is in
    flight, stop it the way a Stop press does, and return the turn's result."""
    token = cid_var.set(_CID)
    try:
        task = asyncio.create_task(start())
    finally:
        cid_var.reset(token)
    await asyncio.wait_for(attempts.in_flight.wait(), timeout=5)
    assert await interrupt_turns(_CID) == 1
    return await asyncio.wait_for(task, timeout=5)


async def _agent_on(tmp_path, channel):
    """An agent on a seeded session that its next turn resumes, with
    *channel* as its Telegram channel."""
    registry = await _seeded_registry(tmp_path)
    agent = _make_agent_with_registry(registry, role="butler")
    agent._channel_manager.register(channel)
    _align_seeded_surface(registry, agent)
    return agent, registry


def _scripter(monkeypatch):
    def script(*attempts: dict) -> Attempts:
        factory = Attempts(*attempts)
        monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
        return factory

    return script


@pytest.fixture
async def stop_fixture(tmp_path, monkeypatch):
    """An agent on a seeded session, a recording channel, and a way to
    script the attempts of its next turn."""
    channel = RecordingChannel()
    agent, registry = await _agent_on(tmp_path, channel)
    yield agent, registry, channel, _scripter(monkeypatch)
    await agent.aclose()


@pytest.fixture
async def stream_fixture(tmp_path, monkeypatch):
    """The same agent on a channel that streams its turns."""
    channel = StreamingChannel()
    agent, _registry = await _agent_on(tmp_path, channel)
    yield agent, channel, _scripter(monkeypatch)
    await agent.aclose()


async def test_stop_with_text_delivers_the_text_once(stop_fixture):
    agent, _registry, channel, script = stop_fixture
    attempts = script({"written": "Hello wor"})

    response = await _stop_once_in_flight(
        lambda: agent.handle_message(_trusted_msg()), attempts)

    assert attempts.clients[0].interrupts == 1
    assert response is not None
    assert response.type is MessageType.RESPONSE
    assert response.content == "Hello wor"
    assert channel.responses == ["Hello wor"]
    assert channel.sends == []


async def test_stop_on_an_empty_retry_attempt_is_silence_not_an_error(
    stop_fixture,
):
    agent, _registry, channel, script = stop_fixture
    attempts = script({"fault": _CLIConnectionError("upstream reset")}, {})

    with patch_retry_sleep():
        response = await _stop_once_in_flight(
            lambda: agent.handle_message(_trusted_msg()), attempts)

    assert [c.interrupts for c in attempts.clients] == [0, 1]
    assert channel.sends == []
    assert channel.responses == []
    assert channel.finished == 1
    # A Telegram message that ends in silence has no response at all, so no
    # error text of Casa's reached the caller either.
    assert response is None


async def test_stop_on_an_empty_retry_attempt_is_no_health_strike(
    stop_fixture,
):
    agent, registry, _channel, script = stop_fixture
    attempts = script({"fault": _CLIConnectionError("upstream reset")}, {})
    report: dict = {}

    with patch.object(
        registry, "note_resume_health", wraps=registry.note_resume_health,
    ) as note, patch_retry_sleep():
        await _stop_once_in_flight(
            lambda: agent._process(_trusted_msg(), turn_report=report),
            attempts)

    # The turn resumed the seeded session and consumed an SDK_ERROR retry:
    # without the stop, this silence is a strike against that session.
    assert [getattr(c.options, "resume", None) for c in attempts.clients] \
        == ["old-sid", "old-sid"]
    assert report["retries"] == [ErrorKind.SDK_ERROR]
    assert note.call_count == 0
    assert "resume_fault_sid" not in registry._data[_KEY]


async def test_stopped_turn_publishes_its_session(stop_fixture):
    agent, registry, _channel, script = stop_fixture
    attempts = script({"written": "Hello wor"})

    await _stop_once_in_flight(
        lambda: agent.handle_message(_trusted_msg()), attempts)

    assert registry.get(_KEY)["sdk_session_id"] == "sid-attempt-1"


async def test_turn_report_says_the_turn_was_stopped(stop_fixture):
    agent, _registry, _channel, script = stop_fixture
    attempts = script({"written": "Hello wor"})
    report: dict = {}

    text = await _stop_once_in_flight(
        lambda: agent._process(_trusted_msg(), turn_report=report), attempts)

    assert text == "Hello wor"
    assert report["stopped"] is True
    # The result that closes a stopped turn is not an error result.
    assert report["error_result"] is None


async def test_turn_report_of_an_unstopped_turn_says_so(stop_fixture):
    agent, _registry, _channel, script = stop_fixture
    script({"answer": "All done."})
    report: dict = {}

    text = await agent._process(_trusted_msg(), turn_report=report)

    assert text == "All done."
    assert report["stopped"] is False


async def test_stream_is_ended_when_process_returns(stream_fixture):
    agent, channel, script = stream_fixture
    script({"answer": "All done."})

    await agent.handle_message(_trusted_msg())

    assert channel.tokens == ["All done."]
    assert channel.ended == 1


async def test_stream_is_ended_when_process_raises(stream_fixture):
    agent, channel, _script = stream_fixture

    with patch.object(agent, "_process", side_effect=RuntimeError("boom")):
        response = await agent.handle_message(_trusted_msg())

    assert channel.ended == 1
    # the turn still ends as a failed turn does: Casa's line, sent plainly
    assert response is not None and response.content
    assert channel.sends == [response.content]


async def test_stream_is_ended_when_the_turn_is_cancelled(stream_fixture):
    agent, channel, script = stream_fixture
    attempts = script({})

    task = asyncio.create_task(agent.handle_message(_trusted_msg()))
    await asyncio.wait_for(attempts.in_flight.wait(), timeout=5)
    assert channel.ended == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert channel.ended == 1
    assert channel.responses == [] and channel.sends == []


async def test_reply_goes_out_through_send_response_after_streaming(
    stream_fixture,
):
    agent, channel, script = stream_fixture
    script({"answer": "All done."})

    response = await agent.handle_message(_trusted_msg())

    assert channel.tokens == ["All done."]
    assert channel.responses == ["All done."]
    assert channel.sends == []
    assert response is not None and response.content == "All done."


async def test_stream_is_ended_before_the_reply_is_delivered(stream_fixture):
    agent, channel, script = stream_fixture
    script({"answer": "All done."})

    await agent.handle_message(_trusted_msg())

    assert channel.events == ["end", "send_response"]


async def test_a_failing_end_only_logs(stream_fixture, caplog):
    agent, channel, script = stream_fixture
    script({"answer": "All done."})
    channel.end_error = RuntimeError("the stream is broken")

    with caplog.at_level(logging.ERROR, logger="agent"):
        response = await agent.handle_message(_trusted_msg())

    assert channel.ended == 1
    assert channel.responses == ["All done."]
    assert channel.sends == []
    assert response is not None and response.content == "All done."
    assert [r.getMessage() for r in caplog.records
            if r.levelno >= logging.ERROR] == ["ending the reply stream failed"]


async def test_an_overlong_streamed_reply_is_paged_at_the_end(
    tmp_path, monkeypatch,
):
    # The real channel on a fake bot, its draft stream on a stepped clock.
    stepped = SteppedTime()
    ch, bot = _mk_channel_with_fake_bot()
    ch._new_draft_stream = functools.partial(
        DraftStream, clock=stepped.clock, sleep=stepped.sleep)
    agent, _registry = await _agent_on(
        tmp_path, make_transport(ch).get("telegram"))

    # Two messages the turn joins into one reply of 6,000 UTF-16 units; after
    # each, the stream gets two rounds to send what was written so far.
    parts = ["\U0001F389" * 1000, "\U0001F389" * 1999]
    reply = "\n\n".join(parts)
    assert utf16_len(reply) == 6000

    async def pause() -> None:
        await stepped.rounds(2)

    monkeypatch.setattr(
        "sdk_client_pool._default_make_client",
        lambda options: WritingClient(options, parts, pause))
    msg = _trusted_msg()
    msg.context["_tg_bot"] = CASA
    try:
        response = await agent.handle_message(msg)
    finally:
        await agent.aclose()

    assert response is not None and response.content == reply
    drafts = [c.kwargs["text"] for c in bot.send_message_draft.await_args_list]
    assert drafts == [parts[0]]                 # the text that still fitted
    assert all(utf16_len(text) <= 4096 for text in drafts)
    sent = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert len(sent) >= 2
    assert all(utf16_len(text) <= 4096 for text in sent)
    assert "".join("".join(sent).split()) == "".join(reply.split())
    assert bot.edit_message_text.await_count == 0
    assert ch._streams == {}


async def test_stop_ends_a_synchronous_hand_off(tmp_path, monkeypatch):
    from tools import init_tools

    # One agent the turn may hand work to, and the bus a hand-off's
    # completion notification would be posted on.
    specialists = tmp_path / "specialists"
    specialists.mkdir()
    _seed_specialist_dir(specialists, "finance", enabled=True)
    _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
    specialist_registry = SpecialistRegistry(
        str(specialists), tombstone_path=str(tmp_path / "del.json"))
    specialist_registry.load()
    bus = MessageBus()
    bus.register("butler", None)
    channel = RecordingChannel()
    agent, _registry = await _agent_on(tmp_path, channel)
    init_tools(
        ChannelManager(), bus, specialist_registry,
        agent_role_map={"butler": _caller_cfg(role="butler")})

    child = types.SimpleNamespace(in_flight=asyncio.Event(), cancelled=False)
    parents: list[HandOffClient] = []

    def make_parent(options) -> HandOffClient:
        parents.append(HandOffClient(options))
        return parents[-1]

    monkeypatch.setattr("sdk_client_pool._default_make_client", make_parent)
    monkeypatch.setattr(
        "tools.ClaudeSDKClient", lambda options: WaitingChild(options, child))

    token = cid_var.set(_CID)
    try:
        turn = asyncio.create_task(agent.handle_message(_trusted_msg()))
    finally:
        cid_var.reset(token)
    try:
        await asyncio.wait_for(child.in_flight.wait(), timeout=5)
        # Stop reaches the turn's own client and no other.
        assert await interrupt_turns(_CID) == 1
        response = await asyncio.wait_for(turn, timeout=5)
    finally:
        await agent.aclose()

    [parent] = parents
    assert parent.handler.cancelled()
    assert child.cancelled is True
    for _ in range(50):
        await asyncio.sleep(0)
    # The hand-off is over, and nothing says it completed.
    assert bus.queues["butler"].empty()
    assert channel.responses == [] and channel.sends == []
    assert response is None


async def test_stop_end_to_end_from_the_update_to_the_reply(
    tmp_path, monkeypatch,
):
    # The real channel on a fake bot, its draft stream on a stepped clock.
    stepped = SteppedTime()
    ch, bot = _mk_channel_with_fake_bot()
    ch._new_draft_stream = functools.partial(
        DraftStream, clock=stepped.clock, sleep=stepped.sleep)
    agent, _registry = await _agent_on(
        tmp_path, make_transport(ch).get("telegram"))
    attempts = _scripter(monkeypatch)(
        {"partials": ("Hello ", "wor"), "written": "Hello wor"})

    # The message carries its cid as an inbound message does, and the bus
    # dispatches it: from here the channel reads the cid from the context it
    # is handed and the pool runs the turn under the one the dispatch set.
    msg = _trusted_msg()
    msg.context["_tg_bot"] = CASA
    msg.context["cid"] = _CID
    bus = MessageBus()
    bus.register(msg.target, agent.handle_message)
    turns = bus.queues[msg.target]
    dispatch = asyncio.create_task(bus.run_agent_loop(msg.target))
    try:
        await bus.send(msg)
        await asyncio.wait_for(attempts.in_flight.wait(), timeout=5)
        await stepped.rounds(1)
        drafts = bot.send_message_draft.await_args_list
        assert [c.kwargs["text"] for c in drafts] == ["Hello wor"]
        assert bot.send_message.await_count == 0

        # Stop, as Telegram reports it: the chat and the draft's id.
        [(draft_id, (chat, _stream))] = ch._streams.items()
        assert drafts[0].kwargs["draft_id"] == int(draft_id)
        await ch._on_stop_update(types.SimpleNamespace(api_kwargs={
            "stopped_message_generation": {
                "chat": {"id": int(chat), "type": "private"},
                "draft_id": draft_id}}))

        # The press reached the turn's own client, once ...
        assert [c.interrupts for c in attempts.clients] == [1]
        await asyncio.wait_for(turns.join(), timeout=5)
        for _ in range(50):
            await asyncio.sleep(0)
        # ... and what it had written is the reply: one ordinary message,
        # with the draft left as it was and no stream behind it.
        assert [c.interrupts for c in attempts.clients] == [1]
        assert [c.kwargs["text"] for c in bot.send_message.await_args_list] \
            == ["Hello wor"]
        assert bot.send_message_draft.await_count == 1
        assert bot.edit_message_text.await_count == 0
        assert ch._streams == {}
        assert [t for t in asyncio.all_tasks()
                if t.get_name() == "telegram-draft-stream"
                and not t.done()] == []
    finally:
        # A turn the press did not reach is let go, so the close below
        # never waits on it.
        for client in attempts.clients:
            client._stop.set()
        try:
            await asyncio.wait_for(turns.join(), timeout=5)
        except asyncio.TimeoutError:
            pass
        dispatch.cancel()
        await agent.aclose()
