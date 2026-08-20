"""#678 red case — an in_casa LAUNCH turn that dies mid-tool-loop.

Specified by Sol (drive redcase round, MODE: SPECIFY) against
03abe71036990af61a914e6257eced8e77df413a. The shape is the MID-TOOL-LOOP
CUTOFF, not the zero-frame case: assistant and tool-result frames are seen —
so ``evidence_seen`` has already latched (drivers/in_casa_driver.py:643-652) —
and the stream then reaches EOF with no ``ResultMessage``, no
``emit_completion`` and no assistant text.

The pinned SDK makes that EOF unambiguous: ``receive_response()`` returns AT
the ``ResultMessage`` (client.py:571-610, "If no ResultMessage is received,
the iterator continues indefinitely"), and ``receive_messages()`` ends only on
the ``{"type": "end"}`` sentinel that the read task's ``finally`` sends
(_internal/query.py:406-418). A turn that ran to its end always yields one.

Pre-run terminus for the invariant, read rather than cited:
  - tests/test_delegate_to_agent.py:124-126@03abe710 — "A stream that ends
    without a ResultMessage certifies nothing — the runner does not raise on
    that path, so it must still be an abort."
  - docs/architecture/engagements.md:104@03abe710 — "A driver fails to start
    after the record exists. The engagement is marked errored, topic cleanup is
    attempted, and the caller is told the start failed."
  - casa/rootfs/opt/casa/tools.py:7434-7437@03abe710 — these aborts stay
    OUTSIDE _finalize_engagement and its retention side effects.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio]


# --- the scripted stream ----------------------------------------------------

class ScriptedCutoffClient:
    """An SDK client whose launch turn is cut off mid-tool-loop.

    Yields one AssistantMessage carrying a ToolUseBlock and one UserMessage
    carrying its ToolResultBlock, then ends the iteration — exactly what the
    pinned SDK produces when the read task's ``finally`` sends ``end`` before
    any ``ResultMessage`` arrives. No text blocks, so nothing is streamed to
    the topic either.
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

    async def query(self, prompt):
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

def _mock_executor_def(tmp_path, **overrides):
    from config import ExecutorDefinition
    p = tmp_path / "prompt.md"
    p.write_text("You are {task}. Context: {context}. State: {world_state_summary}")
    defaults = {
        "type": "configurator",
        "description": "Test configurator for the #678 launch-artifact red case.",
        "model": "claude-sonnet-4-6",
        "driver": "in_casa",
        "enabled": True,
        "tools_allowed": ["Read"],
        "tools_disallowed": [],
        "permission_mode": "acceptEdits",
        "mcp_server_names": ["casa-framework"],
        "idle_reminder_days": 7,
        "prompt_template_path": str(p),
        "hooks_path": None,
        "observer_policy_path": None,
        "doctrine_dir": "",
    }
    defaults.update(overrides)
    return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT, **defaults)


class _Probe:
    """Ordered, counted observation of every operator-visible effect."""

    def __init__(self):
        self.events: list[str] = []
        self.notice_texts: list[str] = []
        self.transition_calls: list[tuple] = []
        self.mark_error_calls: list[tuple] = []
        self.driver_cancel_calls = 0
        self.emit_count = 0
        self.finalize_count = 0
        self.topic_open_at_notice: list[bool] = []
        self.topic_closed = False


def _build(tmp_path, monkeypatch, probe, client_cls, *, driver_kind="in_casa"):
    """Real registry + real InCasaDriver + real engage_executor.handler."""
    import agent as agent_mod
    from engagement_registry import EngagementRegistry
    from drivers.in_casa_driver import InCasaDriver
    from tools import engage_executor, init_tools

    monkeypatch.setattr(
        "drivers.in_casa_driver.ClaudeSDKClient", client_cls,
    )

    registry = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None,
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

    class _Handle:
        async def emit(self, text):
            probe.emit_count += 1

        async def finalize(self, text):
            probe.finalize_count += 1

    driver = InCasaDriver(
        topic_stream_factory=lambda topic_id: _Handle(),
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

    channel = MagicMock()
    channel.engagement_supergroup_id = -100123
    channel.engagement_permission_ok = True
    channel.open_engagement_topic = AsyncMock(return_value=42)
    channel.bot = MagicMock()
    channel.bot.edit_forum_topic = AsyncMock()

    async def _notice(rec, text):
        probe.events.append("notice")
        probe.notice_texts.append(text)
        probe.topic_open_at_notice.append(not probe.topic_closed)

    async def _paint(*, engagement_id, new_state):
        probe.events.append(f"paint:{new_state}")

    async def _close(*, thread_id):
        probe.events.append("topic_close")
        probe.topic_closed = True

    channel._post_engagement_notice = AsyncMock(side_effect=_notice)
    channel.update_topic_state = AsyncMock(side_effect=_paint)
    channel.close_topic = AsyncMock(side_effect=_close)
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)

    defn = _mock_executor_def(tmp_path, driver=driver_kind)
    exec_reg = MagicMock()
    exec_reg.get = MagicMock(return_value=defn)
    exec_reg.list_types = MagicMock(return_value=["configurator"])

    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=registry,
        executor_registry=exec_reg,
    )
    return engage_executor, registry, channel, driver


