"""A delegated run's transcript is kept for its retention (INV-ENG-028).

``tools._run_delegated_agent`` launches one CLI session per call, in the
specialist's working directory, under a session id Casa chooses for that
launch and registers (``memory/transcript_runs.py``) before the request is
captured and before the client starts. The run is confirmed only once its
request's and answer's captures are durable (or the live rules capture
nothing), and its transcript is deleted only by the retention pass (or a
wipe), only the session its row names, 30 days after the run ended.

The fake client below stands in for the CLI: it writes ``<sid>.jsonl`` and
``<sid>/tool-results/x`` into the SDK's project folder for ``options.cwd`` as
its ``__aexit__`` completes. Deletion goes through the REAL public SDK
``delete_session`` against a real projects root under ``tmp_path``, and the
store is a real SQLite file. The kill cases SIGKILL a child process this test
started, by its own pid.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import claude_agent_sdk
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk._internal.sessions import (
    _canonicalize_path, _get_project_dir,
)

from error_kinds import ApiErrorTurn, ErrorKind

try:
    from tests.test_delegate_to_agent import (
        _origin, _specialist_cfg, _with_origin,
    )
except ImportError:
    from test_delegate_to_agent import _origin, _specialist_cfg, _with_origin

from memory import capture, transcript_runs
from test_memory_capture import captured_events
from test_memory_capture import mem  # noqa: F401 — a fixture
from transcript_runs_support import (
    after_retention, events, kill_child, rows, unresolved,
)

pytestmark = [pytest.mark.unit]

SIBLING = "22222222-2222-4222-8222-222222222222"
FALLBACK = "33333333-3333-4333-8333-333333333333"


#: A transcript shaped as the CLI writes it: the SDK's own lookup finds this session.
TRANSCRIPT = ('{"type":"user","message":{"role":"user","content":"Task: question"},'
              '"timestamp":"2026-11-06T10:00:00.000Z","cwd":"/x"}\n')


def _project(cwd: str) -> Path:
    return _get_project_dir(_canonicalize_path(cwd))


def _artifacts(folder: Path, sid: str) -> int:
    """The two top-level paths a session owns: ``<sid>.jsonl`` and ``<sid>/``."""
    return sum(p.exists() for p in (folder / f"{sid}.jsonl", folder / sid))


def _write_session(folder: Path, sid: str, body: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sid}.jsonl").write_text(body, encoding="utf-8")
    (folder / sid / "tool-results").mkdir(parents=True, exist_ok=True)
    (folder / sid / "tool-results" / "x").write_text(body, encoding="utf-8")


def _snapshot(folder: Path, sid: str) -> dict:
    return {
        "jsonl": (folder / f"{sid}.jsonl").read_bytes(),
        "tool": (folder / sid / "tool-results" / "x").read_bytes(),
    }


class _Harness:
    """Fake ``ClaudeSDKClient`` plus a recording wrapper around the real
    public ``delete_session``."""

    def __init__(self, mode: str = "return") -> None:
        self.mode = mode
        self.launches: list = []
        self.written: list[str] = []
        self.exits_done = 0
        self.deletes: list[tuple] = []
        self.deletes_before_exit = 0
        self.entered_receive = asyncio.Event()
        self.hold_exit: asyncio.Event | None = None
        self.exit_written: asyncio.Event | None = None
        self.raised = ApiErrorTurn(ErrorKind.API_ERROR)
        self.body = "own"               # what the CLI double writes as the transcript
        self.costs: list[float] = []    # each result's cumulative total_cost_usd
        self.on_receive = None          # awaited as the turn starts, inside its origin

    def client_cls(self):
        h = self

        class _Client:
            def __init__(self, options):
                self.options = options
                h.launches.append(options)
                sid = (getattr(options, "session_id", None)
                       or getattr(options, "resume", None))
                self.sid = sid if sid else FALLBACK

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                # The CLI flushes its transcript as its process ends.
                _write_session(_project(self.options.cwd), self.sid, h.body)
                h.written.append(self.sid)
                if h.exit_written is not None:
                    h.exit_written.set()
                if h.hold_exit is not None:
                    await h.hold_exit.wait()
                h.exits_done += 1
                return False

            async def query(self, prompt):
                return None

            async def get_mcp_status(self):
                # #1220: a pinned turn asks before its query; nothing pending
                return {"mcpServers": []}

            async def receive_response(self):
                h.entered_receive.set()
                if h.on_receive is not None:
                    await h.on_receive()
                if h.mode == "raise":
                    raise h.raised
                if h.mode == "cancel":
                    await asyncio.Event().wait()
                yield AssistantMessage(content=[TextBlock(text="answer")],
                                       model="claude-sonnet-4-6",
                                       stop_reason="end_turn")
                if h.mode == "cancel_after_text":
                    await asyncio.Event().wait()
                r = ResultMessage.__new__(ResultMessage)
                for k, v in dict(subtype="success", duration_ms=1,
                                 duration_api_ms=1, is_error=False,
                                 num_turns=1, session_id=self.sid,
                                 total_cost_usd=h.costs.pop(0) if h.costs else 0.0,
                                 usage={}, result="answer",
                                 stop_reason="end_turn",
                                 structured_output=None).items():
                    setattr(r, k, v)
                yield r

        return _Client

    def recording_delete(self):
        real = claude_agent_sdk.delete_session
        h = self

        def _delete(session_id, directory=None):
            h.deletes.append((session_id, directory))
            if h.exits_done < len(h.launches):
                h.deletes_before_exit += 1
            return real(session_id, directory)

        return _delete



@pytest.fixture
def env(tmp_path, monkeypatch, mem):  # noqa: F811 — the imported fixture
    import tools

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setattr(transcript_runs, "_background", set())
    cwd = tmp_path / "agent-home" / "finance"
    cwd.mkdir(parents=True)
    cfg = _specialist_cfg()
    cfg.cwd = str(cwd)
    folder = _project(str(cwd))
    _write_session(folder, SIBLING, "sibling")
    # A session of the same id in ANOTHER project must never be touched.
    other = _project(str(tmp_path / "elsewhere"))
    monkeypatch.setattr(tools, "_specialist_telemetry", None, raising=False)
    import agent as agent_mod
    return SimpleNamespace(tools=tools, cfg=cfg, folder=folder, other=other,
                           store=mem)


def _memory_role(cfg):
    """A memory-writing role: its exchange is captured."""
    cfg.memory.token_budget = 1000
    return cfg


def _install(monkeypatch, tools, h: _Harness) -> None:
    monkeypatch.setattr(tools, "ClaudeSDKClient", h.client_cls())
    monkeypatch.setattr(claude_agent_sdk, "delete_session", h.recording_delete())


async def _run(tools, cfg, h: _Harness):
    task = asyncio.ensure_future(_with_origin(
        tools._run_delegated_agent(cfg, "question", "", resolution=None),
        _origin()))
    if h.mode in ("cancel", "cancel_after_text"):
        await h.entered_receive.wait()
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
    return task


async def _finish(task, h: _Harness):
    if h.mode == "return":
        return await task
    if h.mode == "raise":
        with pytest.raises(ApiErrorTurn):
            await task
        return None
    with pytest.raises(asyncio.CancelledError):
        await task
    return None


@pytest.mark.parametrize("mode", ["return", "raise", "cancel"])
async def test_a_delegated_transcript_survives_the_run(env, monkeypatch, mode):
    """Red case: the per-run delete the runner made once its client exited."""
    h = _Harness(mode)
    _install(monkeypatch, env.tools, h)
    await _finish(await _run(env.tools, env.cfg, h), h)
    await transcript_runs.settle()

    (sid,) = h.written
    assert sid == h.launches[0].session_id
    assert h.deletes == []
    assert _artifacts(env.folder, sid) == 2
    (row,) = rows(env.store)
    assert (row["kind"], row["session_id"], row["directory"]) == (
        "delegation", sid, env.cfg.cwd)
    assert row["ended_at"] is not None


#: The child of the kill cases: a memory-writing role's delegated run through
#: the real runner, the CLI double from this module, stopped at the point the
#: case names.
DELEGATED_CHILD = r"""
import tools, agent as agent_mod
from memory import capture as cap, transcript_runs
from test_delegated_transcript_retention import _Harness, _project, _write_session
from test_delegate_to_agent import _origin, _specialist_cfg, _with_origin
point, cwd = sys.argv[5], sys.argv[6]
tools._specialist_telemetry = None
cfg = _specialist_cfg()
cfg.cwd = cwd
cfg.memory.token_budget = 1000
h = _Harness()
base = h.client_cls()

