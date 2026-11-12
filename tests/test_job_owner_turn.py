"""A specialist that started a job for someone else hears the job's end first:
which jobs get that turn, the origin it runs under, and what its answer, its
silence or its failure means for the requester."""
from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

import agent as agent_mod
import agent_lane
import job_owner_turn
import plugin_continuations as pc
import provenance
import tools as tools_mod
from config import AgentConfig, CharacterConfig, MemoryConfig
from job_owner_turn import (
    FOLLOW_UP_FAILED_LINE, MAX_OWNER_PASSES, OwnerResult, owner_origin, owner_role,
    run_owner_turn)
from tools import DelegatedOutput
from two_endpoints import CASA_BOT, OPERATOR, build_two_endpoints

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


def _frozen(**job):
    return {"id": "e1", "role_or_type": "finance", "started_at": 0.0, "notify": True,
            "origin": {"role": "assistant", "chat_id": 7, "user_id": 9, "_tg_bot": 3,
                       "_handoff_chain": ("assistant", "finance"), "_operator_turn": True,
                       "_operator_words": "do q3", "_task_delivery": None,
                       "job": {"title": "Quarter", "self_host": True,
                               "started_by": "operator", **job}}}


def _cfg(role, name, kind="specialist"):
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role, model="x",
                       character=CharacterConfig(name=name), system_prompt="x",
                       memory=MemoryConfig(token_budget=4000), kind=kind)


@pytest.fixture
def roles(monkeypatch):
    m = {"assistant": _cfg("assistant", "Ellen", "resident"),
         "finance": _cfg("finance", "Alex")}
    monkeypatch.setattr(tools_mod, "_agent_role_map", m, raising=False)
    monkeypatch.setattr(tools_mod, "_channel_manager", build_two_endpoints().manager,
                        raising=False)
    return m


@pytest.fixture
def lanes(monkeypatch):
    agent_lane.reset()
    monkeypatch.setattr(tools_mod, "_specialist_limiter", None, raising=False)
    monkeypatch.setattr(pc, "RETRY_EVERY_S", 0.01)
    return agent_lane.LANES


def _stub_turn(monkeypatch, *, says="Filed 4.", lane_error=None):
    """Replace the lane and the turn runner; returns what the runner was handed."""
    seen: list = []

    @contextlib.asynccontextmanager
    async def _lane(chat_id, role, *, deadline):
        if lane_error is not None:
            raise lane_error
        yield None

    async def _runner(cfg, task, origin, *, quota_key):
        seen.append((cfg.role, task, origin, quota_key))
        if isinstance(says, BaseException):
            raise says
        return says if isinstance(says, DelegatedOutput) else DelegatedOutput(text=says)

    monkeypatch.setattr(job_owner_turn, "_lane_turn", _lane)
    monkeypatch.setattr(job_owner_turn, "_run_turn_cancellable", _runner)
    return seen


# -- which jobs get the turn -------------------------------------------------

def test_owner_role_only_for_a_completed_specialist_started_job():
    assert owner_role(_frozen(), "completed") == "finance"
    assert owner_role(_frozen(), "cancelled") is None
    assert owner_role(_frozen(), "error") is None
    assert owner_role(_frozen(self_host=False), "completed") is None
    own = _frozen(); own["origin"]["role"] = "finance"      # started in the specialist's own chat
    assert owner_role(own, "completed") is None
    f = _frozen(); f["origin"]["_owner_pass"] = MAX_OWNER_PASSES
    assert owner_role(f, "completed") is None


def test_owner_role_needs_the_literal_self_host_mark():
    no_job = _frozen(); del no_job["origin"]["job"]
    assert owner_role(no_job, "completed") is None
    assert owner_role(_frozen(self_host="yes"), "completed") is None
    last = _frozen(); last["origin"]["_owner_pass"] = MAX_OWNER_PASSES - 1
    assert owner_role(last, "completed") == "finance"


# -- the origin the turn runs under ------------------------------------------

def test_owner_origin_carries_the_proof_and_not_the_first_message():
    o = owner_origin(_frozen())
    assert o["_operator_task"] is True and o["_owner_pass"] == 1
    assert o["synthetic"] == "continuation"
    assert o["_handoff_chain"] == ("assistant",) and o["delegation_depth"] == 0
    assert o["execution_role"] == "assistant" and o["_tg_bot"] == 3
    assert "_task_delivery" in o
    assert not {"job", "_operator_turn", "_operator_words"} & set(o)


