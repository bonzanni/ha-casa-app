"""An unconfirmed run holds its directory, whatever it names (INV-MEM-031).

Which session an unconfirmed ``transcript_runs`` row names is no evidence:
a resumed run may not have named its session yet, a naming write may have
failed, a run may have moved from one session to another, and a finalize's
engagement may publish a session after its row was registered. So the gate
asks one thing: is any row in this directory unconfirmed.

Each sequence below ends with a deleter that resolves by session, and is red
against a gate that trusts the row's session. Real
SQLite, real transcript files, the real deleters; the kill cases SIGKILL a
child process this test started, by its own pid.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import tools
from memory import transcript_runs
from session_registry import SessionRegistry
from session_sweeper import SessionSweeper
from test_memory_capture import mem  # noqa: F401 — a fixture
from transcript_runs_support import kill_child, rows, write_transcript

pytestmark = [pytest.mark.asyncio]

AGENT_HOME = "/config/agent-home/assistant"
ORIGIN = {"role": "assistant", "channel": "telegram", "chat_id": "4242",
          "_origin_route": "telegram", "_origin_clearance": "private"}


@pytest.fixture
def home(tmp_path, monkeypatch, mem):  # noqa: F811 — the imported fixture
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setattr(transcript_runs, "_background", set())
    return SimpleNamespace(store=mem, tmp=tmp_path)


def _env():
    return {"CLAUDE_CONFIG_DIR": os.environ["CLAUDE_CONFIG_DIR"]}


async def _sweep_resident(sessions_path, *, directory=AGENT_HOME) -> None:
    """The real time-to-live sweep over the child's own pointers, far past
    every retention."""
    reg = SessionRegistry(str(sessions_path))
    await SessionSweeper(
        registry=reg, now=lambda: datetime.now(timezone.utc) + timedelta(days=400),
        directory_for=lambda role: directory)._sweep_once()


# --- a resumed resident run is named before its query ---------------------------

RESUMED_CHILD = r"""
from pathlib import Path
from unittest.mock import patch
from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock
from test_agent_process import ScriptedToolClient, _make_agent, _mk_result
from test_memory_capture import _operator_msg
from transcript_runs_support import write_transcript
sid, home = sys.argv[5:7]

class Client(ScriptedToolClient):
    turns = 0

    async def receive_response(self):
        Client.turns += 1
        if Client.turns == 1:
            yield SystemMessage(subtype="init", data={"session_id": sid})
            yield AssistantMessage(content=[TextBlock(text="first")], model="m",
                                   session_id=sid)
            yield _mk_result(sid)
            return
        # the warm session's second turn: queried, nothing streamed yet
        write_transcript("/config/agent-home/assistant", sid)
        stop()
        yield None

ScriptedToolClient.reset([])
agent = _make_agent(Path(home))
async def main():
    with patch("sdk_client_pool._default_make_client", Client):
        await agent._process(_operator_msg("hello", "901"))
        await agent._process(_operator_msg("again", "902"))
asyncio.run(main())
"""


async def test_a_resumed_resident_run_killed_before_streaming_keeps_its_transcript(home):
    """Warm reuse, then a SIGKILL after the second query and before any
    stream message: the second turn's row is unconfirmed (and unnamed), and
    the real sweeper keeps the resumed session's transcript. Red case: a
    gate under which an unnamed row holds nothing per session."""
    sid, child_home = str(uuid.uuid4()), home.tmp / "child"
    child_home.mkdir()
    kill_child(RESUMED_CHILD, home.store, home.tmp, sid, str(child_home),
               env=_env())
    first, second = rows(home.store)
    assert first["confirmed_at"] is not None
    assert second["confirmed_at"] is None
    path = write_transcript(AGENT_HOME, sid)
    await _sweep_resident(child_home / "sessions.json")
    assert path.exists()


FAILED_NAMING_RESIDENT_CHILD = r"""
import sqlite3
from pathlib import Path
from unittest.mock import patch
from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock
from memory import transcript_runs
import session_registry
from test_agent_process import ScriptedToolClient, _make_agent, _mk_result
from test_memory_capture import _operator_msg
from transcript_runs_support import write_transcript
sid, home = sys.argv[5:7]