class Client(base):
    def __init__(self, options):
        open(os.path.join(os.path.dirname(marker), "launched"), "w").close()
        super().__init__(options)

    async def __aexit__(self, *exc):
        await asyncio.sleep(0.2)          # the CLI process takes time to end
        return await super().__aexit__(*exc)

    async def receive_response(self):
        # the CLI writes its transcript as it goes
        _write_session(_project(self.options.cwd), self.sid, "live")
        if point == "before_result":
            stop()
        async for m in super().receive_response():
            yield m

tools.ClaudeSDKClient = Client
real_record = cap.record

async def record(item):
    if point == "answer" and item.eligibility == "delegated_answer":
        stop()
    return await real_record(item)
cap.record = record
if point == "register":
    def register(*a, **k):
        stop()
    transcript_runs.register = register

asyncio.run(_with_origin(tools._run_delegated_agent(cfg, "question", ""),
                         _origin()))
"""


def _kill_delegated(env, tmp_path, point: str) -> None:
    kill_child(DELEGATED_CHILD, env.store, tmp_path, point, env.cfg.cwd,
               env={"CLAUDE_CONFIG_DIR": os.environ["CLAUDE_CONFIG_DIR"]})


async def test_confirmation_follows_the_answers_capture(env, tmp_path):
    """A real SIGKILL after the ``ResultMessage`` (the client has exited and
    written its transcript) and before the answer's capture: the answer is in
    no event, the run is unresolved and its transcript is kept through every
    pass, and nothing is captured from it later. Red case: a confirmation
    written at the result lets retention delete it."""
    _kill_delegated(env, tmp_path, "answer")
    assert [e["eligibility"] for e in events(env.store)] == ["delegated_request"]
    (row,) = rows(env.store)
    assert row["confirmed_at"] is None and row["boot_id"] != transcript_runs.BOOT_ID
    assert unresolved(env.store) == 1
    sid = row["session_id"]
    assert _artifacts(env.folder, sid) == 2
    for _ in range(2):
        transcript_runs.retention_pass(env.store, now=after_retention())
    assert _artifacts(env.folder, sid) == 2
    assert [e["eligibility"] for e in events(env.store)] == ["delegated_request"]


async def test_kill_before_the_result_leaves_the_run_unresolved(env, tmp_path):
    _kill_delegated(env, tmp_path, "before_result")
    (row,) = rows(env.store)
    assert row["confirmed_at"] is None and row["ended_at"] is None
    assert unresolved(env.store) == 1
    assert _artifacts(env.folder, row["session_id"]) == 2
    transcript_runs.retention_pass(env.store, now=after_retention())
    assert _artifacts(env.folder, row["session_id"]) == 2
    assert [e["eligibility"] for e in events(env.store)] == ["delegated_request"]


async def test_kill_before_registration_starts_no_cli(env, tmp_path):
    _kill_delegated(env, tmp_path, "register")
    assert rows(env.store) == [] and events(env.store) == []
    assert not (tmp_path / "launched").exists()
    assert sorted(p.name for p in env.folder.iterdir()) == [SIBLING, f"{SIBLING}.jsonl"]


async def test_a_failed_request_capture_leaves_the_run_unconfirmed(
        env, monkeypatch):
    """Neither the store nor the spool takes the request; both recover
    before the answer, which is captured. The run stays unconfirmed and its
    transcript survives every pass. Red case: the request captured outside
    the run's obligations, so the answer's receipt alone confirms it."""
    _memory_role(env.cfg)
    real_append = capture.append

    def append(store, item):
        if item.eligibility == "delegated_request":
            return None                      # not even the spool took it
        return real_append(store, item)
    monkeypatch.setattr(capture, "append", append)
    h = _Harness()
    _install(monkeypatch, env.tools, h)
    await _finish(await _run(env.tools, env.cfg, h), h)
    await transcript_runs.settle()

    # read through the spool too: under load the answer can wait there
    assert [e["eligibility"] for e in await captured_events(env.store)] == [
        "delegated_answer"]
    (row,) = rows(env.store)
    assert row["confirmed_at"] is None
    transcript_runs.retention_pass(env.store, now=after_retention())
    assert _artifacts(env.folder, row["session_id"]) == 2


async def test_a_spooled_answer_confirms_the_run(env, monkeypatch):
    """The store fails at the answer: it spools, fsynced, and the run is
    confirmed. The drain then yields exactly one answer event."""
    _memory_role(env.cfg)
    real_append = capture.append

    def append(store, item):
        if item.eligibility == "delegated_answer":
            store._unavailable_reason = "a simulated outage"
            try:
                return real_append(store, item)
            finally:
                store._unavailable_reason = None
        return real_append(store, item)
    monkeypatch.setattr(capture, "append", append)
    monkeypatch.setattr(capture, "_start_drain", lambda store: None)
    h = _Harness()
    _install(monkeypatch, env.tools, h)
    await _finish(await _run(env.tools, env.cfg, h), h)
    await transcript_runs.settle()

    (row,) = rows(env.store)
    assert row["confirmed_at"] is not None
    assert [e["eligibility"] for e in events(env.store)] == ["delegated_request"]
    assert capture.drain_spool(env.store) == 1
    assert [e["eligibility"] for e in events(env.store)] == [
        "delegated_request", "delegated_answer"]


async def test_cancelled_before_result_confirms_without_an_answer(
        env, monkeypatch):
    """An ``end_turn`` assistant message is received, then the run is
    cancelled before its ``ResultMessage``: no answer event, the run is
    confirmed, and the transcript follows retention."""
    _memory_role(env.cfg)
    h = _Harness("cancel_after_text")
    _install(monkeypatch, env.tools, h)
    await _finish(await _run(env.tools, env.cfg, h), h)
    await transcript_runs.settle()

    assert [e["eligibility"] for e in events(env.store)] == ["delegated_request"]
    (row,) = rows(env.store)
    assert row["confirmed_at"] is not None and row["ended_at"] is not None
    sid = row["session_id"]
    assert _artifacts(env.folder, sid) == 2
    counts = transcript_runs.retention_pass(env.store, now=after_retention())
    assert counts["deleted"] == 1 and _artifacts(env.folder, sid) == 0
    assert rows(env.store) == []


async def test_a_non_memory_run_registers_confirmed(env, monkeypatch):
    """A role that captures nothing registers already confirmed, before its
    client starts: its row is only the retention pass's index."""
    seen = []
    h = _Harness()
    base = h.client_cls()

    class _Client(base):
        def __init__(self, options):
            seen.append([(r["kind"], r["confirmed_at"] is not None,
                          r["session_id"]) for r in rows(env.store)])
            super().__init__(options)
    monkeypatch.setattr(env.tools, "ClaudeSDKClient", _Client)
    await _finish(await _run(env.tools, env.cfg, h), h)
    assert seen == [[("delegation", True, h.launches[0].session_id)]]
    assert events(env.store) == []


