"""The job launch through the real driver, and the shared launch rig.

A job launch opens the job's session and queues its brief; the session's
reader owns the job from there. A launch turn that runs to its end — frames,
streamed text, its ``ResultMessage`` — leaves a live job with nothing
reported. (A launch turn cut off before its ``ResultMessage`` is a session
loss, owned by session recovery and pinned with the job session.)

``_build``/``_launch`` and the scripted clients are shared with the other
launch suites.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from turn_scripted_session import TurnScriptedSession

pytestmark = [pytest.mark.asyncio]


# --- the scripted stream ----------------------------------------------------

class ScriptedCutoffClient(TurnScriptedSession):
    """An SDK client whose launch turn is cut off mid-tool-loop.

    Yields one AssistantMessage carrying a ToolUseBlock and one UserMessage
    carrying its ToolResultBlock, then ends the iteration — exactly what the
    pinned SDK produces when the read task's ``finally`` sends ``end`` before
    any ``ResultMessage`` arrives. No text blocks, so no words are logged
    either.
    """

    frames_yielded = 0
    result_messages_yielded = 0
    closed = 0

    def __init__(self, options):
        self.options = options
        type(self).frames_yielded = 0
        type(self).result_messages_yielded = 0
        type(self).closed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def record_query(self, prompt):
        return None

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage,
        )
        use = ToolUseBlock(id="tool-1", name="Read", input={"file_path": "/x"})
        am = AssistantMessage(content=[use], model="claude-sonnet-4-6")
        type(self).frames_yielded += 1
        yield am
        res = ToolResultBlock(tool_use_id="tool-1", content="ok", is_error=False)
        um = UserMessage(content=[res])
        type(self).frames_yielded += 1
        yield um
        # EOF. No ResultMessage — the turn was cut off in flight.

    async def close(self):
        type(self).closed += 1


class ScriptedCompleteClient(ScriptedCutoffClient):
    """The control: the same frames PLUS the turn's terminal ResultMessage and
    a text block, i.e. a turn that ran to its end and posted something. Must
    stay a ``pending`` launch with zero reporting side effects."""

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, ResultMessage, TextBlock, ToolResultBlock,
            ToolUseBlock, UserMessage,
        )
        use = ToolUseBlock(id="tool-1", name="Read", input={"file_path": "/x"})
        type(self).frames_yielded += 1
        yield AssistantMessage(content=[use], model="claude-sonnet-4-6")
        res = ToolResultBlock(tool_use_id="tool-1", content="ok", is_error=False)
        type(self).frames_yielded += 1
        yield UserMessage(content=[res])
        type(self).frames_yielded += 1
        yield AssistantMessage(
            content=[TextBlock(text="I read the file; what next?")],
            model="claude-sonnet-4-6",
        )
        rm = ResultMessage.__new__(ResultMessage)
        rm.subtype = "success"
        rm.duration_ms = 5
        rm.duration_api_ms = 4
        rm.is_error = False
        rm.num_turns = 2
        rm.session_id = "sess-1"
        rm.total_cost_usd = 0.0
        rm.usage = {}
        rm.result = "done"
        rm.stop_reason = None
        rm.parent_tool_use_id = None
        type(self).result_messages_yielded += 1
        type(self).frames_yielded += 1
        yield rm


# --- the harness ------------------------------------------------------------

class _Probe:
    """Ordered, counted observation of every operator-visible effect. The
    job's log is one of them: a ``⚠️`` line is a notice, and each logged word
    entry counts as one ``finalize`` (the turn's words were put out)."""

    def __init__(self):
        self.events: list[str] = []
        self.notice_texts: list[str] = []
        self.log_lines: list[str] = []
        self.transition_calls: list[tuple] = []
        self.mark_error_calls: list[tuple] = []
        self.driver_cancel_calls = 0
        self.finalize_count = 0


class _ProbeLogs:
    """The agent log sink, observed by a probe."""

    enabled = True

    def __init__(self, probe: _Probe) -> None:
        self.probe = probe

    def log(self, key, run, text):
        self.probe.log_lines.append(text)
        if text.startswith("⚠️"):
            self.probe.events.append("notice")
            self.probe.notice_texts.append(text)
        elif not text.startswith(("🔧", "▶", "✅", "🛑", "❌", "📊", "↻")):
            self.probe.finalize_count += 1


def _build(tmp_path, monkeypatch, probe, client_cls):
    """Real registry + real InCasaDriver + the real job launch owner
    (``tools._launch_interactive_engagement``, a specialist-hosted job).

    Returns ``(launcher, registry, channel, driver)``; ``_launch(launcher)``
    runs one launch with the engaging resident's origin bound."""
    import agent as agent_mod
    from engagement_registry import EngagementRegistry
    from drivers.in_casa_driver import InCasaDriver
    from tools import init_tools
    try:
        from tests.test_delegate_to_agent_interactive import (
            _make_alex_cfg, _make_assistant_cfg, launch_job)
    except ImportError:
        from test_delegate_to_agent_interactive import (
            _make_alex_cfg, _make_assistant_cfg, launch_job)

    monkeypatch.setattr(
        "drivers.in_casa_driver.ClaudeSDKClient", client_cls,
    )

    registry = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"),
    )

    real_transition = registry.try_transition_terminal

    async def spy_transition(engagement_id, outcome, **kw):
        probe.transition_calls.append((engagement_id, outcome, kw))
        return await real_transition(engagement_id, outcome, **kw)

    real_mark_error = registry.mark_error

    async def spy_mark_error(engagement_id, kind, message):
        probe.mark_error_calls.append((engagement_id, kind, message))
        return await real_mark_error(engagement_id, kind, message)

    monkeypatch.setattr(registry, "try_transition_terminal", spy_transition)
    monkeypatch.setattr(registry, "mark_error", spy_mark_error)

    logs = _ProbeLogs(probe)
    driver = InCasaDriver(
        agent_logs=logs,
        persist_session_id=registry.persist_session_id,
        record_lookup=registry.get,
    )
    real_cancel = driver.cancel

    async def spy_cancel(engagement):
        probe.driver_cancel_calls += 1
        return await real_cancel(engagement)

    monkeypatch.setattr(driver, "cancel", spy_cancel)
    monkeypatch.setattr(
        agent_mod, "active_engagement_driver", driver, raising=False,
    )

    # No forum group is configured: a job never needs one.
    channel = MagicMock()
    channel.engagement_supergroup_id = 0
    channel.bot = MagicMock()

    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)

    specialist_reg = MagicMock()
    specialist_reg.get.return_value = _make_alex_cfg()
    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=specialist_reg, mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=registry,
        agent_role_map={"assistant": _make_assistant_cfg()},
        agent_logs=logs,
    )
    return launch_job, registry, channel, driver


