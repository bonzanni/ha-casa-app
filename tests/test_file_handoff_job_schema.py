"""S6 §3.1 — a scheduled entry may target a plugin job: the schema admits `job`/`task`/`context`,
exactly one of prompt | prompt_file | job per scheduled type (date: prompt | job), task/context
require job, a job entry forbids clearance and requires channel telegram, webhook forbids them;
the one constructor carries the fields (INV-TRIG-022)."""
from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

import reminders
import tools as tools_mod

SCHEMA = json.loads(pathlib.Path("casa/rootfs/opt/casa/defaults/schema/triggers.v1.json").read_text())


def _doc(trigger: dict, version: int = 2) -> dict:
    return {"schema_version": version, "triggers": [trigger]}


def _ok(trigger):
    jsonschema.validate(_doc(trigger), SCHEMA)


def _bad(trigger):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc(trigger), SCHEMA)


CRON_JOB = {"name": "quarterly-check", "type": "cron", "schedule": "0 7 * * 1", "channel": "telegram",
            "job": "quarterly-accounting:quarterly-check", "task": "Run the weekly check.", "context": ""}
DATE_JOB = {"name": "once", "type": "date", "at": "2026-11-01T07:00:00+01:00", "one_shot": True,
            "channel": "telegram", "job": "quarterly-accounting:quarterly-check"}
INTERVAL_JOB = {"name": "every", "type": "interval", "minutes": 60, "channel": "telegram",
                "job": "quarterly-accounting:quarterly-check"}


@pytest.mark.parametrize("entry", [CRON_JOB, DATE_JOB, INTERVAL_JOB])
def test_a_job_entry_validates_as_cron_date_and_interval(entry):
    _ok(entry)


def test_a_date_job_entry_no_longer_needs_a_prompt_and_still_forbids_prompt_file():
    _ok(DATE_JOB)
    _bad({**DATE_JOB, "prompt_file": "x.md"})
    _bad({k: v for k, v in DATE_JOB.items() if k != "job"})          # neither prompt nor job


@pytest.mark.parametrize("entry", [CRON_JOB, DATE_JOB, INTERVAL_JOB])
def test_a_job_entry_forbids_clearance_and_requires_the_telegram_channel(entry):
    _bad({**entry, "clearance": "family"})
    _bad({**entry, "channel": "voice"})


def test_a_prompt_entry_still_carries_clearance_as_today():
    _ok({"name": "p", "type": "cron", "schedule": "0 7 * * 1", "channel": "telegram",
         "prompt": "hello", "clearance": "family"})
    _ok({"name": "v", "type": "cron", "schedule": "0 7 * * 1", "channel": "voice", "prompt": "hello"})


@pytest.mark.parametrize("entry", [CRON_JOB, DATE_JOB, INTERVAL_JOB])
def test_job_and_prompt_together_are_refused(entry):
    _bad({**entry, "prompt": "and a prompt"})


@pytest.mark.parametrize("base", [
    {"name": "c", "type": "cron", "schedule": "0 7 * * 1", "channel": "telegram", "prompt": "p"},
    {"name": "i", "type": "interval", "minutes": 5, "channel": "telegram", "prompt": "p"},
    {"name": "d", "type": "date", "at": "2026-11-01T07:00:00+01:00", "one_shot": True, "channel": "telegram", "prompt": "p"},
])
def test_task_or_context_without_job_is_refused_on_every_scheduled_type(base):
    _ok(base)
    _bad({**base, "task": "t"})
    _bad({**base, "context": "c"})


def test_a_webhook_entry_may_not_carry_job_task_or_context():
    base = {"name": "w", "type": "webhook", "auth": {"mode": "static_header", "header": "X-K"}}
    _ok(base)
    for extra in ({"job": "a:b"}, {"task": "t"}, {"context": "c"}):
        _bad({**base, **extra})


def test_task_and_context_are_bounded_like_start_jobs_arguments():
    _bad({**CRON_JOB, "task": "x" * 4001})
    _bad({**CRON_JOB, "context": "x" * 8001})
    _bad({**CRON_JOB, "job": "no-colon"})


def test_the_one_constructor_carries_job_task_and_context():
    spec = reminders.spec_from_entry(CRON_JOB)
    assert (spec.job, spec.task, spec.context) == ("quarterly-accounting:quarterly-check", "Run the weekly check.", "")
    plain = reminders.spec_from_entry({"name": "p", "type": "cron", "schedule": "* * * * *", "channel": "telegram", "prompt": "hi"})
    assert (plain.job, plain.task, plain.context) == ("", "", "")


def test_the_typed_surface_mirrors_the_three_keys():
    assert {"job", "task", "context"} <= set(tools_mod._TRIGGER_ENTRY_FIELDS)


# --- the typed upsert refuses what the resident cannot start and what the launcher would refuse ---

from test_config_trigger_tools import configurator_origin, runtime, _payload, _read, _write, HEARTBEAT  # noqa: F401,E402


async def test_the_upsert_refuses_an_unstartable_job_and_a_non_telegram_channel_before_writing(runtime, configurator_origin, monkeypatch):
    import background_jobs
    _write(runtime, [HEARTBEAT])
    monkeypatch.setattr(tools_mod, "_agent_role_map", {"butler": type("C", (), {"delegates": []})()}, raising=False)
    monkeypatch.setattr(background_jobs, "find_job_host", lambda job, caller, delegates: None)
    monkeypatch.setattr(background_jobs, "startable_jobs", lambda caller, delegates: [])
    out = _payload(await tools_mod.config_trigger_upsert.handler({"role": "butler", **CRON_JOB}))
    assert out["status"] == "error" and out["kind"] == "job_not_declared"
    assert [t["name"] for t in _read(runtime)["triggers"]] == ["heartbeat"]           # nothing written
    monkeypatch.setattr(background_jobs, "find_job_host", lambda job, caller, delegates: object())
    out = _payload(await tools_mod.config_trigger_upsert.handler({"role": "butler", **CRON_JOB, "channel": "voice"}))
    assert out["status"] == "error" and out["kind"] == "job_needs_text_channel"
    assert [t["name"] for t in _read(runtime)["triggers"]] == ["heartbeat"]


async def test_the_upsert_writes_a_startable_telegram_job_entry(runtime, configurator_origin, monkeypatch):
    import background_jobs
    _write(runtime, [HEARTBEAT])
    monkeypatch.setattr(tools_mod, "_agent_role_map", {"butler": type("C", (), {"delegates": []})()}, raising=False)
    monkeypatch.setattr(background_jobs, "find_job_host", lambda job, caller, delegates: object())
    out = _payload(await tools_mod.config_trigger_upsert.handler({"role": "butler", **CRON_JOB}))
    assert out["status"] == "ok"
    written = [t for t in _read(runtime)["triggers"] if t["name"] == "quarterly-check"][0]
    assert written["job"] == "quarterly-accounting:quarterly-check" and written["task"] == "Run the weekly check."


def test_the_loader_builds_a_job_entry_without_resolving_any_prose(tmp_path):
    import agent_loader
    specs = agent_loader._build_triggers({"triggers": [CRON_JOB, {"name": "p", "type": "cron", "schedule": "* * * * *",
                                                                  "channel": "telegram", "prompt": "hi"}]},
                                         agent_dir=str(tmp_path))
    assert [s.job for s in specs] == ["quarterly-accounting:quarterly-check", ""]
    assert specs[0].prompt == "" and specs[1].prompt == "hi"
