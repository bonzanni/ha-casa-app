"""Run confirmation (INV-MEM-031, INV-ENG-027; §14 test 7).

Casa's live capture is the only source of memory. Every run that can capture
registers a ``transcript_runs`` row before its first capture, names its
session before anything is captured from it, and is confirmed in its
outermost ``finally`` once every capture it made is durable. A transcript is
deleted only while no unconfirmed run names it, and nothing ever reads a
transcript into memory: a reply lost by a crash before its capture stays
lost, its run unresolved and its transcript kept until a wipe.

Everything runs against a real SQLite store wired into the live seams and
real transcript files laid out as the SDK resolves them. The kill cases
SIGKILL a child process this test started, by its own pid, at a named point.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock

import agent as agent_mod
import tools
from memory import capture, meter, transcript_runs
from memory.store import MemoryStore
from session_registry import SessionRegistry
from session_sweeper import SessionSweeper
from test_agent_process import (
    ScriptedToolClient, _make_agent, _mk_result, _msg, patch_retry_sleep,
)
from test_memory_capture import _operator_msg, mem  # noqa: F401
from transcript_runs_support import (
    CODE_ROOT, after_retention, events, kill_child, project, rows, summaries,
    unresolved, write_transcript,
)

pytestmark = [pytest.mark.asyncio]

AGENT_HOME = "/config/agent-home/assistant"
ORIGIN = {"role": "assistant", "channel": "telegram", "chat_id": "4242",
          "_origin_route": "telegram", "_origin_clearance": "private"}


@pytest.fixture
def home(tmp_path, monkeypatch, mem):  # noqa: F811 — the imported fixture
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setattr(transcript_runs, "_background", set())
    return SimpleNamespace(store=mem, tmp=tmp_path)


def _init(sid: str) -> SystemMessage:
    return SystemMessage(subtype="init", data={"session_id": sid})


def _said(text: str, sid: str | None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)],
                            model="claude-sonnet-4-6", session_id=sid)


async def _resident_turn(home, *scripts, msg=None):
    agent = _make_agent(home.tmp)
    ScriptedToolClient.reset(*scripts)
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            patch_retry_sleep():
        try:
            return await agent._process(msg or _operator_msg("hello", "901"))
        finally:
            await transcript_runs.settle()


# --- the resident turn, killed ------------------------------------------------

#: A resident turn through the real ``Agent._process`` (pooled path), its CLI
#: double writing the transcript as it goes; ``stop()`` at the point named.
RESIDENT_CHILD = r"""
from pathlib import Path
from unittest.mock import patch
from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock
from memory import capture as cap
from test_agent_process import ScriptedToolClient, _make_agent, _mk_result
from test_memory_capture import _operator_msg
from transcript_runs_support import write_transcript
point, sid, home, reply = sys.argv[5:9]

class Client(ScriptedToolClient):
    async def receive_response(self):
        write_transcript("/config/agent-home/assistant", sid,
                         '{"type":"assistant","text":"%s"}\n' % reply)
        async for m in super().receive_response():
            yield m

real_record = cap.record

async def record(item):
    if point == "reply" and item.speaker_kind == "resident":
        stop()
    receipt = await real_record(item)
    if point == "after_reply" and item.speaker_kind == "resident":
        stop()
    return receipt
cap.record = record
ScriptedToolClient.reset([
    SystemMessage(subtype="init", data={"session_id": sid}),
    AssistantMessage(content=[TextBlock(text=reply)], model="m", session_id=sid),
    _mk_result(sid)])
agent = _make_agent(Path(home))
with patch("sdk_client_pool._default_make_client", Client):
    asyncio.run(agent._process(_operator_msg("hello", "901")))
