"""Tests for InCasaDriver — open, the session's turns put in the agent log,
cancel, resume, the engagement binding, the session id, and the API-fault
kind a turn ends with.

Each fake client gets the session surface from ``TurnScriptedSession``: every
written message runs as one scripted turn, echoed first."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

from agent_log_double import RecordingLogs
from turn_scripted_session import TurnScriptedSession

pytestmark = pytest.mark.asyncio


def _mk_text_block(text: str) -> TextBlock:
    """Instantiate TextBlock regardless of SDK shape."""
    try:
        return TextBlock(text=text)
    except TypeError:
        return TextBlock(text)  # type: ignore[call-arg]


def _mk_assistant(text: str) -> AssistantMessage:
    """Instantiate AssistantMessage regardless of SDK shape."""
    block = _mk_text_block(text)
    try:
        return AssistantMessage(content=[block])
    except TypeError:
        m = AssistantMessage.__new__(AssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_system_init(session_id: str):
    """A SystemMessage(subtype='init', data={'session_id': ...}) — the REAL
    source of the CLI session id (ClaudeSDKClient has no session_id attr on the
    pinned SDK). Faithful to production, unlike a fake `client.session_id`."""
    from claude_agent_sdk import SystemMessage
    try:
        return SystemMessage(subtype="init", data={"session_id": session_id})
    except TypeError:
        m = SystemMessage.__new__(SystemMessage)
        m.subtype = "init"          # type: ignore[attr-defined]
        m.data = {"session_id": session_id}  # type: ignore[attr-defined]
        return m


def _make_record(**overrides):
    from engagement_registry import EngagementRecord

    base = dict(
        id="e1", kind="specialist", role_or_type="finance", driver="in_casa",
        status="active", topic_id=42,
        started_at=0.0, last_user_turn_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    base.update(overrides)
    return EngagementRecord(**base)


# ---------------------------------------------------------------------------
# the session rig
# ---------------------------------------------------------------------------

def _mk_result_msg(*, stop_reason=None, session_id="sid-in-casa"):
    """A terminal ResultMessage — a turn's end, and the SECOND carrier of a
    refusal."""
    from claude_agent_sdk import ResultMessage
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id=session_id, stop_reason=stop_reason,
        total_cost_usd=0.0, usage={}, result="")


def _client_of(*messages):
    """A ClaudeSDKClient double whose every turn replays exactly ``messages``."""
    return _session_of(list(messages))


def _session_of(*turns):
    """A ClaudeSDKClient double whose n-th turn replays ``turns[n]`` (the last
    one repeats). Records what was written (``prompts``) and its options."""
    class _FakeClient(TurnScriptedSession):
        instances: list = []

        def __init__(self, options):
            self.options = options
            self.prompts: list[str] = []
            self.disconnected = 0
            type(self).instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def record_query(self, prompt):
            self.prompts.append(prompt)

        async def receive_response(self):
            frames = turns[min(len(self.prompts), len(turns)) - 1]
            for m in frames:
                yield m

        async def disconnect(self):
            self.disconnected += 1
            await super().disconnect()
    return _FakeClient


async def _until(predicate, timeout: float = 5.0) -> None:
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout)


async def _settle(drv, rec, session) -> None:
    """Wait until everything queued for *rec* was written and its turn ended:
    at its ResultMessage, or when its stream ended without one."""
    lane = drv._lanes.get(rec.id)

    def idle():
        if lane is not None and lane.queue:
            return False
        if session.written:
            return False
        return session.reader.done() or (
            not session.turn_open and session.segment is None)
    await _until(idle)
    keep = {session.reader, getattr(lane, "worker", None)}
    await _until(lambda: all(t.done() for t in list(drv._tasks) if t not in keep))
    for _ in range(5):
        await asyncio.sleep(0)


async def _launch(drv, rec, prompt="hi", options=None):
    """Open *rec*'s job session and write its brief; return once that turn
    ended. Returns the session."""
    await drv.open(rec, options or ClaudeAgentOptions(model="sonnet"))
    session = drv._sessions[rec.id]
    drv.enqueue_brief(rec, prompt)
    await _settle(drv, rec, session)
    return session


async def _say(drv, rec, session, text):
    """The operator writes one message; return once its turn ended."""
    drv.submit_operator(rec, text)
    await _settle(drv, rec, session)


# ---------------------------------------------------------------------------
# open, and the turn put in the agent log
# ---------------------------------------------------------------------------

class TestInCasaStart:
    async def test_start_opens_client_and_marks_alive(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        client = _client_of(_mk_result_msg())
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", client)

        drv = InCasaDriver()
        rec = _make_record()

        await drv.open(rec, ClaudeAgentOptions(model="sonnet"))
        assert drv.is_alive(rec) is True
        assert len(client.instances) == 1
        await drv.cancel(rec)

    async def test_start_logs_the_initial_response(self, monkeypatch):
        from agent_logs import LogKey
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient",
                            _client_of(_mk_assistant("Hello from Alex"),
                                       _mk_result_msg()))

        logs = RecordingLogs()
        drv = InCasaDriver(agent_logs=logs)
        rec = _make_record(origin={"job": {"title": "Q3 accounting\nsecond"}})

        await _launch(drv, rec)
        # One entry per message, under the specialist's key and the job's title.
        assert logs.lines == [
            (LogKey("agent", "finance"), "Q3 accounting\nsecond", "Hello from Alex")]

    async def test_a_plugin_jobs_lines_go_to_its_residents_plugin_log(
            self, monkeypatch):
        from agent_logs import LogKey
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient",
                            _client_of(_mk_assistant("working"), _mk_result_msg()))
        logs = RecordingLogs()
        drv = InCasaDriver(agent_logs=logs)
        rec = _make_record(kind="plugin", role_or_type="assistant",
                           origin={"job": {"title": "Sync"}})

        await _launch(drv, rec)
        assert logs.lines == [(LogKey("plugins", "assistant"), "Sync", "working")]

    async def test_start_separates_multiple_assistant_messages_with_double_newline(
        self, monkeypatch,
    ):
        """E-8: when the SDK emits multiple AssistantMessages in one turn
        (typical executor with tool calls), each one is its own log entry —
        never a glued paragraph."""
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
            _mk_assistant("Reading the doctrine files first."),
            _mk_assistant("Now let me look at Ellen's config files."),
            _mk_assistant("The trait belongs in character.yaml."),
            _mk_result_msg()))

        logs = RecordingLogs()
        drv = InCasaDriver(agent_logs=logs)
        rec = _make_record()

        await _launch(drv, rec)

        assert logs.texts() == [
            "Reading the doctrine files first.",
            "Now let me look at Ellen's config files.",
            "The trait belongs in character.yaml.",
        ]

    async def test_start_streams_skips_assistant_message_with_no_text(
        self, monkeypatch,
    ):
        """An AssistantMessage with no TextBlocks and no tool use logs
        nothing."""
        from drivers.in_casa_driver import InCasaDriver

        empty = _mk_assistant("ignored")
        empty.content = []  # type: ignore[attr-defined]
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
            _mk_assistant("Before tool call."), empty,
            _mk_assistant("After tool call."), _mk_result_msg()))

        logs = RecordingLogs()
        drv = InCasaDriver(agent_logs=logs)
        rec = _make_record()

        await _launch(drv, rec)

        assert logs.texts() == ["Before tool call.", "After tool call."]

    async def test_start_empty_turn_logs_nothing(self, monkeypatch):
        """A turn with no text logs nothing."""
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient",
                            _client_of(_mk_result_msg()))

        logs = RecordingLogs()
        drv = InCasaDriver(agent_logs=logs)
        rec = _make_record()

        await _launch(drv, rec)

        assert logs.lines == []

    async def test_start_logs_per_message(self, monkeypatch, caplog):
        """Each SDK message kind triggers an sdk_logging dispatch.
        SystemMessage(subtype=init) → DEBUG; AssistantMessage → INFO +
        per-tool DEBUG; UserMessage with ToolResultBlock → DEBUG;
        ResultMessage → INFO turn_done."""
        import logging
        from drivers.in_casa_driver import InCasaDriver
        from claude_agent_sdk import (
            AssistantMessage, SystemMessage, UserMessage, ResultMessage,
            TextBlock, ToolUseBlock, ToolResultBlock,
        )

        text_block = MagicMock()
        text_block.text = "Reading config."
        text_block.__class__ = TextBlock

        tool_use = MagicMock()
        tool_use.name = "Read"
        tool_use.input = {"file_path": "/x.yaml"}
        tool_use.__class__ = ToolUseBlock

        sys_init = MagicMock()
        sys_init.subtype = "init"
        sys_init.data = {"model": "sonnet", "session_id": "abc12345-xyz"}
        sys_init.__class__ = SystemMessage

        assistant = MagicMock()
        assistant.content = [text_block, tool_use]
        assistant.parent_tool_use_id = None
        assistant.error = None
        assistant.__class__ = AssistantMessage

        result_block = MagicMock()
        result_block.is_error = False
        result_block.content = "ok"
        result_block.__class__ = ToolResultBlock

        user_with_tool_result = MagicMock()
        user_with_tool_result.content = [result_block]
        user_with_tool_result.uuid = None
        user_with_tool_result.__class__ = UserMessage

        result = MagicMock()
        result.num_turns = 1
        result.total_cost_usd = 0.001
        result.usage = {"input_tokens": 100, "output_tokens": 20}
        result.session_id = "abc12345-xyz"
        result.is_error = False
        result.stop_reason = None
        result.subtype = "success"
        result.__class__ = ResultMessage

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
            sys_init, assistant, user_with_tool_result, result))

        logs = RecordingLogs()
        drv = InCasaDriver(agent_logs=logs)
        rec = _make_record()

        with caplog.at_level(logging.DEBUG, logger="sdk"):
            await _launch(drv, rec)

        msgs = [r.getMessage() for r in caplog.records if r.name == "sdk"]
        assert any("system_init" in m and "model=sonnet" in m for m in msgs), msgs
        assert any("assistant_message idx=1" in m and "tool_uses=1" in m for m in msgs), msgs
        assert any(
            "tool_use idx=1" in m and "name=Read" in m and "ms=" in m
            for m in msgs
        ), msgs
        assert "/x.yaml" not in caplog.text
        assert any("tool_result idx=1" in m and "ok=True" in m for m in msgs), msgs
        assert any("turn_done" in m and "turns=1" in m for m in msgs), msgs
        # The main loop's step is logged after the words written before it.
        assert logs.texts() == ["Reading config.", "🔧 Reading files"]

    async def test_a_sub_agents_steps_are_not_logged(self, monkeypatch):
        """Only the main loop's tool steps are logged; a sub-agent's (its
        message carries ``parent_tool_use_id``) are not."""
        from claude_agent_sdk import ToolUseBlock
        from drivers.in_casa_driver import InCasaDriver

        main = AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={})],
            model="m", parent_tool_use_id=None)
        sub = AssistantMessage(
            content=[ToolUseBlock(id="t2", name="Read", input={})],
            model="m", parent_tool_use_id="t0")
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient",
                            _client_of(main, sub, _mk_result_msg()))
        logs = RecordingLogs()
        drv = InCasaDriver(agent_logs=logs)
        rec = _make_record()

        await _launch(drv, rec)
        assert logs.texts() == ["🔧 Running commands"]

    async def test_the_reader_never_awaits_the_log(self, monkeypatch):
        """The log is a synchronous call: whatever the sink's ``log`` returns
        is dropped, never awaited, so no Telegram wait can reach the reader.
        Here it returns a coroutine that records being awaited."""
        from drivers.in_casa_driver import InCasaDriver

        calls: list = []
        awaited: list = []
        returned: list = []

        async def _would_wait():
            awaited.append(True)

        class _Sink:
            enabled = True

            def log(self, key, run, text):
                calls.append(text)
                coro = _would_wait()
                returned.append(coro)
                return coro

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
            _mk_assistant("First."), _mk_assistant("Second."), _mk_result_msg()))
        drv = InCasaDriver(agent_logs=_Sink())
        rec = _make_record()

        await _launch(drv, rec)
        for coro in returned:
            coro.close()
        assert calls == ["First.", "Second."]
        assert awaited == []

    async def test_start_injects_stderr_callback(self, monkeypatch):
        """Bug 4: open() must inject the stderr callback on the options
        before ClaudeSDKClient is built."""
        from drivers.in_casa_driver import InCasaDriver

        client = _client_of(_mk_result_msg())
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", client)

        drv = InCasaDriver()
        rec = _make_record()

        await drv.open(rec, ClaudeAgentOptions(model="sonnet"))

        assert client.instances, "_FakeClient never constructed"
        assert callable(client.instances[0].options.stderr), (
            "ClaudeAgentOptions.stderr must be set by in_casa_driver.open() "
            "before ClaudeSDKClient is built"
        )
        await drv.cancel(rec)


class TestInCasaSendUserTurn:
    async def test_send_user_turn_logs_the_reply(self, monkeypatch):
        """An operator's message is written into the live session and its
        turn's reply is logged."""
        from drivers.in_casa_driver import InCasaDriver

        client = _session_of([_mk_assistant("launch reply"), _mk_result_msg()],
                             [_mk_assistant("re:user said X"), _mk_result_msg()])
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", client)

        logs = RecordingLogs()
        drv = InCasaDriver(agent_logs=logs)
        rec = _make_record()
        session = await _launch(drv, rec, "system prompt")
        logs.lines.clear()

        await _say(drv, rec, session, "user said X")
        assert client.instances[0].prompts == ["system prompt", "user said X"]
        assert logs.texts() == ["re:user said X"]


