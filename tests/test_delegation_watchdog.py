"""S-2 (block-S live finding 2026-07-15): wall-clock ceiling on launched
sync/async delegation tasks.

Live repro: async delegation 07bfeb0b repetition-looped >12 minutes holding
its per-scope Permit + a global slot; sync PINGs kept getting `busy`; only
killing the CLI subprocess failed the delegation. Sync has 60s-then-pending
and voice has the ingress deadline, but once a task is launched in async
mode (or degrades from sync to pending) nothing bounds its wall-clock — at
`specialist_max_concurrency=2` two runaways brick fleet-wide delegation.

The fix under test: `_run_delegated_agent_bounded` wraps the delegated turn
so that a task exceeding `_DELEGATION_CEILING_S` is cancelled (bounded
teardown wait, mirroring the voice path) and then FAILS with a
`DelegationCeilingExceeded` (an `asyncio.TimeoutError` subclass →
`_classify_error` → typed kind "timeout") — deliberately an exception, not
a bare cancel: `_attach_completion_callback`'s cancelled-branch posts NO
notification, while the exception branch runs the exact fail path the live
kill-subprocess probe proved correct (fail_delegation + DelegationComplete
error notify to the engager + permit release via the task done-callback).
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging

import pytest

from bus import MessageBus, MessageType
from channels import ChannelManager
from config import (
    AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig, SessionConfig,
    ToolsConfig,
)
from specialist_limits import SpecialistLimiter
from specialist_registry import DelegationComplete, SpecialistRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


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


def _caller_cfg(role: str = "assistant") -> AgentConfig:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
    cfg.delegates = [DelegateEntry(agent="finance", purpose="p", when="w")]
    return cfg


def _origin(role="assistant", channel="telegram", chat_id="room-1") -> dict:
    return {
        "role": role, "execution_role": role, "channel": channel,
        "chat_id": chat_id, "cid": "c1", "user_text": "please do X",
    }


async def _with_origin(coro, origin: dict):
    import agent as agent_mod
    token = agent_mod.origin_var.set(origin)
    try:
        return await coro
    finally:
        agent_mod.origin_var.reset(token)


def _init_tools(tmp_path, *, limiter=None):
    import tools as tm
    reg = SpecialistRegistry(str(tmp_path / "specs"),
                             tombstone_path=str(tmp_path / "del.json"))
    bus = MessageBus()
    bus.register("assistant", None)
    cm = ChannelManager()
    tm.init_tools(
        cm, bus, reg, agent_role_map={
            "assistant": _caller_cfg(),
            "finance": _specialist_cfg("finance"),
        },
        specialist_limiter=limiter,
    )
    return tm, reg, bus


async def _poll_notification(bus: MessageBus, *, attempts=100, sleep_s=0.02):
    """Poll Ellen's queue for the first NOTIFICATION BusMessage."""
    for _ in range(attempts):
        if not bus.queues["assistant"].empty():
            _pri, _seq, m = await bus.queues["assistant"].get()
            if m.type == MessageType.NOTIFICATION:
                return m
        await asyncio.sleep(sleep_s)
    return None


