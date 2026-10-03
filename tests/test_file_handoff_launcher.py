"""S6 §3.3 — ONE job launcher: the part of start_job that runs from the resolved host onward
is `tools._start_job_on_host`, called by start_job and by the scheduled fire (INV-TRIG-022)."""
from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest

import background_jobs as jobs
import tools as tools_mod

pytestmark = pytest.mark.asyncio


def test_start_job_has_no_launch_path_of_its_own():
    module = inspect.getsource(tools_mod)
    start = module.index("async def start_job(args: dict) -> dict:")
    src = module[start:module.index("\n@tool(", start)]              # the tool's own body, not the fence
    assert "_launch_interactive_engagement(" not in src
    assert src.count("_start_job_on_host(") == 1
    assert "claim_job_start(" not in src and "release_job_start(" not in src


def _host(kind, role="finance"):
    decl = SimpleNamespace(qualified_name="probe:check", title="Check", session="fresh")
    return jobs.JobHost(kind, role, decl, SimpleNamespace(name="probe"))


async def _drive(monkeypatch, host, *, raise_launch=False):
    seen = {}

    async def launch(role, task, context, origin, *, job=None, plugin_host=None, **kw):
        seen.update(role=role, task=task, context=context, origin=dict(origin), job=job,
                    plugin_host=plugin_host, plugin_job_at_launch=origin.get("plugin_job"))
        if raise_launch:
            raise RuntimeError("launch failed")
        return tools_mod._result({"status": "pending", "engagement_id": "e-1"})
    monkeypatch.setattr(tools_mod, "_launch_interactive_engagement", launch)
    monkeypatch.setattr(jobs, "claim_job_start", lambda h, registry: None)
    released = []
    monkeypatch.setattr(jobs, "release_job_start", lambda plugin: released.append(plugin))
    monkeypatch.setattr(jobs, "host_plugin_name", lambda h: "probe")
    origin = {"role": "assistant", "execution_role": "assistant", "channel": "telegram", "chat_id": 1}
    try:
        out = await tools_mod._start_job_on_host(host, "do it", "ctx", origin)
    except RuntimeError:
        out = None
    return seen, released, out


async def test_a_specialist_host_is_pinned_before_the_launch_and_passes_no_plugin_host(monkeypatch):
    seen, released, out = await _drive(monkeypatch, _host("specialist"))
    assert seen["plugin_job_at_launch"] == {"plugin": "probe"}      # set BEFORE the launch awaited
    assert seen["plugin_host"] is None and seen["role"] == "finance"
    assert released == ["probe"] and out is not None


async def test_a_resident_host_passes_itself_as_plugin_host_and_pins_nothing(monkeypatch):
    host = _host("resident", role="assistant")
    seen, released, out = await _drive(monkeypatch, host)
    assert seen["plugin_host"] is host and seen["plugin_job_at_launch"] is None
    assert released == ["probe"]


async def test_the_claim_is_released_even_when_the_launch_raises(monkeypatch):
    seen, released, out = await _drive(monkeypatch, _host("specialist"), raise_launch=True)
    assert released == ["probe"] and out is None


async def test_a_refused_claim_returns_the_refusal_and_launches_nothing(monkeypatch):
    launched = []

    async def launch(*a, **k):
        launched.append(1)
    monkeypatch.setattr(tools_mod, "_launch_interactive_engagement", launch)
    monkeypatch.setattr(jobs, "claim_job_start", lambda h, registry: {"status": "error", "kind": "job_busy"})
    out = await tools_mod._start_job_on_host(_host("specialist"), "do it", "", {"role": "assistant"})
    assert launched == [] and "job_busy" in out["content"][0]["text"]
