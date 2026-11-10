"""An unconfirmed run holds its directory, whatever it names (INV-MEM-031).

Which session an unconfirmed ``transcript_runs`` row names is no evidence:
a resumed run may not have named its session yet, a naming write may have
failed, and a run may have moved from one session to another. So the gate
asks one thing: is any row in this directory unconfirmed.

Each sequence below ends with a deleter that resolves by session, and is red
against a gate that trusts the row's session. Real
SQLite, real transcript files, the real deleters; the kill cases SIGKILL a
child process this test started, by its own pid.
"""
from __future__ import annotations

import asyncio
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
          "_origin_route": "telegram", "_origin_clearance": "private",
          "_tg_bot": 111}


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
from memory import transcript_runs
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
        # The first turn's confirmation is a detached write; it lands before
        # the second turn starts, as it would on an idle machine (a loaded
        # one has let the kill overtake it).
        await transcript_runs.settle()
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
from channels.tg_address import TgAddress
from test_agent_process import _mk_result
from transcript_runs_support import write_transcript
import job_inbox
from pathlib import Path
sid, tombstone, cwd = sys.argv[5:8]
job_inbox.INBOX_ROOT = Path(os.path.dirname(tombstone)) / "job-inbox"

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

from turn_scripted_session import TurnScriptedSession, attach_session

class Client(TurnScriptedSession):
    async def receive_response(self):
        yield SystemMessage(subtype="init", data={"session_id": sid})
        write_transcript(cwd, sid)
        yield AssistantMessage(content=[TextBlock(text="answer")], model="m",
                               session_id=sid)
        yield _mk_result(sid)

async def main():
    reg = EngagementRegistry(tombstone_path=tombstone)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                           driver="in_casa", task="t",
                           origin=json.loads(sys.argv[8]))
    open(os.path.join(os.path.dirname(marker), "engagement-id"), "w").write(rec.id)
    drv = InCasaDriver(persist_session_id=reg.persist_session_id)
    attach_session(drv, rec, Client())
    drv.submit_operator(rec, "topic message", tg_message=TgAddress(111, 4242, 55))
    await asyncio.Event().wait()    # the turn runs on the driver's tasks
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
    reg = EngagementRegistry(tombstone_path=str(tombstone))
    await reg.load()
    assert reg.get(eid).sdk_session_id == sid
    await reg.mark_completed(eid, time.time())
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
    reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"))
    sibling = await reg.create(kind="specialist", role_or_type="finance",
                               driver="in_casa", task="t", origin={})
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


async def test_a_cwd_reload_never_moves_where_an_engagement_is_held(
        home, monkeypatch):
    """A specialist engagement opens in folder A; a configuration reload
    then points the specialist at B before its next turn. That turn's run
    registers A (the cwd persisted at the first open), and with the
    configuration back at A the real reaper keeps A's transcript while that
    run is unconfirmed. Red case: the folder read from the configuration at
    registration (B)."""
    from claude_agent_sdk import ClaudeAgentOptions

    import drivers.in_casa_driver as drv_mod
    from drivers.in_casa_driver import InCasaDriver, _EngagementCapture
    from engagement_registry import LAUNCH_CWD_KEY, EngagementRegistry
    from engagement_transcript_reaper import reap_engagement_transcripts
    from test_engagement_transcript_reaper import _specialists

    a = str(home.tmp / "agent-home" / "finance-a")
    b = str(home.tmp / "agent-home" / "finance-b")

    class _Client:
        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            return self

    monkeypatch.setattr(drv_mod, "ClaudeSDKClient", _Client)
    _specialists(monkeypatch, finance=SimpleNamespace(role="finance", cwd=a))
    reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"))
    rec = await reg.create(kind="specialist", role_or_type="finance",
                           driver="in_casa", task="t", origin=dict(ORIGIN))
    await InCasaDriver().open(
        rec, ClaudeAgentOptions(cwd=a))
    sid = str(uuid.uuid4())
    await reg.persist_session_id(rec.id, sid)          # persists the origin
    assert rec.origin[LAUNCH_CWD_KEY] == a

    _specialists(monkeypatch, finance=SimpleNamespace(role="finance", cwd=b))
    turn = _EngagementCapture(rec)
    await turn.register(sid)
    assert [r["directory"] for r in rows(home.store)] == [a]
    await reg.mark_completed(rec.id, time.time())
    _specialists(monkeypatch, finance=SimpleNamespace(role="finance", cwd=a))
    reloaded = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"))
    await reloaded.load()
    path = write_transcript(a, sid)
    await reap_engagement_transcripts(reloaded, now=time.time() + 400 * 86400)
    assert path.exists()
    turn.run.close()