class TestDelegationCeiling:
    @pytest.mark.parametrize(
        ("unwind_s", "teardown_bound_s"),
        [(0.0, 0.5), (0.08, 0.01)],
        ids=("within-bound", "survivor"),
    )
    async def test_outer_cancel_retrieves_private_inner_exception(
        self, tmp_path, monkeypatch, caplog, unwind_s, teardown_bound_s,
    ):
        """Cancelled bounded runners retrieve both prompt and late failures."""
        import tools as tm

        tm, _reg, _bus = _init_tools(tmp_path)
        private_canary = "PRIVATE-BOUNDED-CANCEL-CANARY-91e7"
        started = asyncio.Event()

        async def _raise_after_cancel(
            cfg, task_text, context_text, resolution=None, output_format=None,
            tool_counts=None,
        ):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if unwind_s:
                    await asyncio.sleep(unwind_s)
                raise RuntimeError(private_canary)

        monkeypatch.setattr(tm, "_run_delegated_agent", _raise_after_cancel)
        monkeypatch.setattr(tm, "_DELEGATION_CEILING_S", 30.0)
        monkeypatch.setattr(
            tm, "_CEILING_TEARDOWN_BOUND_S", teardown_bound_s,
        )

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop_contexts: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: loop_contexts.append(context))
        try:
            async def _cancel_and_forget() -> None:
                outer = asyncio.create_task(tm._run_delegated_agent_bounded(
                    _specialist_cfg(), "private", "",
                    output_format=tm.VOICE_JOB_OUTPUT_FORMAT,
                ))
                await started.wait()
                outer.cancel()
                try:
                    await outer
                except asyncio.CancelledError:
                    pass

            with caplog.at_level(logging.DEBUG):
                await _cancel_and_forget()
                await asyncio.sleep(unwind_s + 0.05)
                for _ in range(3):
                    gc.collect()
                    await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        assert private_canary not in repr(loop_contexts)
        assert private_canary not in caplog.text

    async def test_async_runaway_hits_ceiling_typed_failure_and_release(
        self, tmp_path, monkeypatch,
    ):
        """The S.6 live repro, unit-scale: an async delegation that never
        returns must — WITHOUT anyone killing a subprocess — end in a typed
        error NOTIFICATION to the engager, actually cancel the delegated
        work, and release the concurrency permit."""
        import tools as tm

        limiter = SpecialistLimiter(max_global=1)
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter)

        cancelled = asyncio.Event()

        async def _runaway(
            cfg, task_text, context_text, resolution=None, output_format=None,
            tool_counts=None,
        ):
            try:
                await asyncio.Event().wait()  # never set — hangs forever
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return tm.DelegatedOutput(text="unreachable")

        monkeypatch.setattr(tm, "_run_delegated_agent", _runaway)
        monkeypatch.setattr(tm, "_DELEGATION_CEILING_S", 0.1, raising=False)
        monkeypatch.setattr(
            tm, "_CEILING_TEARDOWN_BOUND_S", 0.5, raising=False)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "EMIT:15000", "context": "",
                "mode": "async",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "pending"
        delegation_id = payload["delegation_id"]

        found = await _poll_notification(bus)
        assert found is not None, (
            "no NOTIFICATION arrived — the runaway async delegation was "
            "never failed by a wall-clock ceiling"
        )
        assert isinstance(found.content, DelegationComplete)
        assert found.content.status == "error"
        assert found.content.kind == "timeout"
        assert found.content.delegation_id == delegation_id
        # The delegated work itself was cancelled (subprocess teardown path).
        assert cancelled.is_set()
        # Permit released — the scope AND global slot are free again.
        assert limiter.in_flight == 0

    async def test_async_completing_before_ceiling_is_unaffected(
        self, tmp_path, monkeypatch,
    ):
        import tools as tm

        limiter = SpecialistLimiter(max_global=1)
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter)

        async def _quick(
            cfg, task_text, context_text, resolution=None, output_format=None,
            tool_counts=None,
        ):
            await asyncio.sleep(0.01)
            return tm.DelegatedOutput(text="done quickly")

        monkeypatch.setattr(tm, "_run_delegated_agent", _quick)
        monkeypatch.setattr(tm, "_DELEGATION_CEILING_S", 5.0, raising=False)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "",
                "mode": "async",
            }),
            _origin(),
        )
        assert json.loads(result["content"][0]["text"])["status"] == "pending"

        found = await _poll_notification(bus)
        assert found is not None
        assert found.content.status == "ok"
        assert found.content.text == "done quickly"
        assert limiter.in_flight == 0

    async def test_sync_degraded_to_pending_is_also_bounded(
        self, tmp_path, monkeypatch,
    ):
        """A sync delegation that degrades to pending at the 60s wait is the
        same unbounded shape as async — the ceiling must cover it too."""
        import tools as tm

        limiter = SpecialistLimiter(max_global=1)
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter)

        async def _runaway(
            cfg, task_text, context_text, resolution=None, output_format=None,
            tool_counts=None,
        ):
            await asyncio.Event().wait()
            return tm.DelegatedOutput(text="unreachable")

        monkeypatch.setattr(tm, "_run_delegated_agent", _runaway)
        monkeypatch.setattr(tm, "_SYNC_WAIT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(tm, "_DELEGATION_CEILING_S", 0.1, raising=False)
        monkeypatch.setattr(
            tm, "_CEILING_TEARDOWN_BOUND_S", 0.5, raising=False)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "",
                "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "pending"  # degraded at the sync wait

        found = await _poll_notification(bus)
        assert found is not None
        assert found.content.status == "error"
        assert found.content.kind == "timeout"
        assert limiter.in_flight == 0

    async def test_inner_exception_still_propagates_unchanged(
        self, tmp_path, monkeypatch,
    ):
        """The bounding wrapper must be transparent to a delegated turn that
        fails on its own — same classified kind as before the ceiling."""
        import tools as tm

        tm, reg, bus = _init_tools(tmp_path)

        async def _boom(
            cfg, task_text, context_text, resolution=None, output_format=None,
            tool_counts=None,
        ):
            raise RuntimeError("model exploded")

        monkeypatch.setattr(tm, "_run_delegated_agent", _boom)
        monkeypatch.setattr(tm, "_DELEGATION_CEILING_S", 5.0, raising=False)

        result = await _with_origin(
            tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "",
                "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown"
        assert "model exploded" in payload["message"]

    async def test_repeated_outer_cancel_does_not_free_permit_early(
        self, tmp_path, monkeypatch,
    ):
        """Codex review finding 2: a SECOND cancel of the outer task (e.g.
        voice deadline teardown overlapping shutdown sweep) must not
        interrupt the inner-unwind wait and free the permit while the
        delegated work is still running."""
        import tools as tm

        limiter = SpecialistLimiter(max_global=1)
        tm, reg, bus = _init_tools(tmp_path, limiter=limiter)

        unwound = asyncio.Event()

        async def _slow_unwind(
            cfg, task_text, context_text, resolution=None, output_format=None,
            tool_counts=None,
        ):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Simulate SDK client teardown taking real time.
                await asyncio.sleep(0.2)
                unwound.set()
                raise
            return tm.DelegatedOutput(text="unreachable")

        monkeypatch.setattr(tm, "_run_delegated_agent", _slow_unwind)
        monkeypatch.setattr(tm, "_DELEGATION_CEILING_S", 30.0, raising=False)
        monkeypatch.setattr(
            tm, "_CEILING_TEARDOWN_BOUND_S", 5.0, raising=False)

        origin = _origin()
        import agent as agent_mod
        token = agent_mod.origin_var.set(origin)
        try:
            outer = asyncio.create_task(
                tm._run_delegated_agent_bounded(
                    _specialist_cfg("finance"), "t", ""))
        finally:
            agent_mod.origin_var.reset(token)
        permit = limiter.try_acquire("probe-scope")
        assert permit is not None
        outer.add_done_callback(tm._permit_release_callback(permit))

        await asyncio.sleep(0.05)  # let inner start
        outer.cancel()
        await asyncio.sleep(0.05)
        outer.cancel()  # the overlapping second cancel
        await asyncio.sleep(0.05)
        # Inner still unwinding (its 0.2s teardown sleep) — the permit must
        # still be held.
        assert not unwound.is_set()
        assert limiter.in_flight == 1, (
            "permit freed while the delegated work was still unwinding"
        )

        with pytest.raises(asyncio.CancelledError):
            await outer
        assert unwound.is_set()
        assert limiter.in_flight == 0

    async def test_ceiling_default_is_sane(self):
        """The shipped constant: minutes-scale (legit specialist turns run
        minutes), strictly greater than the sync degrade wait, bounded well
        under the >12-minute live runaway."""
        import tools as tm

        ceiling = getattr(tm, "_DELEGATION_CEILING_S", None)
        assert ceiling is not None, "_DELEGATION_CEILING_S missing"
        assert ceiling > tm._SYNC_WAIT_TIMEOUT_S
        assert 120.0 <= ceiling <= 720.0
