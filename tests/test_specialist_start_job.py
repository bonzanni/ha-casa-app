"""S7a (INV-BGJOB-008): a specialist's delegated turn starts a job its
OWN plugins declare, hosted on itself.

Every launch case runs ``start_job`` on the real launch, engagement registry,
in-casa driver and Telegram channel (``test_start_job.runtime``) with the real
plugin registry and the real host discovery restored over that fixture's
seams, from an origin built the way a delegated turn builds its own. Each asserts counts — live records, SDK clients,
pending start claims — never only a status.
"""
from __future__ import annotations

import json

import pytest

import agent
import background_jobs as jobs
import plugin_registry
import tools
from plugin_fixtures import entry
from test_specialist_job_host import (  # noqa: F401  (fixtures and helpers)
    _artifact, _counts, _jobs, _load, _real_discovery, call, drain_launch_turns, home, runtime,
)

pytestmark = pytest.mark.asyncio


def _delegated_turn_origin(*, clearance="private", channel="telegram"):
    """A turn the resident delegated to finance from a chat."""
    return {"role": "assistant", "execution_role": "finance", "channel": channel,
            **({"_tg_bot": 111} if channel == "telegram" else {}),
            "chat_id": "1", "user_id": 1, "cid": "dlg-cid", "user_text": "classify",
            "_origin_route": "telegram", "_origin_clearance": clearance,
            "_operator_turn": True, "delegation_depth": 1,
            "_delegation_id": "dlg-1", "_delegation_kind": "specialist",
            "_delegated_session_id": "the-calling-turns-session"}


async def start_as(origin, job="ledger:classify"):
    token = agent.origin_var.set(dict(origin))
    try:
        envelope = await tools.start_job.handler(
            {"job": job, "task": "Classify the ledger", "context": "all rows"})
        return json.loads(envelope["content"][0]["text"])
    finally:
        agent.origin_var.reset(token)


def _the_live_record(runtime):
    """The one record the start created — found in the registry, since a
    specialist's own start never returns its engagement id (#1229)."""
    live = runtime.registry.active_and_idle()
    assert len(live) == 1, live
    return live[0]


@pytest.fixture
def finance_is_specialist(runtime):
    runtime.cfg.kind = "specialist"
    assert tools._agent_role_map["finance"].kind == "specialist"
    return runtime


def _own_ledger(tmp_path, targets=("specialist:finance",)):
    e = entry("ledger", list(targets))
    _artifact(tmp_path, e, _jobs())
    _load(tmp_path, [e])