@pytest.mark.parametrize("started_by", ["scheduled", "agent", None])
def test_owner_origin_without_the_proof_for_other_starters(started_by):
    assert "_operator_task" not in owner_origin(_frozen(started_by=started_by))


def test_owner_pass_advances():
    f = _frozen(); f["origin"]["_owner_pass"] = 3
    assert owner_origin(f)["_owner_pass"] == 4


def test_owner_origin_keeps_the_requesters_above_the_owner():
    f = _frozen()
    f["origin"]["_handoff_chain"] = ("assistant", "legal", "finance")
    o = owner_origin(f)
    assert o["_handoff_chain"] == ("assistant", "legal") and o["delegation_depth"] == 1
    assert o["execution_role"] == "legal" and o["role"] == "assistant"


# -- what the turn's end means -----------------------------------------------

@pytest.mark.asyncio
async def test_an_answer_is_the_owners_whole_text(roles, monkeypatch):
    _stub_turn(monkeypatch, says="Filed 4.")
    assert await run_owner_turn(_frozen(), "r") == OwnerResult("answer", "Filed 4.")


@pytest.mark.asyncio
async def test_a_turn_that_closes_silent_is_progress_only(roles, monkeypatch):
    _stub_turn(monkeypatch, says="<silent/>")
    assert await run_owner_turn(_frozen(), "r") == OwnerResult("silent")
    _stub_turn(monkeypatch, says=DelegatedOutput(text="Started the next pass.\n<silent/>",
                                                 final_message=" <silent/>\n"))
    assert (await run_owner_turn(_frozen(), "r")).kind == "silent"


@pytest.mark.asyncio
async def test_the_marker_inside_an_answer_is_an_answer(roles, monkeypatch):
    _stub_turn(monkeypatch, says="ok <silent/> and two are still missing")
    result = await run_owner_turn(_frozen(), "r")
    assert result == OwnerResult("answer", "ok <silent/> and two are still missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("says", [
    RuntimeError("boom"),
    DelegatedOutput(text="<silent/>", run_subtype="error_max_turns"),
    DelegatedOutput(text="partial", result_message_seen=False),
], ids=["turn-raises", "aborted-while-silent", "no-result"])
async def test_a_turn_that_does_not_complete_is_a_failure(roles, monkeypatch, says):
    _stub_turn(monkeypatch, says=says)
    assert await run_owner_turn(_frozen(), "r") == OwnerResult("failed")


@pytest.mark.asyncio
async def test_a_lane_that_never_frees_is_a_failure(roles, monkeypatch):
    seen = _stub_turn(monkeypatch, lane_error=pc.ContinuationDropped("finance's lane stayed busy"))
    assert await run_owner_turn(_frozen(), "r") == OwnerResult("failed")
    assert seen == []


@pytest.mark.asyncio
async def test_an_owner_that_is_no_longer_loaded_is_a_failure(roles, monkeypatch):
    seen = _stub_turn(monkeypatch)
    del roles["finance"]
    assert await run_owner_turn(_frozen(), "r") == OwnerResult("failed")
    assert seen == []


