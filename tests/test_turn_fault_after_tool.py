"""A fault after a tool ran ends the turn and keeps its session (#1654).

Drives ``Agent.handle_message`` and ``Agent._process`` over the real retry
wrapper, client pool, resume decision, session gate and session registry.
Only the CLI process is a stand-in: ``StandInCli`` plays one scripted turn
per query and emits what the CLI emits — an init message naming the session,
assistant messages that carry it, and, for an API fault that outlasted the
CLI's own retries, an assistant message whose envelope carries ``error``.

What each test counts is what a person would meet: how many CLI processes
were started and with which ``--resume``, how many times the tool's effect
happened, which session the conversation is registered on afterwards, and
the line that comes back.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from error_kinds import _USER_MESSAGES, ApiErrorTurn, ErrorKind
from ingress_identity import ingress_identity
from mcp_registry import McpServerRegistry
from session_registry import (
    SessionRegistry, build_scoped_session_key, reset_channel,
)
from session_reg_helpers import RESIDENT_DIGEST, resident_prov, resident_role_id

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
    from tests.test_agent_process import patch_retry_sleep
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT
    from test_agent_process import patch_retry_sleep


# --------------------------------------------------------------------------
# The stand-in CLI
# --------------------------------------------------------------------------

#: Steps of a scripted turn.
TOOL = "tool"                    # the model calls a tool with an effect
TEXT = "text"                    # the model writes, no tool
OVERLOADED = "overloaded"        # API fault, classified RATE_LIMIT (retryable)
SERVER_ERROR = "server_error"    # API fault, classified SDK_ERROR (retryable)
REFUSAL = "refusal"              # the safety system declined the turn
EXIT = "exit"                    # the CLI process died mid-turn
DONE = "done"                    # the turn ends with an answer

_SEND = "mcp__casa-framework__send_message"


class Hold:
    """A step that parks the turn until the test releases it."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()


class StandInCli:
    """One CLI process. It runs the session it was started to resume, else a
    new one, and plays the next scripted turn for each query."""

    def __init__(self, options, rig: "Rig") -> None:
        self.rig = rig
        self.resume = getattr(options, "resume", None)
        rig.started.append(self.resume)
        self.sid = (self.resume if self.resume and not rig.rekeys
                    else f"sid-{len(rig.started)}")
        self.queries = 0

    async def connect(self):
        if self.resume is not None and self.resume in self.rig.gone:
            # What the CLI does for a session it no longer has: it exits
            # before any message.
            raise ProcessError("No conversation found", exit_code=1)

    async def disconnect(self):
        return None

    async def interrupt(self):
        return None

    async def query(self, prompt, session_id="default"):
        self.queries += 1

    def _assistant(self, block, **extra) -> AssistantMessage:
        return AssistantMessage(
            content=[block], model="m", session_id=self.sid, **extra)

    def _result(self, *, is_error: bool = False, result: str = ""):
        return ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=is_error, num_turns=1, session_id=self.sid, result=result)

    async def receive_response(self):
        # An unscripted turn simply answers, so a rerun the code should not
        # have made shows up in the counts rather than as a harness error.
        steps = self.rig.turns.pop(0) if self.rig.turns else [DONE]
        if self.queries == 1:
            # A process announces its session once; a later turn on the same
            # process names it only on its own messages.
            yield SystemMessage(subtype="init", data={"session_id": self.sid})
        for step in steps:
            if isinstance(step, Hold):
                step.reached.set()
                await step.release.wait()
            elif step == TOOL:
                tool_id = f"t{len(self.rig.effects) + 1}"
                yield self._assistant(ToolUseBlock(
                    id=tool_id, name=_SEND, input={"text": "Dinner at 8"}))
                self.rig.effects.append(self.sid)       # the message is sent
                yield UserMessage(content=[ToolResultBlock(
                    tool_use_id=tool_id, content="sent")])
            elif step == TEXT:
                yield self._assistant(TextBlock(text="Let me tell her."))
            elif step in (OVERLOADED, SERVER_ERROR):
                yield self._assistant(
                    TextBlock(text=f"API Error: {step}"), error=step)
                yield self._result(is_error=True, result=f"API Error: {step}")
                return
            elif step == REFUSAL:
                yield self._assistant(
                    TextBlock(text="API Error: declined"),
                    error="invalid_request", stop_reason="refusal")
                yield self._result(is_error=True, result="API Error: declined")
                return
            elif step == EXIT:
                raise ProcessError("Command failed", exit_code=-9)
            elif step == DONE:
                yield self._assistant(TextBlock(text="Done, I told her."))
                yield self._result()
                return


