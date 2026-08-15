"""Tests for InCasaDriver — start, send_user_turn, cancel, is_alive."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

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


def _mk_factory_with_fake_handle():
    """Return (factory, handle) where factory(topic_id) → handle.

    handle.emit and handle.finalize are AsyncMocks for assertion."""
    from unittest.mock import MagicMock as _MagicMock
    handle = _MagicMock()
    handle.emit = AsyncMock()
    handle.finalize = AsyncMock()

    def _factory(topic_id):
        handle._topic_id_seen = topic_id
        return handle

    return _factory, handle


def _mk_noop_factory():
    """Factory that returns a fresh no-op handle each call. Used by
    tests that don't care about output (cancel, resume, is_alive)."""
    from unittest.mock import MagicMock as _MagicMock

    def _factory(topic_id):
        h = _MagicMock()
        h.emit = AsyncMock()
        h.finalize = AsyncMock()
        return h

    return _factory


def _make_record(**overrides):
    from engagement_registry import EngagementRecord

    base = dict(
        id="e1", kind="specialist", role_or_type="finance", driver="in_casa",
        status="active", topic_id=42,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    base.update(overrides)
    return EngagementRecord(**base)


class TestInCasaStart:
    async def test_start_opens_client_and_marks_alive(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        ctor_calls = []

        class _FakeClient:
            def __init__(self, options):
                ctor_calls.append(options)
                self._connected = False

            async def __aenter__(self):
                self._connected = True
                return self

            async def __aexit__(self, *args):
                self._connected = False

            async def query(self, prompt):
                pass

            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover

            async def close(self):
                self._connected = False

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record()

        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"))
        assert drv.is_alive(rec) is True
        assert len(ctor_calls) == 1

    async def test_start_posts_initial_response_to_topic(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_assistant("Hello from Alex")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        factory, handle = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)
        rec = _make_record()

        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"))
        # Phase 3b: streaming → emit per-AssistantMessage + finalize once.
        handle.emit.assert_awaited_once_with("Hello from Alex")
        handle.finalize.assert_awaited_once_with("Hello from Alex")
        assert handle._topic_id_seen == 42

    async def test_start_separates_multiple_assistant_messages_with_double_newline(
        self, monkeypatch,
    ):
        """E-8: when the SDK emits multiple AssistantMessages in one turn
        (typical executor with tool calls), the final text posted to topic
        must have \\n\\n between AssistantMessage boundaries — not a glued
        paragraph (bug-review-2026-04-29-exploration.md § E-8)."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_assistant("Reading the doctrine files first.")
                yield _mk_assistant("Now let me look at Ellen's config files.")
                yield _mk_assistant("The trait belongs in character.yaml.")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        factory, handle = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)
        rec = _make_record()

        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"))

        # Phase 3b: 3 emits (cumulative) + 1 finalize with full text.
        assert handle.emit.await_count == 3
        emits = [c.args[0] for c in handle.emit.await_args_list]
        assert emits[0] == "Reading the doctrine files first."
        assert emits[1] == (
            "Reading the doctrine files first."
            "\n\nNow let me look at Ellen's config files."
        )
        assert emits[2] == (
            "Reading the doctrine files first."
            "\n\nNow let me look at Ellen's config files."
            "\n\nThe trait belongs in character.yaml."
        )
        handle.finalize.assert_awaited_once()
        text = handle.finalize.await_args.args[0]
        # All three pieces present
        assert "Reading the doctrine files first." in text
        assert "Now let me look at Ellen's config files." in text
        assert "The trait belongs in character.yaml." in text
        # E-8 separator preserved
        assert "first.Now let me" not in text
        assert "first.\n\nNow let me look" in text
        assert "config files.\n\nThe trait belongs" in text

    async def test_start_streams_skips_assistant_message_with_no_text(
        self, monkeypatch,
    ):
        """Phase 3b: AssistantMessage with no TextBlocks (e.g. only
        ToolUseBlocks) produces no emit and no inserted blank line."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_assistant("Before tool call.")
                empty = _mk_assistant("ignored")
                empty.content = []  # type: ignore[attr-defined]
                yield empty
                yield _mk_assistant("After tool call.")
            async def close(self): pass

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", _FakeClient,
        )

        factory, handle = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)
        rec = _make_record()

        await drv.start(
            rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"),
        )

        assert handle.emit.await_count == 2
        emits = [c.args[0] for c in handle.emit.await_args_list]
        assert emits[1] == "Before tool call.\n\nAfter tool call."
        handle.finalize.assert_awaited_once_with(
            "Before tool call.\n\nAfter tool call."
        )

    async def test_start_empty_turn_skips_finalize(self, monkeypatch):
        """SDK yields zero AssistantMessages with text → no finalize call."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover
            async def close(self): pass

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", _FakeClient,
        )

        factory, handle = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)
        rec = _make_record()

        await drv.start(
            rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"),
        )

        handle.emit.assert_not_awaited()
        handle.finalize.assert_not_awaited()

    async def test_start_empty_turn_logs_subprocess_terminated(
        self, monkeypatch, caplog,
    ):
        """G-4 (v0.33.0): SDK yields zero AssistantMessages → driver
        logs a structured `subprocess_terminated reason=...` warning so
        operators have a starting signal for empty-turn / silent-failure
        engagements (the exploration2 G-4 repro shape)."""
        import logging
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover
            async def close(self): pass

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", _FakeClient,
        )

        factory, _handle = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)
        rec = _make_record()

        with caplog.at_level(logging.WARNING, logger="drivers.in_casa_driver"):
            await drv.start(
                rec, prompt="hi",
                options=ClaudeAgentOptions(model="sonnet"),
            )

        warn_lines = [
            r for r in caplog.records
            if "subprocess_terminated" in r.getMessage()
        ]
        assert warn_lines, (
            "expected subprocess_terminated WARNING when SDK yields "
            "zero AssistantMessages; got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        assert "reason=no_assistant_message" in warn_lines[0].getMessage()
        assert warn_lines[0].levelno == logging.WARNING

    async def test_start_logs_per_message(self, monkeypatch, caplog):
        """Phase 4b Bug 3: each SDK message kind triggers an
        sdk_logging dispatch. SystemMessage(subtype=init) → DEBUG;
        AssistantMessage → INFO + per-tool DEBUG; UserMessage with
        ToolResultBlock → DEBUG; ResultMessage → INFO turn_done."""
        import logging
        from drivers.in_casa_driver import InCasaDriver
        from claude_agent_sdk import (
            AssistantMessage, SystemMessage, UserMessage, ResultMessage,
            TextBlock, ToolUseBlock, ToolResultBlock, ClaudeAgentOptions,
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
        assistant.__class__ = AssistantMessage

        result_block = MagicMock()
        result_block.is_error = False
        result_block.content = "ok"
        result_block.__class__ = ToolResultBlock

        user_with_tool_result = MagicMock()
        user_with_tool_result.content = [result_block]
        user_with_tool_result.__class__ = UserMessage

        result = MagicMock()
        result.num_turns = 1
        result.total_cost_usd = 0.001
        result.usage = {"input_tokens": 100, "output_tokens": 20}
        result.__class__ = ResultMessage

        class _FakeClient:
            session_id = "abc12345-xyz-789"
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield sys_init
                yield assistant
                yield user_with_tool_result
                yield result
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        factory, _ = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)
        rec = _make_record()

        with caplog.at_level(logging.DEBUG, logger="sdk"):
            await drv.start(
                rec, prompt="hi",
                options=ClaudeAgentOptions(model="sonnet"),
            )

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

    async def test_start_streaming_unchanged_after_phase4b(
        self, monkeypatch,
    ):
        """Phase 4b Bug 3: the new dispatch must NOT regress Phase 3b's
        per-AssistantMessage stream.emit + finalize semantics."""
        from drivers.in_casa_driver import InCasaDriver
        from claude_agent_sdk import ClaudeAgentOptions

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_assistant("First.")
                yield _mk_assistant("Second.")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        factory, handle = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)
        rec = _make_record()

        await drv.start(
            rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"),
        )

        assert handle.emit.await_count == 2
        emits = [c.args[0] for c in handle.emit.await_args_list]
        assert emits[0] == "First."
        assert emits[1] == "First.\n\nSecond."
        handle.finalize.assert_awaited_once_with("First.\n\nSecond.")

    async def test_start_injects_stderr_callback(self, monkeypatch):
        """Bug 4: in_casa_driver.start() must inject the stderr callback
        on the options before ClaudeSDKClient is built. engagement_id
        flows in via the engagement param."""
        from drivers.in_casa_driver import InCasaDriver
        from claude_agent_sdk import ClaudeAgentOptions

        captured: list = []

        class _FakeClient:
            def __init__(self, options):
                captured.append(options)
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record()

        await drv.start(
            rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"),
        )

        assert captured, "_FakeClient never constructed"
        assert callable(captured[0].stderr), (
            "Phase 4b Bug 4: ClaudeAgentOptions.stderr must be set "
            "by in_casa_driver.start() before ClaudeSDKClient is built"
        )


class TestInCasaSendUserTurn:
    async def test_send_user_turn_streams_reply_to_topic(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        turns = []

        class _FakeClient:
            def __init__(self, options): self._q = []
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt):
                turns.append(prompt)
            async def receive_response(self):
                yield _mk_assistant(f"re:{turns[-1]}")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        factory, handle = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)
        rec = _make_record()
        await drv.start(rec, "system prompt", ClaudeAgentOptions(model="sonnet"))
        turns.clear()  # reset after start's initial delivery
        # Reset emit/finalize tracking after the start-turn delivery so the
        # send_user_turn assertions below see only the user-turn calls.
        handle.emit.reset_mock()
        handle.finalize.reset_mock()

        await drv.send_user_turn(rec, "user said X")
        assert turns == ["user said X"]
        # Phase 3b: emit + finalize for the user turn.
        handle.finalize.assert_awaited_once()
        assert handle.finalize.await_args.args[0] == "re:user said X"

    async def test_send_user_turn_raises_when_not_alive(self):
        from drivers.in_casa_driver import InCasaDriver, DriverNotAliveError

        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record()
        with pytest.raises(DriverNotAliveError):
            await drv.send_user_turn(rec, "x")


class TestInCasaCancel:
    async def test_cancel_closes_client_and_flips_alive(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        close_calls = []

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover
            async def close(self):
                close_calls.append(1)

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record()
        await drv.start(rec, "p", ClaudeAgentOptions(model="sonnet"))
        assert drv.is_alive(rec) is True
        await drv.cancel(rec)
        assert drv.is_alive(rec) is False
        assert close_calls == [1]

    async def test_cancel_is_idempotent(self):
        from drivers.in_casa_driver import InCasaDriver

        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record()
        # Not alive yet: must not raise.
        await drv.cancel(rec)
        await drv.cancel(rec)
        assert drv.is_alive(rec) is False


class TestInCasaResume:
    async def test_resume_reopens_client_with_session_id(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        seen_options = []

        class _FakeClient:
            def __init__(self, options):
                seen_options.append(options)
                self.session_id = "sess-new"
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)
        # Finding 2 (v0.69.10): resume rebuilds the FULL option set (Agent/Task
        # denies + fail-closed callback), not a bare ClaudeAgentOptions(resume=).
        import tools as tools_mod

        def _fake_rebuild(engagement, session_id):
            return ClaudeAgentOptions(
                resume=session_id, disallowed_tools=["Bash", "Agent", "Task"],
            )
        monkeypatch.setattr(tools_mod, "build_engagement_resume_options", _fake_rebuild)

        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record(sdk_session_id="sess-old")
        await drv.resume(rec, session_id="sess-old")
        assert drv.is_alive(rec) is True
        assert seen_options[0].resume == "sess-old"
        # the restrictions must ride along on resume, not be dropped
        assert "Agent" in seen_options[0].disallowed_tools
        assert "Task" in seen_options[0].disallowed_tools

    async def test_get_session_id_returns_stream_sourced_session_id(self, monkeypatch):
        """v0.69.11: get_session_id returns the id captured from the message
        stream (SystemMessage init), NOT a nonexistent client.session_id attr."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_system_init("sess-xyz")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record()
        await drv.start(rec, "p", ClaudeAgentOptions(model="sonnet"))
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

        Pre-fix: engagement_var was bound inside _deliver_turn, AFTER
        __aenter__. The SDK inner task captured engagement_var=None;
        every privileged-tool dispatch saw _effective_caller_role()
        return the engager's origin.role ("assistant") and refused.
        Every in_casa configurator engagement orphaned silently from
        v0.20.0 to v0.28.1.

        Post-fix: start() binds engagement_var BEFORE __aenter__, so the
        SDK inner task's captured context has the engagement record.

        The fake client below models the real SDK pattern: __aenter__
        spawns a task; inside that task we snapshot engagement_var.
        Asserting on the snapshot proves the fix works."""
        import asyncio as _asyncio
        from drivers.in_casa_driver import InCasaDriver
        from tools import engagement_var

        inner_captured: list = []

        class _SDKWithInnerTask:
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

            async def __aexit__(self, *a):
                await self._inbox.put(None)
                if self._task is not None:
                    try:
                        await self._task
                    except _asyncio.CancelledError:
                        pass

            async def _read_loop(self):
                """Inside the SDK's inner task — this is where every tool
                callback would be dispatched in the real SDK. Snapshot
                engagement_var ONCE per prompt to model what the tool
                callback would see."""
                while True:
                    item = await self._inbox.get()
                    if item is None:
                        return
                    inner_captured.append(engagement_var.get(None))
                    await self._outbox.put(item)

            async def query(self, prompt):
                await self._inbox.put(prompt)

            async def receive_response(self):
                text = await self._outbox.get()
                yield _mk_assistant(f"echoed:{text}")

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

        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record(role_or_type="configurator")

        # Pre-state: engagement_var unbound in the parent task.
        assert engagement_var.get(None) is None

        await drv.start(rec, "p", ClaudeAgentOptions(model="sonnet"))

        # The SDK inner task — created during __aenter__ — must have
        # captured engagement_var bound to the engagement record. Pre-fix
        # this was [None]; post-fix it is [rec].
        assert inner_captured == [rec], (
            "E-E regression: SDK inner task did not capture engagement_var. "
            "Bind engagement_var BEFORE ClaudeSDKClient.__aenter__() in "
            "InCasaDriver.start()."
        )
        # Parent task's binding must be cleared after start() returns —
        # we don't leak it into surrounding turns.
        assert engagement_var.get(None) is None

    async def test_deliver_turn_sets_engagement_var_during_sdk_loop(self, monkeypatch):
        """engagement_var is bound for the duration of receive_response()."""
        from drivers.in_casa_driver import InCasaDriver
        from tools import engagement_var

        captured: list = []

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass

            async def receive_response(self):
                # Snapshot engagement_var while inside the loop
                captured.append(engagement_var.get(None))
                yield _mk_assistant("hi")

            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record(role_or_type="configurator")

        # Pre-state: unbound
        assert engagement_var.get(None) is None
        await drv.start(rec, "hi", ClaudeAgentOptions(model="sonnet"))
        # Post-state: reset
        assert engagement_var.get(None) is None
        # During-state: was bound to rec
        assert captured == [rec]

    async def test_deliver_turn_persists_session_id_on_first_message(self, monkeypatch):
        """session_id from the SystemMessage init stream triggers persist once
        (v0.69.11 — was read from a nonexistent client.session_id attr)."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_system_init("sess-abc")
                yield _mk_assistant("hi")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        persist = AsyncMock()
        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory(), persist_session_id=persist)
        rec = _make_record()

        await drv.start(rec, "hi", ClaudeAgentOptions(model="sonnet"))

        persist.assert_awaited_once_with(rec.id, "sess-abc")
        assert rec.sdk_session_id == "sess-abc"
        assert drv.get_session_id(rec) == "sess-abc"   # stream-sourced accessor

    async def test_deliver_turn_persist_idempotent_on_second_turn(self, monkeypatch):
        """Subsequent _deliver_turn calls skip the callback when sid unchanged."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_system_init("sess-abc")
                yield _mk_assistant("hi")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        persist = AsyncMock()
        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory(), persist_session_id=persist)
        rec = _make_record()

        await drv.start(rec, "hi", ClaudeAgentOptions(model="sonnet"))
        await drv.send_user_turn(rec, "another turn")

        # Persist must fire exactly once across both turns (sid unchanged).
        assert persist.await_count == 1
        assert rec.sdk_session_id == "sess-abc"

    async def test_failed_persist_retries_on_next_turn(self, monkeypatch):
        """#302: a raised persist_session_id must NOT mark the ID persisted —
        ``engagement.sdk_session_id`` stays unset so the next message carrying
        the same session ID retries the durable write (pre-fix the in-memory
        assignment ran regardless, and restart lost the resumable session)."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_system_init("sess-abc")
                yield _mk_assistant("hi")
            async def close(self): pass

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        persist = AsyncMock(side_effect=[OSError("registry write failed"), None])
        drv = InCasaDriver(
            topic_stream_factory=_mk_noop_factory(), persist_session_id=persist)
        rec = _make_record()

        await drv.start(rec, "hi", ClaudeAgentOptions(model="sonnet"))
        assert rec.sdk_session_id is None       # failure ⇒ NOT marked persisted

        await drv.send_user_turn(rec, "another turn")
        assert persist.await_count == 2         # same-sid retry happened
        assert rec.sdk_session_id == "sess-abc"

    async def test_deliver_turn_persist_callback_optional(self, monkeypatch):
        """Driver constructed with default persist_session_id=None runs cleanly."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options):
                self.session_id = "sess-abc"
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_assistant("hi")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        # No persist_session_id supplied — uses default None.
        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record()

        await drv.start(rec, "hi", ClaudeAgentOptions(model="sonnet"))
        # Sanity: rec.sdk_session_id stays None because no callback was wired
        # AND we don't write the in-place value when callback is None.
        assert rec.sdk_session_id is None


@pytest.mark.unit
async def test_start_rolls_back_client_when_first_turn_fails(monkeypatch):
    """M14: Bug-13 analogue for in_casa. A first-turn SDK failure must not leak
    the opened client — start() re-raises, but the client is closed and
    deregistered so is_alive() flips False (error records are otherwise
    invisible to every sweeper)."""
    from drivers.in_casa_driver import InCasaDriver

    closed = []

    class _FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            closed.append("aexit")

        async def query(self, prompt):
            raise RuntimeError("CLI subprocess died")

        async def receive_response(self):
            if False:
                yield None  # pragma: no cover

        async def close(self):
            closed.append("close")

    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)
    drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
    rec = _make_record()

    with pytest.raises(RuntimeError):
        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"))

    assert drv.is_alive(rec) is False, "client leaked in _clients after failed start"
    assert rec.id not in drv._ctx_stack and rec.id not in drv._locks
    assert closed, "client was never closed on first-turn failure"


@pytest.mark.unit
async def test_start_rolls_back_client_when_first_turn_cancelled(monkeypatch):
    """#344: the M14 rollback caught only Exception — a CANCELLED initial
    delivery left _clients populated with an unclosed client while the
    record stayed active (later turns believed it alive). Cancellation
    must roll back the same way, then re-raise CancelledError."""
    import asyncio

    from drivers.in_casa_driver import InCasaDriver

    closed = []
    entered = asyncio.Event()

    class _FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            closed.append("aexit")

        async def query(self, prompt):
            entered.set()
            await asyncio.sleep(60)

        async def receive_response(self):
            if False:
                yield None  # pragma: no cover

        async def close(self):
            closed.append("close")

    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)
    drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
    rec = _make_record()

    task = asyncio.create_task(drv.start(
        rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert drv.is_alive(rec) is False, (
        "client leaked in _clients after cancelled start")
    assert rec.id not in drv._ctx_stack and rec.id not in drv._locks
    assert closed, "client was never closed on cancelled first turn"


# ---------------------------------------------------------------------------
# #595 — an engagement turn ended by an API fault carries its kind out
#
# v0.210.0 (#568) stopped the CLI's own error prose being streamed into the
# engagement topic as the agent's words, but the kind died there: the turn
# returned normally having produced nothing, so a safety refusal and a crash
# both landed as `driver_start_failed` (or as nothing at all). These pin the
# carrier. The envelopes come from tests/test_agent_api_error_message.py, which
# holds the shapes MEASURED off the wire against the real CLI — a hand-built
# stub would only prove the driver agrees with my guess about the SDK.
# ---------------------------------------------------------------------------

from test_agent_api_error_message import (          # noqa: E402
    _MODEL_NOT_FOUND_ENVELOPE, _parsed, _refusal_envelope,
)


def _mk_result_msg(*, stop_reason=None):
    """A terminal ResultMessage — the SECOND carrier of a refusal."""
    from claude_agent_sdk import ResultMessage
    m = ResultMessage.__new__(ResultMessage)
    m.session_id = "sid-in-casa"        # type: ignore[attr-defined]
    m.is_error = False                  # type: ignore[attr-defined]
    m.result = ""                       # type: ignore[attr-defined]
    m.stop_reason = stop_reason         # type: ignore[attr-defined]
    return m


def _client_of(*messages):
    """A ClaudeSDKClient double that replays exactly ``messages``."""
    class _FakeClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def query(self, prompt): pass

        async def receive_response(self):
            for m in messages:
                yield m

        async def close(self): pass
    return _FakeClient


class TestInCasaApiFaultCarriesItsKind:
    """#595. Red case for every test here: delete the `raise ApiErrorTurn`
    in `_deliver_turn` and each one fails — `start()` returns cleanly and the
    caller has nothing to classify, which is exactly the reported defect."""

    async def test_refusal_raises_with_the_refusal_kind(self, monkeypatch):
        from error_kinds import ApiErrorTurn, ErrorKind
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient",
            _client_of(_parsed(_refusal_envelope())))
        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())

        with pytest.raises(ApiErrorTurn) as excinfo:
            await drv.start(_make_record(), prompt="hi",
                            options=ClaudeAgentOptions(model="sonnet"))
        assert excinfo.value.kind is ErrorKind.REFUSAL

    async def test_unknown_api_error_value_falls_through_to_api_error(
            self, monkeypatch):
        """`model_not_found` is not in the retryable table and the namespace is
        open — an unrecognised value must classify as API_ERROR, not vanish."""
        from error_kinds import ApiErrorTurn, ErrorKind
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient",
            _client_of(_parsed(_MODEL_NOT_FOUND_ENVELOPE)))
        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())

        with pytest.raises(ApiErrorTurn) as excinfo:
            await drv.start(_make_record(), prompt="hi",
                            options=ClaudeAgentOptions(model="sonnet"))
        assert excinfo.value.kind is ErrorKind.API_ERROR

    async def test_result_stop_reason_is_the_second_carrier(self, monkeypatch):
        """A refusal reported ONLY on the terminal result — no synthesized
        assistant message — must not come back as an empty success."""
        from error_kinds import ApiErrorTurn, ErrorKind
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient",
            _client_of(_mk_result_msg(stop_reason="refusal")))
        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())

        with pytest.raises(ApiErrorTurn) as excinfo:
            await drv.start(_make_record(), prompt="hi",
                            options=ClaudeAgentOptions(model="sonnet"))
        assert excinfo.value.kind is ErrorKind.REFUSAL

    async def test_fault_after_output_truncation_still_raises(self, monkeypatch):
        """Found in design review (Sol r2). The detection used to sit inside
        `if isinstance(...) and not stream_truncated:`, so once a specialist's
        output was clipped the classifier stopped running and a later fault
        ended the turn as a truncated SUCCESS.

        Red case: move the `api_error_kind` check back inside the
        `not stream_truncated` branch and this test fails with "DID NOT RAISE".
        """
        import specialist_limits
        from error_kinds import ApiErrorTurn, ErrorKind
        from drivers.in_casa_driver import InCasaDriver

        over_cap = _mk_assistant(
            "x" * (specialist_limits._MAX_OUTPUT_CHARS + 10))
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient",
            _client_of(over_cap, _parsed(_refusal_envelope())))
        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        # kind="specialist" is what arms the output cap.
        rec = _make_record(kind="specialist")

        with pytest.raises(ApiErrorTurn) as excinfo:
            await drv.start(rec, prompt="hi",
                            options=ClaudeAgentOptions(model="sonnet"))
        assert excinfo.value.kind is ErrorKind.REFUSAL
        assert rec.origin.get("stream_output_truncated") is True, (
            "test no longer exercises the truncated-stream path")

    async def test_sub_agent_scoped_fault_does_not_end_the_turn(
            self, monkeypatch):
        """A fault scoped to a sub-agent (`parent_tool_use_id` set) is that
        sub-agent's problem — the turn can still answer. Its prose is still
        never streamed, because it is still not the agent speaking."""
        from drivers.in_casa_driver import InCasaDriver

        scoped = _parsed(_refusal_envelope(parent_tool_use_id="toolu_01"))
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient",
            _client_of(scoped, _mk_assistant("here is the real answer")))
        factory, handle = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)

        await drv.start(_make_record(), prompt="hi",
                        options=ClaudeAgentOptions(model="sonnet"))

        handle.finalize.assert_awaited_once_with("here is the real answer")
        posted = "".join(str(c) for c in handle.emit.await_args_list)
        assert "Request ID" not in posted, (
            "the CLI's error prose was streamed as the agent's words")

    async def test_text_before_the_fault_is_finalized_then_raised(
            self, monkeypatch):
        """Text that reached Telegram before the fault cannot be retracted, so
        the stream is finalized (coherent last edit) BEFORE the raise."""
        from error_kinds import ApiErrorTurn
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient",
            _client_of(_mk_assistant("partial work so far"),
                       _parsed(_refusal_envelope())))
        factory, handle = _mk_factory_with_fake_handle()
        drv = InCasaDriver(topic_stream_factory=factory)

        with pytest.raises(ApiErrorTurn):
            await drv.start(_make_record(), prompt="hi",
                            options=ClaudeAgentOptions(model="sonnet"))
        handle.finalize.assert_awaited_once_with("partial work so far")

    async def test_a_clean_turn_still_returns_normally(self, monkeypatch):
        """Mutation check: the raise must fire on the fault, not on every
        turn. Without this, a guard that always raised would pass every test
        above."""
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient",
            _client_of(_mk_assistant("all good"),
                       _mk_result_msg(stop_reason="end_turn")))
        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record()

        await drv.start(rec, prompt="hi",
                        options=ClaudeAgentOptions(model="sonnet"))
        assert drv.is_alive(rec) is True

    async def test_send_user_turn_raises_too(self, monkeypatch):
        """The per-turn caller (`_deliver_turn_bg`) posts a topic notice and
        leaves the engagement LIVE — a mid-engagement refusal is one turn's
        failure, not the engagement's. Pin that it raises at all."""
        from error_kinds import ApiErrorTurn
        from drivers.in_casa_driver import InCasaDriver

        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient",
            _client_of(_mk_assistant("first turn fine")))
        drv = InCasaDriver(topic_stream_factory=_mk_noop_factory())
        rec = _make_record()
        await drv.start(rec, prompt="hi",
                        options=ClaudeAgentOptions(model="sonnet"))

        # Second turn refuses: swap the scripted stream on the live client.
        async def _refusing(*a, **k):
            yield _parsed(_refusal_envelope())
        drv._clients[rec.id].receive_response = _refusing

        with pytest.raises(ApiErrorTurn):
            await drv.send_user_turn(rec, "please do the thing")
        assert drv.is_alive(rec) is True, (
            "a refused turn must not tear the engagement's client down")
