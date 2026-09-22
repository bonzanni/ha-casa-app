"""The reminder plumbing of the output boundary (#1038 §7): a reminder's
resolved note rides ``triggers.yaml`` as ``output_note``, crosses ONE
constructor (``reminders.spec_from_entry``) on every route that builds its
spec, and ONE choke point (``scheduled_delivery_markers(note=…)``) on every
route that fires it — so the turn that sends it registers an ``InheritedNote``
whatever that turn's model writes.

Each route is fired separately (round 2, Astra): the immediately registered
reminder, the boot reload, the reconciliation re-registration and the overdue
sweep each cross different lines.
"""
from __future__ import annotations

import ast
import pathlib
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from aiohttp import web

import reminders
from config import TriggerSpec
from provenance import scheduled_delivery_markers
from trigger_registry import TriggerRegistry

pytestmark = [pytest.mark.unit]

WROTE = "Casa: Ellen wrote this without opening “invoice.pdf”."
CODE = pathlib.Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"


def _entry(**over) -> dict:
    e = {"name": "reminder-a1b2c3", "type": "date", "at": "2099-08-03T08:00:00+02:00",
         "one_shot": True, "channel": "telegram", "prompt": "Send this exact message…",
         "managed_by": reminders.OWNER_AGENT}
    e.update(over)
    return e


# ---------------------------------------------------------------------------
# One constructor
# ---------------------------------------------------------------------------

def test_spec_from_entry_carries_the_note_and_defaults_it_empty():
    spec = reminders.spec_from_entry(_entry(output_note=WROTE))
    assert isinstance(spec, TriggerSpec)
    assert spec.output_note == WROTE
    assert spec.name == "reminder-a1b2c3" and spec.at == "2099-08-03T08:00:00+02:00"
    assert spec.one_shot is True and spec.managed_by == reminders.OWNER_AGENT
    assert reminders.spec_from_entry(_entry()).output_note == ""


def test_the_boot_loader_builds_reminder_specs_through_that_constructor(tmp_path):
    from agent_loader import _build_triggers
    data = {"triggers": [_entry(output_note=WROTE),
                         {"name": "heartbeat", "type": "interval", "minutes": 60,
                          "channel": "telegram", "prompt": "hb"}]}
    specs = _build_triggers(data, agent_dir=str(tmp_path))
    by_name = {s.name: s for s in specs}
    assert by_name["reminder-a1b2c3"].output_note == WROTE
    assert by_name["heartbeat"].output_note == ""


def test_trigger_spec_is_constructed_only_where_the_design_allows():
    """The grep test of §7: a new site that hand-builds a reminder's spec
    would drop the note silently (round 2 found two such sites)."""
    allowed = {("config.py", None), ("reminders.py", "spec_from_entry"),
               ("agent_loader.py", "_build_triggers")}
    found = set()
    for path in CODE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "TriggerSpec":
                func = None
                for parent in ast.walk(tree):
                    if isinstance(parent, ast.FunctionDef) and any(n is node for n in ast.walk(parent)):
                        func = parent.name
                found.add((path.name, func))
    assert found <= allowed, found - allowed


def test_a_malformed_note_makes_the_entry_ill_formed_like_any_other_field():
    assert reminders._wellformed(_entry(output_note=WROTE))
    assert not reminders._wellformed(_entry(output_note=["not", "a", "string"]))


# ---------------------------------------------------------------------------
# One choke point
# ---------------------------------------------------------------------------

def test_the_marker_helper_stamps_the_note_only_when_there_is_one():
    assert scheduled_delivery_markers("telegram", "e1", note=WROTE) == {
        "_scheduled_delivery": True, "_scheduled_epoch": "e1", "_inherited_note": WROTE}
    assert "_inherited_note" not in scheduled_delivery_markers("telegram", "e1", note="")
    assert "_inherited_note" not in scheduled_delivery_markers("telegram", "e1")
    # the note is not a delivery-eligibility marker: it rides any channel
    assert scheduled_delivery_markers("voice", "e1", note=WROTE) == {"_inherited_note": WROTE}
    assert scheduled_delivery_markers("voice", "e1") == {}


# ---------------------------------------------------------------------------
# The four routes
# ---------------------------------------------------------------------------

def _registry():
    sched = MagicMock()
    sched.add_job = MagicMock()
    sched.remove_job = MagicMock()
    bus = MagicMock()
    bus.send = AsyncMock()
    reg = TriggerRegistry(scheduler=sched, app=web.Application(), bus=bus,
                          on_one_shot_fired=lambda role, name: None)
    return reg, sched, bus


async def test_d1_a_live_registered_spec_fires_with_the_note(monkeypatch):
    reg, sched, bus = _registry()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    reg.register_agent("assistant", [reminders.spec_from_entry(_entry(at=future, output_note=WROTE))],
                       ["telegram"])
    fire = sched.add_job.call_args.args[0]
    await fire()
    msg = bus.send.await_args.args[0]
    assert msg.context["_inherited_note"] == WROTE
    assert msg.context["_scheduled_delivery"] is True


async def test_d1_a_spec_without_a_note_fires_without_the_marker():
    reg, sched, bus = _registry()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    reg.register_agent("assistant", [reminders.spec_from_entry(_entry(at=future))], ["telegram"])
    await sched.add_job.call_args.args[0]()
    assert "_inherited_note" not in bus.send.await_args.args[0].context


def _agents_dir(tmp_path, entries):
    agents_dir = tmp_path / "agents"
    (agents_dir / "assistant").mkdir(parents=True)
    path = agents_dir / "assistant" / "triggers.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "triggers": entries}, sort_keys=False))
    return agents_dir, path


async def test_d3_reconciliation_re_registers_the_note(tmp_path):
    reg, sched, bus = _registry()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    agents_dir, path = _agents_dir(tmp_path, [_entry(at=future, output_note=WROTE)])
    runtime = types.SimpleNamespace(
        agents_dir=str(agents_dir), bus=bus, trigger_registry=reg,
        role_configs={"assistant": types.SimpleNamespace(channels=["telegram"])})
    reminders._reconcile_registrations(runtime, reg, "assistant", str(path),
                                       datetime.now(timezone.utc))
    await sched.add_job.call_args.args[0]()
    assert bus.send.await_args.args[0].context["_inherited_note"] == WROTE


async def test_d4_the_overdue_sweep_fires_with_the_note_from_the_raw_entry(tmp_path):
    reg, sched, bus = _registry()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    agents_dir, path = _agents_dir(tmp_path, [_entry(at=past, output_note=WROTE)])
    runtime = types.SimpleNamespace(
        agents_dir=str(agents_dir), bus=bus, trigger_registry=reg,
        role_configs={"assistant": types.SimpleNamespace(channels=["telegram"])})
    delivered = await reminders.sweep_reminders(runtime, datetime.now(timezone.utc))
    assert delivered == 1
    msg = bus.send.await_args.args[0]
    assert msg.source == "reminder-sweep"
    assert msg.context["_inherited_note"] == WROTE
