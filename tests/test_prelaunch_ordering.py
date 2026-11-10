"""Prelaunch pipeline runs in order for ALL modes, before side effects (spec A4).

Covers the ordering contract test-enforced by Task 4's brief: a single
``_prelaunch`` call site dominates both the sync/async path and the
interactive branch, so no delegation side effect (progress emission,
delegation/engagement record creation, specialist task start, driver
start) happens before its gates pass — for every mode alike.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import AgentConfig, DelegateEntry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _cfg(role: str, delegates: tuple[str, ...] = ()) -> AgentConfig:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    return cfg


class TestPrelaunchOrdering:
    async def test_progress_before_record_before_task(self, monkeypatch):
        """Voice sync delegation: the progress sink fires, THEN the
        delegation record is created, THEN the specialist task actually
        runs — sentinel proof that _prelaunch's single call site dominates
        every downstream side effect."""
        import agent as agent_mod
        import tools as tm

        trace: list[str] = []

        reg = MagicMock()
        reg.get.return_value = None

        async def _register(record):
            trace.append("record")

        reg.register_delegation = AsyncMock(side_effect=_register)
        reg.cancel_delegation = AsyncMock()
        reg.fail_delegation = AsyncMock()
        reg.complete_delegation = AsyncMock()
        reg.job_registry.finish_voice_result = AsyncMock()

        tm.init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
            agent_role_map={
                "assistant": _cfg("assistant", delegates=("finance",)),
                "finance": _cfg("finance"),
            },
        )

        async def _fake_run(
            cfg, task_text, context_text, resolution=None, output_format=None,
            tool_counts=None,
        ):
            trace.append("task")
            assert output_format is tm.VOICE_JOB_OUTPUT_FORMAT
            return tm.DelegatedOutput(text="ok", structured_output={
                "status": "answered", "spoken_summary": "ok", "answer": "ok",
                "clarification": "", "citations": [], "assumptions": [],
                "provenance": {}, "sensitivity": "household",
                "delivery_ttl_s": 900,
            })

        monkeypatch.setattr(tm, "_run_delegated_agent", _fake_run)

        async def _progress_sink(text: str) -> None:
            trace.append("progress")

        token = agent_mod.origin_var.set({
            "role": "assistant", "execution_role": "assistant",
            "channel": "voice", "chat_id": "c1", "cid": "t", "user_text": "hi",
            "voice_deadline": asyncio.get_running_loop().time() + 20.0,
            "_progress_sink": _progress_sink,
        })
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "ok"
        assert trace == ["progress", "record", "task"]

    async def test_a_job_launch_denied_by_prelaunch_creates_no_topic(
        self, monkeypatch,
    ):
        """A specialist-hosted job's engagement launch obeys the SAME gate
        order: a denial raised inside _prelaunch — here the mode gate, which
        refuses a job on the voice channel (a scheduled fire on a voice
        trigger reaches it) — fires strictly before the launch owner's topic
        and engagement side effects, and before the progress line (the LAST
        step in _prelaunch, after every gate).
        """
        from test_delegate_to_agent_interactive import launch_job
        import agent as agent_mod
        import tools as tm

        tch = MagicMock()
        cm = MagicMock()
        cm.get.return_value = tch
        reg = MagicMock()
        reg.get.return_value = None
        eng_reg = MagicMock()
        eng_reg.create = AsyncMock()

        tm.init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=eng_reg,
            agent_role_map={
                "assistant": _cfg("assistant", delegates=("finance",)),
                "finance": _cfg("finance"),
            },
        )

        progress_calls: list[str] = []

        async def _progress_sink(text: str) -> None:
            progress_calls.append(text)

        res = await launch_job("t", origin={
            "role": "assistant", "execution_role": "assistant",
            "channel": "voice", "chat_id": "c1", "cid": "t", "user_text": "hi",
            "voice_deadline": asyncio.get_running_loop().time() + 20.0,
            "_progress_sink": _progress_sink,
        })

        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "mode_unsupported_on_voice"
        eng_reg.create.assert_not_awaited()
        # Progress is the LAST _prelaunch step — a mode denial precedes it.
        assert progress_calls == []
