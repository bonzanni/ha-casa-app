"""Specialist concurrency caps, I/O bounds, per-role telemetry (spec §4.6).

Covers:
- SpecialistLimiter/Permit unit semantics: hard per-scope cap of 1, global
  cap, no double-count/leak of the global slot on a per-scope-full denial,
  idempotent + cancellation-safe release.
- SpecialistTelemetry unit semantics: per-role delegation/cost aggregation,
  denial counting, WARN past `specialist_cost_alert_threshold`.
- tools.py wiring: `_prelaunch`'s concurrency gate (busy denial, no side
  effects); the launched task's permit release on normal completion AND
  on voice-deadline cancellation; interactive engagement permit handoff +
  release via `_finalize_engagement` and the pre-finalize driver-start-
  failure path; input bounds (`input_too_large`); output truncation +
  `_run_delegated_agent`'s ResultMessage usage/cost capture.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from config import (
    AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig, SessionConfig,
    ToolsConfig,
)
from specialist_limits import SpecialistLimiter, SpecialistTelemetry
from specialist_registry import DelegationRecord, SpecialistRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _specialist_cfg(role: str = "finance") -> AgentConfig:
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are " + role,
        character=CharacterConfig(name=role.capitalize()),
        enabled=True,
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=0),
        session=SessionConfig(strategy="ephemeral", idle_timeout=0),
    )


def _caller_cfg(role: str = "assistant", delegates: tuple[str, ...] = ("finance",)) -> AgentConfig:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    return cfg


def _origin(role="assistant", channel="telegram", chat_id="x") -> dict:
    return {
        "role": role, "execution_role": role, "channel": channel,
        "chat_id": chat_id, "cid": "c1", "user_text": "please do X",
    }


def _mk_result_message(*, session_id="sid", total_cost_usd=None, usage=None):
    from claude_agent_sdk import ResultMessage
    try:
        return ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id=session_id,
            total_cost_usd=total_cost_usd, usage=usage,
        )
    except TypeError:
        rm = ResultMessage.__new__(ResultMessage)
        rm.subtype = "success"
        rm.duration_ms = 1
        rm.duration_api_ms = 1
        rm.is_error = False
        rm.num_turns = 1
        rm.session_id = session_id
        rm.total_cost_usd = total_cost_usd
        rm.usage = usage
        return rm


class _FakeSpecialistClient:
    """Minimal ClaudeSDKClient substitute — yields an AssistantMessage then
    a ResultMessage carrying configurable cost/usage. Mirrors the fixture
    in test_delegate_to_agent.py, extended for Task 6's telemetry."""

    response_text: str = "reply"
    delay_s: float = 0.0
    total_cost_usd: float | None = None
    usage: dict | None = None

    @classmethod
    def reset(cls, response="reply", delay=0.0, total_cost_usd=None, usage=None):
        cls.response_text = response
        cls.delay_s = delay
        cls.total_cost_usd = total_cost_usd
        cls.usage = usage

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        self._text = text

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        if _FakeSpecialistClient.delay_s > 0:
            await asyncio.sleep(_FakeSpecialistClient.delay_s)
        try:
            block = TextBlock(text=_FakeSpecialistClient.response_text)
        except TypeError:
            block = TextBlock(_FakeSpecialistClient.response_text)  # type: ignore[call-arg]
        try:
            asst = AssistantMessage(content=[block])
        except TypeError:
            asst = AssistantMessage.__new__(AssistantMessage)
            asst.content = [block]  # type: ignore[attr-defined]
        yield asst
        yield _mk_result_message(
            total_cost_usd=_FakeSpecialistClient.total_cost_usd,
            usage=_FakeSpecialistClient.usage,
        )


async def _with_origin(coro, origin: dict):
    import agent as agent_mod
    token = agent_mod.origin_var.set(origin)
    try:
        return await coro
    finally:
        agent_mod.origin_var.reset(token)


def _init_tools(tmp_path, *, limiter=None, telemetry=None, agent_role_map=None):
    import tools as tm
    from bus import MessageBus
    from channels import ChannelManager

    reg = SpecialistRegistry(str(tmp_path / "specs"),
                              tombstone_path=str(tmp_path / "del.json"))
    bus = MessageBus()
    bus.register("assistant", None)
    cm = ChannelManager()
    tm.init_tools(
        cm, bus, reg, agent_role_map=agent_role_map or {
            "assistant": _caller_cfg(delegates=("finance",)),
            "finance": _specialist_cfg("finance"),
        },
        specialist_limiter=limiter, specialist_telemetry=telemetry,
    )
    return tm, reg, bus


# ---------------------------------------------------------------------------
# SpecialistLimiter / Permit — pure unit semantics
# ---------------------------------------------------------------------------


