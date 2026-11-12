"""A background job's scratchpad: bound to a file of its own, written through on every change,
loaded by a session built after a restart, never idle-expired, deleted on every path that ends
the job and at boot, and re-stated after a compaction (INV-PAD-010, INV-PAD-011).

The REAL ``PadStore`` and real files under the per-test ``JOB_PAD_ROOT`` (``tests/conftest.py``);
the launches and resumes run the real options builders, the terminal paths the real funnel, the
real launch-death owner, the real Cancel handler and the real boot pass."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
from types import SimpleNamespace

import pytest

from sent_prompt import sent_prompt
from claude_agent_sdk import ClaudeAgentOptions

import agent
import background_jobs as jobs
import casa_core
import job_inbox
import operator_calls
import result_broker as rb
import scratchpad
import tools
from engagement_registry import EngagementRegistry
from scratchpad import GRANT, MAX_BYTES, PAGE_NOTE, SERVER
from test_job_dm_attention import _CQ, _drain, _rig
from test_plugin_job_launch import worker  # noqa: F401 — fixture
from test_scratchpad import _call, _rpc
from test_scratchpad_route import _open, _post
from test_start_job import DECL, Client, drain_launch_turns, runtime  # noqa: F401 — fixture

WORDS = "do the March rows first"


def _file(eid: str):
    return jobs.job_pad_path(eid)


def _on_disk(eid: str):
    return json.loads(_file(eid).read_text())


def _rec(eid: str = "a" * 32, **kw):
    state = jobs.initial_job_state(DECL, started_by="operator")
    return SimpleNamespace(id=eid, task="Tag the March rows", origin={"job": state}, **kw)


def _job_options(eid: str = "a" * 32, *, brief=None, **kw) -> ClaudeAgentOptions:
    return jobs.job_session_options(ClaudeAgentOptions(**kw), _rec(eid), brief=brief)


def _restart(monkeypatch) -> scratchpad.PadStore:
    """A new process: every pad in memory is gone, the files stay."""
    store = scratchpad.PadStore()
    monkeypatch.setattr(scratchpad, "PADS", store)
    return store


async def _start(**origin) -> dict:
    """``start_job`` from the operator's own message, with his words on the turn."""
    token = agent.origin_var.set(dict(
        role="assistant", execution_role="assistant", channel="telegram", _tg_bot=111,
        chat_id="1", cid="test", user_text="classify", _operator_turn=True, _operator_words=WORDS,
        **origin))
    try:
        envelope = await tools.start_job.handler(
            dict(job=DECL.qualified_name, task="Classify the ledger", context="all rows"))
        return json.loads(envelope["content"][0]["text"])
    finally:
        agent.origin_var.reset(token)


BRIEF = ["## brief", "Task:", "Classify the ledger", "Context: all rows",
         "The operator's words:", WORDS]


# --- a launch opens the pad with its brief; a resume never does ----------------------------


def _binds_off_the_loop(monkeypatch) -> list:
    """The thread each bind of the pad store runs in (the real store's own method runs)."""
    threads: list = []
    real = scratchpad.PADS.bind

    def bind(*args, **kwargs):
        threads.append(threading.current_thread() is not threading.main_thread())
        return real(*args, **kwargs)
    monkeypatch.setattr(scratchpad.PADS, "bind", bind)
    return threads


async def test_a_specialist_job_launch_opens_its_pad_with_the_brief(  # noqa: F811
        runtime, monkeypatch):
    off_loop = _binds_off_the_loop(monkeypatch)
    result = await _start()
    assert result["status"] == "pending", result
    await drain_launch_turns()
    eid = result["engagement_id"]
    assert scratchpad.PADS.lines(eid) == BRIEF
    assert _on_disk(eid) == BRIEF
    assert off_loop == [True]
    opts = Client.instances[0].options
    assert GRANT in opts.allowed_tools and opts.mcp_servers[SERVER]["alwaysLoad"] is True
    assert sent_prompt(opts).endswith(jobs.JOB_PROMPT_LINE)


async def test_a_resident_hosted_job_launch_opens_its_pad_with_the_brief(  # noqa: F811
        worker, monkeypatch):
    off_loop = _binds_off_the_loop(monkeypatch)
    result = await _start()
    assert result["status"] == "pending", result
    await drain_launch_turns()
    assert _on_disk(result["engagement_id"]) == BRIEF
    assert off_loop == [True]


async def _resumed(registry, eid, monkeypatch, *, keep_file: bool):
    if not keep_file:
        _file(eid).unlink()
    store = _restart(monkeypatch)
    opts = tools.build_engagement_resume_options(registry.get(eid), "resumed")
    return store, opts


