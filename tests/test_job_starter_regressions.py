"""#1277 regressions beside the starter-line red case: what the change must
NOT alter. Green before the fix and after it; they carry no red-case receipt.

- a non-job engagement's prompt gains no starter line;
- the arm-B marker is read only to record who started a job: the spawn cap,
  desk routing and the turn's transport read the same with or without it.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agent
import tools

try:
    from tests.test_job_starter_line import (  # noqa: F401
        ANSWER_KEY, CONTEXT, LABEL, TASK, LaunchClient, _origin, runtime, starter_lines)
except ImportError:
    from test_job_starter_line import (  # noqa: F401
        ANSWER_KEY, CONTEXT, LABEL, TASK, LaunchClient, _origin, runtime, starter_lines)


class TestNonJobPrompt:
    @pytest.mark.asyncio
    async def test_a_non_job_engagement_prompt_has_no_line(self, runtime):
        before = len(LaunchClient.instances)
        token = agent.origin_var.set(_origin(_operator_turn=True))
        try:
            envelope = await tools.delegate_to_agent.handler(
                {"agent": "finance", "task": TASK, "context": CONTEXT, "mode": "interactive"})
        finally:
            agent.origin_var.reset(token)
        reply = json.loads(envelope["content"][0]["text"])
        assert reply["status"] == "pending", reply
        await tools.drain_launch_turns()
        assert len(LaunchClient.instances) == before + 1
        prompt = LaunchClient.instances[-1].prompts[0]
        assert prompt.startswith("You are engaged with the user in a Telegram forum topic.\n")
        assert starter_lines(prompt) == []
        rec = runtime.registry.get(reply["engagement_id"])
        assert "job" not in rec.origin


class TestConsumersUnchanged:
    """L-11f: the marker is read only to record who started a job — the spawn
    cap, desk routing and the turn's transport read the same with or without it."""

    @pytest.mark.parametrize("base", [
        {"channel": "telegram", "chat_id": LABEL, "_scheduled_delivery": True,
         "message_type": "scheduled", "source": "scheduled-ask", "button_answer": "r1"},
        {"channel": "telegram", "chat_id": 1, "message_type": "channel_in",
         "source": "telegram"},
    ], ids=["scheduled-continuation", "dm"])
    def test_spawn_cap_desk_and_transport_ignore_the_marker(self, base):
        import provenance
        import specialist_desk
        cfg = SimpleNamespace(kind="specialist", role="finance")

        def read(origin):
            token = agent.origin_var.set(dict(origin))
            try:
                transport = provenance.turn_provenance().transport
            finally:
                agent.origin_var.reset(token)
            return (tools._is_agent_context(origin),
                    specialist_desk.desk_for_delegation(origin, cfg, "finance") is None,
                    transport)

        base = {"role": "assistant", "execution_role": "assistant", **base}
        assert read(base) == read({**base, ANSWER_KEY: True})
