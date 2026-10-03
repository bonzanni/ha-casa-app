"""#1212 — a bounded delegated run (a desk reply, a delegation) ended by its
ceiling or cancelled leaves no process its CLI started running.

The fixture is the live shape: a "CLI" whose "server" shells out a helper
AFTER the session began (the plugin tool's ``sleep``), and a client whose
close signals the CLI only — the server then sees EOF and exits, and the
helper is reparented to init. Before #1212 it outlived the run."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import types

import pytest

import pinned_run as pr

try:
    from tests.test_delegation_failure_tool_calls import _specialist_cfg
except ImportError:
    from test_delegation_failure_tool_calls import _specialist_cfg

pytestmark = [pytest.mark.asyncio]

SERVER = r"""
import subprocess, sys
for line in sys.stdin:                       # 'spawn' -> a helper, its pid printed
    if line.strip() == "spawn":
        k = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(1)"],
                             stdin=subprocess.DEVNULL)
        print(k.pid, flush=True)
# EOF: the CLI is gone — exit as an MCP server does, leaving the helper behind
"""

CLI = r"""
import signal, subprocess, sys, time
if "ignore-term" in sys.argv:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
srv = subprocess.Popen([sys.executable, "-c", %r], stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE, text=True)
print(srv.pid, flush=True)
for line in sys.stdin:
    if line.strip() == "spawn":
        srv.stdin.write("spawn\n"); srv.stdin.flush()
        print(srv.stdout.readline().strip(), flush=True)
while True:
    time.sleep(1)