class Rig:
    """The ``make_client`` seam: every call is one CLI process started."""

    def __init__(self) -> None:
        self.turns: list[list] = []
        self.started: list[str | None] = []   # --resume of each process
        self.effects: list[str] = []          # session each send happened in
        self.gone: set[str] = set()           # sessions the CLI no longer has
        self.rekeys = False                   # a resume runs under a new id

    def script(self, *turns: list) -> None:
        self.turns.extend(list(t) for t in turns)

    def __call__(self, options) -> StandInCli:
        return StandInCli(options, self)


KEY = build_scoped_session_key("telegram", "assistant", "42")


def _make_agent(tmp_path) -> Agent:
    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role="assistant",
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        role_id=resident_role_id("assistant"),
        kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov("assistant"),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


def _msg(text: str = "Tell Anna dinner is at 8", *,
         msg_type: MessageType = MessageType.REQUEST,
         channel: str = "telegram", trusted: bool = False) -> BusMessage:
    msg = BusMessage(
        type=msg_type, source="user", target="assistant", content=text,
        channel=channel, context={"chat_id": "42"},
    )
    if trusted:
        msg.trusted_user_origin = ingress_identity(
            "telegram", sender_id="9001", sender_is_operator=True)
    return msg


@pytest.fixture
def rig(monkeypatch) -> Rig:
    rig = Rig()
    monkeypatch.setattr("sdk_client_pool._default_make_client", rig)
    return rig


@pytest.fixture
def retry_sleep():
    """The retry wrapper's backoff, made instant; counts the retries slept."""
    with patch_retry_sleep() as sleep:
        yield sleep


@pytest.fixture
async def agent(tmp_path, rig, retry_sleep):
    agent = _make_agent(tmp_path)
    yield agent
    await agent.aclose()


def _registered(agent) -> str | None:
    return (agent._session_registry.get(KEY) or {}).get("sdk_session_id")


async def _say(agent, text: str = "Tell Anna dinner is at 8", **kw) -> str:
    """One turn through ``handle_message``; the text that comes back."""
    reply = await agent.handle_message(_msg(text, **kw))
    return reply.content if reply is not None else ""


async def _go_cold(agent) -> None:
    """Close the conversation's warm client, as idle eviction does."""
    await agent._pool.close_key(KEY)


# --------------------------------------------------------------------------
# A retryable API fault after a tool call
# --------------------------------------------------------------------------


async def test_a_fault_after_a_tool_call_is_not_rerun_and_keeps_its_session(
        agent, rig, retry_sleep):
    """Fresh session, one tool call, then an API overload: the turn ends as
    its classified error after ONE CLI process, the send happened once, no
    retry was consumed, and the conversation is registered on the session
    that ran the tool.

    Pins INV-TURN-017 (the retryable-fault half). Red case demonstrated:
    before the rule the turn was run again on a second fresh session and
    answered, so nothing was raised; with the rule in place, letting the
    retry wrapper rerun the attempt, or dropping the registration from the
    failure arm, each fails this test."""
    rig.script([TOOL, OVERLOADED])
    report: dict = {}

    with pytest.raises(ApiErrorTurn) as raised:
        await agent._process(_msg(), turn_report=report)

    assert raised.value.kind is ErrorKind.RATE_LIMIT
    assert rig.started == [None]
    assert rig.effects == ["sid-1"]
    assert report["retries"] == []
    assert retry_sleep.await_count == 0
    assert _registered(agent) == "sid-1"