async def test_retention_of_an_older_row_means_the_next_turn_is_fresh(env, monkeypatch, tmp_path):
    """One session, two rows: a conversation's first turn and a later one. Retention
    deletes the session at the older row's age, as it always counted (unchanged), and the
    next continuation finds no transcript, starts fresh and keeps the new session."""
    import plugin_bindings
    import plugin_continuations as pc
    from authz_grants import GrantIdentity

    store = env.store
    h = _Harness()
    h.body = TRANSCRIPT
    _install(monkeypatch, env.tools, h)
    await _finish(await _run(env.tools, env.cfg, h), h)
    sid = h.launches[0].session_id
    bindings = plugin_bindings.BindingStore(str(tmp_path / "b.json"))
    monkeypatch.setattr(plugin_bindings, "BINDINGS", bindings)
    ident = GrantIdentity(operator_id=100, chat_id=42, enforcement_role="finance",
                          artifact_id="1" * 64, engagement_id="")
    binding = bindings.for_turn(plugin="p", turn_key="d-1", identity=ident,
                                chain=("assistant", "finance"), origin={})
    bindings.adopt(binding.ref, sid)

    async def _continue():
        assert await pc._conversation_for(binding, env.cfg) == (sid if resumed else None)
        tok = env.tools._conversation.set(env.tools.ConversationTurn(
            binding.ref, sid if resumed else None))
        try:
            return await _with_origin(
                env.tools._run_delegated_agent(env.cfg, "go on", ""), _origin())
        finally:
            env.tools._conversation.reset(tok)

    resumed = True
    await _continue()
    assert h.launches[1].resume == sid
    await transcript_runs.settle()
    older, newer = rows(store)
    assert older["session_id"] == newer["session_id"] == sid and older["run_id"] != newer["run_id"]

    # past the older row's retention only: the session goes, whichever row named it
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE transcript_runs SET ended_at = ? WHERE run_id = ?",
                     ("2020-01-01T00:00:00+00:00", older["run_id"]))
    counts = transcript_runs.retention_pass(store, now=datetime.fromisoformat(newer["ended_at"]))
    assert counts["deleted"] == 1 and _artifacts(env.folder, sid) == 0

    resumed = False
    await _continue()
    fresh = h.launches[2]
    assert fresh.resume is None and fresh.session_id != sid
    assert binding.session_id == fresh.session_id