""" % SERVER


def _alive(pid: int) -> bool:
    try:
        fd = pr._pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        return not pr._Pinned(pid, fd).exited()
    finally:
        os.close(fd)


def _pidfds() -> int:
    """The pidfds this process holds open."""
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            n += "pidfd" in os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            pass
    return n


class _Tree:
    def __init__(self, *, ignore_term: bool = False) -> None:
        args = [sys.executable, "-c", CLI] + (["ignore-term"] if ignore_term else [])
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True)
        self.pid = self.proc.pid
        self.server = int(self.proc.stdout.readline().strip())
        self.helpers: list[int] = []

    def spawn(self) -> int:
        self.proc.stdin.write("spawn\n")
        self.proc.stdin.flush()
        pid = int(self.proc.stdout.readline().strip())
        self.helpers.append(pid)
        return pid

    def pids(self) -> list[int]:
        return [self.pid, self.server, *self.helpers]

    def cleanup(self) -> None:
        for pid in (*self.helpers, self.server, self.pid):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def trees():
    made: list[_Tree] = []

    def make(**kw) -> _Tree:
        t = _Tree(**kw)
        made.append(t)
        return t
    yield make
    for t in made:
        t.cleanup()


class _Process:
    """The transport's process object (hashable, as the SDK's is: the reaper
    set holds it)."""

    def __init__(self, pid: int) -> None:
        self.pid, self.returncode = pid, None


def _client_class(tree_for, *, hang_close: bool = False, seen: list | None = None):
    """A ClaudeSDKClient stand-in on a real process tree: its close signals
    the CLI ONLY (the SDK's behaviour), and its response stream has the
    plugin tool shell out a helper, then blocks."""
    import tools

    class _Client:
        def __init__(self, _options):
            self.tree = tree_for()
            self._transport = types.SimpleNamespace(_process=_Process(self.tree.pid))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            if hang_close:
                await asyncio.Event().wait()             # a teardown that overruns its bound
            try:
                os.kill(self.tree.pid, 15)
            except ProcessLookupError:
                pass
            await asyncio.to_thread(self.tree.proc.wait)
            # as observed live: the servers see EOF and are gone too before the
            # close returns, their helpers already reparented
            await _until(lambda: not _alive(self.tree.server), timeout=5.0)
            return False

        async def query(self, _prompt):
            if seen is not None:
                seen.append(tools._bounded_tree.get())

        async def receive_response(self):
            await asyncio.to_thread(self.tree.spawn)     # the tool call's helper, after the pin
            await asyncio.Event().wait()                 # the tool blocks
            yield None                                   # pragma: no cover
    return _Client


def _use(monkeypatch, client_cls):
    import tools
    from claude_agent_sdk import ClaudeAgentOptions
    monkeypatch.setattr(tools, "ClaudeSDKClient", client_cls)
    monkeypatch.setattr(tools, "_build_specialist_options",
                        lambda *_a, **_k: ClaudeAgentOptions())
    monkeypatch.setattr(tools, "_delete_own_delegated_transcript",
                        lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(pr.ProcessTree, "GRACE_S", 1.0)
    monkeypatch.setattr(pr.ProcessTree, "EXIT_WAIT_S", 5.0)
    return tools


async def _until(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


async def _run_bounded(tools):
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "assistant", "cid": "c1", "channel": "telegram"})
    try:
        return asyncio.create_task(tools._run_delegated_agent_bounded(_specialist_cfg(), "x", ""))
    finally:
        agent_mod.origin_var.reset(tok)


async def test_a_ceiling_kills_the_helper_a_plugin_server_started_after_the_session_began(trees, monkeypatch, caplog):
    holder: list[_Tree] = []
    tools = _use(monkeypatch, _client_class(lambda: holder.append(trees()) or holder[-1]))
    monkeypatch.setattr(tools, "_DELEGATION_CEILING_S", 1.5)
    monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 5.0)
    before = _pidfds()
    run = await _run_bounded(tools)
    with pytest.raises(tools.DelegationCeilingExceeded):
        await run
    assert _pidfds() == before                          # the reap closed every pidfd it held
    assert "ending its processes failed" not in caplog.text
    tree = holder[0]
    assert len(tree.helpers) == 1                       # the tool did shell out before the ceiling
    # every process the run's CLI started is gone when the runner raises — the helper too,
    # although the close signalled only the CLI and the server left it to init
    assert [p for p in tree.pids() if _alive(p)] == []


async def test_an_outer_cancel_kills_the_helper_too(trees, monkeypatch):
    holder: list[_Tree] = []
    tools = _use(monkeypatch, _client_class(lambda: holder.append(trees()) or holder[-1]))
    monkeypatch.setattr(tools, "_DELEGATION_CEILING_S", 600.0)
    monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 5.0)
    run = await _run_bounded(tools)
    assert await _until(lambda: holder and holder[0].helpers)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert [p for p in holder[0].pids() if _alive(p)] == []


async def test_a_teardown_overrun_and_a_cli_ignoring_sigterm_still_end_every_process_through_a_recancel(trees, monkeypatch):
    # Astra, design round 1: the cleanup after the teardown bound must survive a
    # re-cancel of the runner landing during the SIGTERM grace (diff round 1:
    # the cancel is synchronised with the grace and the processes are judged the
    # moment the runner ends, not after a poll)
    holder: list[_Tree] = []
    tools = _use(monkeypatch, _client_class(
        lambda: holder.append(trees(ignore_term=True)) or holder[-1], hang_close=True))
    monkeypatch.setattr(tools, "_DELEGATION_CEILING_S", 1.5)
    monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 0.2)
    monkeypatch.setattr(pr.ProcessTree, "GRACE_S", 2.0)
    in_grace = asyncio.Event()
    real_wait = pr.ProcessTree._wait_exit

    async def _wait_exit(self, pinned, deadline):
        if pinned is self._cli:
            in_grace.set()                              # the CLI got SIGTERM; the grace runs
        return await real_wait(self, pinned, deadline)
    monkeypatch.setattr(pr.ProcessTree, "_wait_exit", _wait_exit)
    run = await _run_bounded(tools)
    await asyncio.wait_for(in_grace.wait(), timeout=10.0)
    assert run.cancel() is True                         # the runner is still running: it waits on the reap
    with pytest.raises((asyncio.CancelledError, tools.DelegationCeilingExceeded)):
        await run
    assert len(holder[0].helpers) == 1
    assert [p for p in holder[0].pids() if _alive(p)] == []     # at once, no poll


async def test_the_run_never_sees_its_own_tree_on_the_contextvar(trees, monkeypatch):
    holder: list[_Tree] = []
    seen: list = []
    tools = _use(monkeypatch, _client_class(lambda: holder.append(trees()) or holder[-1],
                                            seen=seen))
    monkeypatch.setattr(tools, "_DELEGATION_CEILING_S", 1.5)
    monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 5.0)
    run = await _run_bounded(tools)
    with pytest.raises(tools.DelegationCeilingExceeded):
        await run
    assert seen == [None]                               # cleared at the run's entry


async def test_a_nested_bounded_run_owns_its_own_tree_and_each_pins_only_its_own_cli(trees, monkeypatch):
    # Terra, diff round 1: a delegation made from inside a delegated run (the
    # parent's response stream awaits a bounded run of its own)
    holder: list[_Tree] = []
    made: list = []
    real = pr.ProcessTree

    class _Spy(real):
        def __init__(self, **kw):
            super().__init__(**kw)
            made.append(self)
    base = _client_class(lambda: holder.append(trees()) or holder[-1])

    class _Parent(base):
        async def receive_response(self):
            import tools as t
            await asyncio.to_thread(self.tree.spawn)
            if self.tree is holder[0]:
                await t._run_delegated_agent_bounded(_specialist_cfg(), "nested", "")
            await asyncio.Event().wait()
            yield None                                   # pragma: no cover
    tools = _use(monkeypatch, _Parent)
    monkeypatch.setattr(pr, "ProcessTree", _Spy)
    monkeypatch.setattr(tools, "_DELEGATION_CEILING_S", 3.0)
    monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 10.0)
    run = await _run_bounded(tools)
    assert await _until(lambda: len(holder) == 2 and holder[1].helpers)
    parent, nested = holder
    assert len(made) == 2 and made[0] is not made[1]
    assert made[0]._cli.pid == parent.pid and made[1]._cli.pid == nested.pid
    assert nested.pid not in made[0].pinned_pids()      # the parent's tree never owns the nested CLI
    assert parent.pid not in made[1].pinned_pids()
    with pytest.raises(tools.DelegationCeilingExceeded):
        await run
    assert [p for p in parent.pids() + nested.pids() if _alive(p)] == []


async def test_a_normal_end_pins_the_cli_and_closes_every_pinned_fd(trees, monkeypatch):
    holder: list[_Tree] = []
    base = _client_class(lambda: holder.append(trees()) or holder[-1])
    made: list = []
    real = pr.ProcessTree

    class _Spy(real):
        def __init__(self, **kw):
            super().__init__(**kw)
            made.append(self)

    class _Done(base):
        async def receive_response(self):
            from claude_agent_sdk import ResultMessage
            yield ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                                is_error=False, num_turns=1, session_id="t", result="ok")
    tools = _use(monkeypatch, _Done)
    monkeypatch.setattr(pr, "ProcessTree", _Spy)
    before = _pidfds()
    run = await _run_bounded(tools)
    await run
    assert len(made) == 1
    assert made[0].pinned_pids() >= {holder[0].pid, holder[0].server}   # pinned at entry
    assert _pidfds() == before                                              # and every pidfd closed


@pytest.mark.parametrize("ending", ["ceiling", "cancel"])
async def test_a_run_ended_while_its_client_is_still_being_entered_leaves_no_helper(trees, monkeypatch, ending):
    # Astra, diff round 2: the real SDK client, its initialisation unanswered —
    # the CLI and a server's helper exist, nothing is pinned yet, and the SDK's
    # cleanup clears the transport before the reap could late-pin
    from claude_agent_sdk import ClaudeSDKClient
    from claude_agent_sdk._internal.transport import Transport
    holder: list[_Tree] = []
    ready = asyncio.Event()
    base = _client_class(lambda: holder.append(trees()) or holder[-1])

    class _Hanging(Transport):
        async def connect(self):
            self.fake = base(None)
            self._process = self.fake._transport._process
            await asyncio.to_thread(self.fake.tree.spawn)

        async def write(self, data):
            ready.set()                                  # initialize sent; the CLI is slow to answer

        async def read_messages(self):
            await asyncio.Event().wait()
            yield {}                                     # pragma: no cover

        async def close(self):
            await self.fake.__aexit__(None, None, None)

        def is_ready(self):
            return True

        async def end_input(self):
            pass

    class _Starting(ClaudeSDKClient):
        def __init__(self, options):
            super().__init__(options, transport=_Hanging())
    tools = _use(monkeypatch, _Starting)
    monkeypatch.setattr(tools, "_DELEGATION_CEILING_S", 0.5 if ending == "ceiling" else 600.0)
    monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 5.0)
    run = await _run_bounded(tools)
    await asyncio.wait_for(ready.wait(), 5.0)
    assert all(_alive(p) for p in holder[0].pids())
    if ending == "cancel":
        assert run.cancel()
    with pytest.raises((asyncio.CancelledError, tools.DelegationCeilingExceeded)):
        await run
    assert [p for p in holder[0].pids() if _alive(p)] == []


async def test_a_reap_that_fails_inside_logs_at_error_and_closes_its_fds(monkeypatch, caplog):
    import logging
    tree = pr.ProcessTree(run_id="review-error")
    fd = os.open("/dev/null", os.O_RDONLY)
    tree._children.append(pr._Pinned(123, fd))
    monkeypatch.setattr(tree, "_late_pin", lambda: None)
    monkeypatch.setattr(tree, "_repin", lambda: None)

    async def _nothing():
        pass

    async def _confirmed(deadline):
        return True

    def _fail():
        raise TypeError("unhashable process")
    monkeypatch.setattr(tree, "_signal", _nothing)
    monkeypatch.setattr(tree, "_confirm_exits", _confirmed)
    monkeypatch.setattr(tree, "_discard_reaper", _fail)
    with caplog.at_level(logging.ERROR):
        assert await tree.reap("assistant") is False
    assert any(r.levelno == logging.ERROR and "ending its processes failed: TypeError" in r.message
               for r in caplog.records)
    with pytest.raises(OSError):
        os.fstat(fd)