"""


def _kill_resident(home, point: str, sid: str, reply: str) -> None:
    child_home = home.tmp / "child"
    child_home.mkdir(exist_ok=True)
    kill_child(RESIDENT_CHILD, home.store, home.tmp, point, sid,
               str(child_home), reply,
               env={"CLAUDE_CONFIG_DIR": os.environ["CLAUDE_CONFIG_DIR"]})


async def _every_pass(home, sid: str, *, stale_days: float) -> None:
    """The sweeper over a pointer to *sid* *stale_days* old, the engagement
    reaper and the delegated retention pass, all after retention."""
    from engagement_registry import EngagementRegistry
    from engagement_transcript_reaper import reap_engagement_transcripts
    reg = SessionRegistry(str(home.tmp / "sweep-sessions.json"))
    now = datetime.now(timezone.utc)
    async with reg._lock:
        reg._data[f"telegram-{sid[:8]}"] = {
            "agent": "assistant", "sdk_session_id": sid,
            "last_active": (now - timedelta(days=stale_days)).isoformat()}
        await reg._save_locked()
    await SessionSweeper(registry=reg, now=lambda: now,
                         directory_for=lambda role: AGENT_HOME)._sweep_once()
    await reap_engagement_transcripts(
        EngagementRegistry(tombstone_path=str(home.tmp / "none.json"), bus=None),
        now=time.time() + 400 * 86400)
    await asyncio.to_thread(transcript_runs.retention_pass, home.store,
                            now=after_retention())


async def test_kill_between_reply_and_capture_keeps_the_transcript_unresolved(home):
    """A real SIGKILL after the resident's message is received and before its
    capture. After the restart the reply is in no event, the run is
    unresolved (the meter counts 1), the transcript survives every pass, and
    nothing is ever captured from it. Red case: a receipt check that
    captures from the transcript."""
    sid, reply = str(uuid.uuid4()), f"reply-{uuid.uuid4().hex}"
    _kill_resident(home, "reply", sid, reply)

    restarted = MemoryStore(home.store.directory)
    assert restarted.open()
    assert [e["text"] for e in events(restarted)] == ["hello"]
    (row,) = rows(restarted)
    assert (row["kind"], row["session_id"], row["confirmed_at"]) == (
        "resident", sid, None)
    assert row["boot_id"] != transcript_runs.BOOT_ID
    assert unresolved(restarted) == 1
    assert meter.measure(restarted)["unresolved_runs"] == 1

    path = project(AGENT_HOME) / f"{sid}.jsonl"
    for _ in range(2):
        await _every_pass(home, sid, stale_days=400)
    assert path.exists()
    assert not any(reply in e["text"] for e in events(restarted))
    assert [e["text"] for e in events(restarted)] == ["hello"]


async def test_a_fresh_session_is_named_before_its_first_capture(home):
    """A kill just after the first resident capture of a fresh session
    leaves a row naming that session, so its transcript is protected. Red
    case: the session recorded only at turn end (the pointer's
    publication)."""
    sid, reply = str(uuid.uuid4()), f"reply-{uuid.uuid4().hex}"
    _kill_resident(home, "after_reply", sid, reply)
    (row,) = rows(home.store)
    assert row["session_id"] == sid and row["confirmed_at"] is None
    assert [e["text"] for e in events(home.store)] == ["hello", reply]
    await _every_pass(home, sid, stale_days=400)
    assert (project(AGENT_HOME) / f"{sid}.jsonl").exists()


# --- content is never read back ------------------------------------------------

async def test_transcript_content_is_never_captured(home):
    """A transcript holding a canary that no live capture saw, every run in
    it confirmed: after every pass and a restart the canary is in no event,
    and the file goes once retention ends, not before. Red case: a "capture
    first" reaper."""
    sid, canary = str(uuid.uuid4()), uuid.uuid4().hex
    path = write_transcript(
        AGENT_HOME, sid, json.dumps({"type": "assistant", "text": canary}) + "\n")
    run = transcript_runs.register(home.store, kind="resident",
                                   role="assistant", directory=AGENT_HOME,
                                   session_id=sid)
    transcript_runs.confirm(home.store, run)

    await _every_pass(home, sid, stale_days=10)          # inside retention
    assert path.exists()
    await _every_pass(home, sid, stale_days=400)
    assert not path.exists()
    restarted = MemoryStore(home.store.directory)
    assert restarted.open()
    assert not any(canary in e["text"] for e in events(restarted))
    assert events(restarted) == []