async def test_the_turn_after_a_kept_session_resumes_it(agent, rig):
    """The next message continues the session the failed turn ran in, so the
    model's transcript holds the tool call and its result."""
    rig.script([TOOL, OVERLOADED], [DONE])

    line = await _say(agent)
    answer = await _say(agent, "did you tell her?")

    assert line == _USER_MESSAGES[ErrorKind.RATE_LIMIT]
    assert answer == "Done, I told her."
    assert rig.started == [None, "sid-1"]
    assert rig.effects == ["sid-1"]


async def test_a_memory_role_keeps_the_view_of_the_kept_session(
        agent, rig, tmp_path, monkeypatch):
    """A role served a memory view resumes a session only on a view record
    bound to exactly it. The failed turn binds its view to the session it
    keeps, so the next turn is a resume and not a silent fresh start."""
    from memory import sessions
    from memory_serving_support import fact, ledger_store, wire
    store = ledger_store(tmp_path, [fact("thermostat", "OV-20C")])
    wire(monkeypatch, store)
    rig.script([TOOL, OVERLOADED], [DONE])

    await _say(agent)
    with store.transaction() as conn:
        record = sessions.get(conn, KEY)
    assert record is not None and record.sdk_session_id == "sid-1"

    await _say(agent, "did you tell her?")
    assert rig.started == [None, "sid-1"]
    assert _registered(agent) == "sid-1"


async def test_a_fault_before_any_tool_call_is_still_retried(agent, rig):
    """Text but no tool call: nothing ran that a rerun could repeat, so the
    retry stays."""
    rig.script([TEXT, OVERLOADED], [DONE])
    report: dict = {}

    text = await agent._process(_msg(), turn_report=report)

    assert text == "Done, I told her."
    assert rig.started == [None, None]
    assert report["retries"] == [ErrorKind.RATE_LIMIT]
    assert _registered(agent) == "sid-2"


async def test_a_retry_that_then_runs_a_tool_and_fails_keeps_its_own_session(
        agent, rig):
    """Attempt 1 fails before any tool, attempt 2 fails after one: exactly
    two processes, one retry, and the registry names attempt 2's session."""
    rig.script([OVERLOADED], [TOOL, OVERLOADED])
    report: dict = {}

    with pytest.raises(ApiErrorTurn):
        await agent._process(_msg(), turn_report=report)

    assert rig.started == [None, None]
    assert rig.effects == ["sid-2"]
    assert report["retries"] == [ErrorKind.RATE_LIMIT]
    assert _registered(agent) == "sid-2"


async def test_a_scheduled_turn_is_not_rerun_either(agent, rig):
    """A turn Casa's schedule fired never takes a pooled client; the rule is
    the same on that path."""
    rig.script([TOOL, OVERLOADED])
    report: dict = {}

    with pytest.raises(ApiErrorTurn):
        await agent._process(
            _msg("check the calendar", msg_type=MessageType.SCHEDULED),
            turn_report=report)

    assert rig.started == [None]
    assert rig.effects == ["sid-1"]
    assert report["retries"] == []
    assert _registered(agent) == "sid-1"


@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
async def test_a_resumed_turn_that_faults_after_a_tool_keeps_its_session(
        agent, rig, cold):
    """A turn continuing an existing session, on its warm client or after a
    reconnect: no second attempt, and the conversation stays on that
    session."""
    rig.script([DONE], [TOOL, SERVER_ERROR])
    await _say(agent, "hello")
    if cold:
        await _go_cold(agent)
    before = list(rig.started)

    line = await _say(agent)

    assert line == _USER_MESSAGES[ErrorKind.SDK_ERROR]
    assert rig.started == before + (["sid-1"] if cold else [])
    assert rig.effects == ["sid-1"]
    assert _registered(agent) == "sid-1"


async def test_a_refusal_after_a_tool_call_keeps_no_session(agent, rig):
    """A declined turn's session is not one to continue: nothing is
    registered for a fresh one, and a resumed one is still cleared."""
    rig.script([TOOL, REFUSAL])
    assert await _say(agent) == _USER_MESSAGES[ErrorKind.REFUSAL]
    assert _registered(agent) is None

    rig.script([DONE], [TOOL, REFUSAL])
    await _say(agent, "hello")
    assert _registered(agent) is not None
    assert await _say(agent) == _USER_MESSAGES[ErrorKind.REFUSAL]
    assert _registered(agent) is None