class TestSpecialistLimiterUnit:
    def test_same_scope_second_acquire_is_busy(self):
        limiter = SpecialistLimiter(max_global=5)
        p1 = limiter.try_acquire("scope-a")
        assert p1 is not None
        p2 = limiter.try_acquire("scope-a")
        assert p2 is None

    def test_global_saturation_across_different_scopes_is_busy(self):
        limiter = SpecialistLimiter(max_global=1)
        p1 = limiter.try_acquire("scope-a")
        assert p1 is not None
        p2 = limiter.try_acquire("scope-b")
        assert p2 is None  # global cap saturated, even though scope-b is idle

    def test_per_scope_full_does_not_consume_global_slot(self):
        limiter = SpecialistLimiter(max_global=2)
        p1 = limiter.try_acquire("scope-a")
        assert p1 is not None
        assert limiter.in_flight == 1
        # Same scope again — denied, must NOT touch the global count.
        denied = limiter.try_acquire("scope-a")
        assert denied is None
        assert limiter.in_flight == 1
        # A DIFFERENT scope must still get the second global slot.
        p2 = limiter.try_acquire("scope-b")
        assert p2 is not None
        assert limiter.in_flight == 2

    def test_release_frees_scope_for_reacquire(self):
        limiter = SpecialistLimiter(max_global=1)
        p1 = limiter.try_acquire("scope-a")
        assert p1 is not None
        assert limiter.try_acquire("scope-a") is None
        p1.release()
        assert limiter.in_flight == 0
        p2 = limiter.try_acquire("scope-a")
        assert p2 is not None

    def test_release_is_idempotent(self):
        limiter = SpecialistLimiter(max_global=1)
        p1 = limiter.try_acquire("scope-a")
        p1.release()
        p1.release()  # second call must be a silent no-op
        assert limiter.in_flight == 0
        # A leaked double-decrement would make this negative / wrap; the
        # scope must be exactly re-acquirable once.
        p2 = limiter.try_acquire("scope-a")
        assert p2 is not None
        assert limiter.in_flight == 1

    def test_max_global_must_be_positive(self):
        with pytest.raises(ValueError):
            SpecialistLimiter(max_global=0)


# ---------------------------------------------------------------------------
# SpecialistTelemetry — pure unit semantics
# ---------------------------------------------------------------------------


class TestSpecialistTelemetryUnit:
    def test_launch_counts_cost_aggregates_independently(self):
        """record_launch counts; record_cost aggregates. A launch that never
        yields a ResultMessage (setup failure) still counts."""
        telem = SpecialistTelemetry()
        telem.record_launch("finance")
        telem.record_cost("finance", cost_usd=0.01,
                          usage={"input_tokens": 100, "output_tokens": 50})
        telem.record_launch("finance")  # counted, but no cost follows
        snap = telem.snapshot("finance")
        assert snap.delegations == 2
        assert snap.total_cost_usd == pytest.approx(0.01)
        assert snap.total_input_tokens == 100
        assert snap.total_output_tokens == 50

    def test_roles_are_independent(self):
        telem = SpecialistTelemetry()
        telem.record_cost("finance", cost_usd=1.0)
        telem.record_cost("mtg", cost_usd=2.0)
        assert telem.snapshot("finance").total_cost_usd == pytest.approx(1.0)
        assert telem.snapshot("mtg").total_cost_usd == pytest.approx(2.0)

    def test_snapshot_unseen_role_is_zeroed(self):
        telem = SpecialistTelemetry()
        snap = telem.snapshot("ghost")
        assert snap.delegations == 0
        assert snap.total_cost_usd == 0.0

    def test_record_denial_increments_denials(self):
        telem = SpecialistTelemetry()
        telem.record_denial("finance", kind="busy")
        telem.record_denial("finance", kind="input_too_large")
        assert telem.snapshot("finance").denials == 2

    def test_warns_past_cost_threshold(self, caplog):
        telem = SpecialistTelemetry(cost_alert_threshold=0.05)
        with caplog.at_level(logging.INFO, logger="specialist_limits"):
            telem.record_cost("finance", cost_usd=0.01)  # under threshold
            assert not any(r.levelno >= logging.WARNING for r in caplog.records)
            caplog.clear()
            telem.record_cost("finance", cost_usd=0.10)  # 0.11 > 0.05
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "specialist_cost_alert" in warnings[0].message
        assert "finance" in warnings[0].message

    def test_no_threshold_never_warns(self, caplog):
        telem = SpecialistTelemetry(cost_alert_threshold=None)
        with caplog.at_level(logging.INFO, logger="specialist_limits"):
            telem.record_cost("finance", cost_usd=1000.0)
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# _run_delegated_agent — ResultMessage capture + telemetry + output bound
# ---------------------------------------------------------------------------