# --- the gate -----------------------------------------------------------------

async def test_an_unconfirmed_run_holds_its_directory(home):
    """One rule: while a row is unconfirmed nothing in its directory is
    deleted, whatever session it names or fails to name; a row whose
    directory is unknown holds the session it names; a confirmed row holds
    nothing."""
    s = home.store
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    named = transcript_runs.register(s, kind="resident", role="assistant",
                                     directory="/d/one", session_id=a)
    for sid in (a, b, None):
        assert transcript_runs.blocks_deletion(s, directory="/d/one", session_id=sid)
    assert not transcript_runs.blocks_deletion(s, directory="/d/two", session_id=b)
    assert not transcript_runs.blocks_deletion(s, directory="/d/two")
    transcript_runs.register(s, kind="engagement", role="r", directory="/d/two")
    assert transcript_runs.blocks_deletion(s, directory="/d/two", session_id=b)
    assert transcript_runs.blocks_deletion(s, directory="/d/two")
    transcript_runs.register(s, kind="engagement", role="r", directory=None,
                             session_id=b)
    assert transcript_runs.blocks_deletion(s, directory="/d/three", session_id=b)
    assert not transcript_runs.blocks_deletion(s, directory="/d/three",
                                               session_id=str(uuid.uuid4()))
    transcript_runs.confirm(s, named)
    for sid in (a, None):
        assert not transcript_runs.blocks_deletion(s, directory="/d/one", session_id=sid)


# --- the run's life -------------------------------------------------------------

async def test_a_retry_into_a_new_session_moves_the_row(home, monkeypatch):
    """An attempt in session one is captured under a row naming one; its
    retry starts session two, and the row names two before the retry's
    first capture."""
    s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
    named_at_capture = []
    real = capture.record

    async def record(item):
        if item.speaker_kind == "resident":
            named_at_capture.append((item.text, rows(home.store)[0]["session_id"]))
        return await real(item)
    monkeypatch.setattr(capture, "record", record)
    await _resident_turn(
        home,
        [_init(s1), _said("first try", s1), ConnectionError("broken")],
        [_init(s2), _said("second try", s2), _mk_result(s2)])
    assert named_at_capture == [("first try", s1), ("second try", s2)]
    (row,) = rows(home.store)
    assert row["session_id"] == s2 and row["confirmed_at"] is not None


class _Hanging(ScriptedToolClient):
    """Yields its script, then waits until cancelled."""

    async def receive_response(self):
        async for m in super().receive_response():
            yield m
        await asyncio.Event().wait()


async def test_cancelled_and_failed_turns_confirm(home, monkeypatch):
    """A turn that fails after a capture, and one cancelled after a capture,
    are both confirmed: the live rules capture nothing more for them."""
    sid = str(uuid.uuid4())
    with pytest.raises(ValueError):
        await _resident_turn(home, [_init(sid), _said("partial", sid),
                                    ValueError("not retried")])
    (failed,) = rows(home.store)
    assert failed["confirmed_at"] is not None

    agent = _make_agent(home.tmp)
    sid2 = str(uuid.uuid4())
    _Hanging.reset([_init(sid2), _said("before the cancel", sid2)])
    with patch("sdk_client_pool._default_make_client", _Hanging):
        task = asyncio.create_task(agent._process(_operator_msg("again", "902")))
        for _ in range(200):
            if any(e["text"] == "before the cancel" for e in events(home.store)):
                break
            await asyncio.sleep(0.01)
        # the capture has returned and the turn waits on the stream: a
        # cancellation that interrupted the capture itself would rightly
        # leave the run unconfirmed (test_a_capture_that_cannot_spool…)
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await transcript_runs.settle()
    cancelled = [r for r in rows(home.store) if r["session_id"] == sid2]
    assert len(cancelled) == 1 and cancelled[0]["confirmed_at"] is not None