class TestInCasaCancel:
    async def test_cancel_closes_client_and_flips_alive(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        client = _client_of(_mk_result_msg())
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", client)

        drv = InCasaDriver()
        rec = _make_record()
        await _launch(drv, rec, "p")
        assert drv.is_alive(rec) is True
        await drv.cancel(rec)
        assert drv.is_alive(rec) is False
        assert client.instances[0].disconnected == 1

    async def test_cancel_is_idempotent(self):
        from drivers.in_casa_driver import InCasaDriver

        drv = InCasaDriver()
        rec = _make_record()
        # Not alive yet: must not raise.
        await drv.cancel(rec)
        await drv.cancel(rec)
        assert drv.is_alive(rec) is False


class TestInCasaResume:
    async def test_resume_reopens_client_with_session_id(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        client = _client_of(_mk_result_msg())
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", client)
        # Finding 2 (v0.69.10): resume rebuilds the FULL option set (Agent/Task
        # denies + fail-closed callback), not a bare ClaudeAgentOptions(resume=).
        import tools as tools_mod

        def _fake_rebuild(engagement, session_id):
            return ClaudeAgentOptions(
                resume=session_id, disallowed_tools=["Bash", "Agent", "Task"],
            )
        monkeypatch.setattr(tools_mod, "build_engagement_resume_options", _fake_rebuild)

        drv = InCasaDriver()
        rec = _make_record(sdk_session_id="sess-old")
        await drv.resume(rec, session_id="sess-old")
        assert drv.is_alive(rec) is True
        seen = client.instances[0].options
        assert seen.resume == "sess-old"
        # the restrictions must ride along on resume, not be dropped
        assert "Agent" in seen.disallowed_tools
        assert "Task" in seen.disallowed_tools
        await drv.cancel(rec)

    async def test_get_session_id_returns_stream_sourced_session_id(self, monkeypatch):
        """v0.69.11: get_session_id returns the id captured from the message
        stream (SystemMessage init), NOT a nonexistent client.session_id attr."""
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
            _mk_system_init("sess-xyz"), _mk_result_msg(session_id="sess-xyz")))

        drv = InCasaDriver()
        rec = _make_record()
        await _launch(drv, rec, "p")
        assert drv.get_session_id(rec) == "sess-xyz"
        # cleared after cancel (no leak)
        await drv.cancel(rec)
        assert drv.get_session_id(rec) is None