async def test_a_specialist_turn_starts_its_own_job_hosted_on_itself(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(_delegated_turn_origin())
    assert result["status"] == "pending", result
    assert result["agent"] == "finance" and result["job"] == "ledger:classify"
    await drain_launch_turns()
    rec = _the_live_record(runtime)
    assert (rec.kind, rec.role_or_type) == ("specialist", "finance")
    assert rec.origin["plugin_job"] == {"plugin": "ledger"}
    # the work's origin: the turn's resident, chat and clearance, at depth 1,
    # without the markers that identify the calling turn
    assert rec.origin["role"] == "assistant"
    assert str(rec.origin["chat_id"]) == "1"
    assert rec.origin["_origin_clearance"] == "private"
    assert rec.origin["delegation_depth"] == 1
    assert "_delegation_id" not in rec.origin
    # a binding the job mints must not name the calling turn's conversation
    assert "_delegated_session_id" not in rec.origin
    assert _counts(runtime) == (1, 1, 0)


async def test_a_member_turn_starts_the_job_at_the_members_clearance(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(_delegated_turn_origin(clearance="household"))
    assert result["status"] == "pending", result
    await drain_launch_turns()
    rec = _the_live_record(runtime)
    assert rec.origin["_origin_clearance"] == "household"
    assert _counts(runtime) == (1, 1, 0)


@pytest.mark.parametrize("targets", [("specialist:backup",), ("resident:assistant",)])
async def test_a_job_the_specialists_own_plugins_do_not_declare_is_not_startable(
        finance_is_specialist, tmp_path, monkeypatch, targets):
    """Another specialist's job, or the resident's own: never hosted by the
    caller, never hosted elsewhere on its behalf."""
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path, targets)
    result = await start_as(_delegated_turn_origin())
    assert result["kind"] == "job_not_declared", result
    assert result["message"] == "Startable jobs: none"
    assert _counts(runtime) == (0, 0, 0)


async def test_an_unknown_job_names_only_the_specialists_own_jobs(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(_delegated_turn_origin(), job="ledger:nope")
    assert result["kind"] == "job_not_declared"
    assert result["message"] == "Startable jobs: ledger:classify"
    assert _counts(runtime) == (0, 0, 0)


async def test_a_start_inside_a_delegated_turn_takes_the_engagement_permit_without_waiting(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    turn_permit = runtime.limiter.try_acquire(tools._delegation_scope(_delegated_turn_origin(), "finance"))
    assert turn_permit is not None
    try:
        result = await start_as(_delegated_turn_origin())
        assert result["status"] == "pending", result
        assert "finance:engagement" in runtime.limiter._active_scopes
    finally:
        turn_permit.release()
    assert _counts(runtime) == (1, 1, 0)


async def test_a_voice_delegation_cannot_start_a_job(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(_delegated_turn_origin(channel="voice"))
    assert result["kind"] == "job_needs_text_channel"
    assert _counts(runtime) == (0, 0, 0)


async def test_the_resident_start_of_a_specialist_job_is_unchanged(
        finance_is_specialist, tmp_path, monkeypatch):
    """Ellen's start keeps her origin: role and execution role the resident."""
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await call()
    assert result["status"] == "pending", result
    await drain_launch_turns()
    rec = runtime.registry.get(result["engagement_id"])
    assert rec.origin["execution_role"] == "assistant"
    assert rec.origin["delegation_depth"] == 1
    # only a job its own host started is marked as one
    assert "self_host" not in rec.origin["job"]
    assert _counts(runtime) == (1, 1, 0)


async def test_a_specialist_engagement_turn_is_refused_at_dispatch(finance_is_specialist):
    """A job or engagement session never holds the tool; were it reachable,
    the #541 ceiling refuses it before the handler runs (start_job is outside
    it, and S7a keeps it outside)."""
    assert "start_job" not in tools._SPECIALIST_DISPATCH_CEILING
    from types import SimpleNamespace
    token = tools.engagement_var.set(SimpleNamespace(kind="specialist", role_or_type="finance",
                                                     id="e", origin={}))
    try:
        envelope = await tools.start_job.handler({"job": "ledger:classify", "task": "t",
                                                  "context": ""})
    finally:
        tools.engagement_var.reset(token)
    assert json.loads(envelope["content"][0]["text"])["kind"] == "specialist_tool_ceiling"


# ---------------------------------------------------------------------------
# The grant: offered only to a pinned or delegated build loading a job plugin
# ---------------------------------------------------------------------------

START_JOB = "mcp__casa-framework__start_job"


def _build(cfg, **kw):
    return tools._build_specialist_options(cfg, **kw)


async def test_a_delegated_build_loading_a_job_plugin_offers_start_job(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _own_ledger(tmp_path)
    opts = _build(runtime.cfg)
    assert START_JOB in opts.allowed_tools


async def test_a_job_or_engagement_build_never_offers_start_job(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _own_ledger(tmp_path)
    for grants in (tools.SPECIALIST_CASA_GRANTS,
                   tools.SPECIALIST_CASA_GRANTS + jobs.JOB_CASA_GRANTS):
        opts = _build(runtime.cfg, extra_casa_tools=grants)
        assert START_JOB not in opts.allowed_tools


async def test_a_build_with_no_job_plugin_is_unchanged(finance_is_specialist, tmp_path):
    runtime = finance_is_specialist
    e = entry("ledger", ["specialist:finance"])
    _artifact(tmp_path, e, None)
    _load(tmp_path, [e])
    opts = _build(runtime.cfg)
    assert START_JOB not in opts.allowed_tools


async def test_a_resident_delegated_build_never_offers_start_job(runtime, tmp_path):
    """Only a specialist: a delegated resident keeps its own configuration."""
    runtime.cfg.kind = "resident"
    _own_ledger(tmp_path)
    opts = _build(runtime.cfg)
    assert START_JOB not in opts.allowed_tools


async def test_own_plugins_are_those_assigned_and_loadable_at_the_call(
        finance_is_specialist, tmp_path, monkeypatch):
    """Ruling O: a plugin withheld for an unresolved secret is not startable;
    once its secret resolves it is the specialist's own and loadable, so its
    job starts — whatever the calling session was built with."""
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    import plugin_grants
    missing = {"ledger": ["LEDGER_TOKEN"]}
    monkeypatch.setattr(plugin_grants, "blocking_unresolved_env_vars_for_resolved",
                        lambda rp, environ=None: missing.get(rp.name, []))
    withheld = await start_as(_delegated_turn_origin())
    assert withheld["kind"] == "job_not_declared", withheld
    assert _counts(runtime) == (0, 0, 0)
    missing.clear()
    wired = await start_as(_delegated_turn_origin())
    assert wired["status"] == "pending", wired
    assert _counts(runtime) == (1, 1, 0)


async def test_a_specialists_own_start_during_its_jobs_pending_terminal_write_is_refused(
        finance_is_specialist, tmp_path, monkeypatch):
    """#1173 through S7a's entry point: a specialist's own `start_job` that
    lands while its plugin's last job is still writing its end is refused
    `job_busy` — the self-hosted start reaches the same claim as every other —
    and when that write fails and the job rolls back to live, exactly one job
    of the plugin is live."""
    import asyncio
    import threading

    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    reg = runtime.registry
    rec = await reg.create("specialist", "finance", "in_casa", "classify",
                           {"job": {"name": "ledger:classify", "title": "Classify"},
                            "plugin_job": {"plugin": "ledger"}})
    entered, release = threading.Event(), threading.Event()
    real_write = reg._write_tombstone

    def held_then_failed(snapshot):
        entered.set()
        release.wait(10)
        raise OSError("disk full")

    monkeypatch.setattr(reg, "_write_tombstone", held_then_failed)
    transition = asyncio.ensure_future(
        reg.try_transition_terminal(rec.id, "completed", strict=True))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert reg.active_and_idle() == []
        result = await start_as(_delegated_turn_origin())
    finally:
        release.set()
        with pytest.raises(OSError):
            await transition
        monkeypatch.setattr(reg, "_write_tombstone", real_write)
    # #1229: a specialist's own start names no internal id, refusals included
    assert result.get("kind") == "job_busy", result
    assert "engagement_id" not in result and "topic_id" not in result, result
    assert rec.status == "active"
    assert _counts(runtime) == (1, 0, 0)


async def test_a_specialists_own_start_marks_the_job_as_started_by_its_host(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(_delegated_turn_origin())
    assert result["status"] == "pending", result
    await drain_launch_turns()
    job = _the_live_record(runtime).origin["job"]
    assert job["self_host"] is True and job["started_by"] == "operator"


def _turn_of_an_operators_task(**extra):
    """A delegated turn that is not the operator's own message but carries
    Casa's mark that it belongs to a task the operator started."""
    origin = _delegated_turn_origin()
    del origin["_operator_turn"]
    return {**origin, "_operator_task": True, **extra}


async def test_a_job_started_under_an_operators_task_is_the_operators_and_keeps_the_pass_count(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(_turn_of_an_operators_task(_owner_pass=2))
    assert result["status"] == "pending", result
    await drain_launch_turns()
    rec = _the_live_record(runtime)
    assert rec.origin["job"]["started_by"] == "operator"
    assert rec.origin["job"]["self_host"] is True
    assert (rec.origin["_operator_task"], rec.origin["_owner_pass"]) == (True, 2)
    # the same turn without the mark is an agent's start
    for live in runtime.registry.active_and_idle():
        await runtime.driver.cancel(live)
        await runtime.registry.try_transition_terminal(live.id, "cancelled")
    origin = _turn_of_an_operators_task()
    del origin["_operator_task"]
    assert (await start_as(origin))["status"] == "pending"
    await drain_launch_turns()
    assert _the_live_record(runtime).origin["job"]["started_by"] == "agent"


async def test_the_host_can_start_the_next_job_from_its_turn_at_the_first_jobs_end(
        finance_is_specialist, tmp_path, monkeypatch):
    """The job's host hears its job's end in a turn of its own, taken on the
    real lane and the real concurrency limit while the ended job's context is
    still current. A follow-up job it starts there is admitted: the ended job
    holds no slot, the turn is not the ended job's, and the follow-up is the
    operator's and one pass further along."""
    import agent_lane
    import job_owner_turn
    from handoff import child_chain

    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    agent_lane.reset()
    assert (await start_as(_delegated_turn_origin()))["status"] == "pending"
    await drain_launch_turns()
    first = _the_live_record(runtime)
    nested: list = []

    async def _owners_turn(cfg, task, origin, *, quota_key):
        # the delegated runner's own derivation of the turn's origin
        child = {**origin, "execution_role": cfg.role, "_delegation_id": quota_key,
                 "_delegation_kind": cfg.kind, "delegation_depth": 1,
                 "_handoff_chain": child_chain(origin, cfg.role)}
        nested.append(await start_as(child))
        return tools.DelegatedOutput(text="<silent/>")

    monkeypatch.setattr(job_owner_turn, "_run_turn_cancellable", _owners_turn)
    bound = tools.engagement_var.set(first)
    try:
        await tools._finalize_engagement(
            first, outcome="completed", text="61 of 65", next_steps=[],
            driver=runtime.driver)
    finally:
        tools.engagement_var.reset(bound)
    await drain_launch_turns()

    assert [r.get("status") for r in nested] == ["pending"], nested
    follow_up = _the_live_record(runtime)
    assert follow_up.id != first.id and first.status == "completed"
    assert follow_up.origin["job"]["started_by"] == "operator"
    assert follow_up.origin["job"]["self_host"] is True
    assert (follow_up.origin["_operator_task"], follow_up.origin["_owner_pass"]) == (True, 1)
    assert "_operator_turn" not in follow_up.origin
    # the host closed silent: nothing stays owed for the first job
    assert first.terminal_notification_pending is False
    assert not agent_lane.LANES.get("finance").lock.locked()