async def test_a_capture_that_cannot_spool_leaves_the_run_unconfirmed(
        home, monkeypatch):
    sid = str(uuid.uuid4())
    real = capture.append
    monkeypatch.setattr(
        capture, "append",
        lambda store, item: None if item.speaker_kind == "resident"
        else real(store, item))
    await _resident_turn(home, [_init(sid), _said("lost", sid), _mk_result(sid)])
    (row,) = rows(home.store)
    assert row["session_id"] == sid and row["confirmed_at"] is None


async def test_ineligible_turns_register_nothing(home):
    """Voice and webhook turns, an executor's engagement turn: none can
    capture, so none registers."""
    for channel in ("voice", "webhook"):
        sid = str(uuid.uuid4())
        await _resident_turn(home, [_init(sid), _said("answer", sid),
                                    _mk_result(sid)],
                             msg=_msg(channel, str(uuid.uuid4())))
    await _engagement_turn(kind="executor")
    assert rows(home.store) == [] and events(home.store) == []


# --- the engagement turn -----------------------------------------------------------

class _TopicStream:
    async def emit(self, text):
        return None

    async def finalize(self, text):
        return None


def _engagement_record(kind="specialist", role="finance", sid=None):
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id=uuid.uuid4().hex, kind=kind, role_or_type=role,
        driver="in_casa", status="active", topic_id=99, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=sid, task="t", origin=dict(ORIGIN))


async def _engagement_turn(kind="specialist", sid=None, script=None):
    from drivers.in_casa_driver import InCasaDriver
    sid = sid or str(uuid.uuid4())
    script = script or [_init(sid), _said("engagement answer", sid),
                        _mk_result(sid)]

    class _Client:
        async def query(self, prompt):
            return None

        async def receive_response(self):
            for m in script:
                yield m

    drv = InCasaDriver(topic_stream_factory=lambda tid: _TopicStream())
    rec = _engagement_record(kind=kind)
    drv._clients[rec.id] = _Client()
    drv._locks[rec.id] = asyncio.Lock()
    ticket = drv.admit_inbound(rec.id, "topic message")
    await drv.send_user_turn(rec, "topic message", tg_message_id=55,
                             inbound_token=ticket)
    await transcript_runs.settle()
    return rec, sid


# --- the finalize summaries -------------------------------------------------------

#: A finalize through the real ``tools._finalize_engagement``: the terminal
#: flip is durable, then ``stop()`` before the first summary's receipt.
SUMMARY_CHILD = r"""
import json
from unittest.mock import AsyncMock, MagicMock
import agent as agent_mod
import tools
from engagement_registry import EngagementRegistry
from memory import capture as cap
driver_kind, tombstone, cwd, root, sid = sys.argv[5:10]
agent_mod.active_semantic_memory = None
tools.EXECUTOR_CWD = cwd
tools._ENGAGEMENTS_ROOT = root
real_record = cap.record

async def record(item):
    if isinstance(item, cap.Summary):
        stop()
    return await real_record(item)
cap.record = record

async def main():
    reg = EngagementRegistry(tombstone_path=tombstone, bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver=driver_kind,
        task="t", origin=json.loads(sys.argv[10]), topic_id=None)
    if driver_kind == "in_casa":
        await reg.persist_session_id(rec.id, sid)
    open(os.path.join(os.path.dirname(marker), "engagement-id"), "w").write(rec.id)
    tools.init_tools(channel_manager=MagicMock(), bus=MagicMock(notify=AsyncMock()),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                     trigger_registry=MagicMock(), engagement_registry=reg)
    await tools._finalize_engagement(
        rec, outcome="completed", text="done", artifacts=[], next_steps=[],
        driver=MagicMock(cancel=AsyncMock()))
asyncio.run(main())
"""