class TestRunDelegatedAgentTelemetry:
    pytestmark = pytest.mark.asyncio

    async def test_run_delegated_agent_aggregates_cost_not_count(
        self, tmp_path, monkeypatch,
    ):
        """_run_delegated_agent aggregates cost from the ResultMessage but
        does NOT increment the launch count (that is the caller's job at
        ownership transfer — see record_launch)."""
        import tools as tm

        telem = SpecialistTelemetry()
        tm, reg, bus = _init_tools(tmp_path, telemetry=telem)

        _FakeSpecialistClient.reset(
            response="ok", total_cost_usd=0.0042,
            usage={"input_tokens": 200, "output_tokens": 80},
        )
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSpecialistClient)
        cfg = _specialist_cfg("finance")
        output = await tm._run_delegated_agent(cfg, "do x", "", resolution=None)
        assert output.text == "ok"

        snap = telem.snapshot("finance")
        assert snap.delegations == 0  # counting is the caller's job
        assert snap.total_cost_usd == pytest.approx(0.0042)
        assert snap.total_input_tokens == 200
        assert snap.total_output_tokens == 80

    async def test_handler_counts_launch_and_aggregates_cost(
        self, tmp_path, monkeypatch,
    ):
        """Full sync path through the handler: the launch is counted once
        (at ownership transfer) AND the ResultMessage cost is aggregated."""
        import tools as tm

        telem = SpecialistTelemetry()
        tm, reg, bus = _init_tools(tmp_path, telemetry=telem)
        _FakeSpecialistClient.reset(
            response="ok", total_cost_usd=0.05,
            usage={"input_tokens": 10, "output_tokens": 5},
        )
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSpecialistClient)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            }),
            _origin(),
        )
        assert json.loads(result["content"][0]["text"])["status"] == "ok"
        snap = telem.snapshot("finance")
        assert snap.delegations == 1
        assert snap.total_cost_usd == pytest.approx(0.05)

    async def test_sync_output_truncated_wire_flag(
        self, tmp_path, monkeypatch, caplog,
    ):
        """The output cap is enforced at the caller and the truncation is
        exposed as a WIRE-LEVEL `output_truncated` flag on the sync result
        (not merely a log line)."""
        import tools as tm
        import specialist_limits as sl

        tm, reg, bus = _init_tools(tmp_path)
        monkeypatch.setattr(sl, "_MAX_OUTPUT_CHARS", 10)

        _FakeSpecialistClient.reset(response="x" * 50)
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSpecialistClient)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "ok"
        assert payload["output_truncated"] is True
        assert len(payload["text"]) == 10
        assert "50 > 10 chars" in caplog.text

    async def test_sync_output_not_truncated_flag_false(
        self, tmp_path, monkeypatch,
    ):
        import tools as tm

        tm, reg, bus = _init_tools(tmp_path)
        _FakeSpecialistClient.reset(response="short")
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSpecialistClient)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["output_truncated"] is False
        assert payload["text"] == "short"


# ---------------------------------------------------------------------------
# delegate_to_agent wiring — concurrency gate + permit release
# ---------------------------------------------------------------------------


