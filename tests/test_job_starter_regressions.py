"""#1277 regressions beside the starter-line red case: what the change must
NOT alter. Green before the fix and after it; they carry no red-case receipt.

- the arm-B marker is read only to record who started a job: the spawn cap,
  desk routing and the turn's transport read the same with or without it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent
import tools

try:
    from tests.test_job_starter_line import ANSWER_KEY, LABEL
except ImportError:
    from test_job_starter_line import ANSWER_KEY, LABEL


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