@pytest.mark.parametrize("driver", ["in_casa", "claude_code"])
async def test_a_summary_row_blocks_until_its_summaries_are_captured(
        home, monkeypatch, driver):
    """A real SIGKILL after the terminal flip and before the summary's
    receipt leaves the summary in no table and the ``summary`` row
    unresolved. Every transcript in the engagement's folder — its own and a
    sibling engagement's in the same shared folder — survives every reaper
    pass; a claude_code workspace survives every workspace sweep past its
    retention. Red case: a summary with no row lets the reaper delete
    them."""
    from drivers import workspace
    from engagement_registry import EngagementRegistry
    from engagement_transcript_reaper import reap_engagement_transcripts
    from test_workspace_sweeper import _write_meta

    cwd, root = str(home.tmp / "executor-cwd"), home.tmp / "engagements"
    root.mkdir()
    monkeypatch.setattr(tools, "EXECUTOR_CWD", cwd)
    monkeypatch.setattr(tools, "_ENGAGEMENTS_ROOT", str(root))
    tombstone, sid = home.tmp / "engagements.json", str(uuid.uuid4())
    kill_child(SUMMARY_CHILD, home.store, home.tmp, driver, str(tombstone),
               cwd, str(root), sid, json.dumps(ORIGIN),
               env={"CLAUDE_CONFIG_DIR": os.environ["CLAUDE_CONFIG_DIR"]})
    eid = (home.tmp / "engagement-id").read_text()

    assert [r["status"] for r in json.loads(tombstone.read_text())] == ["completed"]
    assert summaries(home.store) == []
    expected_dir = cwd if driver == "in_casa" else str(root / eid)
    (row,) = rows(home.store)
    assert (row["kind"], row["directory"], row["session_id"],
            row["confirmed_at"]) == ("summary", expected_dir, None, None)
    assert unresolved(home.store) == 1

    if driver == "in_casa":
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await reg.load()
        sibling = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin=dict(ORIGIN), topic_id=None)
        other = str(uuid.uuid4())
        await reg.persist_session_id(sibling.id, other)
        await reg.mark_completed(sibling.id, time.time())
        mine, theirs = write_transcript(cwd, sid), write_transcript(cwd, other)
        for _ in range(2):
            await reap_engagement_transcripts(reg, now=time.time() + 400 * 86400)
            await asyncio.to_thread(transcript_runs.retention_pass, home.store,
                                    now=after_retention())
        assert mine.exists() and theirs.exists()
    else:
        ws = root / eid
        (ws / ".home" / ".claude" / "projects").mkdir(parents=True)
        _write_meta(ws, status="COMPLETED", retention_iso="2020-01-01T00:00:00Z")
        monkeypatch.setattr(workspace, "CONTROL_ROOT", workspace.CONTROL_ROOT)
        for _ in range(2):
            await workspace._sweep_workspaces(engagements_root=str(root),
                                              log_root=str(home.tmp / "logs"))
        assert ws.is_dir()


async def test_a_lost_terminal_flip_confirms_its_summary_row(home, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import FinalizeResult, _finalize_engagement, init_tools

    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)
    reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"), bus=None)
    rec = await reg.create(kind="executor", role_or_type="configurator",
                           driver="in_casa", task="t", origin=dict(ORIGIN),
                           topic_id=None)
    await reg.mark_completed(rec.id, time.time())
    init_tools(channel_manager=MagicMock(), bus=MagicMock(notify=AsyncMock()),
               specialist_registry=MagicMock(), mcp_registry=MagicMock(),
               trigger_registry=MagicMock(), engagement_registry=reg)
    result = await _finalize_engagement(
        rec, outcome="completed", text="done", artifacts=[], next_steps=[],
        driver=MagicMock(cancel=AsyncMock()))
    await transcript_runs.settle()
    assert result is FinalizeResult.ALREADY_TERMINAL
    (row,) = rows(home.store)
    assert row["kind"] == "summary" and row["confirmed_at"] is not None
    assert summaries(home.store) == []


# --- every capture path has its row ------------------------------------------------