@pytest.mark.parametrize("keep_file", [True, False])
async def test_a_specialist_job_resume_loads_its_pad_and_never_seeds(  # noqa: F811
        runtime, monkeypatch, keep_file):
    result = await _start()
    await drain_launch_turns()
    eid = result["engagement_id"]
    store, opts = await _resumed(runtime.registry, eid, monkeypatch, keep_file=keep_file)
    assert store.lines(eid) == (BRIEF if keep_file else [])
    assert _on_disk(eid) == (BRIEF if keep_file else [])
    await _call(opts, op="append", text="did 70 of 100")
    assert _on_disk(eid) == [*(BRIEF if keep_file else []), "did 70 of 100"]


@pytest.mark.parametrize("keep_file", [True, False])
async def test_a_resident_hosted_job_resume_loads_its_pad_and_never_seeds(  # noqa: F811
        worker, monkeypatch, keep_file):
    result = await _start()
    await drain_launch_turns()
    eid = result["engagement_id"]
    store, opts = await _resumed(worker.registry, eid, monkeypatch, keep_file=keep_file)
    assert store.lines(eid) == (BRIEF if keep_file else [])
    await _call(opts, op="append", text="did 70 of 100")
    assert _on_disk(eid) == [*(BRIEF if keep_file else []), "did 70 of 100"]


def test_a_pad_in_memory_is_kept_and_the_brief_ignored():
    _job_options(brief=("first", None))
    scratchpad.PADS.respond("a" * 32, {"op": "write", "text": "progress"})
    _file("a" * 32).write_text(json.dumps(["older"]))      # memory is authoritative
    _job_options(brief=("second", "other words"))
    assert scratchpad.PADS.lines("a" * 32) == ["progress"]


def test_a_pad_in_memory_with_no_file_is_bound_and_its_next_change_written():
    scratchpad.PADS.put("a" * 32, ["kept"])
    _job_options(brief=("never written", None))
    assert scratchpad.PADS.lines("a" * 32) == ["kept"] and not _file("a" * 32).exists()
    scratchpad.PADS.respond("a" * 32, {"op": "append", "text": "next"})
    assert _on_disk("a" * 32) == ["kept", "next"]


def test_put_on_a_bound_pad_writes_its_file():
    _job_options()
    scratchpad.PADS.put("a" * 32, ["replaced"])
    assert _on_disk("a" * 32) == ["replaced"]


def test_a_brief_over_the_cap_leaves_an_empty_bound_pad():
    _job_options(brief=("x" * (MAX_BYTES + 1), None))
    assert scratchpad.PADS.lines("a" * 32) == [] and _on_disk("a" * 32) == []


# --- the file ----------------------------------------------------------------------------------


def test_the_file_is_private_in_a_directory_bind_creates():
    assert not jobs.JOB_PAD_ROOT.exists()
    _job_options(brief=("task", None))
    assert stat.S_IMODE(os.stat(jobs.JOB_PAD_ROOT).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(_file("a" * 32)).st_mode) == 0o600


async def test_a_trailing_empty_line_and_a_lone_surrogate_round_trip(monkeypatch):
    opts = _job_options()
    await _call(opts, op="write", text="price \ud83d cut\n\n")
    kept = scratchpad.PADS.lines("a" * 32)
    assert kept == ["price \ud83d cut", ""]
    _restart(monkeypatch)
    _job_options()
    assert scratchpad.PADS.lines("a" * 32) == kept


@pytest.mark.parametrize("content", [
    "not json", json.dumps({"lines": []}), json.dumps(["ok", 3]),
    json.dumps(["x" * (MAX_BYTES + 1)])], ids=["text", "object", "not-str", "over-cap"])
def test_an_invalid_file_gives_an_empty_bound_pad_and_a_warning(caplog, content):
    jobs.JOB_PAD_ROOT.mkdir()
    _file("a" * 32).write_text(content)
    with caplog.at_level(logging.WARNING, logger="scratchpad"):
        _job_options(brief=("never written", None))
    assert scratchpad.PADS.lines("a" * 32) == []
    assert _file("a" * 32).read_text() == content
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    scratchpad.PADS.respond("a" * 32, {"op": "write", "text": "new"})
    assert _on_disk("a" * 32) == ["new"]


async def test_a_failed_write_keeps_the_change_and_the_reply(monkeypatch, caplog):
    opts = _job_options()

    def _fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(scratchpad, "atomic_write_json", _fail)
    with caplog.at_level(logging.WARNING, logger="scratchpad"):
        reply, error = await _call(opts, op="write", text="kept in memory")
    assert error is False and "Wrote 1 line." in reply
    assert scratchpad.PADS.lines("a" * 32) == ["kept in memory"]
    assert _on_disk("a" * 32) == []
    assert any("disk full" in r.getMessage() for r in caplog.records)