async def test_retention_counts_from_the_run_end(env, monkeypatch):
    """Deleted 30 days after ``ended_at``, never sooner; a row an earlier
    process never ended counts from ``started_at``; a running row of this
    process is never deleted."""
    store = env.store
    h = _Harness()
    _install(monkeypatch, env.tools, h)
    await _finish(await _run(env.tools, env.cfg, h), h)
    await transcript_runs.settle()
    (row,) = rows(store)
    ended = datetime.fromisoformat(row["ended_at"])
    days = transcript_runs.retention_days()

    earlier = transcript_runs.register(
        store, kind="delegation", role="finance", directory=env.cfg.cwd,
        session_id=str(uuid.uuid4()), confirmed=True, boot_id="an-earlier-boot")
    running = transcript_runs.register(
        store, kind="delegation", role="finance", directory=env.cfg.cwd,
        session_id=str(uuid.uuid4()), confirmed=True)
    for r in rows(store):
        if r["run_id"] in (earlier, running):
            _write_session(env.folder, r["session_id"], "x")

    transcript_runs.retention_pass(
        store, now=ended + timedelta(days=days) - timedelta(minutes=1))
    assert _artifacts(env.folder, row["session_id"]) == 2
    assert len(rows(store)) == 3

    counts = transcript_runs.retention_pass(
        store, now=ended + timedelta(days=days, minutes=1))
    assert counts["deleted"] == 2
    assert _artifacts(env.folder, row["session_id"]) == 0
    assert [r["run_id"] for r in rows(store)] == [running]


