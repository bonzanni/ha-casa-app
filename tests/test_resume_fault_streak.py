"""#650 — resume-fault streak + retry-tainted-silence notice (beyond the
frozen red cases in test_agent_process.py::TestIssue650RedCases).

Covers: SessionRegistry.note_resume_health (sid guard, increment/reset,
save-failure rollback), register()'s advisory-field lifecycle,
_resume_decision's fault_streak gate, the mixed-retry-kind strike (seam r1,
Sol), the stale-resume-recovery recorder (seam r2, Sol), the literal-sentinel
streaming shapes (seam r2, Terra + refutation record), voice/invoke REQUEST
behavior, and gate-ordered notes under concurrent same-key asks (seam r1).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from agent import (
    RESUME_FAULT_LIMIT,
    _resume_decision,
    _strips_to_silence,
)
from bus import BusMessage, MessageType
from error_kinds import ErrorKind, _USER_MESSAGES
from ingress_identity import ingress_identity
from session_registry import SessionRegistry, build_scoped_session_key
from session_reg_helpers import (
    RESIDENT_DIGEST,
    STUB_USER_PROV,
    resident_prov,
    resident_role_id,
)

try:
    from tests.test_agent_process import (
        FakeClient,
        _make_agent,
        _make_agent_with_registry,
        _msg,
        patch_retry_sleep,
        _RecordingFinalDeliveryChannel,
    )
except ImportError:
    from test_agent_process import (
        FakeClient,
        _make_agent,
        _make_agent_with_registry,
        _msg,
        patch_retry_sleep,
        _RecordingFinalDeliveryChannel,
    )

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_KEY = "k1"


async def _seeded_registry(tmp_path, sid: str = "old-sid") -> SessionRegistry:
    reg = SessionRegistry(str(tmp_path / "sessions.json"))
    await reg.register(
        build_scoped_session_key("telegram", "butler", "fault-scope"),
        resident_role_id("butler"), sid,
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov("butler"),
        user_provenance=STUB_USER_PROV,
    )
    return reg


def _trusted_msg(chat: str = "fault-scope") -> BusMessage:
    m = _msg("telegram", chat, "status?")
    m.trusted_user_origin = ingress_identity(
        "telegram", sender_id="9001", sender_is_operator=True,
    )
    return m


def _entry(reg: SessionRegistry, key: str) -> dict:
    return reg._data[key]  # test-only white-box read


async def _close_warm(agent) -> None:
    """Close the warm pool entry between asks — FakeClient's one-shot
    failure schedule is consumed at construction, so a warm reuse would
    silently succeed instead of exercising the next scripted outcome the
    way a real per-turn client does."""
    key = build_scoped_session_key("telegram", "butler", "fault-scope")
    await agent._pool.close_key(key)


class TestNoteResumeHealth:
    async def test_unhealthy_starts_and_increments_streak(self, tmp_path):
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(
            _KEY, "resident:butler", "S", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        await reg.note_resume_health(
            _KEY, resumed_sid="S", current_sid=None, healthy=False)
        assert _entry(reg, _KEY)["resume_fault_sid"] == "S"
        assert _entry(reg, _KEY)["resume_fault_streak"] == 1
        await reg.note_resume_health(
            _KEY, resumed_sid="S", current_sid=None, healthy=False)
        assert _entry(reg, _KEY)["resume_fault_streak"] == 2

    async def test_streak_follows_the_sid_chain(self, tmp_path):
        """A resumed turn that publishes a SUCCESSOR sid (CLI --resume can
        re-key the session) continues the streak: the fault field always
        records the sid the NEXT ask would resume."""
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(
            _KEY, "resident:butler", "S0", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        # Doomed sentinel-return ask: resumed S0, published S1.
        await reg.register(
            _KEY, "resident:butler", "S1", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        await reg.note_resume_health(
            _KEY, resumed_sid="S0", current_sid="S1", healthy=False)
        assert _entry(reg, _KEY)["resume_fault_sid"] == "S1"
        assert _entry(reg, _KEY)["resume_fault_streak"] == 1
        # Next doomed ask resumes S1 and raises (no publish): continues.
        await reg.note_resume_health(
            _KEY, resumed_sid="S1", current_sid=None, healthy=False)
        assert _entry(reg, _KEY)["resume_fault_sid"] == "S1"
        assert _entry(reg, _KEY)["resume_fault_streak"] == 2

    async def test_healthy_drops_both_fields(self, tmp_path):
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(
            _KEY, "resident:butler", "S", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        await reg.note_resume_health(
            _KEY, resumed_sid="S", current_sid=None, healthy=False)
        await reg.note_resume_health(
            _KEY, resumed_sid="S", current_sid=None, healthy=True)
        assert "resume_fault_sid" not in _entry(reg, _KEY)
        assert "resume_fault_streak" not in _entry(reg, _KEY)

    async def test_declines_on_sid_mismatch_and_missing_entry(self, tmp_path):
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.note_resume_health(
            "absent", resumed_sid="S", current_sid=None, healthy=False)
        await reg.register(
            _KEY, "resident:butler", "S2", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        # Entry names S2; the note touched neither S2 as resumed nor as
        # current -> declined (a foreign conversation is never struck).
        await reg.note_resume_health(
            _KEY, resumed_sid="S", current_sid=None, healthy=False)
        assert "resume_fault_sid" not in _entry(reg, _KEY)

    async def test_save_failure_rolls_back_strike(self, tmp_path):
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(
            _KEY, "resident:butler", "S", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        await reg.note_resume_health(
            _KEY, resumed_sid="S", current_sid=None, healthy=False)
        with patch.object(
            reg, "_save_locked", side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                await reg.note_resume_health(
                    _KEY, resumed_sid="S", current_sid=None, healthy=False)
        # In-memory state matches the last PERSISTED state: streak 1.
        assert _entry(reg, _KEY)["resume_fault_streak"] == 1

    async def test_save_failure_rolls_back_reset(self, tmp_path):
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(
            _KEY, "resident:butler", "S", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        await reg.note_resume_health(
            _KEY, resumed_sid="S", current_sid=None, healthy=False)
        with patch.object(
            reg, "_save_locked", side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                await reg.note_resume_health(
                    _KEY, resumed_sid="S", current_sid=None, healthy=True)
        assert _entry(reg, _KEY)["resume_fault_sid"] == "S"
        assert _entry(reg, _KEY)["resume_fault_streak"] == 1

    async def test_register_preserves_fields_for_the_note_to_own(self, tmp_path):
        """register() never touches the advisory fields — a resumed turn can
        publish a successor sid, so the note (same gate) owns the lifecycle."""
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(
            _KEY, "resident:butler", "S", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        await reg.note_resume_health(
            _KEY, resumed_sid="S", current_sid=None, healthy=False)
        for sid in ("S", "S2"):
            await reg.register(
                _KEY, "resident:butler", sid, binding_digest="d",
                speaker_provenance=resident_prov("butler"),
                user_provenance=STUB_USER_PROV,
            )
            assert _entry(reg, _KEY)["resume_fault_streak"] == 1

    async def test_fresh_healthy_turn_retires_leftover_fields(self, tmp_path):
        """After the fault-streak escape, the fresh turn's healthy note
        (resumed_sid None, current_sid = its new registration) drops the
        pre-escape chain's fields."""
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(
            _KEY, "resident:butler", "S", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        await reg.note_resume_health(
            _KEY, resumed_sid="S", current_sid=None, healthy=False)
        await reg.register(
            _KEY, "resident:butler", "FRESH", binding_digest="d",
            speaker_provenance=resident_prov("butler"),
            user_provenance=STUB_USER_PROV,
        )
        await reg.note_resume_health(
            _KEY, resumed_sid=None, current_sid="FRESH", healthy=True)
        assert "resume_fault_sid" not in _entry(reg, _KEY)
        assert "resume_fault_streak" not in _entry(reg, _KEY)


class TestFaultStreakDecision:
    def _entry_dict(self, sid: str = "S", **extra) -> dict:
        from datetime import datetime, timezone
        return {
            "agent": resident_role_id("butler"),
            "sdk_session_id": sid,
            "last_active": datetime.now(timezone.utc).isoformat(),
            "binding_digest": RESIDENT_DIGEST,
            **extra,
        }

    def _decide(self, entry: dict):
        from datetime import datetime, timezone
        return _resume_decision(
            "telegram", entry, datetime.now(timezone.utc),
            role_id=resident_role_id("butler"),
            binding_digest=RESIDENT_DIGEST,
        )

    def test_at_limit_goes_fresh_with_retain(self):
        d = self._decide(self._entry_dict(
            resume_fault_sid="S", resume_fault_streak=RESUME_FAULT_LIMIT,
        ))
        assert d.action == "new"
        assert d.resume_sid is None
        assert d.retain_old is True
        assert d.old is not None and d.old.sdk_session_id == "S"
        assert d.reason == "fault_streak"

    def test_below_limit_resumes(self):
        d = self._decide(self._entry_dict(
            resume_fault_sid="S", resume_fault_streak=RESUME_FAULT_LIMIT - 1,
        ))
        assert d.action == "resume"
        assert d.resume_sid == "S"

    def test_stale_fields_for_other_sid_are_inert(self):
        d = self._decide(self._entry_dict(
            resume_fault_sid="OTHER", resume_fault_streak=99,
        ))
        assert d.action == "resume"

    def test_malformed_streak_is_tolerated(self):
        d = self._decide(self._entry_dict(
            resume_fault_sid="S", resume_fault_streak="not-a-number",
        ))
        assert d.action == "resume"


def test_strips_to_silence_predicate():
    assert _strips_to_silence(None)
    assert _strips_to_silence("")
    assert _strips_to_silence("  \n ")
    assert _strips_to_silence("<silent/>")
    assert _strips_to_silence(" <silent/>\n<silent/> ")
    assert not _strips_to_silence("<silent/> but actually, one thing")
    assert not _strips_to_silence("hello")


class TestAgentStrikeShapes:
    async def test_mixed_retry_kinds_still_strike_and_escape(self, tmp_path):
        """Seam r1 (Sol): [SDK_ERROR, RATE_LIMIT, <silent/>] per ask must
        strike — the strike keys on SDK_ERROR anywhere in the consumed
        retries, NOT on the reclassified (last-kind) notice — and two such
        asks end the resume streak."""
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        resumes: list[str | None] = []

        class _ResumeRecordingClient(FakeClient):
            def __init__(self, options):
                resumes.append(getattr(options, "resume", None))
                super().__init__(options)

        FakeClient.reset()
        FakeClient.failure_schedule = [
            CLIConnectionError("upstream reset"),
            RuntimeError("rate limit exceeded"),
            None,
            CLIConnectionError("upstream reset"),
            RuntimeError("rate limit exceeded"),
            None,
            None,
        ]
        FakeClient.response_text = "<silent/>"

        reg = await _seeded_registry(tmp_path)
        agent = _make_agent_with_registry(reg, role="butler")
        channel = _RecordingFinalDeliveryChannel()
        agent._channel_manager.register(channel)

        with patch(
            "sdk_client_pool._default_make_client", _ResumeRecordingClient,
        ), patch_retry_sleep():
            await agent.handle_message(_trusted_msg())
            await _close_warm(agent)
            await agent.handle_message(_trusted_msg())
            await _close_warm(agent)
            # The notice surfaces the LAST retry kind (rate_limit here)...
            assert channel.deliveries == [
                _USER_MESSAGES[ErrorKind.RATE_LIMIT]] * 2
            # ...but the strike counted the SDK_ERROR evidence.
            FakeClient.response_text = "recovered"
            await agent.handle_message(_trusted_msg())

        # ask1 resumed old-sid; its silent "success" published the successor
        # sdk-sid-1, which ask2 resumed; ask3 escaped fresh at the limit.
        assert resumes == ["old-sid"] * 3 + ["sdk-sid-1"] * 3 + [None]
        assert channel.deliveries[-1] == "recovered"

    async def test_silence_after_non_sdk_retries_notices_but_never_strikes(
        self, tmp_path,
    ):
        """Silence whose retries carried NO SDK_ERROR (congestion-shaped)
        gets the visibility notice but records no poison verdict."""
        resumes: list[str | None] = []

        class _ResumeRecordingClient(FakeClient):
            def __init__(self, options):
                resumes.append(getattr(options, "resume", None))
                super().__init__(options)

        FakeClient.reset()
        FakeClient.failure_schedule = [
            RuntimeError("rate limit exceeded"), None,
            RuntimeError("rate limit exceeded"), None,
            RuntimeError("rate limit exceeded"), None,
        ]
        FakeClient.response_text = "<silent/>"

        reg = await _seeded_registry(tmp_path)
        agent = _make_agent_with_registry(reg, role="butler")
        channel = _RecordingFinalDeliveryChannel()
        agent._channel_manager.register(channel)

        with patch(
            "sdk_client_pool._default_make_client", _ResumeRecordingClient,
        ), patch_retry_sleep():
            for _ in range(3):
                await agent.handle_message(_trusted_msg())
                await _close_warm(agent)

        assert len(channel.deliveries) == 3          # visible every time
        # Each silent "success" published the successor sdk-sid-1; no ask
        # ever went fresh, and no strike was recorded.
        assert resumes == ["old-sid", "old-sid"] + ["sdk-sid-1"] * 4
        key = build_scoped_session_key("telegram", "butler", "fault-scope")
        assert "resume_fault_sid" not in reg._data[key]

    async def test_stale_resume_recovery_retries_are_recorded(self, tmp_path):
        """Seam r2 (Sol): the recorder wraps the SECOND retry_sdk_call too —
        a ProcessError recovery followed by a retry-tainted silent fresh turn
        must still produce the visibility notice."""
        from claude_agent_sdk import ProcessError

        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            CLIConnectionError("upstream reset"),
            None,
        ]
        FakeClient.response_text = "<silent/>"

        reg = await _seeded_registry(tmp_path)
        agent = _make_agent_with_registry(reg, role="butler")
        channel = _RecordingFinalDeliveryChannel()
        agent._channel_manager.register(channel)

        captured: dict[str, Any] = {}
        orig_process = agent._process

        async def _spy(msg, on_token=None, turn_report=None):
            captured["report"] = turn_report
            return await orig_process(
                msg, on_token=on_token, turn_report=turn_report,
            )

        agent._process = _spy  # type: ignore[method-assign]

        with patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep():
            await agent.handle_message(_trusted_msg())

        assert captured["report"]["retries"] == [ErrorKind.SDK_ERROR]
        assert channel.deliveries == [_USER_MESSAGES[ErrorKind.SDK_ERROR]]

    async def test_streak_resets_on_recovered_answer(self, tmp_path):
        """LIMIT-1 strikes then a real answer: the streak is gone, and the
        next doomed ask starts again at 1 — no creeping escape."""
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        FakeClient.reset()
        # ask1: 3 SDK errors (strike 1); ask2: success (reset);
        # ask3: 3 SDK errors (strike 1 again).
        FakeClient.failure_schedule = [
            CLIConnectionError("a"), CLIConnectionError("b"),
            CLIConnectionError("c"),
            None,
            CLIConnectionError("d"), CLIConnectionError("e"),
            CLIConnectionError("f"),
        ]
        FakeClient.response_text = "pong"

        reg = await _seeded_registry(tmp_path)
        key = build_scoped_session_key("telegram", "butler", "fault-scope")
        agent = _make_agent_with_registry(reg, role="butler")

        with patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep():
            await agent.handle_message(_trusted_msg())
            assert reg._data[key]["resume_fault_streak"] == 1
            await agent.handle_message(_trusted_msg())
            assert "resume_fault_streak" not in reg._data[key]
            await _close_warm(agent)
            await agent.handle_message(_trusted_msg())
            assert reg._data[key]["resume_fault_streak"] == 1