class TestConcurrencyWiring:
    pytestmark = pytest.mark.asyncio

    async def test_second_same_scope_delegation_is_busy(self, tmp_path, monkeypatch):
        limiter = SpecialistLimiter(max_global=5)
        telem = SpecialistTelemetry()
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter, telemetry=telem)

        _FakeSpecialistClient.reset(response="slow", delay=0.2)
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSpecialistClient)

        origin = _origin(chat_id="room-1")
        first = asyncio.create_task(_with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t1", "context": "", "mode": "async",
            }),
            origin,
        ))
        await asyncio.sleep(0.02)  # let the first _prelaunch acquire its permit

        second = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t2", "context": "", "mode": "async",
            }),
            origin,
        )
        payload = json.loads(second["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "busy"
        assert telem.snapshot("finance").denials == 1

        first_payload = json.loads((await first)["content"][0]["text"])
        assert first_payload["status"] == "pending"
        await asyncio.sleep(0.3)  # drain the background delegation

    async def test_global_saturation_across_scopes_is_busy(self, tmp_path, monkeypatch):
        limiter = SpecialistLimiter(max_global=1)
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter)

        _FakeSpecialistClient.reset(response="slow", delay=0.2)
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSpecialistClient)

        first = asyncio.create_task(_with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t1", "context": "", "mode": "async",
            }),
            _origin(chat_id="room-1"),
        ))
        await asyncio.sleep(0.02)

        second = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t2", "context": "", "mode": "async",
            }),
            _origin(chat_id="room-2"),  # DIFFERENT scope, same global cap
        )
        payload = json.loads(second["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "busy"

        await first
        await asyncio.sleep(0.3)

    async def test_permit_released_after_normal_completion(self, tmp_path, monkeypatch):
        limiter = SpecialistLimiter(max_global=1)
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter)

        _FakeSpecialistClient.reset(response="fast", delay=0.0)
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSpecialistClient)

        origin = _origin(chat_id="room-1")
        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            }),
            origin,
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "ok"

        # The slot must be free again — a fresh acquire for the SAME scope
        # succeeds now that the delegation's own task released it.
        scope = tm._delegation_scope(origin, "finance")
        permit = limiter.try_acquire(scope)
        assert permit is not None
        permit.release()

    async def test_permit_released_after_voice_deadline_cancellation(
        self, tmp_path, monkeypatch,
    ):
        """Mirrors test_voice_delegation_budget.py's teardown test: a
        specialist that runs past the voice budget is cancelled — the
        permit `_prelaunch` acquired must be released by the task's
        `_permit_release_callback` done-callback, freeing the scope for the
        NEXT acquire."""
        import agent as agent_mod

        limiter = SpecialistLimiter(max_global=1)
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter)

        async def _slow(
            cfg, task_text, context_text, resolution=None, output_format=None,
            tool_counts=None,
        ):
            await asyncio.sleep(10)  # cooperatively cancellable
            return tm.DelegatedOutput(text="too late")

        monkeypatch.setattr(tm, "_run_delegated_agent", _slow)

        deadline = (asyncio.get_running_loop().time()
                    + tm._VOICE_FALLBACK_RESERVE_S + 0.05)
        origin = _origin(channel="voice", chat_id="room-1")
        origin["voice_deadline"] = deadline
        result = await asyncio.wait_for(
            _with_origin(
                tm.delegate_to_agent.handler({
                    "agent": "finance", "task": "t", "context": "", "mode": "sync",
                }),
                origin,
            ),
            timeout=5.0,
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "deadline_exceeded"

        # Cancellation teardown already awaited the task's own unwind
        # (asyncio.wait with a timeout inside _voice_deadline_exceeded), so
        # by the time handler() returned, the permit must already be free.
        scope = tm._delegation_scope(origin, "finance")
        permit = limiter.try_acquire(scope)
        assert permit is not None, (
            "permit was not released on cancellation — scope leaked"
        )
        permit.release()

    # -- Leak-seam regressions (Sol reproduced these: scope stayed held) --

    async def test_progress_sink_cancellation_frees_scope(
        self, tmp_path, monkeypatch,
    ):
        """CRITICAL 1: the permit is acquired in `_prelaunch`, THEN the
        voice progress sink is awaited. A cancellation during that await
        (voice barge-in) must not leak the slot — `_prelaunch` releases on
        BaseException before re-raising."""
        limiter = SpecialistLimiter(max_global=1)
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter)

        entered = asyncio.Event()

        async def _blocking_sink(text: str) -> None:
            entered.set()
            await asyncio.sleep(10)  # block inside _prelaunch's progress await

        async def _never(cfg, *a, **k):  # would run only if we got past prelaunch
            return "x"
        monkeypatch.setattr(tm, "_run_delegated_agent", _never)

        origin = {
            "role": "assistant", "execution_role": "assistant",
            "channel": "voice", "chat_id": "room-1", "cid": "c1",
            "user_text": "hi",
            "voice_deadline": asyncio.get_running_loop().time() + 20.0,
            "_progress_sink": _blocking_sink,
        }
        task = asyncio.create_task(_with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            }),
            origin,
        ))
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        scope = tm._delegation_scope(origin, "finance")
        p = limiter.try_acquire(scope)
        assert p is not None, "permit leaked when cancelled in the progress sink"
        p.release()

    async def test_registration_cancellation_frees_scope(
        self, tmp_path, monkeypatch,
    ):
        """CRITICAL 1: a cancellation during `register_delegation` (an await
        AFTER the permit is acquired, before the task is created) must free
        the slot via the outer `owned` finally."""
        limiter = SpecialistLimiter(max_global=1)
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter)

        entered = asyncio.Event()

        async def _blocking_register(record):
            entered.set()
            await asyncio.sleep(10)
        monkeypatch.setattr(reg, "register_delegation", _blocking_register)

        origin = _origin(chat_id="room-1")
        task = asyncio.create_task(_with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            }),
            origin,
        ))
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        scope = tm._delegation_scope(origin, "finance")
        p = limiter.try_acquire(scope)
        assert p is not None, "permit leaked when cancelled during registration"
        p.release()

    async def test_permit_released_when_task_cancelled_before_start(self):
        """CRITICAL 1: a task cancelled BEFORE its coroutine ever runs has no
        coroutine-`finally` to release the permit — the dedicated
        `_permit_release_callback` done-callback must still fire and free
        the slot."""
        import tools as tm

        limiter = SpecialistLimiter(max_global=1)
        permit = limiter.try_acquire("scope-a")
        assert permit is not None

        async def _never():
            return "x"
        task = asyncio.create_task(_never())
        task.add_done_callback(tm._permit_release_callback(permit))
        task.cancel()  # cancel before the loop runs _never's body
        with pytest.raises(asyncio.CancelledError):
            await task

        p = limiter.try_acquire("scope-a")
        assert p is not None, "done-callback did not release on pre-start cancel"
        p.release()

    async def test_cancel_delegation_does_not_release_permit(self, tmp_path):
        """NEW CRITICAL: SpecialistRegistry.cancel_delegation must NOT release
        the permit. Voice teardown calls it after only a bounded wait while
        the specialist task may still be unwinding — releasing here would free
        the slot for a NEW delegation while the original still executes. The
        task's done-callback is the SOLE authoritative release (it fires only
        when the task ACTUALLY ends)."""
        limiter = SpecialistLimiter(max_global=1)
        reg = SpecialistRegistry(
            str(tmp_path / "specs"), tombstone_path=str(tmp_path / "d.json"))
        scope = "room-1:finance"
        permit = limiter.try_acquire(scope)
        assert permit is not None

        rec = DelegationRecord(
            id="d1", agent="finance", started_at=0.0, origin={})
        rec.permit = permit
        await reg.register_delegation(rec)
        await reg.cancel_delegation("d1")

        # The slot must STILL be held — cancel_delegation released nothing.
        assert limiter.try_acquire(scope) is None, (
            "cancel_delegation prematurely released the permit while the task "
            "may still be running"
        )
        permit.release()  # cleanup (simulates the real done-callback firing)

    async def test_complete_and_fail_delegation_do_not_release_permit(
        self, tmp_path,
    ):
        """complete/fail_delegation are likewise not the release point (the
        done-callback is) — they must leave the slot held."""
        limiter = SpecialistLimiter(max_global=2)
        reg = SpecialistRegistry(
            str(tmp_path / "specs"), tombstone_path=str(tmp_path / "d.json"))

        p1 = limiter.try_acquire("a:finance")
        r1 = DelegationRecord(id="d1", agent="finance", started_at=0.0, origin={})
        r1.permit = p1
        await reg.register_delegation(r1)
        await reg.complete_delegation("d1")
        assert limiter.try_acquire("a:finance") is None
        p1.release()

        p2 = limiter.try_acquire("b:finance")
        r2 = DelegationRecord(id="d2", agent="finance", started_at=0.0, origin={})
        r2.permit = p2
        await reg.register_delegation(r2)
        await reg.fail_delegation("d2", RuntimeError("x"))
        assert limiter.try_acquire("b:finance") is None
        p2.release()

    async def test_telemetry_raise_does_not_release_live_permit(
        self, tmp_path, monkeypatch,
    ):
        """IMPORTANT 1: `owned` is cleared the instant ownership transfers,
        BEFORE the launch count, and the count is non-raising. So even if
        record_launch raises, the outer finally must NOT release the now-live
        task's permit, and the launch still succeeds (returns pending)."""
        from unittest.mock import MagicMock

        limiter = SpecialistLimiter(max_global=1)
        telem = SpecialistTelemetry()
        monkeypatch.setattr(
            telem, "record_launch",
            MagicMock(side_effect=RuntimeError("telemetry boom")))
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter, telemetry=telem)

        _FakeSpecialistClient.reset(response="slow", delay=0.2)
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSpecialistClient)

        origin = _origin(chat_id="room-1")
        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "async",
            }),
            origin,
        )
        payload = json.loads(result["content"][0]["text"])
        # Launch succeeded despite the telemetry raise (guarded).
        assert payload["status"] == "pending"
        # Task is still running (0.2s) — the permit must still be HELD.
        scope = tm._delegation_scope(origin, "finance")
        assert limiter.try_acquire(scope) is None, (
            "telemetry raise leaked/released the live task's permit"
        )
        await asyncio.sleep(0.3)  # let the task finish → done-callback frees