async def test_only_the_named_session_is_deleted(env, monkeypatch):
    """The pass deletes the one session the row names, in the row's own
    folder: a sibling session there and a session of the same id in another
    project are untouched."""
    h = _Harness()
    _install(monkeypatch, env.tools, h)
    await _finish(await _run(env.tools, env.cfg, h), h)
    await transcript_runs.settle()
    (sid,) = h.written
    _write_session(env.other, sid, "other-project")
    sibling_before = _snapshot(env.folder, SIBLING)

    transcript_runs.retention_pass(env.store, now=after_retention())

    assert h.deletes == [(sid, env.cfg.cwd)]
    assert _artifacts(env.folder, sid) == 0
    assert _artifacts(env.folder, SIBLING) == 2
    assert _snapshot(env.folder, SIBLING) == sibling_before
    assert _artifacts(env.other, sid) == 2


async def test_a_delegated_run_launches_in_the_folder_its_row_names(
        env, monkeypatch, tmp_path):
    """A configuration reload between the run's registration and its options
    build cannot move the CLI out of the folder its row names: the launch
    takes the row's directory, where the retention pass deletes."""
    original = env.cfg.cwd
    real = env.tools._build_specialist_options

    def build(cfg, **kw):
        cfg.cwd = str(tmp_path / "moved")           # the reload lands here
        return real(cfg, **kw)
    monkeypatch.setattr(env.tools, "_build_specialist_options", build)
    h = _Harness()
    _install(monkeypatch, env.tools, h)
    await _finish(await _run(env.tools, env.cfg, h), h)
    await transcript_runs.settle()
    (row,) = rows(env.store)
    assert row["directory"] == original
    assert h.launches[0].cwd == original
