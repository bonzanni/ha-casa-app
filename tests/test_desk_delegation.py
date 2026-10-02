"""S4 §8: a delegation the resident launches from an operator DM to a
specialist is one use of that specialist's desk — sync, degraded or async —
under the desk lock from the log read to the exchange's commit: the desk
block is fitted into the context after the resident's own validated
arguments, the exchange is appended on completion, a full queue is the
tool's typed busy result with no task (INV-DESK-003).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import result_broker as rb
import specialist_desk as sd
import tools as tools_mod
from test_delegate_to_agent import (
    STUB_ROLE_ARTIFACT, AgentConfig, ChannelManager, MessageBus, SpecialistRegistry,
    _caller_cfg, _seed_specialist_dir, _use_synthetic_roles_dir, _with_origin,
)

pytestmark = pytest.mark.asyncio

OPERATOR = 42


def _dm_origin(**over):
    base = {"role": "assistant", "execution_role": "assistant", "channel": "telegram",
            "source": "telegram", "message_type": "channel_in", "chat_id": str(OPERATOR),
            "user_id": OPERATOR, "cid": "c1", "user_text": "please do X",
            "_operator_turn": True}
    base.update(over)
    return base


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr(sd, "DESKS", sd.DeskRegistry(now=lambda: clock["t"]))
    monkeypatch.setattr(rb, "POSTS", rb.PostLedger())
    specialists = tmp_path / "ex"
    specialists.mkdir()
    _seed_specialist_dir(specialists, "finance", enabled=True)
    _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
    reg = SpecialistRegistry(str(specialists), tombstone_path=str(tmp_path / "del.json"))
    reg.load()
    from tools import init_tools
    init_tools(ChannelManager(), MessageBus(), reg,
               agent_role_map={"assistant": _caller_cfg(delegates=("finance",)),
                               # production merges specialists into the map with their
                               # loaded kind; the desk gate reads it (round 6)
                               "finance": AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
                                                      role="finance", kind="specialist")})
    calls = []
    env = SimpleNamespace(clock=clock, calls=calls, respond=None, gate=None)

    async def runner(cfg, task_text, context_text, resolution=None, output_format=None):
        calls.append(SimpleNamespace(task=task_text, context=context_text))
        if env.gate is not None:
            await env.gate.wait()
        return tools_mod.DelegatedOutput(text=f"answer {len(calls)}")
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", runner)
    return env


def _payload(result):
    return json.loads(result["content"][0]["text"])


async def _delegate(task="draft invoice", context="lesina march", mode="sync", origin=None):
    from tools import delegate_to_agent
    return _payload(await _with_origin(
        delegate_to_agent.handler({"agent": "finance", "task": task, "context": context,
                                   "mode": mode}),
        origin if origin is not None else _dm_origin()))


async def test_a_sync_delegation_from_the_operator_dm_reads_and_writes_the_desk(env):
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    desk.append("operator", "earlier words", now=4990.0)
    payload = await _delegate()
    assert payload["status"] == "ok" and payload["text"] == "answer 1"
    (call,) = env.calls
    assert call.task == "draft invoice"
    assert call.context.startswith("lesina march")                 # the resident's context first
    assert "<desk>" in call.context and "earlier words" in call.context
    assert [(e.who, e.text) for e in desk.log] == [
        ("operator", "earlier words"), ("resident", "draft invoice"), ("specialist", "answer 1")]
    assert desk.last_used == 5000.0 and not desk.lock.locked() and desk.waiting == 0


async def test_the_thread_begins_with_the_first_delegation(env):
    assert sd.DESKS.get(OPERATOR, "finance") is None
    await _delegate()
    desk = sd.DESKS.get(OPERATOR, "finance")
    assert desk is not None and [e.who for e in desk.log] == ["resident", "specialist"]
    assert "<desk>" not in env.calls[0].context                     # nothing to read yet


@pytest.mark.parametrize("origin", [
    _dm_origin(_operator_turn=False), {"role": "assistant", "channel": "telegram", "chat_id": "x",
                                      "cid": "c1", "user_text": "q"},
    _dm_origin(channel="webhook", source="webhook"),
], ids=["not-operator", "no-chat", "webhook"])
async def test_a_delegation_from_anywhere_else_touches_no_desk(env, origin):
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    desk.append("operator", "earlier words", now=4990.0)
    payload = await _delegate(origin=origin)
    assert payload["status"] == "ok"
    assert "<desk>" not in env.calls[0].context
    assert [e.text for e in desk.log] == ["earlier words"]


async def test_a_full_queue_is_the_typed_busy_result_with_no_task(env):
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    desk.waiting = sd.DESK_QUEUE_MAX
    payload = await _delegate()
    assert payload == {**payload, "status": "error", "kind": "busy", "agent": "finance"}
    assert env.calls == [] and desk.waiting == sd.DESK_QUEUE_MAX and desk.log == []


async def test_an_async_delegation_waits_for_the_desk_and_still_returns_pending(env):
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    await desk.lock.acquire()                                    # a desk turn is running
    payload = await _delegate(mode="async")
    assert payload["status"] == "pending"
    await asyncio.sleep(0.05)
    assert env.calls == [] and desk.waiting == 1                 # waiting, not running
    desk.append("operator", "while you waited", now=5000.0)
    desk.lock.release()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if desk.log and desk.log[-1].who == "specialist":
            break
    assert "while you waited" in env.calls[0].context             # read AFTER the holder committed
    assert [e.who for e in desk.log] == ["operator", "resident", "specialist"]
    assert desk.waiting == 0 and not desk.lock.locked()


async def test_the_combined_context_never_exceeds_the_desk_cap(env):
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    for i in range(12):
        desk.append("operator" if i % 2 == 0 else "specialist", f"{i:02d}" + "x" * 398, now=4990.0)
    big = "c" * 8000
    payload = await _delegate(context=big)
    assert payload["status"] == "ok"
    (call,) = env.calls
    assert call.context.startswith(big) and len(call.context) <= sd.DESK_CONTEXT_CHARS
    assert "11" + "x" * 398 in call.context                        # newest exchanges kept


# --- round 1 folds -------------------------------------------------------------------

async def test_a_delegation_cancelled_before_its_task_releases_the_reservation(env, monkeypatch):
    from tools import delegate_to_agent
    gate = asyncio.Event()

    async def _blocked_register(record):
        await gate.wait()
    monkeypatch.setattr(tools_mod._specialist_registry, "register_delegation", _blocked_register)
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    task = asyncio.create_task(_with_origin(
        delegate_to_agent.handler({"agent": "finance", "task": "t", "context": "", "mode": "sync"}),
        _dm_origin()))
    await asyncio.sleep(0.02)
    assert desk.waiting == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert desk.waiting == 0 and env.calls == []


async def test_a_delegation_behind_a_running_desk_turn_queues_instead_of_being_refused(env, monkeypatch):
    """The desk turn holds the (chat, specialist) permit; the resident's
    delegation must queue behind it (lock first, permit after), not be
    refused busy by the permit it has no business holding yet."""
    from specialist_limits import SpecialistLimiter
    limiter = SpecialistLimiter(max_global=2)
    monkeypatch.setattr(tools_mod, "_specialist_limiter", limiter)
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    await desk.lock.acquire()
    held = limiter.try_acquire(tools_mod._delegation_scope(_dm_origin(), "finance"))
    assert held is not None
    payload = await _delegate(mode="async")
    assert payload["status"] == "pending" and env.calls == []
    held.release()
    desk.lock.release()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if desk.log:
            break
    assert len(env.calls) == 1 and [e.who for e in desk.log] == ["resident", "specialist"]
    assert limiter.try_acquire(tools_mod._delegation_scope(_dm_origin(), "finance")) is not None


async def test_the_delegation_permit_is_released_inside_the_desk_lock(env, monkeypatch):
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    seen = {}

    class _Permit:
        def __init__(self):
            self.released = False

        def release(self):
            if not self.released:
                self.released = True
                seen["under_lock"] = desk.lock.locked()

        @property
        def scope(self):
            return f"{OPERATOR}:finance"

    class _Limiter:
        def try_acquire(self, scope):
            return _Permit()
    monkeypatch.setattr(tools_mod, "_specialist_limiter", _Limiter())
    payload = await _delegate()
    assert payload["status"] == "ok" and seen == {"under_lock": True}


async def test_the_delegation_exchange_is_stamped_at_completion(env, monkeypatch):
    async def runner(cfg, task_text, context_text, resolution=None, output_format=None):
        env.calls.append(SimpleNamespace(task=task_text, context=context_text))
        env.clock["t"] += 500.0
        return tools_mod.DelegatedOutput(text="slow")
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", runner)
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    await _delegate()
    assert desk.last_used == 5500.0 and all(e.at == 5500.0 for e in desk.log)


async def test_a_reply_arriving_during_a_delegations_registration_runs_after_it(env, monkeypatch):
    """The delegation reserved its place first; a swipe-reply whose task
    reaches the desk while the delegation is still registering waits its
    turn (arrival order = reservation order)."""
    from tools import delegate_to_agent
    gate = asyncio.Event()
    real_register = tools_mod._specialist_registry.register_delegation

    async def _slow_register(record):
        await gate.wait()
        await real_register(record)
    monkeypatch.setattr(tools_mod._specialist_registry, "register_delegation", _slow_register)
    desk = sd.DESKS.get_or_create(OPERATOR, "finance")
    delegation = asyncio.create_task(_with_origin(
        delegate_to_agent.handler({"agent": "finance", "task": "first", "context": "", "mode": "sync"}),
        _dm_origin()))
    await asyncio.sleep(0.02)
    assert desk.waiting == 1                                    # reserved, still registering
    order = []
    later = desk.reserve()

    async def reply():
        async with desk.use(later):
            order.append("reply")
    reply_task = asyncio.create_task(reply())
    await asyncio.sleep(0.02)
    assert order == []                                          # not admitted ahead of the delegation
    gate.set()
    await asyncio.gather(delegation, reply_task)
    assert env.calls[0].task == "first" and order == ["reply"]
    assert [e.who for e in desk.log] == ["resident", "specialist"]


async def test_the_prelaunch_seam_keeps_its_five_argument_call(env, monkeypatch):
    """Test doubles of `_prelaunch` take (agent, origin, mode, task, context)
    positionally — the voice-handoff suite's does; the desk must not change
    the seam's call (its skip-permit decision rides a ContextVar)."""
    seen = {}

    async def _five(agent, origin, mode, task, context):
        seen["skip"] = tools_mod._desk_skip_permit.get()
        return agent, None, None, None, tools_mod._result({
            "status": "error", "kind": "probe", "message": "stop here"})
    monkeypatch.setattr(tools_mod, "_prelaunch", _five)
    payload = await _delegate()
    assert payload["kind"] == "probe" and seen["skip"] is True
    assert tools_mod._desk_skip_permit.get() is False                # reset after the call