# ---------------------------------------------------------------------------
# Interactive engagement — permit handoff + release
# ---------------------------------------------------------------------------


class TestInteractiveEngagementPermitRelease:
    pytestmark = pytest.mark.asyncio

    async def test_finalize_engagement_releases_permit(self, tmp_path):
        """The exact release point `_finalize_engagement` runs after winning
        the terminal transition — covers emit_completion / cancel_engagement
        / reap alike, since they all funnel through it."""
        import tools as tm
        from engagement_registry import EngagementRegistry

        limiter = SpecialistLimiter(max_global=1)
        eng_reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        tm.init_tools(
            channel_manager=None, bus=None,
            specialist_registry=SpecialistRegistry(
                str(tmp_path / "specs"), tombstone_path=str(tmp_path / "d.json")),
            engagement_registry=eng_reg,
            agent_role_map={"assistant": _caller_cfg()},
            specialist_limiter=limiter,
        )

        scope = "room-1:finance"
        permit = limiter.try_acquire(scope)
        assert permit is not None
        assert limiter.try_acquire(scope) is None  # sanity: scope is held

        rec = await eng_reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin=_origin(chat_id="room-1"), topic_id=None,
        )
        rec.permit = permit

        won = await tm._finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=None,
        )
        assert bool(won) is True   # FinalizeResult.FINALIZED (G4 D5)

        # Released — the scope is acquirable again.
        again = limiter.try_acquire(scope)
        assert again is not None
        again.release()

    async def test_interactive_mode_hands_permit_to_engagement_record(
        self, tmp_path,
    ):
        """delegate_to_agent(mode="interactive") transfers the permit
        acquired in _prelaunch onto the just-created engagement record
        rather than releasing it early."""
        import agent as agent_mod
        from unittest.mock import AsyncMock, MagicMock
        from engagement_registry import EngagementRegistry
        import tools as tm

        limiter = SpecialistLimiter(max_global=1)
        eng_reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        tch = MagicMock()
        tch.engagement_permission_ok = True
        tch.engagement_supergroup_id = -1001
        tch.open_engagement_topic = AsyncMock(return_value=555)
        tch.send_to_topic = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = tch
        bus = MagicMock()
        bus.notify = AsyncMock()
        tm.init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=SpecialistRegistry(
                str(tmp_path / "specs"), tombstone_path=str(tmp_path / "d.json")),
            engagement_registry=eng_reg,
            agent_role_map={
                "assistant": _caller_cfg(),
                "finance": _specialist_cfg("finance"),
            },
            specialist_limiter=limiter,
        )
        driver = MagicMock()
        driver.start = AsyncMock()
        agent_mod.active_engagement_driver = driver

        origin = _origin(chat_id="room-1")
        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "Plan Q2", "context": "",
                "mode": "interactive",
            }),
            origin,
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "pending"

        rec = eng_reg.by_topic_id(555)
        assert rec is not None
        assert rec.permit is not None
        # The scope is held — a second interactive attempt for the same
        # specialist from the same room must be denied as busy.
        second = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "second", "context": "",
                "mode": "interactive",
            }),
            origin,
        )
        second_payload = json.loads(second["content"][0]["text"])
        assert second_payload["kind"] == "engagement_busy"

        # Cleanup: release directly (bypassing the full finalize funnel —
        # already covered by test_finalize_engagement_releases_permit).
        rec.permit.release()

    async def test_driver_start_failure_releases_permit_inline(self, tmp_path):
        """A pre-finalize failure (driver.start raises) never reaches
        _finalize_engagement — delegate_to_agent must release the permit
        itself at the point of failure."""
        import agent as agent_mod
        from unittest.mock import AsyncMock, MagicMock
        from engagement_registry import EngagementRegistry
        import tools as tm

        limiter = SpecialistLimiter(max_global=1)
        eng_reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        tch = MagicMock()
        tch.engagement_permission_ok = True
        tch.engagement_supergroup_id = -1001
        tch.open_engagement_topic = AsyncMock(return_value=555)
        tch.send_to_topic = AsyncMock()
        tch.update_topic_state = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = tch
        bus = MagicMock()
        bus.notify = AsyncMock()
        tm.init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=SpecialistRegistry(
                str(tmp_path / "specs"), tombstone_path=str(tmp_path / "d.json")),
            engagement_registry=eng_reg,
            agent_role_map={
                "assistant": _caller_cfg(),
                "finance": _specialist_cfg("finance"),
            },
            specialist_limiter=limiter,
        )
        driver = MagicMock()
        driver.start = AsyncMock(side_effect=RuntimeError("boom"))
        agent_mod.active_engagement_driver = driver

        origin = _origin(chat_id="room-1")
        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "Plan Q2", "context": "",
                "mode": "interactive",
            }),
            origin,
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "driver_start_failed"

        scope = tm._delegation_scope(origin, "finance")
        permit = limiter.try_acquire(scope)
        assert permit is not None, "permit leaked on driver_start_failed"
        permit.release()

    async def test_direct_mark_error_releases_permit(self, tmp_path):
        """CRITICAL 2: a DIRECT `mark_error` (resume/orphan failure route in
        channels/telegram.py) makes the engagement terminal WITHOUT going
        through `_finalize_engagement`. The permit must still be released by
        the registry terminal transition itself, or the scope leaks (Sol
        reproduced: a later `_finalize_engagement` can't recover because
        `try_transition_terminal` returns False for an already-terminal
        record)."""
        from engagement_registry import EngagementRegistry

        limiter = SpecialistLimiter(max_global=1)
        eng_reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        scope = "room-1:finance"
        permit = limiter.try_acquire(scope)
        assert permit is not None
        assert limiter.try_acquire(scope) is None  # scope held

        rec = await eng_reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin=_origin(chat_id="room-1"), topic_id=None,
        )
        rec.permit = permit

        # Direct terminal transition — bypasses _finalize_engagement entirely.
        await eng_reg.mark_error(rec.id, kind="resume_failed", message="boom")

        again = limiter.try_acquire(scope)
        assert again is not None, "mark_error did not release the permit"
        again.release()

    async def test_interactive_result_observer_aggregates_cost(self):
        """IMPORTANT 2: InCasaDriver calls its `result_observer` with the
        engagement + ResultMessage on every turn, so interactive specialist
        cost reaches SpecialistTelemetry (the ephemeral path is covered by
        _run_delegated_agent). Verifies the driver seam + a telemetry-shaped
        observer end to end."""
        from drivers.in_casa_driver import InCasaDriver
        from engagement_registry import EngagementRecord

        telem = SpecialistTelemetry()

        def _observer(engagement, result_msg):
            if getattr(engagement, "kind", "") != "specialist":
                return
            from tokens import extract_usage
            telem.record_cost(
                engagement.role_or_type,
                cost_usd=float(getattr(result_msg, "total_cost_usd", 0.0) or 0.0),
                usage=extract_usage(result_msg),
            )

        drv = InCasaDriver(
            topic_stream_factory=lambda tid: _FakeTopicStream(),
            result_observer=_observer,
        )

        rec = EngagementRecord(
            id="e1", kind="specialist", role_or_type="finance",
            driver="in_casa", status="active", topic_id=99,
            started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None, origin={}, task="t",
        )
        client = _FakeDriverClient(
            total_cost_usd=0.07, usage={"input_tokens": 30, "output_tokens": 9})
        drv._clients["e1"] = client
        drv._locks["e1"] = asyncio.Lock()

        await drv._deliver_turn(rec, "hello")

        snap = telem.snapshot("finance")
        assert snap.total_cost_usd == pytest.approx(0.07)
        assert snap.total_input_tokens == 30

    async def test_interactive_specialist_stream_output_capped(
        self, monkeypatch,
    ):
        """IMPORTANT 2: InCasaDriver caps each interactive SPECIALIST turn's
        accumulated/emitted assistant text at `_MAX_OUTPUT_CHARS`, appends a
        truncation marker, and persists a flag — the stream was otherwise
        unbounded before emit_completion."""
        import specialist_limits as sl
        from drivers.in_casa_driver import InCasaDriver
        from engagement_registry import EngagementRecord

        monkeypatch.setattr(sl, "_MAX_OUTPUT_CHARS", 20)
        stream = _CapturingStream()
        drv = InCasaDriver(topic_stream_factory=lambda tid: stream)

        rec = EngagementRecord(
            id="e1", kind="specialist", role_or_type="finance",
            driver="in_casa", status="active", topic_id=99,
            started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None, origin={}, task="t",
        )
        drv._clients["e1"] = _FakeDriverClient(text="x" * 100)
        drv._locks["e1"] = asyncio.Lock()

        await drv._deliver_turn(rec, "hello")

        assert stream.final is not None
        assert stream.final.endswith("[truncated]")
        assert len(stream.final) <= 20 + len(" … [truncated]") + 2
        assert rec.origin.get("stream_output_truncated") is True

    async def test_interactive_executor_stream_not_capped(self, monkeypatch):
        """The stream cap is specialist-only — executor engagements stream
        unrestricted (their output surface is a different concern)."""
        import specialist_limits as sl
        from drivers.in_casa_driver import InCasaDriver
        from engagement_registry import EngagementRecord

        monkeypatch.setattr(sl, "_MAX_OUTPUT_CHARS", 20)
        stream = _CapturingStream()
        drv = InCasaDriver(topic_stream_factory=lambda tid: stream)

        rec = EngagementRecord(
            id="e2", kind="executor", role_or_type="configurator",
            driver="in_casa", status="active", topic_id=99,
            started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None, origin={}, task="t",
        )
        drv._clients["e2"] = _FakeDriverClient(text="y" * 100)
        drv._locks["e2"] = asyncio.Lock()

        await drv._deliver_turn(rec, "hello")

        assert stream.final == "y" * 100  # not truncated
        assert "stream_output_truncated" not in rec.origin