@pytest.mark.asyncio
async def test_a_cancelled_turn_is_cancelled_not_reported(roles, monkeypatch):
    _stub_turn(monkeypatch, says=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run_owner_turn(_frozen(), "r")


@pytest.mark.asyncio
async def test_the_turn_is_handed_the_job_and_an_origin_the_owner_can_act_under(
        roles, monkeypatch):
    from authz_grants import resolve_grant_identity
    seen = _stub_turn(monkeypatch)
    frozen = _frozen()
    frozen["origin"].update(_tg_bot=CASA_BOT, chat_id=OPERATOR, user_id=OPERATOR)
    await run_owner_turn(frozen, "61 of 65 payments accounted for")
    (role, task, origin, quota_key), = seen
    assert role == "finance" and quota_key.startswith("owner-")
    assert task == job_owner_turn.OWNER_TASK.format(
        title="Quarter", result="61 of 65 payments accounted for")
    assert '"Quarter"' in task and "61 of 65 payments accounted for" in task
    assert origin == {**owner_origin(frozen), "cid": origin["cid"]}
    assert origin["cid"].startswith("own-")
    # the owner's own turn, as the delegated runner derives it from that origin
    tok = agent_mod.origin_var.set({**origin, "execution_role": "finance"})
    try:
        identity, why = resolve_grant_identity("finance", artifact_id="1" * 64)
    finally:
        agent_mod.origin_var.reset(tok)
    assert why is None
    assert (identity.operator_id, identity.chat_id, identity.bot_id) == (
        OPERATOR, OPERATOR, CASA_BOT)
    assert identity.engagement_id == "" and identity.delivery is None


@pytest.mark.asyncio
async def test_the_turn_waits_for_the_owners_lane_with_a_bound(roles, monkeypatch):
    handed: list = []

    @contextlib.asynccontextmanager
    async def _lane(chat_id, role, *, deadline):
        handed.append((chat_id, role, deadline - time.monotonic()))
        yield None

    _stub_turn(monkeypatch)
    monkeypatch.setattr(job_owner_turn, "_lane_turn", _lane)
    await run_owner_turn(_frozen(), "r")
    (chat_id, role, wait), = handed
    assert (chat_id, role) == (7, "finance")
    assert pc.GIVE_UP_AFTER_S - 5 < wait <= pc.GIVE_UP_AFTER_S


@pytest.mark.asyncio
async def test_the_turn_runs_outside_the_ended_jobs_engagement(roles, lanes, monkeypatch):
    """The job's end is handled in the ended job's own context, where its
    engagement is still bound; the owner's turn is an ordinary delegated turn."""
    seen: dict = {}

    async def _bounded(cfg, task_text, context_text, resolution=None, output_format=None):
        seen["engagement"] = tools_mod.engagement_var.get(None)
        seen["provenance"] = provenance.turn_provenance()
        seen["role"] = cfg.role
        return DelegatedOutput(text="Filed 4.")

    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _bounded)
    record = SimpleNamespace(id="e1", kind="specialist", status="completed", origin={})
    tok = tools_mod.engagement_var.set(record)
    try:
        result = await run_owner_turn(_frozen(), "r")
        assert tools_mod.engagement_var.get(None) is record
    finally:
        tools_mod.engagement_var.reset(tok)
    assert result == OwnerResult("answer", "Filed 4.")
    assert seen["role"] == "finance" and seen["engagement"] is None
    assert seen["provenance"].transport == "continuation"
    assert seen["provenance"].execution != "engagement"
    assert not lanes.get("finance").lock.locked() and lanes.get("finance").waiting == 0


@pytest.mark.asyncio
async def test_the_engagement_is_bound_again_after_a_turn_that_fails(roles, monkeypatch):
    _stub_turn(monkeypatch, says=RuntimeError("boom"))
    record = SimpleNamespace(id="e1")
    tok = tools_mod.engagement_var.set(record)
    try:
        assert (await run_owner_turn(_frozen(), "r")).kind == "failed"
        assert tools_mod.engagement_var.get(None) is record
    finally:
        tools_mod.engagement_var.reset(tok)


def test_the_fallback_line_and_the_task_say_what_they_must():
    assert FOLLOW_UP_FAILED_LINE == "(The follow-up on this result did not run.)"
    assert "close with exactly <silent/> and nothing else" in job_owner_turn.OWNER_TASK
    assert MAX_OWNER_PASSES == 5


# -- the lane wait is bounded and leaves nothing behind -----------------------

@pytest.mark.asyncio
async def test_a_lane_that_stays_busy_past_the_deadline_drops_the_turn(lanes):
    lane = lanes.get("finance")

    async def _late_use():
        async with pc._lane_turn(7, "finance", deadline=time.monotonic() - 1):
            pytest.fail("admitted to a lane that is in use")

    async with lane.use(lane.reserve()):
        with pytest.raises(pc.ContinuationDropped, match="stayed busy"):
            await asyncio.wait_for(_late_use(), 5)
        assert lane.waiting == 0
    assert not lane.lock.locked()


@pytest.mark.asyncio
async def test_a_waiter_cancelled_while_it_waits_leaves_no_place_behind(lanes):
    lane = lanes.get("finance")

    async def _use():
        async with pc._lane_turn(7, "finance", deadline=time.monotonic() + 60):
            return True

    async with lane.use(lane.reserve()):
        waiter = asyncio.create_task(_use())
        for _ in range(20):
            await asyncio.sleep(0)
        assert lane.waiting == 1 and not waiter.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert lane.waiting == 0
    assert await asyncio.wait_for(_use(), 5) is True
    assert lane.waiting == 0 and not lane.lock.locked()