async def test_a_route_write_reaches_the_file(monkeypatch):
    calls = operator_calls.OperatorCalls()
    monkeypatch.setattr(operator_calls, "CALLS", calls)
    _job_options(env=rb.broker_env("c1"))
    _open("c1")
    assert await _post({"client": "c1", "op": "write", "section": "page",
                        "text": "row"}) == {"lines": 1, "bytes": 3}
    assert _on_disk("a" * 32) == ["## page", "row"]


def test_a_role_that_disallows_the_tool_gets_no_pad_and_no_file():
    opts = _job_options(brief=("task", None), disallowed_tools=[GRANT])
    assert SERVER not in opts.mcp_servers and GRANT not in opts.allowed_tools
    assert scratchpad.PADS.lines("a" * 32) is None
    assert not jobs.JOB_PAD_ROOT.exists()


async def test_a_delegated_pad_has_no_path_and_makes_no_file(tmp_path):
    opts = scratchpad.attach(ClaudeAgentOptions(), "S1", task="find them")
    await _call(opts, op="append", text="note")
    assert scratchpad.PADS._pads["S1"].path is None
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


async def test_a_job_is_told_its_pad_outlives_a_restart_and_goes_with_the_job():
    assert "they go when it ends or Casa restarts" in scratchpad.DESCRIPTION
    assert "Casa restarts" not in jobs.JOB_DESCRIPTION
    assert "go when the job ends" in jobs.JOB_DESCRIPTION
    assert "Casa restart" in jobs.JOB_PROMPT_LINE and "job ends" in jobs.JOB_PROMPT_LINE
    for options, told in ((_job_options(), jobs.JOB_DESCRIPTION),
                          (scratchpad.attach(ClaudeAgentOptions(), "S1"),
                           scratchpad.DESCRIPTION)):
        [listed] = (await _rpc(options, "tools/list", {}))["result"]["tools"]
        assert listed["description"] == told


# --- lifetime ----------------------------------------------------------------------------------


def test_expiry_never_drops_a_job_pad(monkeypatch):
    clock = SimpleNamespace(t=1000.0)
    store = scratchpad.PadStore(now=lambda: clock.t)
    monkeypatch.setattr(scratchpad, "PADS", store)
    _job_options(brief=("task", None))
    store.put("delegated", ["x"])
    clock.t += scratchpad.IDLE_S * 3
    assert store.expire(lambda key: False) == 1
    assert store.keys() == ["a" * 32] and _file("a" * 32).exists()


def test_a_discard_with_no_pad_in_memory_removes_the_file_through_its_path(monkeypatch):
    _job_options(brief=("task", None))
    store = _restart(monkeypatch)
    store.discard("a" * 32, path=_file("a" * 32))
    assert not _file("a" * 32).exists()
    store.discard("a" * 32, path=_file("a" * 32))          # a missing file is fine


def test_a_discard_removes_the_pads_own_file_and_its_route():
    _job_options(brief=("task", None), env=rb.broker_env("c1"))
    scratchpad.PADS.discard("a" * 32)
    assert scratchpad.PADS.lines("a" * 32) is None and not _file("a" * 32).exists()
    assert scratchpad.PADS.from_plugin("c1", {"op": "read", "section": "brief"})["error"] \
        == "no_pad"


# --- every path that ends the job deletes it ----------------------------------------------------


async def _rig_with_pad(tmp_path, monkeypatch, *, in_memory: bool):
    r = await _rig(tmp_path, monkeypatch)
    jobs.job_session_options(ClaudeAgentOptions(), r.rec, brief=("task", None))
    assert _file(r.rec.id).exists()
    if not in_memory:
        _restart(monkeypatch)
    return r


@pytest.mark.parametrize("with_driver", [True, False])
@pytest.mark.parametrize("in_memory", [True, False])
async def test_the_finalize_funnel_deletes_the_pad(tmp_path, monkeypatch, in_memory,
                                                   with_driver):
    r = await _rig_with_pad(tmp_path, monkeypatch, in_memory=in_memory)
    await tools._finalize_engagement(r.rec, outcome="completed", text="done",
                                     next_steps=[], driver=r.drv if with_driver else None)
    assert scratchpad.PADS.lines(r.rec.id) is None and not _file(r.rec.id).exists()


async def test_a_launch_death_deletes_the_pad(tmp_path, monkeypatch):
    r = await _rig_with_pad(tmp_path, monkeypatch, in_memory=True)
    result = await tools._report_launch_death(
        None, r.rec, kind="launch_incomplete", detail="x", driver=None)
    assert result is tools.LaunchDeathResult.REPORTED
    assert scratchpad.PADS.lines(r.rec.id) is None and not _file(r.rec.id).exists()