# --------------------------------------------------------------------------
# The CLI process dies
# --------------------------------------------------------------------------


async def test_a_cli_exit_after_a_tool_call_is_not_rerun_fresh(agent, rig):
    """A resuming turn whose CLI dies after its tool ran: the conversation is
    neither dropped nor run again from blank. The error surfaces, the stored
    session survives, and the next turn resumes it.

    Pins INV-TURN-017 (the CLI-exit half). Red case demonstrated: before the
    rule the stored session was cleared and the turn rerun fresh, so the
    reply was an answer and a second process had started; with the rule in
    place, clearing whenever the attempt was resuming (ignoring whether a
    message of the turn had arrived) fails this test."""
    rig.script([DONE], [TOOL, EXIT], [DONE])
    await _say(agent, "hello")

    line = await _say(agent)

    assert line == _USER_MESSAGES[ErrorKind.UNKNOWN]
    assert rig.started == [None]
    assert rig.effects == ["sid-1"]
    assert _registered(agent) == "sid-1"

    assert await _say(agent, "did you tell her?") == "Done, I told her."
    assert rig.started == [None, "sid-1"]


async def test_a_cli_exit_after_text_only_is_raised_and_touches_nothing(
        agent, rig):
    """Messages arrived, no tool ran: the turn fails with no fresh rerun and
    no registration."""
    rig.script([DONE], [TEXT, EXIT])
    await _say(agent, "hello")
    generation = agent._session_registry.generation(KEY)

    line = await _say(agent)

    assert line == _USER_MESSAGES[ErrorKind.UNKNOWN]
    assert rig.started == [None]
    assert _registered(agent) == "sid-1"
    assert agent._session_registry.generation(KEY) == generation


async def test_a_cli_exit_after_a_tool_call_on_a_fresh_session_keeps_it(
        agent, rig):
    rig.script([TOOL, EXIT])

    line = await _say(agent)

    assert line == _USER_MESSAGES[ErrorKind.UNKNOWN]
    assert rig.started == [None]
    assert rig.effects == ["sid-1"]
    assert _registered(agent) == "sid-1"


async def test_a_session_the_cli_no_longer_has_is_cleared_and_rerun_fresh(
        agent, rig):
    """The CLI exits before any message of the turn: the stale-resume case.
    The stored session is cleared and the turn runs once on a fresh one."""
    rig.script([DONE], [TOOL, DONE])
    await _say(agent, "hello")
    await _go_cold(agent)
    rig.gone.add("sid-1")

    answer = await _say(agent)

    assert answer == "Done, I told her."
    assert rig.started == [None, "sid-1", None]
    assert rig.effects == ["sid-3"]
    assert _registered(agent) == "sid-3"


async def test_a_fresh_rerun_that_then_faults_after_a_tool_keeps_its_session(
        agent, rig):
    """Stale resume, then a tool call, then a fault, in one turn: the old
    session is cleared, and the fresh rerun's session is the one kept."""
    rig.script([DONE], [TOOL, OVERLOADED])
    await _say(agent, "hello")
    await _go_cold(agent)
    rig.gone.add("sid-1")

    line = await _say(agent)

    assert line == _USER_MESSAGES[ErrorKind.RATE_LIMIT]
    assert rig.started == [None, "sid-1", None]
    assert rig.effects == ["sid-3"]
    assert _registered(agent) == "sid-3"


# --------------------------------------------------------------------------
# What the person gets
# --------------------------------------------------------------------------


class _TelegramLike:
    name = "telegram"

    def __init__(self) -> None:
        self.deliveries: list[str] = []

    async def send_response(self, text: str, context: dict) -> None:
        self.deliveries.append(text)

    async def send(self, text: str, context: dict) -> None:
        self.deliveries.append(text)


class _VoiceLike:
    name = "voice"

    def __init__(self) -> None:
        self.error_lines: list[str] = []

    async def emit_error_line(self, kind, context, config):
        self.error_lines.append(kind)
        return True