class TestInCasaEngagementContext:
    async def test_engagement_var_propagates_into_sdk_inner_task(self, monkeypatch):
        """E-E (v0.29.0): the SDK's __aenter__ creates an inner asyncio Task
        (claude_agent_sdk._internal.query.Query._read_task via
        loop.create_task) which captures the current ContextVar copy at
        creation time. Tool callbacks dispatched from that inner task
        therefore see whatever engagement_var was bound to at __aenter__
        time — NOT what the parent task binds later.

        open() binds engagement_var BEFORE __aenter__, so the SDK inner
        task's captured context has the engagement record.

        The fake client below models the real SDK pattern: __aenter__
        spawns a task; inside that task we snapshot engagement_var.
        Asserting on the snapshot proves the binding."""
        import asyncio as _asyncio
        from drivers.in_casa_driver import InCasaDriver
        from tools import engagement_var

        inner_captured: list = []

        class _SDKWithInnerTask(TurnScriptedSession):
            def __init__(self, options):
                self._inbox: _asyncio.Queue = _asyncio.Queue()
                self._outbox: _asyncio.Queue = _asyncio.Queue()
                self._task: _asyncio.Task | None = None

            async def __aenter__(self):
                # Same shape as Query.start in claude_agent_sdk:
                # loop.create_task(self._read_messages()) — the new task
                # captures the CURRENT context.
                loop = _asyncio.get_running_loop()
                self._task = loop.create_task(self._read_loop())
                return self

            async def _read_loop(self):
                """Inside the SDK's inner task — where every tool callback
                is dispatched in the real SDK. Snapshot engagement_var ONCE
                per prompt to model what the tool callback would see."""
                while True:
                    item = await self._inbox.get()
                    if item is None:
                        return
                    inner_captured.append(engagement_var.get(None))
                    await self._outbox.put(item)

            async def record_query(self, prompt):
                await self._inbox.put(prompt)

            async def receive_response(self):
                text = await self._outbox.get()
                yield _mk_assistant(f"echoed:{text}")
                yield _mk_result_msg()

            async def close(self):
                await self._inbox.put(None)
                if self._task is not None:
                    try:
                        await self._task
                    except _asyncio.CancelledError:
                        pass

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", _SDKWithInnerTask,
        )

        drv = InCasaDriver()
        rec = _make_record(role_or_type="configurator", kind="executor")

        # Pre-state: engagement_var unbound in the parent task.
        assert engagement_var.get(None) is None

        await _launch(drv, rec, "p")

        assert inner_captured == [rec], (
            "E-E regression: SDK inner task did not capture engagement_var. "
            "Bind engagement_var BEFORE ClaudeSDKClient.__aenter__() in "
            "InCasaDriver.open()."
        )
        # The caller's binding is cleared after open() returns — it does not
        # leak into the surrounding task.
        assert engagement_var.get(None) is None
        await drv.cancel(rec)

    async def test_deliver_turn_sets_engagement_var_during_sdk_loop(self, monkeypatch):
        """engagement_var is bound while the session's reader consumes the
        message stream, and never in the caller's task."""
        from drivers.in_casa_driver import InCasaDriver
        from tools import engagement_var

        captured: list = []

        class _Client(_client_of()):
            async def receive_response(self):
                captured.append(engagement_var.get(None))
                yield _mk_assistant("hi")
                yield _mk_result_msg()

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _Client)

        drv = InCasaDriver()
        rec = _make_record(role_or_type="configurator", kind="executor")

        assert engagement_var.get(None) is None
        await _launch(drv, rec, "hi")
        assert engagement_var.get(None) is None
        assert captured == [rec]

    async def test_deliver_turn_persists_session_id_on_first_message(self, monkeypatch):
        """session_id from the SystemMessage init stream triggers persist once
        (v0.69.11 — was read from a nonexistent client.session_id attr)."""
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
            _mk_system_init("sess-abc"), _mk_assistant("hi"),
            _mk_result_msg(session_id="sess-abc")))

        persist = AsyncMock()
        drv = InCasaDriver(persist_session_id=persist)
        rec = _make_record()

        await _launch(drv, rec, "hi")

        persist.assert_awaited_once_with(rec.id, "sess-abc")
        assert rec.sdk_session_id == "sess-abc"
        assert drv.get_session_id(rec) == "sess-abc"   # stream-sourced accessor

    async def test_deliver_turn_persist_idempotent_on_second_turn(self, monkeypatch):
        """A later turn carrying the same session id skips the callback."""
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
            _mk_system_init("sess-abc"), _mk_assistant("hi"),
            _mk_result_msg(session_id="sess-abc")))

        persist = AsyncMock()
        drv = InCasaDriver(persist_session_id=persist)
        rec = _make_record()

        session = await _launch(drv, rec, "hi")
        await _say(drv, rec, session, "another turn")

        # Persist fires exactly once across both turns (sid unchanged).
        assert persist.await_count == 1
        assert rec.sdk_session_id == "sess-abc"

    async def test_failed_persist_retries_on_next_turn(self, monkeypatch):
        """#302: a raised persist_session_id must NOT mark the ID persisted —
        ``engagement.sdk_session_id`` stays unset so the next message carrying
        the same session ID retries the durable write."""
        from drivers.in_casa_driver import InCasaDriver

        # The result carries no session id, so each turn names it once.
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
            _mk_system_init("sess-abc"), _mk_assistant("hi"),
            _mk_result_msg(session_id="")))

        persist = AsyncMock(side_effect=[OSError("registry write failed"), None])
        drv = InCasaDriver(
            persist_session_id=persist)
        rec = _make_record()

        session = await _launch(drv, rec, "hi")
        assert persist.await_count == 1
        assert rec.sdk_session_id is None       # failure ⇒ NOT marked persisted

        await _say(drv, rec, session, "another turn")
        assert persist.await_count == 2         # same-sid retry happened
        assert rec.sdk_session_id == "sess-abc"

    async def test_deliver_turn_persist_callback_optional(self, monkeypatch):
        """Driver constructed with default persist_session_id=None runs cleanly."""
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
            _mk_system_init("sess-abc"), _mk_assistant("hi"),
            _mk_result_msg(session_id="sess-abc")))

        drv = InCasaDriver()
        rec = _make_record()

        await _launch(drv, rec, "hi")
        # rec.sdk_session_id stays None: no callback was wired, and the
        # in-place value is written only after a durable write.
        assert rec.sdk_session_id is None
        assert drv.get_session_id(rec) == "sess-abc"