class _StreamingTelegramLikeChannel:
    """Reproduces telegram's first-token-send + final-edit shape (seam r2,
    Terra): the first on_token call 'sends', later finalize edits it."""

    name = "telegram"

    def __init__(self) -> None:
        self.stream_sends: list[str] = []
        self.edits: list[str] = []
        self.final_sends: list[str] = []
        self._message_id: int | None = None

    def create_on_token(self, context):
        async def _tok(accumulated: str) -> None:
            if self._message_id is None:
                self.stream_sends.append(accumulated)
                self._message_id = 1
            else:
                self.edits.append(accumulated)
        return _tok

    async def finalize_stream(self, text, context, on_token):
        if self._message_id is None:
            self.final_sends.append(text)
        else:
            self.edits.append(text)
        return None

    async def send(self, text, context):
        self.final_sends.append(text)
        return None


class TestNoticeDeliveryShapes:
    async def test_empty_doomed_reply_is_one_final_send(self, tmp_path):
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        FakeClient.reset()
        FakeClient.failure_schedule = [CLIConnectionError("x"), None]
        FakeClient.response_text = ""

        agent = _make_agent(tmp_path, role="assistant")
        channel = _StreamingTelegramLikeChannel()
        agent._channel_manager.register(channel)

        with patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep():
            await agent.handle_message(_trusted_msg(chat="s1"))

        assert channel.stream_sends == []
        assert channel.final_sends == [_USER_MESSAGES[ErrorKind.SDK_ERROR]]

    async def test_literal_sentinel_doomed_reply_is_one_final_send(
        self, tmp_path,
    ):
        """#666 (INV-TURN-009): the literal sentinel is never streamed, so the
        classified retry-tainted reply is ONE fresh send and no edit.

        This pin previously asserted one streamed sentinel plus one superseding
        edit — the pre-existing G-3 cost. With the sentinel held, Telegram's
        on_token closure holds no message id, `finalize_stream` takes its
        fresh-send branch, and the failed-final-edit residual disappears with
        the message that carried it."""
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        FakeClient.reset()
        FakeClient.failure_schedule = [CLIConnectionError("x"), None]
        FakeClient.response_text = "<silent/>"

        agent = _make_agent(tmp_path, role="assistant")
        channel = _StreamingTelegramLikeChannel()
        agent._channel_manager.register(channel)

        with patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep():
            await agent.handle_message(_trusted_msg(chat="s2"))

        assert channel.stream_sends == []
        assert channel.edits == []
        assert channel.final_sends == [_USER_MESSAGES[ErrorKind.SDK_ERROR]]