class _FakeTopicStream:
    async def emit(self, text):  # noqa: D401
        pass

    async def finalize(self, text):
        pass


class _CapturingStream:
    def __init__(self):
        self.emitted: list[str] = []
        self.final: str | None = None

    async def emit(self, text):
        self.emitted.append(text)

    async def finalize(self, text):
        self.final = text


class _FakeDriverClient:
    """Minimal client for InCasaDriver._deliver_turn: yields one
    AssistantMessage (``text``) then a ResultMessage carrying cost/usage."""

    def __init__(self, *, total_cost_usd=None, usage=None, text="done"):
        self._total_cost_usd = total_cost_usd
        self._usage = usage
        self._text = text

    async def query(self, prompt):
        self._prompt = prompt

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        try:
            block = TextBlock(text=self._text)
        except TypeError:
            block = TextBlock(self._text)  # type: ignore[call-arg]
        try:
            asst = AssistantMessage(content=[block])
        except TypeError:
            asst = AssistantMessage.__new__(AssistantMessage)
            asst.content = [block]  # type: ignore[attr-defined]
        yield asst
        yield _mk_result_message(
            total_cost_usd=self._total_cost_usd, usage=self._usage)


# ---------------------------------------------------------------------------
# Input bounds
# ---------------------------------------------------------------------------


