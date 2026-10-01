"""#1121 — regressions around the turn-limit stop (INV-TURN-014).

Each of these is GREEN at the base (ebd8cc3f) and is kept by mutation: they pin
what the change must NOT do — widen the limit predicate to other error results
or to empty text, strike or retry a limit stop, acknowledge a narration whose
model text was really delivered any differently than today, let a caller-carried
marker speak a line on a healthy voice turn, or move the voice residents'
limits. The red cases live in ``test_pin_1121_turn_limit.py``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from claude_agent_sdk import ResultMessage

from bus import BusMessage, MessageType
from session_registry import build_scoped_session_key
from test_agent_process import (
    ScriptedToolClient, _make_agent, _make_agent_with_registry, _mk_assistant,
)
from test_pin_1121_turn_limit import (
    CASA, LINE, SID, TRANSPORTS, _Tg, _dm, _limit_script, _limit_warnings,
    _narration, _reset, _run, _voice_frames, _work,
)
from test_resume_fault_streak import _align_seeded_surface, _seeded_registry

pytestmark = [pytest.mark.asyncio]


def _result(subtype: str, *, is_error: bool, result: str | None) -> ResultMessage:
    return ResultMessage(
        subtype=subtype, duration_ms=10, duration_api_ms=10, is_error=is_error,
        num_turns=3, session_id=SID, stop_reason="end_turn",
        total_cost_usd=0.1, usage={"input_tokens": 1, "output_tokens": 1},
        result=result,
    )


async def test_other_error_results_get_no_limit_line(tmp_path, caplog):
    """Mutant: the limit predicate widened to every ``is_error`` result."""
    caplog.set_level(logging.INFO)
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    _reset(_work() + [_mk_assistant("partial"),
                      _result("error_during_execution", is_error=True,
                              result=None)])
    await _run(agent, _dm())
    assert tg.lines() == []
    assert _limit_warnings(caplog, "assistant", "telegram") == []


@pytest.mark.parametrize("final", ["", "<silent/>"], ids=["empty", "sentinel"])
async def test_a_successful_silent_turn_gets_no_limit_line(
        tmp_path, caplog, final):
    """Mutant: the limit predicate keyed on empty text."""
    caplog.set_level(logging.INFO)
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    script = _work()
    if final:
        script.append(_mk_assistant(final))
    script.append(_result("success", is_error=False, result=final))
    _reset(script)
    await _run(agent, _dm())
    assert tg.send.await_count == 0
    assert tg.texts("finalize_response_stream") == []
    assert _limit_warnings(caplog, "assistant", "telegram") == []


async def test_a_limit_stop_is_published_reset_and_never_retried(tmp_path):
    """No retry consumed: the stop's session is published through the pool,
    a seeded fault streak is reset, and exactly one client ran."""
    reg = await _seeded_registry(tmp_path)
    key = build_scoped_session_key("telegram", "butler", "fault-scope")
    reg._data[key]["resume_fault_sid"] = "old-sid"
    reg._data[key]["resume_fault_streak"] = 1
    agent = _make_agent_with_registry(reg, role="butler")
    tg = _Tg()
    agent._channel_manager.register(tg)
    _align_seeded_surface(reg, agent)
    _reset(_limit_script(None))
    await _run(agent, _dm(chat="fault-scope", role="butler"))
    assert len(ScriptedToolClient.instances) == 1
    e = reg._data[key]
    assert e.get("sdk_session_id") == SID
    assert e.get("resume_fault_streak") is None
    assert e.get("resume_fault_sid") is None


async def test_a_narration_that_delivered_its_text_is_acknowledged(tmp_path):
    """A cut narration whose partial narration reached the chat keeps today's
    acknowledgement; Casa's line is an extra message, not the announcement."""
    agent = _make_agent(tmp_path, role="assistant")
    tg = _Tg()
    agent._channel_manager.register(tg)
    msg, ack = _narration()
    _reset(_limit_script("Finance reconciled the first two accounts."))
    await _run(agent, msg)
    assert ack.await_count == 1


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_voice_limit_with_no_speech_and_no_retry_keeps_s1(
        tmp_path, transport):
    frames = await _voice_frames(tmp_path, transport, _limit_script(None))
    assert [f["kind"] for f in frames if f["type"] == "error"] == ["empty_turn"]
    assert [f for f in frames if f["type"] == "block"] == []


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_a_caller_carried_marker_speaks_nothing_on_a_healthy_turn(
        tmp_path, transport, monkeypatch):
    """A caller cannot make a healthy voice turn announce a limit stop: the
    agent writes the marker on every voice turn."""
    import channels.voice.channel as vc_mod
    real = vc_mod.sanitize_external_context
    monkeypatch.setattr(
        vc_mod, "sanitize_external_context",
        lambda ctx: {**real(ctx), "_turn_limit_stop": True})
    script = _work(1) + [_mk_assistant("All lights are off. Done"),
                         _result("success", is_error=False, result="ok")]
    frames = await _voice_frames(tmp_path, transport, script)
    blocks = [f["text"] for f in frames if f["type"] == "block"]
    assert not any("step" in b.lower() for b in blocks)
    assert frames[-1]["type"] == "done"


async def test_voice_residents_keep_their_limits():
    for role, limit in (("butler", 10), ("concierge", 6)):
        runtime = yaml.safe_load(
            (CASA / f"defaults/agents/{role}/runtime.yaml").read_text())
        assert runtime["tools"]["max_turns"] == limit, role


async def test_trusted_invoke_runs_with_the_assistants_limit(tmp_path):
    from test_pin_1121_turn_limit import _restricted_max_turns
    agent = _make_agent(tmp_path, role="assistant")
    agent.config.tools.max_turns = 80
    assert await _restricted_max_turns(agent, "invoke") == 80