class TestRequestTurns:
    async def test_voice_doomed_silence_emits_one_error_line_empty_response(
        self, tmp_path,
    ):
        """Voice: the error frame is the disclosure; the M4 RESPONSE stays
        empty exactly as on every classified voice error today (seam r2,
        Terra — design claim corrected, contract pinned)."""
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})

        class _VoiceLikeChannel:
            name = "voice"

            def __init__(self):
                self.error_lines: list[str] = []

            async def emit_error_line(self, kind, context, config):
                self.error_lines.append(kind)
                return True

        FakeClient.reset()
        FakeClient.failure_schedule = [CLIConnectionError("x"), None]
        FakeClient.response_text = "<silent/>"

        agent = _make_agent(tmp_path, role="assistant")
        channel = _VoiceLikeChannel()
        agent._channel_manager.register(channel)

        msg = BusMessage(
            type=MessageType.REQUEST, source="voice", target="assistant",
            content="status?", channel="voice", context={"chat_id": "v1"},
            trusted_user_origin=ingress_identity("voice_sse"),
        )
        with patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep():
            resp = await agent.handle_message(msg)

        assert channel.error_lines == ["sdk_error"]
        assert resp is not None
        assert resp.content == ""

    async def test_invoke_doomed_silence_returns_mapped_response(
        self, tmp_path,
    ):
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        FakeClient.reset()
        FakeClient.failure_schedule = [CLIConnectionError("x"), None]
        FakeClient.response_text = ""

        agent = _make_agent(tmp_path, role="assistant")

        msg = BusMessage(
            type=MessageType.REQUEST, source="http", target="assistant",
            content="status?", channel="webhook",
            context={"chat_id": "req-1"},
            trusted_user_origin=ingress_identity("invoke"),
        )
        with patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep():
            resp = await agent.handle_message(msg)

        assert resp is not None
        assert resp.content == _USER_MESSAGES[ErrorKind.SDK_ERROR]


