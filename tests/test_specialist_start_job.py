"""S7a (INV-BGJOB-008): a specialist's desk or delegated turn starts a job its
OWN plugins declare, hosted on itself.

Every launch case runs ``start_job`` on the real launch, engagement registry,
in-casa driver and Telegram channel (``test_start_job.runtime``) with the real
plugin registry and the real host discovery restored over that fixture's
seams, from an origin built the way a desk turn and a delegated turn build
theirs. Each asserts counts — live records, SDK clients, topics opened,
pending start claims — never only a status.
"""
from __future__ import annotations

import json

import pytest

import agent
import background_jobs as jobs
import plugin_registry
import specialist_desk
import tools
from plugin_fixtures import entry
from test_specialist_job_host import (  # noqa: F401  (fixtures and helpers)
    _artifact, _counts, _jobs, _load, _real_discovery, call, home, runtime,
)

pytestmark = pytest.mark.asyncio


def _desk_turn_origin():
    """A desk turn's origin as the specialist's tool handler sees it: the
    desk's own origin, then the delegated runner's increment and role."""
    origin = specialist_desk._desk_origin(
        resident_role="assistant", desk_role="finance", chat_id=1, user_id=1,
        user_name="operator", message_id=7, cid="desk-cid", text="run the check",
        turn_id="desk-turn-1")
    origin["_origin_clearance"] = "private"
    return {**origin, "delegation_depth": origin["delegation_depth"] + 1,
            "execution_role": "finance", "_delegation_kind": "specialist"}


def _delegated_turn_origin(*, clearance="private", channel="telegram"):
    """A turn the resident delegated to finance from a chat."""
    return {"role": "assistant", "execution_role": "finance", "channel": channel,
            "chat_id": "1", "user_id": 1, "cid": "dlg-cid", "user_text": "classify",
            "_origin_route": "telegram", "_origin_clearance": clearance,
            "_operator_turn": True, "delegation_depth": 1,
            "_delegation_id": "dlg-1", "_delegation_kind": "specialist"}


async def start_as(origin, job="ledger:classify"):
    token = agent.origin_var.set(dict(origin))
    try:
        envelope = await tools.start_job.handler(
            {"job": job, "task": "Classify the ledger", "context": "all rows"})
        return json.loads(envelope["content"][0]["text"])
    finally:
        agent.origin_var.reset(token)


@pytest.fixture
def finance_is_specialist(runtime):
    runtime.cfg.kind = "specialist"
    assert specialist_desk.is_specialist(tools._agent_role_map["finance"])
    return runtime


def _own_ledger(tmp_path, targets=("specialist:finance",)):
    e = entry("ledger", list(targets))
    _artifact(tmp_path, e, _jobs())
    _load(tmp_path, [e])


@pytest.mark.parametrize("origin_of", [_desk_turn_origin, _delegated_turn_origin])
async def test_a_specialist_turn_starts_its_own_job_hosted_on_itself(
        finance_is_specialist, tmp_path, monkeypatch, origin_of):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(origin_of())
    assert result["status"] == "pending", result
    assert result["agent"] == "finance" and result["job"] == "ledger:classify"
    await tools.drain_launch_turns()
    rec = runtime.registry.get(result["engagement_id"])
    assert (rec.kind, rec.role_or_type) == ("specialist", "finance")
    assert rec.origin["plugin_job"] == {"plugin": "ledger"}
    # the work's origin: the turn's resident, chat and clearance, at depth 1,
    # without the markers that identify the calling turn
    assert rec.origin["role"] == "assistant"
    assert str(rec.origin["chat_id"]) == "1"
    assert rec.origin["_origin_clearance"] == "private"
    assert rec.origin["delegation_depth"] == 1
    assert "desk" not in rec.origin and "_delegation_id" not in rec.origin
    assert _counts(runtime) == (1, 1, 1, 0)


async def test_a_member_turn_starts_the_job_at_the_members_clearance(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(_delegated_turn_origin(clearance="household"))
    assert result["status"] == "pending", result
    await tools.drain_launch_turns()
    rec = runtime.registry.get(result["engagement_id"])
    assert rec.origin["_origin_clearance"] == "household"
    assert _counts(runtime) == (1, 1, 1, 0)


@pytest.mark.parametrize("targets", [("specialist:backup",), ("resident:assistant",)])
async def test_a_job_the_specialists_own_plugins_do_not_declare_is_not_startable(
        finance_is_specialist, tmp_path, monkeypatch, targets):
    """Another specialist's job, or the resident's own: never hosted by the
    caller, never hosted elsewhere on its behalf."""
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path, targets)
    result = await start_as(_desk_turn_origin())
    assert result["kind"] == "job_not_declared", result
    assert result["message"] == "Startable jobs: none"
    assert _counts(runtime) == (0, 0, 0, 0)


async def test_an_unknown_job_names_only_the_specialists_own_jobs(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(_desk_turn_origin(), job="ledger:nope")
    assert result["kind"] == "job_not_declared"
    assert result["message"] == "Startable jobs: ledger:classify"
    assert _counts(runtime) == (0, 0, 0, 0)


async def test_a_start_inside_a_desk_turn_takes_the_engagement_permit_without_waiting(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    desk_permit = runtime.limiter.try_acquire(tools._delegation_scope(_desk_turn_origin(), "finance"))
    assert desk_permit is not None
    try:
        result = await start_as(_desk_turn_origin())
        assert result["status"] == "pending", result
        assert "finance:engagement" in runtime.limiter._active_scopes
    finally:
        desk_permit.release()
    assert _counts(runtime) == (1, 1, 1, 0)


async def test_a_voice_delegation_cannot_start_a_job(
        finance_is_specialist, tmp_path, monkeypatch):
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await start_as(_delegated_turn_origin(channel="voice"))
    assert result["kind"] == "job_needs_text_channel"
    assert _counts(runtime) == (0, 0, 0, 0)


async def test_the_resident_start_of_a_specialist_job_is_unchanged(
        finance_is_specialist, tmp_path, monkeypatch):
    """Ellen's start keeps her origin: role and execution role the resident."""
    runtime = finance_is_specialist
    _real_discovery(monkeypatch, tmp_path)
    _own_ledger(tmp_path)
    result = await call()
    assert result["status"] == "pending", result
    await tools.drain_launch_turns()
    rec = runtime.registry.get(result["engagement_id"])
    assert rec.origin["execution_role"] == "assistant"
    assert rec.origin["delegation_depth"] == 1
    assert _counts(runtime) == (1, 1, 1, 0)


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
# The grant: offered only to a desk or delegated build loading a job plugin
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
    withheld = await start_as(_desk_turn_origin())
    assert withheld["kind"] == "job_not_declared", withheld
    assert _counts(runtime) == (0, 0, 0, 0)
    missing.clear()
    wired = await start_as(_desk_turn_origin())
    assert wired["status"] == "pending", wired
    assert _counts(runtime) == (1, 1, 1, 0)


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
                            "plugin_job": {"plugin": "ledger"}}, 7)
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
        result = await start_as(_desk_turn_origin())
    finally:
        release.set()
        with pytest.raises(OSError):
            await transition
        monkeypatch.setattr(reg, "_write_tombstone", real_write)
    assert (result.get("kind"), result.get("engagement_id")) == ("job_busy", rec.id), result
    assert rec.status == "active"
    assert _counts(runtime) == (1, 0, 0, 0)