async def _launch(launcher, task="Plan Q2"):
    """One job launch, as ``_start_job_on_host`` hands it to the launch owner,
    with the engaging resident's origin (``role: assistant``) bound."""
    return await launcher(task)


# --- the red case -----------------------------------------------------------

class TestInCasaLaunchTerminalArtifact:
    async def test_a_complete_launch_turn_stays_pending_and_reports_nothing(
        self, tmp_path, monkeypatch,
    ):
        """The control that kills an unconditional-failure mutation: a launch
        turn with its frames, a text block and its ResultMessage is a
        legitimate launch. Nothing is reported, nothing is closed, the session
        stays alive."""
        import asyncio
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCompleteClient,
        )

        envelope = await _launch(launcher)
        payload = json.loads(envelope["content"][0]["text"])
        assert payload.get("status") == "pending", payload

        async def _turn_ended():
            while probe.finalize_count < 1:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(_turn_ended(), 5)

        assert ScriptedCompleteClient.result_messages_yielded == 1
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "active"
        assert probe.transition_calls == []
        assert probe.mark_error_calls == []
        assert probe.driver_cancel_calls == 0
        assert probe.finalize_count == 1
        # The job's log: its start, the step and the words; no notice.
        assert probe.log_lines == ["▶ started", "🔧 Reading files",
                                   "I read the file; what next?"]
        assert probe.notice_texts == []
        assert driver.is_alive(registry.get(created_id)) is True
        await driver.cancel(registry.get(created_id))