class TestGateOrderedNotes:
    async def test_concurrent_asks_cannot_overshoot_the_limit(self, tmp_path):
        """Seam r1 (Terra): three same-key trusted asks running as concurrent
        tasks, with deliveries BLOCKED, must still serialize their strikes
        through the session write gate — the third ask decides fresh at
        exactly the limit. If notes committed only after delivery, all three
        would resume 'old-sid' (9 constructions, no fresh)."""
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        resumes: list[str | None] = []

        class _ResumeRecordingClient(FakeClient):
            def __init__(self, options):
                resumes.append(getattr(options, "resume", None))
                super().__init__(options)

        class _BlockingChannel:
            name = "telegram"

            def __init__(self):
                self.gate = asyncio.Event()
                self.blocked = 0
                self.deliveries: list[str] = []

            async def send(self, text, context):
                self.blocked += 1
                await self.gate.wait()
                self.deliveries.append(text)

            async def send_response(self, text, context):
                await self.send(text, context)

        FakeClient.reset()
        FakeClient.failure_schedule = [
            CLIConnectionError(f"e{i}") for i in range(6)
        ] + [None]
        FakeClient.response_text = "recovered"

        reg = await _seeded_registry(tmp_path)
        agent = _make_agent_with_registry(reg, role="butler")
        channel = _BlockingChannel()
        agent._channel_manager.register(channel)

        async def _wait_blocked(n: int) -> None:
            for _ in range(500):
                if channel.blocked >= n:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError(f"never reached blocked={n}")

        with patch(
            "sdk_client_pool._default_make_client", _ResumeRecordingClient,
        ), patch_retry_sleep():
            t1 = asyncio.create_task(agent.handle_message(_trusted_msg()))
            await _wait_blocked(1)
            t2 = asyncio.create_task(agent.handle_message(_trusted_msg()))
            await _wait_blocked(2)
            t3 = asyncio.create_task(agent.handle_message(_trusted_msg()))
            await _wait_blocked(3)
            channel.gate.set()
            await asyncio.wait_for(
                asyncio.gather(t1, t2, t3), timeout=10,
            )

        assert resumes == ["old-sid"] * 6 + [None]
        assert len(channel.deliveries) == 3
        assert "recovered" in channel.deliveries


