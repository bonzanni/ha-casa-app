"""#233/#224 (v0.118.0): diagnostic telemetry for concierge voice delegation.

Two blind spots made the live failure undiagnosable:

1. `validate_voice_handoff_static` silently PASSES THROUGH (leaving the
   delegation on the ordinary sync path — voice budget, no handoff, no progress
   block) when the requested mode isn't exactly ``"sync"`` OR when the origin's
   ``role`` disagrees with ``execution_role``. Production took that path for a
   concierge voice turn; the two causes are observationally identical, so the
   decision branch must be logged.
2. A delegated specialist emits NO SDK logs at all (sdk_logging's
   system_init/tool_use/turn_done are only called by agent.py's resident loop,
   and a voice delegation also suppresses SDK protocol diagnostics), so a
   delegation cancelled at the voice budget left no trace of where its time
   went.

These tests pin the telemetry itself — the fields the diagnosis depends on, and
the guarantee that the phase line is emitted even when the delegation is
CANCELLED (the case we could not see). They assert NO behaviour change.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def _voice_origin(**overrides):
    """A concierge WS voice origin, as agent.py builds it."""
    origin = {
        "role": "concierge",
        "execution_role": "concierge",
        "channel": "voice",
        "voice_transport": "ws",
        "_voice_handoff_reservation": MagicMock(
            reserve=MagicMock(), release=MagicMock(), commit=MagicMock()),
    }
    origin.update(overrides)
    return origin


def _decision_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if "voice_handoff_decision" in r.getMessage()]


class TestHandoffDecisionLog:
    """The one line that tells apart the two candidate bypasses."""

    def test_non_sync_mode_logs_passthrough_with_the_mode(self, caplog):
        """H-A: the model passed a mode that isn't exactly "sync" (the tool
        schema takes a free-form string). The log must show BOTH that we
        passed through AND the offending value."""
        import tools as tools_mod

        with caplog.at_level(logging.INFO, logger="tools"):
            mode, reservation, err = tools_mod.validate_voice_handoff_static(
                "mtg", _voice_origin(), "synchronous")

        # Behaviour unchanged: passthrough.
        assert (mode, reservation, err) == ("synchronous", None, None)
        lines = _decision_lines(caplog)
        assert lines, "the passthrough branch must be logged"
        assert "decision=passthrough_not_eligible" in lines[0]
        assert "mode=<other>" in lines[0]   # never echoed
        assert "mode_is_sync=False" in lines[0]
        # ... and it must be visibly NOT the role-mismatch cause.
        assert "role_matches=True" in lines[0]

    def test_role_mismatch_logs_passthrough_with_both_roles(self, caplog, monkeypatch):
        """H-B: execution_role passes the ACL while origin['role'] fails the
        normalizer. Same passthrough, different cause — the log must
        distinguish it."""
        import tools as tools_mod

        monkeypatch.setattr(tools_mod, "_agent_role_map",
                            {"concierge": MagicMock(), "assistant": MagicMock()})
        origin = _voice_origin(role="assistant")   # execution_role stays concierge
        with caplog.at_level(logging.INFO, logger="tools"):
            mode, reservation, err = tools_mod.validate_voice_handoff_static(
                "mtg", origin, "sync")

        assert (mode, reservation, err) == ("sync", None, None)
        lines = _decision_lines(caplog)
        assert lines
        assert "decision=passthrough_not_eligible" in lines[0]
        assert "mode_is_sync=True" in lines[0]        # NOT the mode cause
        assert "role_matches=False" in lines[0]       # IS the role cause
        assert "origin_role=assistant" in lines[0]     # a KNOWN role
        assert "execution_role=concierge" in lines[0]
        # The per-predicate booleans make the failing term directly queryable.
        assert "origin_role_is_concierge=False" in lines[0]
        assert "caller_is_concierge=True" in lines[0]
        assert "channel_is_voice=True" in lines[0]

    def test_background_unavailable_is_logged(self, caplog, monkeypatch):
        """#224's fail-closed branch must be visible too."""
        import tools as tools_mod

        cfg = MagicMock()
        cfg.delegates = [MagicMock(agent="mtg")]
        monkeypatch.setattr(tools_mod, "_agent_role_map", {"concierge": cfg})
        # No route_id/capabilities -> deferred_delivery_available() is False.
        with caplog.at_level(logging.INFO, logger="tools"):
            mode, reservation, err = tools_mod.validate_voice_handoff_static(
                "mtg", _voice_origin(), "sync")

        assert err is not None and reservation is None
        lines = _decision_lines(caplog)
        assert lines and "decision=background_unavailable" in lines[0]
        assert "endpoint_can_receive=False" in lines[0]
        # Sol review (Medium): the line must say WHICH predicate failed.
        assert "requires_handoff=True" in lines[0]
        assert "reserve_ok=True" in lines[0]
        assert "route_id=False" in lines[0]
        assert "offer_modality=None" in lines[0]

    def test_client_supplied_capabilities_are_never_echoed(self, caplog):
        """Sol review (High): route capabilities arrive on the WS registration
        frame — i.e. CLIENT-supplied. They must be reported as fixed booleans,
        never rendered into the line."""
        import tools as tools_mod

        origin = _voice_origin(voice_route_capabilities=[
            "background_jobs",
            "voice_handoff\nINJECTED forged log line: secret=hunter2",
        ])
        with caplog.at_level(logging.INFO, logger="tools"):
            tools_mod.validate_voice_handoff_static("mtg", origin, "sync")
        line = _decision_lines(caplog)[0]
        assert "hunter2" not in line and "INJECTED" not in line
        # Capabilities are still reported as a COUNT only — never rendered —
        # so a forged capability string cannot reach the log line.
        assert "cap_other=" in line

    def test_log_never_raises_on_a_hostile_origin(self, caplog):
        """Diagnostics must never break a turn: an origin whose values are
        exotic (unsortable capabilities, huge mode) still returns normally."""
        import tools as tools_mod

        origin = _voice_origin(voice_route_capabilities={1, "b"})
        with caplog.at_level(logging.INFO, logger="tools"):
            mode, _, _ = tools_mod.validate_voice_handoff_static(
                "mtg", origin, "x" * 500)
        assert mode == "x" * 500          # behaviour untouched

    def test_model_supplied_values_never_reach_the_log_verbatim(self, caplog):
        """Sol review (Blocking): a SYNTAX filter is not enough — a short plain
        token can still be meaningful content. Every value is matched against
        the closed set Casa itself defines and reported as <other> otherwise."""
        import tools as tools_mod

        for leak in ("sync but here is the user's private question",
                     "admin:credential",          # passes a syntax filter!
                     "patient.name"):
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="tools"):
                tools_mod.validate_voice_handoff_static(
                    leak, _voice_origin(), leak)
            line = _decision_lines(caplog)[0]
            assert "credential" not in line and "patient" not in line
            assert "private question" not in line
            assert line.count("<other>") >= 2, line   # mode AND agent masked
            assert "mode_is_sync=False" in line       # diagnosis still possible

    def test_known_values_are_preserved(self, caplog, monkeypatch):
        """The allow-list must not blind the ordinary case."""
        import tools as tools_mod

        cfg = MagicMock()
        cfg.delegates = [MagicMock(agent="mtg")]
        monkeypatch.setattr(
            tools_mod, "_agent_role_map", {"concierge": cfg, "mtg": cfg})
        with caplog.at_level(logging.INFO, logger="tools"):
            tools_mod.validate_voice_handoff_static(
                "mtg", _voice_origin(), "sync")
        line = _decision_lines(caplog)[0]
        assert "agent=mtg" in line and "mode=sync" in line
        assert "caller_role=concierge" in line
        assert "transport=ws" in line