def locked(*a, **k):
    raise sqlite3.OperationalError("database is locked")
transcript_runs.name_session = locked
real_register = session_registry.SessionRegistry.register

async def register(self, *a, **k):
    await real_register(self, *a, **k)
    stop()          # the pointer is published; the run is not confirmed
session_registry.SessionRegistry.register = register

class Client(ScriptedToolClient):
    async def receive_response(self):
        write_transcript("/config/agent-home/assistant", sid)
        async for m in super().receive_response():
            yield m

ScriptedToolClient.reset([
    SystemMessage(subtype="init", data={"session_id": sid}),
    AssistantMessage(content=[TextBlock(text="said")], model="m", session_id=sid),
    _mk_result(sid)])
agent = _make_agent(Path(home))
with patch("sdk_client_pool._default_make_client", Client):
    asyncio.run(agent._process(_operator_msg("hello", "901")))
"""


async def test_a_failed_naming_keeps_the_resident_transcript(home):
    """Every naming write fails, the turn publishes its pointer and is
    killed before its confirmation: the row is unnamed and unconfirmed, and
    the real sweeper keeps the transcript the pointer names. Red case: a
    gate under which an unnamed row holds nothing per session."""
    sid, child_home = str(uuid.uuid4()), home.tmp / "child"
    child_home.mkdir()
    kill_child(FAILED_NAMING_RESIDENT_CHILD, home.store, home.tmp, sid,
               str(child_home), env=_env())
    (row,) = rows(home.store)
    assert (row["session_id"], row["confirmed_at"]) == (None, None)
    assert SessionRegistry(str(child_home / "sessions.json")).get(
        next(iter(SessionRegistry(str(child_home / "sessions.json")).all_entries())))[
            "sdk_session_id"] == sid
    path = write_transcript(AGENT_HOME, sid)
    await _sweep_resident(child_home / "sessions.json")
    assert path.exists()


FAILED_NAMING_ENGAGEMENT_CHILD = r"""
import sqlite3
from types import SimpleNamespace
from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ResultMessage
from memory import capture as cap, transcript_runs
import tools
from drivers.in_casa_driver import InCasaDriver
from engagement_registry import EngagementRegistry
from test_agent_process import _mk_result
from transcript_runs_support import write_transcript
sid, tombstone, cwd = sys.argv[5:8]

class Reg:
    def get(self, role):
        return SimpleNamespace(role=role, cwd=cwd)
tools._specialist_registry = Reg()

def locked(*a, **k):
    raise sqlite3.OperationalError("database is locked")
transcript_runs.name_session = locked
real_record = cap.record

async def record(item):
    receipt = await real_record(item)
    if getattr(item, "speaker_kind", "") == "specialist":
        stop()      # captured, result not yet confirmed
    return receipt
cap.record = record

class Stream:
    async def emit(self, text): return None
    async def finalize(self, text): return None

class Client:
    async def query(self, prompt): return None
    async def receive_response(self):
        yield SystemMessage(subtype="init", data={"session_id": sid})
        write_transcript(cwd, sid)
        yield AssistantMessage(content=[TextBlock(text="answer")], model="m",
                               session_id=sid)
        yield _mk_result(sid)

async def main():
    reg = EngagementRegistry(tombstone_path=tombstone, bus=None)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                           driver="in_casa", task="t",
                           origin=json.loads(sys.argv[8]), topic_id=99)
    open(os.path.join(os.path.dirname(marker), "engagement-id"), "w").write(rec.id)
    drv = InCasaDriver(topic_stream_factory=lambda tid: Stream(),
                       persist_session_id=reg.persist_session_id)
    drv._clients[rec.id] = Client()
    drv._locks[rec.id] = asyncio.Lock()
    ticket = drv.admit_inbound(rec.id, "topic message")
    await drv.send_user_turn(rec, "topic message", tg_message_id=55,
                             inbound_token=ticket)
