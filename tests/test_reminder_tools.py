"""#396 — set_reminder / cancel_reminder."""

from __future__ import annotations

import json
import types
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
import yaml
from aiohttp import web

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

FUTURE = "2099-08-03T08:00:00+02:00"
FUTURE_THURSDAY = "2099-08-06T07:00:00+02:00"   # 2099-08-06 is a Thursday


def _payload(res):
    return json.loads(res["content"][0]["text"])


def _seed(path):
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({
            "schema_version": 1,
            "triggers": [{"name": "heartbeat", "type": "interval",
                          "minutes": 60, "channel": "telegram",
                          "prompt": "hb"}],
        }, fh, sort_keys=False)


@dataclass
class Env:
    agents_dir: str
    path: str            # assistant triggers.yaml — reminders AND operator
    butler_path: str
    registry: object
    scheduler: object


@pytest.fixture
def env(tmp_path, monkeypatch, request):
    import agent as agent_mod
    import reminders
    from timekeeping import resolve_tz
    from tools import init_tools
    from trigger_registry import TriggerRegistry

    # A derived cron entry renders its hour in the APP's timezone, so the
    # expectations below are only meaningful against a KNOWN zone. This used to
    # ride on the shipped default being Europe/Amsterdam; it is now empty (it
    # defers to Home Assistant's own zone), which would make these assertions
    # depend on the machine running them. Pin it explicitly instead.
    monkeypatch.setenv("CASA_TZ", "Europe/Amsterdam")
    resolve_tz.cache_clear()
    request.addfinalizer(resolve_tz.cache_clear)

    agents_dir = tmp_path / "agents"
    for role in ("assistant", "butler"):
        (agents_dir / role).mkdir(parents=True)
        _seed(agents_dir / role / "triggers.yaml")

    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.remove_job = MagicMock()
    bus = MagicMock()

    def _remove_fired(role, name):
        reminders.remove_entry(
            reminders.triggers_path(str(agents_dir), role), name)

    registry = TriggerRegistry(scheduler=scheduler, app=web.Application(),
                               bus=bus, on_one_shot_fired=_remove_fired)

    # The tools read only ``cfg.channels``; a real AgentConfig needs a
    # role_artifact and buys nothing here. The full type is exercised by the
    # agent_loader suites.
    assistant = types.SimpleNamespace(role="assistant", channels=["telegram"])
    butler = types.SimpleNamespace(role="butler", channels=["telegram"])

    runtime = types.SimpleNamespace(
        agents_dir=str(agents_dir), bus=bus, trigger_registry=registry,
        role_configs={"assistant": assistant, "butler": butler},
    )

    init_tools(channel_manager=MagicMock(), bus=bus,
               specialist_registry=MagicMock(), mcp_registry=MagicMock(),
               trigger_registry=registry, runtime=runtime)

    token = agent_mod.origin_var.set(
        {"role": "assistant", "channel": "telegram"})
    try:
        yield Env(
            agents_dir=str(agents_dir),
            path=str(agents_dir / "assistant" / "triggers.yaml"),
            butler_path=str(agents_dir / "butler" / "triggers.yaml"),
            registry=registry, scheduler=scheduler,
        )
    finally:
        agent_mod.origin_var.reset(token)


def _entries(path):
    import os
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["triggers"]


def _find(path, name):
    return [t for t in _entries(path) if t["name"] == name][0]


def _reminder_names(path):
    """Only the entries the AGENT owns. The operator's share this file now, so
    "no reminder was written" can no longer be spelled as "the file is empty"."""
    return [t["name"] for t in _entries(path)
            if t.get("managed_by") == "agent"]


# ---------------------------------------------------------------------------
# set_reminder
# ---------------------------------------------------------------------------


async def test_one_shot_writes_a_date_entry(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": FUTURE, "text": "🗑 Garbage day — get the bins out before pickup."}))

    assert out["status"] == "ok"
    assert out["name"].startswith("reminder-")
    entry = _find(env.path, out["name"])
    assert entry["type"] == "date"
    assert entry["one_shot"] is True
    assert entry["channel"] == "telegram"
    assert "Garbage day" in entry["prompt"]


async def test_the_prompt_is_imperative(env):
    """A scheduled turn may legitimately stay silent, so delivery must not be
    left to judgement — this is the morning-briefing lesson (v0.132.0)."""
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))
    prompt = _find(env.path, out["name"])["prompt"]
    assert prompt.lower().startswith("send this exact message")
    assert "Bins." in prompt


async def test_the_prompt_suppresses_closing_narration(env):
    """#511: the scheduled turn's own final text is ALSO delivered, so without
    the `<silent/>` convention every fire costs a second message ("Sent.").
    The prompt keeps the imperative send (delivery stays deterministic — the
    v0.132.0 lesson) and additionally instructs the silence sentinel for the
    turn's closing output."""
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))
    prompt = _find(env.path, out["name"])["prompt"]
    assert "<silent/>" in prompt
    # The send instruction must survive — silence applies to the narration,
    # never to the delivery itself.
    assert prompt.lower().startswith("send this exact message")


