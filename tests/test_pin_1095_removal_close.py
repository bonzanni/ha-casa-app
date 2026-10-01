"""#1095 — removing a specialist closes its open conversations.

Ruling (ruling-1095-3): after the confirmation, an uninstall closes that
specialist's open conversations, as the warning said; a disable cannot be
confirmed beforehand, so Casa closes them when the disable takes effect and
tells the operator right away.

The close goes through the REAL terminal funnel (`_finalize_engagement`,
outcome `cancelled`), over a REAL `EngagementRegistry`; the channel and the bus
are doubles that count the topic close and the engager notification. The
uninstall cases reuse `test_plugin_erase_flow`'s fixtures (the REAL erase gate);
the disable cases reuse `test_reload_disabled_specialist_scopes`' harness (the
REAL dispatcher, bus, runtime and teardown).

RED at the base: nothing closes a removed specialist's engagements, so each
stays `active`/`idle`; the boot pass does not exist.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import plugin_erase_consent as pec
import plugin_erasure as pe
from test_plugin_erase_flow import _spec, flow, sflow  # noqa: F401 — fixtures
from test_reload_disabled_specialist_scopes import ROLE, harness  # noqa: F401 — fixture

SUBJECT = "specialist:fin"
PENDING = "open_conversations_unconfirmed"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    import tools as tools_mod
    from engagement_registry import EngagementRegistry
    r = EngagementRegistry(tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    monkeypatch.setattr(tools_mod, "_engagement_registry", r)
    return r


@pytest.fixture
def funnel(monkeypatch):
    """The funnel's channel and bus, counted. `chat_id` is the operator's, so
    the erase gate still finds an operator identity on the same channel."""
    import tools as tools_mod
    tch = MagicMock(name="telegram")
    tch.chat_id = "42"
    for m in ("send_to_topic", "send_response_to_topic", "close_topic",
              "update_topic_state"):
        setattr(tch, m, AsyncMock(return_value=1))
    cm = SimpleNamespace(get=lambda name: tch if name == "telegram" else None)
    bus = MagicMock(name="bus")
    bus.notify = AsyncMock()
    monkeypatch.setattr(tools_mod, "_channel_manager", cm, raising=False)
    monkeypatch.setattr(tools_mod, "_bus", bus, raising=False)
    return SimpleNamespace(tch=tch, bus=bus)


async def _open(reg, *, slug="fin", topic=101, task="Review household budget",
                kind="specialist", job=None):
    origin = {"channel": "telegram", "chat_id": "42", "user_id": 42, "role": "assistant"}
    if job is not None:
        origin["job"] = job
    return await reg.create(kind=kind, role_or_type=slug, driver="in_casa", task=task,
                            origin=origin, topic_id=topic)


def _out(r) -> dict:
    return json.loads(r["content"][0]["text"])


async def _uninstall(tm, **args):
    return _out(await tm.specialist_uninstall.handler({"slug": "fin", **args}))


def _cancelled(reg, *ids) -> int:
    return sum(reg.get(i).status == "cancelled" for i in ids)


def _closed_rows(out: dict) -> list:
    return [r for r in out.get("closed_conversations", [])
            if r.get("outcome") == "closed"]


# --- uninstall ---------------------------------------------------------------------------

async def test_rc11_a_confirmed_uninstall_closes_after_the_commit(reg, sflow, funnel,
                                                                  monkeypatch):
    sflow.specs = []                                     # no eraser: one call commits
    order: list = []
    stubbed_seq = sflow.tm._bundle_reload_and_verify

    async def seq(slug, **kw):
        order.append(("reload", reg.get(a.id).status))
        return await stubbed_seq(slug, **kw)
    monkeypatch.setattr(sflow.tm, "_bundle_reload_and_verify", seq)
    a = await _open(reg)
    out = await _uninstall(sflow.tm, acknowledged_conversations=[a.id])
    assert out.get("ok") is True, out
    assert sflow.uninstalled == [True]
    assert order == [("reload", "active")]          # closed only after the reload
    assert _cancelled(reg, a.id) == 1
    assert funnel.tch.close_topic.await_count == 1
    assert funnel.bus.notify.await_count == 1
    assert [r.get("engagement_id") for r in _closed_rows(out)] == [a.id], out


async def test_rc12_a_background_job_is_listed_and_closed(reg, sflow, funnel):
    sflow.specs = []
    a = await _open(reg, job={"name": "daily-budget"})
    out = await _uninstall(sflow.tm)
    assert out.get("kind") == PENDING, out
    [row] = out.get("conversations", [{}])
    assert row.get("engagement_id") == a.id and row.get("background_job") is True, out
    assert sflow.uninstalled == []
    out = await _uninstall(sflow.tm, acknowledged_conversations=[a.id])
    assert out.get("ok") is True, out
    assert _cancelled(reg, a.id) == 1


async def test_rc13_j2_an_arrival_during_the_erasure_is_closed_and_named(reg, sflow, funnel):
    """M1: acknowledge → question → Erase tap → the eraser runs → a new
    conversation opens → the finishing `erase_data=true` call commits with no
    re-warn, closes both, and names the arrival as opened after the
    confirmation. Neither `erase_data=true` call is ever checked."""
    tm = sflow.tm
    a = await _open(reg)
    out = await _uninstall(tm, acknowledged_conversations=[a.id])
    assert out.get("kind") == "erase_choice_pending", out
    [prompt] = sflow.prompts
    pec.CHOICES.mint(prompt["key"], pec.ERASE)
    out = await _uninstall(tm, erase_data=True)
    assert out.get("kind") == "erasure_running", out
    b = await _open(reg, topic=102, task="Plan the holiday budget")
    q = pe.QUESTIONS.current(SUBJECT)
    pe.RECORDS.put("plugin:fin.bank", "1" * 64, "complete", "bank gone", q)
    out = await _uninstall(tm, erase_data=True, acknowledged_conversations=[a.id])
    assert out.get("ok") is True, out
    assert sflow.uninstalled == [True]
    assert _cancelled(reg, a.id, b.id) == 2
    rows = {r.get("engagement_id"): r for r in _closed_rows(out)}
    assert set(rows) == {a.id, b.id}, out
    assert rows[b.id].get("opened_after_confirmation") is True
    assert rows[a.id].get("opened_after_confirmation") is False


async def test_rc13b_the_finishing_call_without_ids_closes_everything_unwarned(reg, sflow, funnel):
    tm = sflow.tm
    a = await _open(reg)
    q = pe.QUESTIONS.open(SUBJECT)
    pe.RECORDS.put("plugin:fin.bank", "1" * 64, "complete", "bank gone", q)
    out = await _uninstall(tm, erase_data=True)
    assert out.get("ok") is True, out
    assert sflow.uninstalled == [True]
    assert _cancelled(reg, a.id) == 1


# --- disable: the named retirement step ------------------------------------------------

async def test_rc14_the_disabled_teardown_closes_and_tells(reg, funnel, harness, monkeypatch):
    import reload as reload_mod
    monkeypatch.setattr(reload_mod, "_INCOMPLETE_RETIREMENTS", set())
    a = await _open(reg, slug=ROLE)
    other = await _open(reg, slug="travel", topic=103, task="Book the train")
    actions: list = []
    await reload_mod._teardown_disabled_specialist(actions, harness.runtime, ROLE)
    assert _cancelled(reg, a.id) == 1
    assert reg.get(other.id).status == "active"
    assert funnel.tch.close_topic.await_count == 1
    assert funnel.bus.notify.await_count == 1
    assert not any("teardown_incomplete" in str(x) for x in actions), actions


async def test_rc15_the_agents_sweep_reaches_a_disabled_specialist_with_no_agent(
        reg, funnel, harness, monkeypatch):
    """G1: boot builds no Agent for a specialist (only the sweep's backfill
    does), so one disabled before any backfill is in neither `runtime.agents`
    nor `_INCOMPLETE_RETIREMENTS`; the sweep must still close its open
    conversations."""
    import reload as reload_mod
    monkeypatch.setattr(reload_mod, "_INCOMPLETE_RETIREMENTS", set())
    harness.runtime.agents.pop(ROLE)
    harness.bus.unregister(ROLE)
    a = await _open(reg, slug=ROLE)
    from reload import dispatch
    env = await dispatch("agents", runtime=harness.runtime)
    assert env["status"] == "ok", env
    assert harness.registry.is_disabled(ROLE) is True
    assert _cancelled(reg, a.id) == 1


async def test_rc17_a_failed_disabled_close_is_told_named_and_retried(
        reg, funnel, harness, monkeypatch):
    import casa_core
    import reload as reload_mod
    import tools as tools_mod
    monkeypatch.setattr(reload_mod, "_INCOMPLETE_RETIREMENTS", set())
    a = await _open(reg, slug=ROLE)
    attempts: list = []

    async def failing(engagement, **kw):
        attempts.append(engagement.id)
        return tools_mod.FinalizeResult.PERSIST_FAILED
    monkeypatch.setattr(tools_mod, "_finalize_engagement", failing)
    notices: list = []

    async def notify(cm, text):
        notices.append(text)
    monkeypatch.setattr(casa_core, "operator_notify", notify)
    actions: list = []
    await reload_mod._teardown_disabled_specialist(actions, harness.runtime, ROLE)
    assert attempts == [a.id]
    assert sum(a.id in n or "101" in n for n in notices) == 1, notices
    assert sum("teardown_incomplete_close_engagements" in str(x) for x in actions) == 1, actions
    assert ROLE in reload_mod._INCOMPLETE_RETIREMENTS
    assert reg.get(a.id).status == "active"


# --- disable: the boot pass ------------------------------------------------------------

_BOOT_PASS = "_close_disabled_specialist_engagements"


def _main_source() -> str:
    import inspect
    import casa_core
    return inspect.getsource(casa_core.main)


def test_rc16a_the_boot_pass_runs_after_the_channels_and_before_job_resume():
    """Structural pin (labelled as such): INV-ENG-018 tells a closure armed in
    this process live, so the pass must run after the channels start (and after
    the recovered-outcome replay), and before background jobs resume, or a
    disabled specialist's job would be resumed."""
    src = _main_source()
    start = src.find("await channel_manager.start_all()")
    replay = src.find("await _notify_recovered_engagement_outcomes(")
    resume = src.find("await _resume_background_jobs(")
    calls = [i for i in range(len(src)) if src.startswith(f"await {_BOOT_PASS}(", i)]
    assert len(calls) == 1, f"{len(calls)} awaited calls of the boot close pass in main"
    assert 0 < start < replay < calls[0] < resume


async def test_rc16b_the_boot_pass_closes_disabled_only(reg, funnel):
    import casa_core
    fn = getattr(casa_core, _BOOT_PASS, None)
    assert fn is not None, "no boot close pass"
    dis = await _open(reg, slug="travel", topic=103, task="Book the train")
    bad = await _open(reg, slug="broken", topic=104, task="Load failure keeps me")
    await reg.mark_idle(dis.id)
    await reg.mark_idle(bad.id)

    class _Specs:
        def is_disabled(self, role):
            return role == "travel"

        def get(self, role):
            return None                              # neither is loaded-and-enabled
    await fn(reg, _Specs())
    assert reg.get(dis.id).status == "cancelled"
    assert reg.get(bad.id).status == "idle"
    assert funnel.tch.close_topic.await_count == 1