class TestStrikeGuards:
    async def test_terminal_rate_limit_exhaustion_never_strikes(self, tmp_path):
        """A RATE_LIMIT-exhausted terminal raise on a resumed trusted ask is
        congestion-shaped: mapped message delivered, zero strikes — even
        though retries were consumed."""
        FakeClient.reset()
        FakeClient.failure_schedule = [
            RuntimeError("rate limit exceeded") for _ in range(3)
        ]
        reg = await _seeded_registry(tmp_path)
        key = build_scoped_session_key("telegram", "butler", "fault-scope")
        agent = _make_agent_with_registry(reg, role="butler")
        channel = _RecordingFinalDeliveryChannel()
        agent._channel_manager.register(channel)

        with patch("sdk_client_pool._default_make_client", FakeClient), \
                patch_retry_sleep():
            await agent.handle_message(_trusted_msg())

        assert channel.deliveries == [_USER_MESSAGES[ErrorKind.RATE_LIMIT]]
        assert "resume_fault_sid" not in reg._data[key]
        assert "resume_fault_streak" not in reg._data[key]

    async def test_fresh_doomed_turn_is_not_resume_evidence(self, tmp_path):
        """Strikes require that the turn RESUMED: with no prior session, the
        first doomed-silent ask must not count, so the escape needs LIMIT
        strikes from genuinely resumed asks (fresh at ask 4, not ask 3)."""
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        resumes: list[str | None] = []

        class _ResumeRecordingClient(FakeClient):
            def __init__(self, options):
                resumes.append(getattr(options, "resume", None))
                super().__init__(options)

        FakeClient.reset()
        FakeClient.failure_schedule = [
            CLIConnectionError("a"), CLIConnectionError("b"), None,
            CLIConnectionError("c"), CLIConnectionError("d"), None,
            CLIConnectionError("e"), CLIConnectionError("f"), None,
            None,
        ]
        FakeClient.response_text = "<silent/>"

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        agent = _make_agent_with_registry(reg, role="butler")
        channel = _RecordingFinalDeliveryChannel()
        agent._channel_manager.register(channel)

        with patch(
            "sdk_client_pool._default_make_client", _ResumeRecordingClient,
        ), patch_retry_sleep():
            for _ in range(3):
                await agent.handle_message(_trusted_msg())
                await _close_warm(agent)
            FakeClient.response_text = "recovered"
            await agent.handle_message(_trusted_msg())

        # ask1 fresh (no strike), ask2 strike 1, ask3 strike 2, ask4 escapes.
        assert resumes == [None] * 3 + ["sdk-sid-1"] * 6 + [None]
