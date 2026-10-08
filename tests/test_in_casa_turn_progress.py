"""#1350: an in-process executor turn keeps a step log in its topic — one
line per turn, posted at the first main-loop tool use, closed by the turn
with its real outcome; specialists, sub-agent steps and topic-less records
get none."""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from drivers.in_casa_driver import InCasaDriver
from engagement_registry import EngagementRecord

pytestmark = [pytest.mark.unit]


class _Stream:
    async def emit(self, text):
        pass

    async def finalize(self, text):
        return None


def _result(subtype="success", stop_reason="end_turn"):
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s1", total_cost_usd=0.0, usage={},
        result="", stop_reason=stop_reason)


def _assistant(*blocks, parent=None):
    return AssistantMessage(content=list(blocks), model="m",
                            parent_tool_use_id=parent)


class _Client:
    def __init__(self, messages, *, raise_after=False):
        self._messages = messages
        self._raise_after = raise_after

    async def query(self, prompt):
        pass

    async def receive_response(self):
        for m in self._messages:
            yield m
        if self._raise_after:
            raise RuntimeError("stream broke")


class _Line:
    def __init__(self):
        self.steps: list[str] = []
        self.closed: list[bool] = []

    def step(self, activity):
        self.steps.append(activity)

    def finish(self, *, ok):
        self.closed.append(ok)


def _rec(kind="executor", topic_id=99):
    return EngagementRecord(
        id="e1", kind=kind, role_or_type="configurator", driver="in_casa",
        status="active", topic_id=topic_id, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=None, origin={}, task="t")


def _driver(client, lines):
    def factory(topic_id):
        line = _Line()
        lines.append((topic_id, line))
        return line
    drv = InCasaDriver(topic_stream_factory=lambda tid: _Stream(),
                       turn_progress_factory=factory)
    drv._clients["e1"] = client
    drv._locks["e1"] = asyncio.Lock()
    return drv


_TOOLS = [
    _assistant(TextBlock("Looking."),
               ToolUseBlock(id="t1", name="Read", input={})),
    _assistant(ToolUseBlock(id="t2", name="Grep", input={}), parent="t1"),
    _assistant(ToolUseBlock(id="t3", name="mcp__casa-framework__plugin_update",
                            input={})),
]


async def test_an_executor_turn_logs_its_main_loop_steps_and_closes_ok():
    lines: list = []
    drv = _driver(_Client(_TOOLS + [_result()]), lines)
    await drv._deliver_turn(_rec(), "go")
    assert len(lines) == 1
    topic_id, line = lines[0]
    assert topic_id == 99
    assert line.steps == ["reading files", "updating plugins"]
    assert line.closed == [True]


async def test_a_turn_that_raises_closes_as_stopped():
    lines: list = []
    drv = _driver(_Client(_TOOLS, raise_after=True), lines)
    with pytest.raises(RuntimeError):
        await drv._deliver_turn(_rec(), "go")
    assert lines[0][1].closed == [False]


async def test_a_result_carried_refusal_closes_as_stopped():
    from error_kinds import ApiErrorTurn
    lines: list = []
    drv = _driver(_Client(_TOOLS + [_result(stop_reason="refusal")]), lines)
    with pytest.raises(ApiErrorTurn):
        await drv._deliver_turn(_rec(), "go")
    assert lines[0][1].closed == [False]


@pytest.mark.parametrize("kind, topic_id", [("specialist", 99), ("executor", None)])
async def test_no_step_log_for_a_specialist_or_a_topicless_record(kind, topic_id):
    lines: list = []
    drv = _driver(_Client(_TOOLS + [_result()]), lines)
    await drv._deliver_turn(_rec(kind=kind, topic_id=topic_id), "go")
    assert lines == []


async def test_a_hung_progress_line_never_delays_or_changes_the_turn():
    """#1350 x1 (Astra, Terra): the turn hands the close off and returns."""
    from drivers.turn_progress import TurnProgressLine, _closing

    async def hang(*_a, **_k):
        await asyncio.Event().wait()

    lines: list = []

    def factory(topic_id):
        line = TurnProgressLine(send=hang, edit=hang)
        lines.append(line)
        return line

    drv = InCasaDriver(topic_stream_factory=lambda tid: _Stream(),
                       turn_progress_factory=factory)
    drv._clients["e1"] = _Client(_TOOLS + [_result()])
    drv._locks["e1"] = asyncio.Lock()
    await asyncio.wait_for(drv._deliver_turn(_rec(), "go"), timeout=0.5)
    assert len(_closing) == 1          # the close runs on its own
    for t in list(_closing):
        t.cancel()
    await asyncio.gather(*list(_closing), return_exceptions=True)