async def test_a_held_session_keeps_its_pointer_and_goes_on_a_later_sweep(home):
    """An expired, confirmed resident session is due while another turn in
    the same folder is unconfirmed: the sweep keeps its pointer and its
    transcript; once that turn is confirmed, the next sweep deletes both.
    Red case: the pointer dropped at the first sweep, so nothing ever
    revisits the transcript."""
    sid = str(uuid.uuid4())
    path = write_transcript(AGENT_HOME, sid)
    reg_path = home.tmp / "sessions.json"
    reg = SessionRegistry(str(reg_path))
    async with reg._lock:
        reg._data["telegram-held"] = {
            "agent": "assistant", "sdk_session_id": sid,
            "last_active": (datetime.now(timezone.utc)
                            - timedelta(days=60)).isoformat()}
        await reg._save_locked()
    other = transcript_runs.register(home.store, kind="resident",
                                     role="assistant", directory=AGENT_HOME,
                                     session_id=str(uuid.uuid4()))
    sweeper = SessionSweeper(registry=reg, directory_for=lambda role: AGENT_HOME)
    await sweeper._sweep_once()
    assert reg.get("telegram-held") is not None and path.exists()
    transcript_runs.confirm(home.store, other)
    await sweeper._sweep_once()
    assert reg.get("telegram-held") is None and not path.exists()


async def test_a_rebuilt_or_resumed_engagement_stays_in_its_launch_folder(
        home, monkeypatch):
    """An engagement opened in A is resumed, twice, after a reload pointed
    its specialist at B: every client still launches in A, so at day 31 the
    reaper deletes its session there. Red case: a resume taking the
    configured cwd, leaving the session it continues in B where nothing
    reaps it."""
    from claude_agent_sdk import ClaudeAgentOptions

    import drivers.in_casa_driver as drv_mod
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry
    from engagement_transcript_reaper import reap_engagement_transcripts
    from test_engagement_transcript_reaper import _specialists

    a = str(home.tmp / "agent-home" / "finance-a")
    b = str(home.tmp / "agent-home" / "finance-b")
    launched = []

    class _Client:
        def __init__(self, options):
            launched.append(options.cwd)
            self._closed = asyncio.Event()

        async def __aenter__(self):
            return self

        async def receive_messages(self):
            await self._closed.wait()
            return
            yield

        async def disconnect(self):
            self._closed.set()

    monkeypatch.setattr(drv_mod, "ClaudeSDKClient", _Client)
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda engagement, sid: ClaudeAgentOptions(cwd=b, resume=sid))
    _specialists(monkeypatch, finance=SimpleNamespace(role="finance", cwd=a))
    reg = EngagementRegistry(tombstone_path=str(home.tmp / "e.json"))
    rec = await reg.create(kind="specialist", role_or_type="finance",
                           driver="in_casa", task="t", origin=dict(ORIGIN))
    drv = InCasaDriver(record_lookup=reg.get)
    drv._persist_applied_profiles = lambda engagement: asyncio.sleep(0)
    await drv.open(rec, ClaudeAgentOptions(cwd=a))
    sid = str(uuid.uuid4())
    await reg.persist_session_id(rec.id, sid)
    _specialists(monkeypatch, finance=SimpleNamespace(role="finance", cwd=b))
    for _ in range(2):
        await drv.cancel(rec)                    # suspended, then resumed
        await drv.resume(rec, sid)
    assert launched == [a, a, a]
    await reg.mark_completed(rec.id, time.time())
    await drv.cancel(rec)
    path = write_transcript(a, sid)
    await reap_engagement_transcripts(reg, now=time.time() + 31 * 86400)
    assert not path.exists()


async def test_no_run_of_the_session_starts_between_the_sweeps_check_and_delete(
        home, monkeypatch):
    """The sweep's final check passes; a turn on the same session key then
    tries to start and register its run before the delete. Holding the
    key's session-write gate across check, eviction and delete, the sweep
    deletes with no unconfirmed run in the folder, and the turn starts
    after. Red case: a check made outside that gate, which lets the turn's
    row land between check and delete."""
    import session_sweeper
    from session_gate import session_write_gate

    sid, key = str(uuid.uuid4()), "telegram-interleaved"
    path = write_transcript(AGENT_HOME, sid)
    reg = SessionRegistry(str(home.tmp / "sessions.json"))
    async with reg._lock:
        reg._data[key] = {
            "agent": "assistant", "sdk_session_id": sid,
            "last_active": (datetime.now(timezone.utc)
                            - timedelta(days=60)).isoformat()}
        await reg._save_locked()
    loop = asyncio.get_running_loop()
    turn_rows = []

    async def turn():
        async with session_write_gate(key):
            turn_rows.append(transcript_runs.register(
                home.store, kind="resident", role="assistant",
                directory=AGENT_HOME))

    real_blocked = transcript_runs.blocked

    def blocked(**kw):
        answer = real_blocked(**kw)
        loop.call_soon_threadsafe(lambda: started.append(loop.create_task(turn())))
        return answer
    started: list = []
    monkeypatch.setattr(transcript_runs, "blocked", blocked)
    held_at_delete = []
    real_delete = session_sweeper._sdk_delete_session

    def delete(session_id, directory=None):
        held_at_delete.append(transcript_runs.blocks_deletion(
            home.store, directory=directory))
        return real_delete(session_id, directory)
    monkeypatch.setattr(session_sweeper, "_sdk_delete_session", delete)

    await SessionSweeper(registry=reg,
                         directory_for=lambda role: AGENT_HOME)._sweep_once()
    await asyncio.gather(*started)
    assert held_at_delete == [False]
    assert not path.exists() and reg.get(key) is None
    assert len(turn_rows) == 1
