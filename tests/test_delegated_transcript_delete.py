"""An ephemeral delegation deletes its own CLI transcript when its client exits
(#1168, INV-ENG-023).

``tools._run_delegated_agent`` launches one CLI session per call in the
specialist's working directory. That folder is shared with the specialist's
other sessions — concurrent delegations and its ``in_casa`` engagements — so
the runner must name the one session it launched and delete exactly that one,
and only once the client has exited: the CLI writes its transcript until its
process ends.

The fake client below stands in for the CLI: it writes ``<sid>.jsonl`` and
``<sid>/tool-results/x`` into the SDK's project folder for ``options.cwd`` as
its ``__aexit__`` completes. Deletion goes through the REAL public SDK
``delete_session`` against a real projects root under ``tmp_path``, so a
deletion that addressed the wrong folder, the wrong session or ran before the
exit would leave files behind, and one that listed the folder would take the
sibling session with it.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

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

pytestmark = [pytest.mark.unit]

SIBLING = "22222222-2222-4222-8222-222222222222"
FALLBACK = "33333333-3333-4333-8333-333333333333"


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

    def client_cls(self):
        h = self

        class _Client:
            def __init__(self, options):
                self.options = options
                h.launches.append(options)
                sid = getattr(options, "session_id", None)
                self.sid = sid if sid else FALLBACK

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                # The CLI flushes its transcript as its process ends.
                _write_session(_project(self.options.cwd), self.sid, "own")
                h.written.append(self.sid)
                if h.exit_written is not None:
                    h.exit_written.set()
                if h.hold_exit is not None:
                    await h.hold_exit.wait()
                h.exits_done += 1
                return False

            async def query(self, prompt):
                return None

            async def receive_response(self):
                h.entered_receive.set()
                if h.mode == "raise":
                    raise h.raised
                if h.mode == "cancel":
                    await asyncio.Event().wait()
                yield AssistantMessage(content=[TextBlock(text="answer")],
                                       model="claude-sonnet-4-6")
                r = ResultMessage.__new__(ResultMessage)
                for k, v in dict(subtype="success", duration_ms=1,
                                 duration_api_ms=1, is_error=False,
                                 num_turns=1, session_id=self.sid,
                                 total_cost_usd=0.0, usage={}, result="answer",
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
def env(tmp_path, monkeypatch):
    import tools

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    cwd = tmp_path / "agent-home" / "finance"
    cwd.mkdir(parents=True)
    cfg = _specialist_cfg()
    cfg.cwd = str(cwd)
    folder = _project(str(cwd))
    _write_session(folder, SIBLING, "sibling")
    # A session of the same id in ANOTHER project must never be touched.
    other = _project(str(tmp_path / "elsewhere"))
    monkeypatch.setattr(tools, "_specialist_telemetry", None, raising=False)
    return tools, cfg, folder, other


def _install(monkeypatch, tools, h: _Harness) -> None:
    monkeypatch.setattr(tools, "ClaudeSDKClient", h.client_cls())
    monkeypatch.setattr(claude_agent_sdk, "delete_session", h.recording_delete())


async def _run(tools, cfg, h: _Harness):
    task = asyncio.ensure_future(_with_origin(
        tools._run_delegated_agent(cfg, "question", "", resolution=None),
        _origin()))
    if h.mode == "cancel":
        await h.entered_receive.wait()
        task.cancel()
    return task


def _assert_own_session_deleted(h: _Harness, folder: Path, cfg,
                                sibling_before: dict) -> None:
    assert len(h.launches) == 1
    assert h.exits_done == 1
    assert len(h.written) == 1
    assert _artifacts(folder, h.written[0]) == 0, (
        "the session this delegation launched was left on disk")
    sid = h.launches[0].session_id
    assert sid is not None and str(uuid.UUID(sid)) == sid, (
        f"the runner launched without a Casa-chosen session id: {sid!r}")
    assert len(h.deletes) == 1, f"delete_session calls: {h.deletes}"
    assert h.deletes == [(sid, cfg.cwd)]
    assert h.deletes_before_exit == 0
    assert _artifacts(folder, sid) == 0
    assert _artifacts(folder, SIBLING) == 2
    assert _snapshot(folder, SIBLING) == sibling_before


@pytest.mark.parametrize("mode", ["return", "raise", "cancel"])
async def test_runner_deletes_its_own_session_after_the_client_exits(
        env, monkeypatch, mode):
    tools, cfg, folder, other = env
    _write_session(other, FALLBACK, "other-project")
    other_before = _snapshot(other, FALLBACK)
    sibling_before = _snapshot(folder, SIBLING)
    h = _Harness(mode)
    _install(monkeypatch, tools, h)

    task = await _run(tools, cfg, h)
    if mode == "return":
        out = await task
        assert out.text == "answer"
    elif mode == "raise":
        with pytest.raises(ApiErrorTurn) as ei:
            await task
        assert ei.value is h.raised
    else:
        with pytest.raises(asyncio.CancelledError):
            await task

    _assert_own_session_deleted(h, folder, cfg, sibling_before)
    assert _artifacts(other, FALLBACK) == 2
    assert _snapshot(other, FALLBACK) == other_before


async def test_overlapping_runners_each_delete_only_their_own_session(
        env, monkeypatch):
    tools, cfg, folder, _other = env
    sibling_before = _snapshot(folder, SIBLING)
    a, b = _Harness(), _Harness()
    b.hold_exit, b.exit_written = asyncio.Event(), asyncio.Event()
    deletes: list[tuple] = []
    real = claude_agent_sdk.delete_session

    def _delete(session_id, directory=None):
        deletes.append((session_id, directory))
        return real(session_id, directory)

    clients = iter([b.client_cls(), a.client_cls()])
    monkeypatch.setattr(tools, "ClaudeSDKClient",
                        lambda options: next(clients)(options))
    monkeypatch.setattr(claude_agent_sdk, "delete_session", _delete)

    task_b = await _run(tools, cfg, b)
    await b.exit_written.wait()           # B has written, its exit is paused
    task_a = await _run(tools, cfg, a)
    await task_a

    sid_a, sid_b = a.launches[0].session_id, b.launches[0].session_id
    assert sid_a and sid_b and sid_a != sid_b
    assert _artifacts(folder, sid_a) == 0
    assert _artifacts(folder, sid_b) == 2
    assert _artifacts(folder, SIBLING) == 2
    assert len(deletes) == 1

    b.hold_exit.set()
    await task_b
    assert _artifacts(folder, sid_a) == 0
    assert _artifacts(folder, sid_b) == 0
    assert _artifacts(folder, SIBLING) == 2
    assert _snapshot(folder, SIBLING) == sibling_before
    assert len(deletes) == 2


@pytest.mark.parametrize("mode", ["return", "raise", "cancel"])
async def test_a_failing_delete_is_logged_and_never_changes_the_outcome(
        env, monkeypatch, caplog, mode):
    tools, cfg, folder, _other = env
    h = _Harness(mode)
    _install(monkeypatch, tools, h)
    marker = "do-not-log-this-detail"
    calls: list[tuple] = []

    def _failing(session_id, directory=None):
        calls.append((session_id, directory))
        raise OSError(marker)

    monkeypatch.setattr(claude_agent_sdk, "delete_session", _failing)
    caplog.set_level(logging.DEBUG)

    task = await _run(tools, cfg, h)
    if mode == "return":
        assert (await task).text == "answer"
    elif mode == "raise":
        with pytest.raises(ApiErrorTurn) as ei:
            await task
        assert ei.value is h.raised
    else:
        with pytest.raises(asyncio.CancelledError):
            await task

    sid = h.launches[0].session_id
    assert sid is not None
    assert calls == [(sid, cfg.cwd)]
    assert _artifacts(folder, sid) == 2
    assert _artifacts(folder, SIBLING) == 2
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "transcript" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert marker not in warnings[0].getMessage()