#: Every function outside ``memory/`` that hands an item to capture, and so
#: every capture path: the resident turn's
#: opening and messages, the engagement turn's operator text and answers,
#: the delegated request and answer, the finalize summaries.
CAPTURE_PATHS = {
    "agent.py::_TurnCapture._opening",
    "agent.py::_TurnCapture._message",
    "drivers/in_casa_driver.py::_EngagementCapture._kept",
    "tools.py::_capture_delegated",
    "tools.py::_capture_summary",
}


def _qualified(tree: ast.Module):
    """``(qualname, node)`` of every function, methods included."""
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f"{prefix}{child.name}", child))
                walk(child, f"{prefix}{child.name}.")
    walk(tree, "")
    return out


def _attr_calls(node, attrs):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr in attrs
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id in ("capture", "memory_capture", "cap")]


async def test_the_capture_path_list_is_complete():
    """Each capture call site outside ``memory/``, found by name, is one of
    the listed paths, and every call of a listed path is awaited as one of
    its run's obligations (``<run>.kept(...)``), so it has a registered row
    when it is called. Red case: an unlisted capture call."""
    found, sources = set(), {}
    for path in sorted(CODE_ROOT.rglob("*.py")):
        rel = path.relative_to(CODE_ROOT).as_posix()
        if rel.startswith("memory/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sources[rel] = tree
        for name, fn in _qualified(tree):
            if _attr_calls(fn, {"record", "append"}) and not any(
                    _attr_calls(inner, {"record", "append"})
                    for _n, inner in _qualified(fn)):
                found.add(f"{rel}::{name}")
    assert found == CAPTURE_PATHS

    kept_args, calls = set(), []
    for listed in CAPTURE_PATHS:
        rel, qual = listed.split("::")
        leaf = qual.rsplit(".", 1)[-1]
        for n in ast.walk(sources[rel]):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == leaf:
                calls.append((listed, n))
            if isinstance(f, ast.Attribute) and f.attr == "kept":
                kept_args.update(id(a) for a in n.args)
    assert {listed for listed, _n in calls} == CAPTURE_PATHS
    assert [listed for listed, n in calls if id(n) not in kept_args] == []


async def test_every_run_kind_registers_before_its_first_capture(
        home, monkeypatch):
    """A resident turn, an engagement turn, a delegated run and a finalize
    each find their row present, unconfirmed and of their kind at every
    capture call. Red case: a delegated request captured before its run is
    registered.
    """
    from test_delegated_transcript_retention import _Harness
    seen = []
    real = capture.record

    async def record(item):
        open_kinds = {r["kind"] for r in rows(home.store)
                      if r["confirmed_at"] is None}
        seen.append((getattr(item, "eligibility", "summary"), open_kinds))
        return await real(item)
    monkeypatch.setattr(capture, "record", record)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)

    sid = str(uuid.uuid4())
    await _resident_turn(home, [_init(sid), _said("resident answer", sid),
                                _mk_result(sid)])
    await _engagement_turn()

    cfg = _delegated_cfg(home)
    h = _Harness()
    monkeypatch.setattr(tools, "ClaudeSDKClient", h.client_cls())
    token = agent_mod.origin_var.set(dict(ORIGIN, cid="c"))
    try:
        await tools._run_delegated_agent(cfg, "delegated task", "")
    finally:
        agent_mod.origin_var.reset(token)
    await transcript_runs.settle()

    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"), bus=None)
    rec = await reg.create(kind="executor", role_or_type="configurator",
                           driver="in_casa", task="t", origin=dict(ORIGIN),
                           topic_id=None)
    tools.init_tools(channel_manager=MagicMock(), bus=MagicMock(notify=AsyncMock()),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                     trigger_registry=MagicMock(), engagement_registry=reg)
    await tools._finalize_engagement(
        rec, outcome="completed", text="done", artifacts=[], next_steps=[],
        driver=MagicMock(cancel=AsyncMock()))
    await transcript_runs.settle()

    expected = {
        "write_trusted_turn": "resident", "resident_message": "resident",
        "engagement_turn": "engagement", "engagement_answer": "engagement",
        "delegated_request": "delegation", "delegated_answer": "delegation",
        "summary": "summary",
    }
    assert [e for e, _k in seen] == [
        "write_trusted_turn", "resident_message", "engagement_turn",
        "engagement_answer", "delegated_request", "delegated_answer",
        "summary", "summary"]
    for eligibility, open_kinds in seen:
        assert open_kinds == {expected[eligibility]}, (eligibility, open_kinds)
    assert all(r["confirmed_at"] is not None for r in rows(home.store))