import json
asyncio.run(main())
"""


async def test_a_failed_naming_keeps_the_engagement_transcript(home, monkeypatch):
    """An in_casa engagement turn whose naming write fails: the record names
    the session, the run is killed after its capture and before its
    confirmation, the engagement later ends; the real reaper keeps the
    transcript. Red case: the unnamed row ignored per session."""
    from engagement_registry import EngagementRegistry
    from engagement_transcript_reaper import reap_engagement_transcripts
    from test_engagement_transcript_reaper import _specialists

    sid, cwd = str(uuid.uuid4()), str(home.tmp / "agent-home" / "finance")
    tombstone = home.tmp / "engagements.json"
    kill_child(FAILED_NAMING_ENGAGEMENT_CHILD, home.store, home.tmp, sid,
               str(tombstone), cwd, json.dumps(ORIGIN), env=_env())
    (row,) = rows(home.store)
    assert (row["kind"], row["directory"], row["session_id"],
            row["confirmed_at"]) == ("engagement", cwd, None, None)
    eid = (home.tmp / "engagement-id").read_text()
    _specialists(monkeypatch, finance=SimpleNamespace(role="finance", cwd=cwd))
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reg.load()
    assert reg.get(eid).sdk_session_id == sid
    await reg.mark_completed(eid, time.time())
    path = write_transcript(cwd, sid)
    await reap_engagement_transcripts(reg, now=time.time() + 400 * 86400)
    assert path.exists()


# --- a finalize whose engagement publishes a session after its row --------------

LATE_SESSION_FINALIZE_CHILD = r"""
from unittest.mock import AsyncMock, MagicMock
import agent as agent_mod
import tools
from engagement_registry import EngagementRegistry
from memory import capture as cap
tombstone, cwd, sid = sys.argv[5:8]
agent_mod.active_semantic_memory = None
tools.EXECUTOR_CWD = cwd
real_register = tools._register_summary_run
real_record = cap.record

async def record(item):
    if isinstance(item, cap.Summary):
        stop()
    return await real_record(item)
cap.record = record

async def main():
    reg = EngagementRegistry(tombstone_path=tombstone, bus=None)
    rec = await reg.create(kind="executor", role_or_type="configurator",
                           driver="in_casa", task="t",
                           origin=json.loads(sys.argv[8]), topic_id=None)

    async def register(engagement):
        run = await real_register(engagement)
        # the driver publishes its first session after the row's snapshot
        await reg.persist_session_id(rec.id, sid)
        return run
    tools._register_summary_run = register
    tools.init_tools(channel_manager=MagicMock(), bus=MagicMock(notify=AsyncMock()),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                     trigger_registry=MagicMock(), engagement_registry=reg)
    await tools._finalize_engagement(
        rec, outcome="completed", text="done", artifacts=[], next_steps=[],
        driver=MagicMock(cancel=AsyncMock()))