async def _launch(engage_executor, task="remove the weather plugin"):
    import agent as agent_mod
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram",
        "chat_id": "c1", "cid": "x", "user_text": "hi",
    })
    try:
        return await engage_executor.handler({
            "executor_type": "configurator", "task": task, "context": "",
        })
    finally:
        agent_mod.origin_var.reset(token)


# --- the red case -----------------------------------------------------------

class TestInCasaLaunchTerminalArtifact:
    """INV-ENG-011: an in_casa LAUNCH turn ends holding the turn's own
    terminal artifact (a ResultMessage) AND either a durably-terminal record
    or operator-visible topic output — or the launch REPORTS the death."""

    async def test_mid_tool_loop_cutoff_reports_death_before_topic_close(
        self, tmp_path, monkeypatch,
    ):
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )

        envelope = await _launch(engage_executor)

        # The stream really was the mid-tool-loop cutoff, not the easy case.
        assert ScriptedCutoffClient.frames_yielded == 2
        assert ScriptedCutoffClient.result_messages_yielded == 0
        assert probe.emit_count == 0
        assert probe.finalize_count == 0

        payload = json.loads(envelope["content"][0]["text"])
        assert payload.get("status") == "error", payload
        assert payload.get("kind") == "launch_turn_incomplete", payload
        assert "without ResultMessage" in payload.get("message", ""), payload
        assert envelope.get("is_error") is True, envelope

        assert len(registry._records) == 1
        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "error", rec.status
        assert rec.origin.get("error_kind") == "launch_turn_incomplete"
        assert "without ResultMessage" in rec.origin.get("error_message", "")
        assert rec.completed_at is not None

        # ONE durable, strict terminal transition — not mark_error.
        assert len(probe.transition_calls) == 1, probe.transition_calls
        eid, outcome, kw = probe.transition_calls[0]
        assert (eid, outcome) == (created_id, "error")
        assert kw["strict"] is True
        assert kw["error_kind"] == "launch_turn_incomplete"
        assert probe.mark_error_calls == []

        # One of each operator-visible effect, notice STRICTLY before close.
        assert channel._post_engagement_notice.await_count == 1
        assert probe.driver_cancel_calls == 1
        assert channel.update_topic_state.await_count == 1
        assert channel.close_topic.await_count == 1
        assert channel.update_topic_state.await_args.kwargs == {
            "engagement_id": created_id, "new_state": "failed",
        }
        assert channel.close_topic.await_args.kwargs == {"thread_id": 42}

        assert probe.events.count("notice") == 1
        assert probe.events.count("topic_close") == 1
        assert probe.events.index("notice") < probe.events.index("topic_close")
        assert probe.topic_open_at_notice == [True]
        assert "without ResultMessage" in probe.notice_texts[0]

        # The client is retired: no half-alive engagement behind a dead pipe.
        assert driver.is_alive(rec) is False

    async def test_a_complete_launch_turn_stays_pending_and_reports_nothing(
        self, tmp_path, monkeypatch,
    ):
        """The control that kills an unconditional-failure mutation: the same
        frames plus the turn's ResultMessage and a text block is a legitimate
        conversational launch end. Nothing is reported, nothing is closed."""
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCompleteClient,
        )

        envelope = await _launch(engage_executor)

        assert ScriptedCompleteClient.result_messages_yielded == 1
        payload = json.loads(envelope["content"][0]["text"])
        assert payload.get("status") == "pending", payload

        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "active"
        assert probe.transition_calls == []
        assert probe.mark_error_calls == []
        assert channel._post_engagement_notice.await_count == 0
        assert channel.update_topic_state.await_count == 0
        assert channel.close_topic.await_count == 0
        assert probe.driver_cancel_calls == 0
        assert probe.finalize_count == 1
        assert driver.is_alive(registry.get(created_id)) is True