def _delegated_cfg(home):
    from test_delegate_to_agent import _specialist_cfg
    cfg = _specialist_cfg()
    cfg.cwd = str(home.tmp / "agent-home" / "finance")
    cfg.memory.token_budget = 1000
    return cfg


async def test_a_summary_run_is_one_row_holding_its_folder(home):
    """Whatever sessions the record names, the finalize's summary run is one
    row naming the engagement's folder; until it is confirmed it holds every
    transcript there, and close confirms it."""
    from engagement_registry import JOB_SIDS_KEY, EngagementRegistry
    a, b, sibling = (str(uuid.uuid4()) for _ in range(3))
    reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa", task="t",
        origin=dict(ORIGIN, job={JOB_SIDS_KEY: [a, b]}), topic_id=None)
    await reg.persist_session_id(rec.id, b)
    run = await tools._register_summary_run(rec, allowed=True)
    (row,) = rows(home.store)
    assert (row["kind"], row["directory"], row["session_id"]) == (
        "summary", tools.EXECUTOR_CWD, None)
    for s in (a, b, sibling):
        assert transcript_runs.blocks_deletion(
            home.store, directory=tools.EXECUTOR_CWD, session_id=s)
    run.close()
    await transcript_runs.settle()
    assert rows(home.store)[0]["confirmed_at"] is not None
    assert not transcript_runs.blocks_deletion(
        home.store, directory=tools.EXECUTOR_CWD, session_id=sibling)


@pytest.mark.parametrize("before,during", [(False, True), (True, False)])
async def test_a_reload_during_finalize_never_captures_an_uncovered_summary(
        home, monkeypatch, before, during):
    """Whether the summary is written is decided once, before the terminal
    flip, and both the summary run's registration and the capture read that
    decision: a memory setting changed during the teardown (a reload) never
    yields a summary no run covers, nor a registered run with nothing
    owed. Red case: the tail reading the setting again."""
    import delegated_memory
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    setting = {"writes": before}
    monkeypatch.setattr(delegated_memory, "role_writes_memory",
                        lambda role: setting["writes"])
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)

    async def cancel(engagement):
        setting["writes"] = during           # the reload lands in the teardown
    reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"), bus=None)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                           driver="in_casa", task="t", origin=dict(ORIGIN),
                           topic_id=None)
    init_tools(channel_manager=MagicMock(), bus=MagicMock(notify=AsyncMock()),
               specialist_registry=MagicMock(), mcp_registry=MagicMock(),
               trigger_registry=MagicMock(), engagement_registry=reg)
    await _finalize_engagement(
        rec, outcome="completed", text="done", artifacts=[], next_steps=[],
        driver=MagicMock(cancel=cancel))
    await transcript_runs.settle()
    assert setting["writes"] is during
    if before:
        (row,) = rows(home.store)
        assert row["kind"] == "summary" and row["confirmed_at"] is not None
        assert len(summaries(home.store)) == 1
    else:
        assert rows(home.store) == [] and summaries(home.store) == []