import json
asyncio.run(main())
"""


async def test_a_session_published_after_the_summary_row_is_kept(home, monkeypatch):
    """The summary run is registered while the engagement names no session;
    its first session is published after that, the flip lands, and the
    finalize is killed before the summary's receipt. The folder row holds
    the folder, so the real reaper keeps that session's transcript. Red
    case: a folder-only row ignored per session."""
    from engagement_registry import EngagementRegistry
    from engagement_transcript_reaper import reap_engagement_transcripts

    sid, cwd = str(uuid.uuid4()), str(home.tmp / "executor-cwd")
    tombstone = home.tmp / "engagements.json"
    monkeypatch.setattr(tools, "EXECUTOR_CWD", cwd)
    kill_child(LATE_SESSION_FINALIZE_CHILD, home.store, home.tmp,
               str(tombstone), cwd, sid, json.dumps(ORIGIN), env=_env())
    assert [(r["kind"], r["directory"], r["session_id"], r["confirmed_at"])
            for r in rows(home.store)] == [("summary", cwd, None, None)]
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reg.load()
    (rec,) = reg.terminal_records()
    assert rec.sdk_session_id == sid
    path = write_transcript(cwd, sid)
    await reap_engagement_transcripts(reg, now=time.time() + 400 * 86400)
    assert path.exists()


# --- an unnamed row in a shared folder ---------------------------------------------

async def test_an_unnamed_row_holds_a_shared_folder_until_it_is_confirmed(
        home, monkeypatch):
    """An unconfirmed, unnamed row in a specialist's shared folder keeps a
    sibling engagement's transcript there; once it is confirmed, the
    sibling's goes."""
    from engagement_registry import EngagementRegistry
    from engagement_transcript_reaper import reap_engagement_transcripts
    from test_engagement_transcript_reaper import _specialists

    cwd = str(home.tmp / "agent-home" / "finance")
    _specialists(monkeypatch, finance=SimpleNamespace(role="finance", cwd=cwd))
    run = transcript_runs.register(home.store, kind="engagement",
                                   role="finance", directory=cwd)
    reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"), bus=None)
    sibling = await reg.create(kind="specialist", role_or_type="finance",
                               driver="in_casa", task="t", origin={},
                               topic_id=None)
    other = str(uuid.uuid4())
    await reg.persist_session_id(sibling.id, other)
    await reg.mark_completed(sibling.id, time.time())
    path = write_transcript(cwd, other)
    later = time.time() + 400 * 86400

    await reap_engagement_transcripts(reg, now=later)
    assert path.exists()
    transcript_runs.confirm(home.store, run)
    await reap_engagement_transcripts(reg, now=later)
    assert not path.exists()


RETRY_FAILED_NAMING_CHILD = r"""
import sqlite3
from pathlib import Path
from unittest.mock import patch
from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock
from memory import transcript_runs
import session_registry
from test_agent_process import ScriptedToolClient, _make_agent, _mk_result, patch_retry_sleep
from test_memory_capture import _system_msg
from transcript_runs_support import write_transcript
a, b, home = sys.argv[5:8]
real_name = transcript_runs.name_session

def name_session(store, run_id, session_id, **kw):
    if session_id == b:
        raise sqlite3.OperationalError("database is locked")
    return real_name(store, run_id, session_id, **kw)
transcript_runs.name_session = name_session
real_register = session_registry.SessionRegistry.register

async def register(self, *args, **kw):
    await real_register(self, *args, **kw)
    stop()          # B is published; the run is not confirmed
session_registry.SessionRegistry.register = register

class Client(ScriptedToolClient):
    async def receive_response(self):
        async for m in super().receive_response():
            if isinstance(m, SystemMessage):
                write_transcript("/config/agent-home/assistant",
                                 m.data["session_id"])
            yield m

def said(text, sid):
    return AssistantMessage(content=[TextBlock(text=text)], model="m",
                            session_id=sid)
ScriptedToolClient.reset(
    [SystemMessage(subtype="init", data={"session_id": a}), said("one", a),
     ConnectionError("broken")],
    [SystemMessage(subtype="init", data={"session_id": b}), said("two", b),
     _mk_result(b)])
agent = _make_agent(Path(home))
with patch("sdk_client_pool._default_make_client", Client), patch_retry_sleep():
    asyncio.run(agent._process(_system_msg("ping")))
"""


async def test_a_run_that_moved_to_a_session_it_could_not_name_keeps_it(home):
    """A turn's first attempt runs in session A (named), its retry moves to
    session B whose naming write fails; B is published and the run is killed
    before its confirmation. The row still names A, and the real sweeper
    keeps B's transcript. Red case: a gate that trusts the row's session."""
    a, b, child_home = str(uuid.uuid4()), str(uuid.uuid4()), home.tmp / "child"
    child_home.mkdir()
    kill_child(RETRY_FAILED_NAMING_CHILD, home.store, home.tmp, a, b,
               str(child_home), env=_env())
    (row,) = rows(home.store)
    assert (row["session_id"], row["confirmed_at"]) == (a, None)
    reg = SessionRegistry(str(child_home / "sessions.json"))
    assert [e["sdk_session_id"] for e in reg.all_entries().values()] == [b]
    path_b = write_transcript(AGENT_HOME, b)
    await _sweep_resident(child_home / "sessions.json")
    assert path_b.exists()