async def test_telegram_gets_exactly_one_line_of_the_faults_kind(agent, rig):
    channel = _TelegramLike()
    agent._channel_manager.register(channel)
    rig.script([TOOL, SERVER_ERROR])

    await agent.handle_message(_msg(trusted=True))

    assert channel.deliveries == [_USER_MESSAGES[ErrorKind.SDK_ERROR]]


async def test_voice_gets_exactly_one_error_line_of_the_faults_kind(agent, rig):
    channel = _VoiceLike()
    agent._channel_manager.register(channel)
    rig.script([TOOL, SERVER_ERROR])

    await agent.handle_message(BusMessage(
        type=MessageType.REQUEST, source="voice", target="assistant",
        content="tell Anna dinner is at 8", channel="voice",
        context={"chat_id": "v1"},
        trusted_user_origin=ingress_identity("voice_sse"),
    ))

    assert channel.error_lines == ["sdk_error"]
    assert rig.started == [None]


# --------------------------------------------------------------------------
# Resume health
# --------------------------------------------------------------------------


async def test_a_fault_on_the_kept_session_is_one_strike_against_it(agent, rig):
    """The first failure started fresh, so it is no evidence about a resume.
    The next turn resumed the kept session; its terminal SDK error is one
    strike, recorded against that session."""
    rig.script([TOOL, SERVER_ERROR], [TOOL, SERVER_ERROR])

    await agent.handle_message(_msg(trusted=True))
    entry = agent._session_registry.get(KEY)
    assert entry["sdk_session_id"] == "sid-1"
    assert "resume_fault_streak" not in entry

    await agent.handle_message(_msg("again please", trusted=True))
    entry = agent._session_registry.get(KEY)
    assert rig.started == [None, "sid-1"]
    assert entry["sdk_session_id"] == "sid-1"
    assert entry["resume_fault_sid"] == "sid-1"
    assert entry["resume_fault_streak"] == 1


async def test_a_strike_follows_the_session_the_failed_resume_ran_as(
        agent, rig):
    """The CLI can run a resumed session under a new id. The failed turn
    registers that id, and the strike is recorded against it: the session
    the next ask would resume."""
    rig.script([DONE], [TOOL, SERVER_ERROR])
    await agent.handle_message(_msg("hello", trusted=True))
    await _go_cold(agent)
    rig.rekeys = True

    await agent.handle_message(_msg(trusted=True))

    entry = agent._session_registry.get(KEY)
    assert rig.started == [None, "sid-1"]
    assert entry["sdk_session_id"] == "sid-2"
    assert entry["resume_fault_sid"] == "sid-2"
    assert entry["resume_fault_streak"] == 1


# --------------------------------------------------------------------------
# What queues behind the failed turn
# --------------------------------------------------------------------------


async def test_a_message_queued_behind_the_failed_turn_resumes_its_session(
        agent, rig):
    """The second message waits on the conversation's session gate; the
    failed turn registers its session before releasing it."""
    hold = Hold()
    rig.script([TOOL, hold, OVERLOADED], [DONE])

    first = asyncio.create_task(_say(agent))
    await asyncio.wait_for(hold.reached.wait(), timeout=2)
    second = asyncio.create_task(_say(agent, "and tell Marco too"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second.done()
    hold.release.set()

    assert await asyncio.wait_for(first, timeout=2) == (
        _USER_MESSAGES[ErrorKind.RATE_LIMIT])
    assert await asyncio.wait_for(second, timeout=2) == "Done, I told her."
    assert rig.started == [None, "sid-1"]


async def test_a_new_queued_behind_the_failed_turn_leaves_no_session(
        agent, rig):
    """A ``/new`` waits on the same gate and then drops the kept session."""
    hold = Hold()
    rig.script([TOOL, hold, OVERLOADED])

    first = asyncio.create_task(_say(agent))
    await asyncio.wait_for(hold.reached.wait(), timeout=2)
    reset = asyncio.create_task(reset_channel(KEY, agent._session_registry))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not reset.done()
    hold.release.set()

    await asyncio.wait_for(first, timeout=2)
    await asyncio.wait_for(reset, timeout=2)
    assert agent._session_registry.get(KEY) is None