async def test_a_finalize_cancelled_before_its_tail_confirms_its_summary_row(
        home, monkeypatch):
    """The flip lands, then the finalize is cancelled while it waits on the
    driver (before the tail exists): no capture is outstanding, so the
    summary run is confirmed rather than left holding its folder. Red case:
    a row whose cleanup started only with the tail."""
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)
    reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"), bus=None)
    rec = await reg.create(kind="executor", role_or_type="configurator",
                           driver="in_casa", task="t", origin=dict(ORIGIN),
                           topic_id=None)
    init_tools(channel_manager=MagicMock(), bus=MagicMock(notify=AsyncMock()),
               specialist_registry=MagicMock(), mcp_registry=MagicMock(),
               trigger_registry=MagicMock(), engagement_registry=reg)
    waiting = asyncio.Event()

    async def finalize_summary(engagement, outcome):
        waiting.set()
        await asyncio.Event().wait()
    driver = MagicMock(cancel=AsyncMock(), finalize_summary=finalize_summary)
    task = asyncio.create_task(_finalize_engagement(
        rec, outcome="completed", text="done", artifacts=[], next_steps=[],
        driver=driver))
    await asyncio.wait_for(waiting.wait(), 5)
    assert reg.get(rec.id).status == "completed"      # the flip landed
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transcript_runs.settle()
    (row,) = rows(home.store)
    assert row["kind"] == "summary" and row["confirmed_at"] is not None
    assert summaries(home.store) == []


@pytest.mark.parametrize("path", ["resident", "engagement", "delegation", "summary"])
async def test_a_run_cancelled_during_its_registration_confirms_its_row(
        home, monkeypatch, path):
    """The registration insert commits, then the run is cancelled before
    the worker returns: nothing was captured, so the row is adopted and
    confirmed rather than left holding its folder. Red case: the committed
    row abandoned by the cancelled wait."""
    import threading
    import time as _time

    from engagement_registry import EngagementRegistry
    from test_delegated_transcript_retention import _Harness

    committed = threading.Event()
    real = transcript_runs.register

    def register(*a, **k):
        run_id = real(*a, **k)
        committed.set()
        _time.sleep(0.3)                    # committed; the worker not back
        return run_id
    monkeypatch.setattr(transcript_runs, "register", register)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)

    if path == "resident":
        sid = str(uuid.uuid4())
        ScriptedToolClient.reset([_init(sid), _said("x", sid), _mk_result(sid)])
        agent = _make_agent(home.tmp)
        cm = patch("sdk_client_pool._default_make_client", ScriptedToolClient)
        cm.start()
        coro = agent._process(_operator_msg("hello", "901"))
    elif path == "engagement":
        from drivers.in_casa_driver import InCasaDriver

        class _Client:
            async def query(self, prompt):
                return None

            async def receive_response(self):
                if False:
                    yield None
        drv = InCasaDriver(topic_stream_factory=lambda tid: _TopicStream())
        rec = _engagement_record()
        drv._clients[rec.id] = _Client()
        drv._locks[rec.id] = asyncio.Lock()
        ticket = drv.admit_inbound(rec.id, "m")
        coro = drv.send_user_turn(rec, "m", tg_message_id=55,
                                  inbound_token=ticket)
        cm = None
    elif path == "delegation":
        cfg = _delegated_cfg(home)
        monkeypatch.setattr(tools, "ClaudeSDKClient", _Harness().client_cls())
        coro = tools._run_delegated_agent(cfg, "task", "")
        cm = None
    else:
        reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"), bus=None)
        rec = await reg.create(kind="executor", role_or_type="configurator",
                               driver="in_casa", task="t", origin=dict(ORIGIN),
                               topic_id=None)
        tools.init_tools(channel_manager=MagicMock(),
                         bus=MagicMock(notify=AsyncMock()),
                         specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                         trigger_registry=MagicMock(), engagement_registry=reg)
        coro = tools._finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[], next_steps=[],
            driver=MagicMock(cancel=AsyncMock()))
        cm = None
    if path == "delegation":
        task = asyncio.create_task(_with_origin_task(coro))
    else:
        task = asyncio.create_task(coro)
    try:
        await asyncio.to_thread(committed.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if cm is not None:
            cm.stop()
    await transcript_runs.settle()
    (row,) = rows(home.store)
    assert row["confirmed_at"] is not None, row
    assert events(home.store) == []


async def _with_origin_task(coro):
    token = agent_mod.origin_var.set(dict(ORIGIN, cid="c"))
    try:
        return await coro
    finally:
        agent_mod.origin_var.reset(token)