async def test_weekly_writes_a_cron_entry_with_a_day_name(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": FUTURE_THURSDAY, "text": "Gym.", "repeat": "weekly"}))

    entry = _find(env.path, out["name"])
    assert entry["type"] == "cron"
    assert entry["schedule"] == "0 7 * * thu"
    assert entry["one_shot"] is False
    # The anchor is retained as the scheduler's start_date so the series does
    # not fire before the first occurrence the user asked for (Sol r1 #3).
    assert entry["at"].startswith("2099-08-06T07:00:00")


async def test_recurring_reminder_is_not_one_shot(env):
    from tools import set_reminder

    for repeat in ("daily", "weekdays", "weekly", "monthly"):
        out = _payload(await set_reminder.handler({
            "at": FUTURE_THURSDAY, "text": "x", "repeat": repeat}))
        assert out["status"] == "ok", repeat
        assert _find(env.path, out["name"])["one_shot"] is False


async def test_the_job_is_registered_immediately(env):
    """Live now, not at next boot."""
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))
    ids = [c.kwargs.get("id") for c in env.scheduler.add_job.call_args_list]
    assert f"assistant:{out['name']}" in ids


async def test_response_echoes_the_resolved_time_and_repeat(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))
    assert out["repeat"] == "none"
    assert out["at"].startswith("2099-08-03T08:00:00")


async def test_the_operator_entries_are_never_disturbed(env):
    """Inverted by #398 release 2: the reminder now goes INTO the operator's
    own triggers.yaml, so "never touch the file" is no longer the guarantee.
    What must hold is that every operator entry survives with its meaning and
    its position intact — compared through the LOADER's view, which is what
    boot actually reads."""
    import agent_loader
    from tools import set_reminder

    before = agent_loader._read_yaml(env.path)["triggers"]
    assert before == [{"name": "heartbeat", "type": "interval", "minutes": 60,
                       "channel": "telegram", "prompt": "hb"}]

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))

    after = agent_loader._read_yaml(env.path)["triggers"]
    assert after[:1] == before, "the heartbeat must be byte-identical in meaning"
    assert [t["name"] for t in after] == ["heartbeat", out["name"]]


async def test_the_entry_records_its_ownership(env):
    """Provenance is written HERE and nowhere else. Without it the sweep would
    not deliver the reminder late and cancellation could not remove it."""
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))
    assert _find(env.path, out["name"])["managed_by"] == "agent"


async def test_writes_only_to_the_calling_roles_own_file(env):
    """INV-TRIG-010: the bound that keeps this from being a general writer."""
    from tools import set_reminder

    await set_reminder.handler({"at": FUTURE, "text": "Bins."})
    assert all(not t["name"].startswith("reminder-")
               for t in _entries(env.butler_path))


async def test_rejects_an_unknown_repeat(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": FUTURE, "text": "x", "repeat": "fortnightly"}))
    assert out["status"] == "error"
    assert _reminder_names(env.path) == []


async def test_rejects_a_naive_at(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": "2099-08-03T08:00:00", "text": "x"}))
    assert out["status"] == "error"


async def test_rejects_a_time_in_the_past(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": "2000-01-01T08:00:00+02:00", "text": "x"}))
    assert out["status"] == "error"
    assert _reminder_names(env.path) == []


async def test_rejects_empty_text(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "   "}))
    assert out["status"] == "error"


async def test_registration_failure_rolls_back_the_entry(env):
    """A reminder recorded but never registered would look set and never
    fire until the next boot. Fail closed instead."""
    from tools import set_reminder

    env.scheduler.add_job.side_effect = RuntimeError("scheduler is down")
    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))

    assert out["status"] == "error"
    assert _reminder_names(env.path) == []
    # The rollback must not take the operator's trigger with it.
    assert [t["name"] for t in _entries(env.path)] == ["heartbeat"]


async def test_concurrent_reconcile_registration_is_not_a_failure(env):
    """#458 follow-up: the write lands off the loop, so a sweep's
    `_reconcile_registrations` can register this entry from the file before the
    tool does, making the tool's own register raise "already scheduled". The
    reminder is written AND live, so the tool must report success and keep the
    entry — not roll it back on a spurious error."""
    from tools import set_reminder

    # register_agent raises (as a duplicate would) but the job IS live — the
    # exact end-state a concurrent reconcile leaves.
    env.scheduler.add_job.side_effect = RuntimeError("already scheduled")
    env.registry.has_job = lambda role, name: True

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))

    assert out["status"] == "ok"
    # The entry must survive — it is live, and dropping it would strand a
    # reminder that will fire.
    names = [t["name"] for t in _entries(env.path)]
    assert "heartbeat" in names and len(names) == 2


async def test_refuses_outside_a_turn_context(env):
    import agent as agent_mod
    from tools import set_reminder

    token = agent_mod.origin_var.set({})
    try:
        out = _payload(await set_reminder.handler({"at": FUTURE, "text": "x"}))
    finally:
        agent_mod.origin_var.reset(token)
    assert out["status"] == "error"