# ---------------------------------------------------------------------------
# #595 — a turn ended by an API fault carries its kind out
#
# v0.210.0 (#568) stopped the CLI's own error prose being streamed into the
# engagement topic as the agent's words. These pin the carrier: the turn-end
# handler receives the fault's kind with the turn's result. The envelopes come
# from tests/test_agent_api_error_message.py, which holds the shapes MEASURED
# off the wire against the real CLI — a hand-built stub would only prove the
# driver agrees with my guess about the SDK.
# ---------------------------------------------------------------------------

from test_agent_api_error_message import (          # noqa: E402
    _MODEL_NOT_FOUND_ENVELOPE, _parsed, _refusal_envelope,
)


async def _turn_kinds(monkeypatch, *turns, rec=None, logs=None):
    """Run one session through ``turns`` (the first is the brief's) and
    return what the turn-end handler received for each: the API-fault kind
    or None."""
    from drivers.in_casa_driver import InCasaDriver
    client = _session_of(*turns)
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", client)
    kinds: list = []
    drv = InCasaDriver(agent_logs=logs,
                       on_turn_end=lambda r, res, err: kinds.append(err))
    rec = rec or _make_record()
    session = await _launch(drv, rec)
    for n in range(1, len(turns)):
        await _say(drv, rec, session, f"message {n}")
    await _until(lambda: len(kinds) == len(turns))
    return kinds, drv, rec


