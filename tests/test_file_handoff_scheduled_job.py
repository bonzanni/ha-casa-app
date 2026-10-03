"""S6 §3.2–§3.4 — a scheduled trigger whose target is a plugin job starts the job directly
through the one launcher, with the live operator's identity and clearance in the origin, and no
resident turn; a fire that cannot start it tells the operator once, as a past event; registration
never checks startability and is never boot-fatal for it (INV-TRIG-022)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import background_jobs as jobs
import provenance
import tools as tools_mod
from config import TriggerSpec
from trigger_registry import TriggerError, TriggerRegistry

pytestmark = pytest.mark.asyncio


def _sched():
    s = MagicMock(); s.add_job = MagicMock(); s.remove_job = MagicMock(); return s


def _bus():
    b = MagicMock(); b.send = AsyncMock(); return b


def _registry(sched, bus):
    return TriggerRegistry(scheduler=sched, app=web.Application(), bus=bus, on_one_shot_fired=None)


def _job_spec(**over):
    kw = dict(name="quarterly-check", type="cron", schedule="0 7 * * 1", channel="telegram",
              job="probe:check", task="Run it.", context="")
    kw.update(over)
    return TriggerSpec(**kw)


async def _fire(sched):
    await sched.add_job.call_args.args[0]()


async def test_a_job_trigger_fires_the_launcher_and_sends_no_resident_turn(monkeypatch):
    sched, bus = _sched(), _bus()
    started = AsyncMock(return_value=None)
    monkeypatch.setattr(tools_mod, "start_scheduled_job", started, raising=False)
    reg = _registry(sched, bus)
    reg.register_agent("assistant", [_job_spec()], ["telegram"])
    await _fire(sched)
    bus.send.assert_not_awaited()
    started.assert_awaited_once()
    role, trig = started.await_args.args
    assert role == "assistant" and trig.job == "probe:check"


async def test_a_job_trigger_registers_without_checking_startability(monkeypatch):
    monkeypatch.setattr(jobs, "find_job_host", lambda *a, **k: None)
    reg = _registry(_sched(), _bus())
    reg.register_agent("assistant", [_job_spec()], ["telegram"])             # must not raise (R1-2)
    with pytest.raises(TriggerError):
        reg.register_agent("assistant", [_job_spec(name="v", channel="voice")], ["telegram", "voice"])


@pytest.fixture
def fire_env(monkeypatch):
    notices, echoes, launches = [], [], []

    async def notice(chat_id, text):
        notices.append((chat_id, text)); return True
    channel = SimpleNamespace(deliver_desk_notice=notice)
    monkeypatch.setattr(tools_mod, "_telegram_channel", lambda: channel, raising=False)
    import specialist_desk as sd
    monkeypatch.setattr(sd, "record_echo", lambda chat, line: echoes.append(line))
    monkeypatch.setattr(sd, "label_for", lambda role: "📊 Finance" if role == "finance" else "🏠 Ellen")
    monkeypatch.setattr(tools_mod, "_agent_role_map", {"assistant": SimpleNamespace(delegates=[SimpleNamespace(agent="finance")])}, raising=False)
    decl = SimpleNamespace(qualified_name="probe:check", title="Quarterly check", session="fresh")
    host = jobs.JobHost("specialist", "finance", decl, SimpleNamespace(name="probe"))
    monkeypatch.setattr(jobs, "find_job_host", lambda job, caller, delegates: host if job == "probe:check" else None)
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (4242, 4242))
    import ingress_identity
    monkeypatch.setattr(ingress_identity, "ingress_identity",
                        lambda route, **kw: SimpleNamespace(server_origin=SimpleNamespace(clearance="private")))

    async def launch(h, task, context, origin):
        launches.append(dict(host=h, task=task, context=context, origin=dict(origin)))
        return tools_mod._result({"status": "pending", "engagement_id": "e-9"})
    monkeypatch.setattr(tools_mod, "_start_job_on_host", launch)
    return SimpleNamespace(notices=notices, echoes=echoes, launches=launches, host=host, decl=decl, launch=launch)


async def test_the_fire_launches_through_the_one_launcher_with_the_operators_identity_and_clearance(fire_env):
    await tools_mod.start_scheduled_job("assistant", _job_spec())
    [l] = fire_env.launches
    assert l["host"] is fire_env.host and l["task"] == "Run it."
    o = l["origin"]
    assert (o["chat_id"], o["user_id"]) == (4242, 4242)
    assert o["_origin_route"] == "telegram" and o["_origin_clearance"] == "private"
    assert o["_scheduled_job"] is True and o["source"] == "scheduler" and o["trigger"] == "quarterly-check"
    assert o["role"] == "assistant" and o["channel"] == "telegram"
    assert fire_env.notices == [] and fire_env.echoes == []
    assert "_scheduled_job" in provenance.RESERVED_CONTEXT_KEYS


async def test_an_empty_task_falls_back_to_the_jobs_title(fire_env):
    await tools_mod.start_scheduled_job("assistant", _job_spec(task=""))
    assert fire_env.launches[0]["task"] == "Quarterly check"


async def test_a_refused_fire_tells_once_as_a_past_event_and_launches_nothing(fire_env, monkeypatch):
    async def busy(h, task, context, origin):
        return tools_mod._result({"status": "error", "kind": "job_busy"})
    monkeypatch.setattr(tools_mod, "_start_job_on_host", busy)
    await tools_mod.start_scheduled_job("assistant", _job_spec())
    assert fire_env.notices == [(4242, '📊 Finance: "Quarterly check" did not start — another job of this plugin held the claim at this occurrence.')]
    assert fire_env.echoes == [fire_env.notices[0][1]]


async def test_no_host_tells_with_the_residents_label_and_no_operator_only_logs(fire_env, monkeypatch):
    await tools_mod.start_scheduled_job("assistant", _job_spec(job="gone:job"))
    assert fire_env.notices == [(4242, '🏠 Ellen: "gone:job" did not start — no host was available at this occurrence.')]
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: None)
    fire_env.notices.clear(); fire_env.echoes.clear()
    await tools_mod.start_scheduled_job("assistant", _job_spec())
    assert fire_env.notices == [] and fire_env.launches == []