class TestDelegatedPhaseLog:
    """`delegated_phases` must be emitted even when the delegation is
    CANCELLED at the voice budget — the exact case that was invisible."""

    async def _run(self, caplog, monkeypatch, *, cancel: bool):
        import tools as tools_mod

        class _FakeClient:
            def __init__(self, options):
                self._options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def query(self, _prompt):
                return None

            async def receive_response(self):
                if cancel:
                    # Stand in for the budget cancellation: block until the
                    # caller cancels this task.
                    await asyncio.sleep(3600)
                yield tools_mod.AssistantMessage(
                    content=[tools_mod.TextBlock(text="ok")], model="m")

        monkeypatch.setattr(tools_mod, "ClaudeSDKClient", _FakeClient)
        monkeypatch.setattr(
            tools_mod, "_build_specialist_options",
            lambda cfg, resolution=None, output_format=None: MagicMock())
        monkeypatch.setattr(tools_mod, "_agent_role_map", {"mtg": MagicMock()})

        cfg = MagicMock()
        cfg.role = "mtg"
        cfg.memory.token_budget = 0

        coro = tools_mod._run_delegated_agent(cfg, "task", "", output_format=None)
        with caplog.at_level(logging.INFO, logger="tools"):
            if cancel:
                task = asyncio.create_task(coro)
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await coro
        return [r.getMessage() for r in caplog.records
                if "delegated_phases" in r.getMessage()]

    async def test_phase_line_on_success(self, caplog, monkeypatch):
        lines = await self._run(caplog, monkeypatch, cancel=False)
        assert lines, "a completed delegation must report its phases"
        line = lines[0]
        assert "role=mtg" in line
        for field in ("options_at_ms=", "connect_at_ms=", "query_at_ms=",
                      "first_msg_at_ms=", "used_tool=", "msgs=", "total_ms="):
            assert field in line, f"{field} missing from: {line}"

    async def test_phase_line_on_cancellation(self, caplog, monkeypatch):
        """THE case we could not see: cancelled at the voice budget."""
        lines = await self._run(caplog, monkeypatch, cancel=True)
        assert lines, (
            "a delegation cancelled at the voice budget MUST still report "
            "where its time went — this is the whole point of the telemetry"
        )
        line = lines[0]
        # It got as far as connect+query but never a message: that shape is
        # what distinguishes 'stuck starting' from 'slow model work'.
        assert "connect_at_ms=" in line and "first_msg_at_ms=None" in line
        assert "got_result=False" in line

    async def test_phase_line_when_cancelled_during_options_build(
            self, caplog, monkeypatch):
        """Sol review (Medium): a cancellation landing during
        `_build_specialist_options` must ALSO be reported — "cancelled before
        the client even started" is one of the answers we're looking for."""
        import tools as tools_mod

        import threading

        # A gate, NOT a long sleep: `asyncio.to_thread` work is uncancellable,
        # so a sleeping worker would outlive the test and stall loop shutdown.
        gate = threading.Event()

        def _slow_options(cfg, resolution=None, output_format=None):
            gate.wait(timeout=30)
            return MagicMock()

        monkeypatch.setattr(tools_mod, "_build_specialist_options", _slow_options)
        monkeypatch.setattr(tools_mod, "_agent_role_map", {"mtg": MagicMock()})
        cfg = MagicMock()
        cfg.role = "mtg"
        cfg.memory.token_budget = 0

        try:
            with caplog.at_level(logging.INFO, logger="tools"):
                task = asyncio.create_task(
                    tools_mod._run_delegated_agent(
                        cfg, "task", "", output_format=None))
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            gate.set()      # let the worker thread finish

        lines = [r.getMessage() for r in caplog.records
                 if "delegated_phases" in r.getMessage()]
        assert lines, "cancellation during the options build must still log"
        # Never reached the options mark, so every later milestone is None.
        assert "options_at_ms=None" in lines[0]
        assert "connect_at_ms=None" in lines[0]
        assert "got_result=False" in lines[0]