class TestInCasaApiFaultCarriesItsKind:
    """#595. Red case for every test here: drop the kind the reader records
    for the turn (or read it only from one carrier) and each one fails — the
    turn ends as a clean one and the job's turn-end guard has nothing to
    classify, which is exactly the reported defect. The names keep "raises"
    from when the kind was raised out of the turn; the kind is now the
    turn end's."""

    async def test_refusal_raises_with_the_refusal_kind(self, monkeypatch):
        from error_kinds import ErrorKind
        kinds, _drv, _rec = await _turn_kinds(
            monkeypatch, [_parsed(_refusal_envelope()), _mk_result_msg()])
        assert kinds == [ErrorKind.REFUSAL]

    async def test_unknown_api_error_value_falls_through_to_api_error(
            self, monkeypatch):
        """`model_not_found` is not in the retryable table and the namespace is
        open — an unrecognised value must classify as API_ERROR, not vanish."""
        from error_kinds import ErrorKind
        kinds, _drv, _rec = await _turn_kinds(
            monkeypatch, [_parsed(_MODEL_NOT_FOUND_ENVELOPE), _mk_result_msg()])
        assert kinds == [ErrorKind.API_ERROR]

    async def test_result_stop_reason_is_the_second_carrier(self, monkeypatch):
        """A refusal reported ONLY on the terminal result — no synthesized
        assistant message — must not come back as an empty success."""
        from error_kinds import ErrorKind
        kinds, _drv, _rec = await _turn_kinds(
            monkeypatch, [_mk_result_msg(stop_reason="refusal")])
        assert kinds == [ErrorKind.REFUSAL]

    async def test_fault_after_output_truncation_still_raises(self, monkeypatch):
        """Found in design review. The detection once sat inside the branch
        that streamed text below the output cap, so once a specialist's
        output reached the cap the classifier stopped running and a later
        fault ended the turn as a SUCCESS. However long the output, the fault
        after it is classified."""
        import specialist_limits
        from error_kinds import ErrorKind

        half = "x" * (specialist_limits._MAX_OUTPUT_CHARS // 2 + 10)
        logs = RecordingLogs()
        kinds, _drv, _rec = await _turn_kinds(
            monkeypatch,
            [_mk_assistant(half), _mk_assistant(half),
             _parsed(_refusal_envelope()), _mk_result_msg()],
            rec=_make_record(kind="specialist"), logs=logs)
        assert kinds == [ErrorKind.REFUSAL]
        assert logs.texts() == [half, half]

    async def test_sub_agent_scoped_fault_does_not_end_the_turn(
            self, monkeypatch):
        """A fault scoped to a sub-agent (`parent_tool_use_id` set) is that
        sub-agent's problem — the turn can still answer, and it ends clean.
        Its prose is still never streamed, because it is still not the agent
        speaking."""
        logs = RecordingLogs()
        scoped = _parsed(_refusal_envelope(parent_tool_use_id="toolu_01"))
        kinds, _drv, _rec = await _turn_kinds(
            monkeypatch,
            [scoped, _mk_assistant("here is the real answer"), _mk_result_msg()],
            logs=logs)

        assert kinds == [None]
        assert logs.texts() == ["here is the real answer"], (
            "the CLI's error prose was logged as the agent's words")

    async def test_text_before_the_fault_is_logged_then_raised(
            self, monkeypatch):
        """Text logged before the fault stays logged, and the turn ends with
        the fault's kind; the fault's prose is never logged."""
        from error_kinds import ErrorKind
        logs = RecordingLogs()
        kinds, _drv, _rec = await _turn_kinds(
            monkeypatch,
            [_mk_assistant("partial work so far"), _parsed(_refusal_envelope()),
             _mk_result_msg()],
            logs=logs)
        assert kinds == [ErrorKind.REFUSAL]
        assert logs.texts() == ["partial work so far"]

    async def test_a_clean_turn_still_returns_normally(self, monkeypatch):
        """Mutation check: the kind is carried on the fault, not on every
        turn. Without this, a reader that always reported a fault would pass
        every test above."""
        kinds, drv, rec = await _turn_kinds(
            monkeypatch,
            [_mk_assistant("all good"), _mk_result_msg(stop_reason="end_turn")])
        assert kinds == [None]
        assert drv.is_alive(rec) is True

    async def test_a_later_turn_carries_its_kind_too(self, monkeypatch):
        """A mid-job refusal is one turn's fault, not the job's: the later
        turn ends with its kind and the session stays alive."""
        from error_kinds import ErrorKind
        kinds, drv, rec = await _turn_kinds(
            monkeypatch,
            [_mk_assistant("first turn fine"), _mk_result_msg()],
            [_parsed(_refusal_envelope()), _mk_result_msg()])
        assert kinds == [None, ErrorKind.REFUSAL]
        assert drv.is_alive(rec) is True, (
            "a refused turn must not tear the job's session down")


# ---------------------------------------------------------------------------
# A specialist's log carries every text block, across its tool calls
# ---------------------------------------------------------------------------

async def test_a_specialists_conversation_logs_every_text_block(monkeypatch):
    """The text written before a tool call, the step, and the text written
    after it are all logged, in that order."""
    from claude_agent_sdk import (
        ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage,
    )
    from drivers.in_casa_driver import InCasaDriver

    before = "Now let me look that up."
    after = "Found it. Shall I go ahead?"

    def _said(*blocks):
        return AssistantMessage(content=list(blocks), model="m",
                                parent_tool_use_id=None)

    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _client_of(
        _said(TextBlock(before)),
        _said(ToolUseBlock(id="t1", name="n", input={})),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")]),
        _said(TextBlock(after)),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s1",
            total_cost_usd=0.0, usage={}, result="",
            stop_reason="end_turn")))

    rec = _make_record()
    logs = RecordingLogs()
    drv = InCasaDriver(agent_logs=logs)
    await _launch(drv, rec, "go")
    assert logs.texts() == [before, "🔧 Working", after]