async def test_cancel_deletes_the_pad_of_a_job_no_session_opened_since_a_restart(
        tmp_path, monkeypatch):
    r = await _rig_with_pad(tmp_path, monkeypatch, in_memory=False)
    cq = _CQ(f"v1|job_cancel|{r.rec.id}", 1)
    await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
    await _drain(r)
    assert r.rec.status == "cancelled"
    assert not _file(r.rec.id).exists()


async def test_a_journal_settle_that_raises_still_deletes_the_pad(tmp_path, monkeypatch):
    r = await _rig_with_pad(tmp_path, monkeypatch, in_memory=True)

    async def _raise(*args):
        raise RuntimeError("settle failed")
    monkeypatch.setattr(r.drv, "settle_journal_terminal", _raise)
    await tools._settle_job_journal(r.rec, r.drv)
    assert not _file(r.rec.id).exists()


# --- boot --------------------------------------------------------------------------------------


async def _registry(tmp_path):
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "boot.json"))
    live = await reg.create("specialist", "finance", "in_casa", "t", {"job": {}})
    ended = await reg.create("specialist", "finance", "in_casa", "t", {"job": {}})
    await reg.try_transition_terminal(ended.id, "cancelled", strict=True)
    for eid in (live.id, ended.id, "f" * 32):
        scratchpad.PADS.bind(eid, _file(eid), brief=["note"])  # "f" * 32: no record has it
    return reg, live, ended


@pytest.mark.parametrize("channel, runner", [(None, None), (object(), None),
                                             (None, "runner")])
async def test_boot_removes_unknown_and_ended_pads_and_keeps_live_ones(
        tmp_path, monkeypatch, channel, runner):
    reg, live, ended = await _registry(tmp_path)
    _restart(monkeypatch)
    if runner:
        monkeypatch.setattr(jobs, "_runner", SimpleNamespace(driver=None))
    await casa_core._resume_background_jobs(reg, channel)
    assert sorted(p.stem for p in jobs.JOB_PAD_ROOT.glob("*.pad")) == [live.id]


async def test_the_boot_settle_waits_out_a_terminal_write_that_rolls_back(
        tmp_path, monkeypatch):
    reg, live, _ = await _registry(tmp_path)
    job_inbox.append(live.id, uuid="u1", text="hello", notice=None)
    settled = []

    async def _settle(rec, driver):
        settled.append(rec.id)
    monkeypatch.setattr(tools, "_settle_job_journal", _settle)
    release, entered = threading.Event(), threading.Event()

    def _held_then_failed(snapshot):
        entered.set()
        release.wait(10)
        raise OSError("disk full")
    reg._write_tombstone = _held_then_failed
    flip = asyncio.create_task(
        reg.try_transition_terminal(live.id, "cancelled", strict=True))
    while not entered.is_set():
        await asyncio.sleep(0.005)
    assert reg.get(live.id).status == "cancelled"      # in memory, not yet durable
    boot = asyncio.create_task(casa_core._settle_ended_job_state(reg, object()))
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(OSError):
        await flip
    await boot
    assert reg.get(live.id).status == "active"
    assert settled == [] and _file(live.id).exists()
    assert [e["uuid"] for e in job_inbox.load(live.id)] == ["u1"]


# --- after a compaction -------------------------------------------------------------------------


async def _compacted(opts) -> str:
    [matcher] = opts.hooks["SessionStart"]
    out = await matcher.hooks[0]({"hook_event_name": "SessionStart", "source": "compact"},
                                 None, {"signal": None})
    return out["hookSpecificOutput"]["additionalContext"]


async def test_after_a_compaction_the_job_is_told_its_pad():
    opts = _job_options(brief=("Tag the rows", "his list"))
    await _call(opts, op="append", section="done", text="rows 1-40")
    text = await _compacted(opts)
    header, pad = text.split("\n\n", 1)
    assert header == jobs.job_header(_rec())
    assert pad.splitlines() == [
        PAGE_NOTE, scratchpad._shape(scratchpad.PADS.lines("a" * 32)),
        "## brief", "Task:", "Tag the rows", "The operator's words:", "his list",
        "Read your pad before you continue."]


async def test_the_brief_it_is_told_is_cut_at_four_thousand_characters():
    opts = _job_options(brief=("y" * 9000, None))
    pad = (await _compacted(opts)).split("\n\n", 1)[1]
    brief = pad.splitlines()[2]
    assert brief.startswith("## brief")
    text = "\n".join(pad.splitlines()[2:-1])
    assert text.startswith("## brief\nTask:\n" + "y" * 100)
    assert text.endswith("…[cut: the brief has 9015 characters; read it in your pad]")
    assert len(text.split("…[cut")[0]) == scratchpad.REINJECT_BRIEF_CHARS


async def test_an_empty_pad_adds_nothing_after_a_compaction():
    assert await _compacted(_job_options()) == jobs.job_header(_rec())
    assert scratchpad.reinject_text("no-such-pad") == ""