# ---------------------------------------------------------------------------
# cancel_reminder
# ---------------------------------------------------------------------------


async def test_cancels_a_reminder_it_created(env):
    from tools import cancel_reminder, set_reminder

    created = _payload(await set_reminder.handler({
        "at": FUTURE, "text": "Bins."}))
    out = _payload(await cancel_reminder.handler({"name": created["name"]}))

    assert out["status"] == "ok"
    assert all(t["name"] != created["name"]
               for t in _entries(env.path))
    env.scheduler.remove_job.assert_called_with(
        f"assistant:{created['name']}")


async def test_cancel_refuses_a_non_reminder_name(env):
    """INV-TRIG-010 red case: operator triggers are not the agent's to delete.

    The refusal now comes from the entry's absent ownership marker rather than
    from its name, which is what makes it sound — the schema permits an operator
    to author a `reminder-`-prefixed name of their own.
    """
    from tools import cancel_reminder

    out = _payload(await cancel_reminder.handler({"name": "heartbeat"}))
    assert out["status"] == "error"
    assert out["kind"] == "not_authorized"
    assert any(t["name"] == "heartbeat" for t in _entries(env.path))


async def test_cancel_unknown_reminder_reports_not_found(env):
    from tools import cancel_reminder

    out = _payload(await cancel_reminder.handler({"name": "reminder-zzzzzz"}))
    assert out["status"] == "error"
    assert out["kind"] == "not_found"


async def test_cancel_refuses_outside_a_turn_context(env):
    import agent as agent_mod
    from tools import cancel_reminder

    token = agent_mod.origin_var.set({})
    try:
        out = _payload(await cancel_reminder.handler(
            {"name": "reminder-a1b2c3"}))
    finally:
        agent_mod.origin_var.reset(token)
    assert out["status"] == "error"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def test_both_tools_are_registered_on_the_mcp_surface():
    from tools import CASA_TOOLS

    names = {getattr(t, "name", None) for t in CASA_TOOLS}
    assert "set_reminder" in names
    assert "cancel_reminder" in names


# ---------------------------------------------------------------------------
# get_schedule must show reminders — cancel_reminder needs the name it reports
# ---------------------------------------------------------------------------


async def test_get_schedule_lists_a_one_off_reminder(tmp_path, monkeypatch):
    """End-to-end over a REAL scheduler: set a reminder, then see it in
    get_schedule labelled as one-off, not as an interval."""
    from datetime import datetime, timedelta, timezone

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import agent as agent_mod
    import reminders
    from tools import get_schedule, init_tools, set_reminder
    from trigger_registry import TriggerRegistry

    agents_dir = tmp_path / "agents"
    (agents_dir / "assistant").mkdir(parents=True)
    _seed(agents_dir / "assistant" / "triggers.yaml")

    sched = AsyncIOScheduler(timezone=timezone.utc)
    sched.start(paused=True)
    bus = MagicMock()
    registry = TriggerRegistry(scheduler=sched, app=web.Application(), bus=bus)
    runtime = types.SimpleNamespace(
        agents_dir=str(agents_dir), bus=bus, trigger_registry=registry,
        role_configs={"assistant": types.SimpleNamespace(
            role="assistant", channels=["telegram"])},
    )
    init_tools(channel_manager=MagicMock(), bus=bus,
               specialist_registry=MagicMock(), mcp_registry=MagicMock(),
               trigger_registry=registry, runtime=runtime)
    token = agent_mod.origin_var.set({"role": "assistant",
                                      "channel": "telegram"})
    try:
        at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        created = _payload(await set_reminder.handler(
            {"at": at, "text": "Bins."}))
        assert created["status"] == "ok"

        res = await get_schedule.handler({"within_hours": 24})
        text = res["content"][0]["text"]
    finally:
        agent_mod.origin_var.reset(token)
        sched.shutdown(wait=False)

    assert created["name"] in text
    assert "one-off" in text
    assert "interval" not in text


async def test_a_name_collision_cannot_destroy_an_existing_reminder(env, monkeypatch):
    """Sol r2 #3: if the generated name collided, registration failed on the
    duplicate job id and the rollback removed BOTH entries — losing a reminder
    the user had already been promised."""
    import reminders
    from tools import set_reminder

    first = _payload(await set_reminder.handler({"at": FUTURE, "text": "One."}))
    assert first["status"] == "ok"

    # Force the generator to keep proposing the name already in the store.
    monkeypatch.setattr(reminders.secrets, "token_hex",
                        lambda n: first["name"].removeprefix("reminder-"))

    second = _payload(await set_reminder.handler({"at": FUTURE, "text": "Two."}))

    assert second["status"] == "error"
    assert _reminder_names(env.path) == [first["name"]], \
        "the pre-existing reminder must survive"
    assert "heartbeat" in [t["name"] for t in _entries(env.path)]