class TestInputBounds:
    pytestmark = pytest.mark.asyncio

    async def test_task_too_large_is_rejected(self, tmp_path, monkeypatch):
        import specialist_limits as sl

        telem = SpecialistTelemetry()
        tm, reg, bus = _init_tools(tmp_path, telemetry=telem)
        monkeypatch.setattr(sl, "_MAX_TASK_CHARS", 10)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "x" * 11, "context": "",
                "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "input_too_large"
        assert payload["field"] == "task"
        assert telem.snapshot("finance").denials == 1

    async def test_context_too_large_is_rejected(self, tmp_path, monkeypatch):
        import specialist_limits as sl

        tm, reg, bus = _init_tools(tmp_path)
        monkeypatch.setattr(sl, "_MAX_CONTEXT_CHARS", 10)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "ok", "context": "x" * 11,
                "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "input_too_large"
        assert payload["field"] == "context"

    async def test_task_within_bounds_is_not_rejected_for_size(
        self, tmp_path, monkeypatch,
    ):
        """Negative control: a task under the cap never hits input_too_large
        (it may still fail for unrelated reasons in this minimal fixture,
        but never with kind == input_too_large)."""
        import specialist_limits as sl

        tm, reg, bus = _init_tools(tmp_path)
        monkeypatch.setattr(sl, "_MAX_TASK_CHARS", 10)
        _FakeSpecialistClient.reset(response="ok")
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSpecialistClient)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "short", "context": "",
                "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload.get("kind") != "input_too_large"

    async def test_input_bounds_run_after_acl(self, tmp_path, monkeypatch):
        """FINAL-REVIEW: the input-size check runs AFTER the ACL. An
        unauthorized caller sending oversized input must be denied
        `delegation_not_declared` (never `input_too_large` — which would both
        leak the size gate to an unknown caller AND record target-keyed
        telemetry on a caller-supplied `args["agent"]` BEFORE authorization).
        No telemetry counter may move."""
        import specialist_limits as sl

        telem = SpecialistTelemetry()
        # Caller "assistant" declares ONLY "finance"; it delegates to "ghost".
        tm, reg, bus = _init_tools(tmp_path, telemetry=telem, agent_role_map={
            "assistant": _caller_cfg(delegates=("finance",)),
            "finance": _specialist_cfg("finance"),
        })
        monkeypatch.setattr(sl, "_MAX_TASK_CHARS", 10)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "ghost", "task": "x" * 500, "context": "",
                "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "delegation_not_declared"
        # No telemetry keyed on the caller-supplied target pre-auth.
        assert telem.snapshot("ghost").denials == 0
        assert telem.snapshot("finance").denials == 0

    async def test_missing_origin_oversized_input_is_not_declared(
        self, tmp_path, monkeypatch,
    ):
        """A missing origin (called outside a turn) + oversized input →
        `delegation_not_declared` (the ACL's unknown-caller denial), not
        `input_too_large`, and no counter moves."""
        import specialist_limits as sl

        telem = SpecialistTelemetry()
        tm, reg, bus = _init_tools(tmp_path, telemetry=telem)
        monkeypatch.setattr(sl, "_MAX_TASK_CHARS", 10)

        # NOT wrapped in _with_origin — origin_var stays None.
        result = await tm.delegate_to_agent.handler({
            "agent": "finance", "task": "x" * 500, "context": "",
            "mode": "sync",
        })
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "delegation_not_declared"
        assert telem.snapshot("finance").denials == 0
