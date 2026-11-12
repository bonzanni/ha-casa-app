"""Real tool descriptions: protected plugin tools are listed ahead of time into a
small in-memory cache, so an approval card can quote what the tool says about
itself without starting a server on the approval path."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

import plugin_continuations as pc
import tool_descriptions as td
from plugin_registry import ResolvedPlugin


# tests/conftest.py resets the module's state after every test.


def _tool(name, description=None, schema=None):
    return SimpleNamespace(name=name, description=description, inputSchema=schema)


def _plugin(tmp_path, name, artifact_id, protected, *, servers=1):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    mcp = {f"s{i}": {"command": "python", "args": ["x.py"]} for i in range(servers)}
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": mcp}))
    manifest = {"name": name, "casa": {"protectedTools": protected}}
    return ResolvedPlugin(name=name, artifact_id=artifact_id, path=str(root),
                          version="1", manifest=manifest)


def _resolution(*plugins):
    return SimpleNamespace(plugins=list(plugins))


async def _drain():
    # Yield each pass: awaiting an already-done task never reaches the event loop, so its
    # done callback (which drops it from the set) would not run.
    while td._tasks:
        await asyncio.sleep(0.005)


# ---- list_plugin_tools --------------------------------------------------------

@pytest.mark.asyncio
async def test_list_plugin_tools_never_calls_a_tool(monkeypatch):
    calls: list[str] = []

    class FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            calls.append("session_exit")
            return False

        async def initialize(self):
            calls.append("initialize")

        async def list_tools(self):
            calls.append("list_tools")
            return SimpleNamespace(tools=[_tool("send", "Sends.", {"type": "object"})])

        async def call_tool(self, *a, **k):
            calls.append("call_tool")
            raise AssertionError("a listing must never call a tool")

    class FakeStdio:
        def __init__(self, params):
            calls.append("stdio_client")

        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *exc):
            calls.append("stdio_exit")
            return False

    import mcp
    import mcp.client.stdio as mcp_stdio
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)
    monkeypatch.setattr(mcp_stdio, "stdio_client", FakeStdio)

    server = {"command": "python", "args": [], "env": {}, "cwd": "."}
    listed = await pc.list_plugin_tools(server)
    # Exactly these two requests, then the session and the server's stdio are torn down
    # (session first), so no server process outlives the listing.
    assert calls == ["stdio_client", "initialize", "list_tools", "session_exit", "stdio_exit"]
    assert listed == [{"name": "send", "description": "Sends.",
                       "inputSchema": {"type": "object"}}]


@pytest.mark.asyncio
async def test_list_plugin_tools_gives_up_after_its_budget(monkeypatch):
    class Hang:
        def __init__(self, params):
            pass

        async def __aenter__(self):
            await asyncio.sleep(3600)

        async def __aexit__(self, *exc):
            return False

    import mcp.client.stdio as mcp_stdio
    monkeypatch.setattr(mcp_stdio, "stdio_client", Hang)
    with pytest.raises(TimeoutError):
        await pc.list_plugin_tools({"command": "x", "args": [], "env": {}, "cwd": "."},
                                   timeout_s=0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("ignore_sigterm", [False, True], ids=["silent", "ignores_sigterm"])
async def test_list_plugin_tools_stops_a_silent_real_server(tmp_path, ignore_sigterm):
    """A real server that never answers: the call times out, returns within the budget plus
    the MCP client's own shutdown waits (about two seconds after stdin closes, two more after
    SIGTERM before the kill), and the process is gone, also when it ignores SIGTERM."""
    import os
    import sys
    import time

    pid_file = tmp_path / "pid"
    ignore = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_sigterm else ""
    code = (ignore + "import os, time, pathlib, sys; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
    server = {"command": sys.executable, "args": ["-c", code, str(pid_file)],
              "env": {}, "cwd": str(tmp_path)}
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await pc.list_plugin_tools(server, timeout_s=1.0)
    assert time.monotonic() - started < 8
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    # A terminated child may linger as a zombie until its parent reaps it.
    status = f"/proc/{pid}/status"
    assert not os.path.exists(status) or "\nState:\tZ" in open(status).read()


# ---- the fill -----------------------------------------------------------------

def _fake_list(monkeypatch, by_path: dict, *, gate: "dict | None" = None):
    seen: list[str] = []

    async def fake(server, *, timeout_s=20.0):
        seen.append(server["cwd"])
        if gate and server["cwd"] in gate:
            await gate[server["cwd"]].wait()
        out = by_path[server["cwd"]]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(pc, "list_plugin_tools", fake)
    return seen


@pytest.mark.asyncio
async def test_fill_stores_only_protected_tools_under_the_artifact_id(monkeypatch, tmp_path):
    rp = _plugin(tmp_path, "mail", "a" * 64, [{"name": "send", "summary": "Send"}, "trash"])
    _fake_list(monkeypatch, {rp.path: [
        {"name": "send", "description": "Sends a message.",
         "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}}},
        {"name": "search", "description": "Reads.", "inputSchema": {}},
        {"name": "trash", "description": None, "inputSchema": None}]})
    td.bind_loop(asyncio.get_running_loop())
    td.schedule_fill(_resolution(rp))
    await asyncio.sleep(0)
    await _drain()
    assert td.get("a" * 64, "send") == {
        "description": "Sends a message.",
        "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}}}
    assert td.get("a" * 64, "trash") == {"description": None, "inputSchema": None}
    assert td.get("a" * 64, "search") is None          # not protected: never cached
    assert td.get("b" * 64, "send") is None            # another artifact: no entry


@pytest.mark.asyncio
async def test_a_late_fill_cannot_overwrite_another_artifact(monkeypatch, tmp_path):
    """An update: the same plugin name, a new artifact id. The old artifact's listing
    finishes last and must leave the new artifact's entry alone."""
    old = _plugin(tmp_path / "old", "mail", "a" * 64, ["do"])
    new = _plugin(tmp_path / "new", "mail", "b" * 64, ["do"])
    assert old.name == new.name and old.path != new.path
    gate = {old.path: asyncio.Event()}
    _fake_list(monkeypatch, {
        old.path: [{"name": "do", "description": "old words", "inputSchema": {"v": 1}}],
        new.path: [{"name": "do", "description": "new words", "inputSchema": {"v": 2}}]},
        gate=gate)
    td.bind_loop(asyncio.get_running_loop())
    td.schedule_fill(_resolution(old))
    await asyncio.sleep(0)
    td.schedule_fill(_resolution(new))               # the update lands while old lists
    for _ in range(5):
        await asyncio.sleep(0)
    assert td.get(new.artifact_id, "do")["description"] == "new words"
    assert td.get(old.artifact_id, "do") is None     # still listing
    gate[old.path].set()                             # the old listing finishes last
    await _drain()
    assert td.get(new.artifact_id, "do") == {"description": "new words",
                                             "inputSchema": {"v": 2}}
    assert td.get(old.artifact_id, "do")["description"] == "old words"


@pytest.mark.asyncio
async def test_schedule_fill_from_a_worker_thread_runs_on_the_bound_loop(monkeypatch, tmp_path):
    rp = _plugin(tmp_path, "mail", "c" * 64, ["send"])
    ran_on: list = []

    async def fake(server, *, timeout_s=20.0):
        ran_on.append(asyncio.get_running_loop())
        return [{"name": "send", "description": "d", "inputSchema": {}}]

    monkeypatch.setattr(pc, "list_plugin_tools", fake)
    loop = asyncio.get_running_loop()
    td.bind_loop(loop)
    await asyncio.to_thread(td.schedule_fill, _resolution(rp))   # no RuntimeError
    for _ in range(50):
        if td.get("c" * 64, "send"):
            break
        await asyncio.sleep(0.01)
    await _drain()
    assert ran_on == [loop]
    assert td.get("c" * 64, "send") == {"description": "d", "inputSchema": {}}


def test_schedule_fill_before_the_loop_is_bound_does_nothing(tmp_path):
    rp = _plugin(tmp_path, "mail", "d" * 64, ["send"])
    td.schedule_fill(_resolution(rp))                  # no loop, no raise
    assert td.get("d" * 64, "send") is None


@pytest.mark.asyncio
async def test_unlistable_plugin_leaves_a_logged_miss(monkeypatch, tmp_path, caplog):
    two = _plugin(tmp_path, "two", "e" * 64, ["send"], servers=2)
    boom = _plugin(tmp_path, "boom", "f" * 64, ["send"])
    seen = _fake_list(monkeypatch, {boom.path: RuntimeError("server died")})
    td.bind_loop(asyncio.get_running_loop())
    with caplog.at_level(logging.INFO):
        td.schedule_fill(_resolution(two, boom))
        await asyncio.sleep(0)
        await _drain()
    assert seen == [boom.path]                         # no task for the two-server plugin
    assert td.get("e" * 64, "send") is None and td.get("f" * 64, "send") is None
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "two" in text and "e" * 64 in text
    assert "boom" in text and "f" * 64 in text and "server died" in text


@pytest.mark.asyncio
async def test_an_unlistable_plugin_is_read_and_logged_once(monkeypatch, tmp_path, caplog):
    two = _plugin(tmp_path, "two", "7" * 64, ["send"], servers=2)
    import plugin_store
    reads: list = []
    real = plugin_store.mcp_servers_map
    monkeypatch.setattr(plugin_store, "mcp_servers_map",
                        lambda path: reads.append(path) or real(path))
    _fake_list(monkeypatch, {})
    td.bind_loop(asyncio.get_running_loop())
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            td.schedule_fill(_resolution(two))
            await asyncio.sleep(0)
            await _drain()
    assert len(reads) == 1
    assert [r for r in caplog.records if "7" * 64 in r.getMessage()].__len__() == 1


@pytest.mark.asyncio
async def test_a_timeout_is_logged_and_never_raised(monkeypatch, tmp_path, caplog):
    rp = _plugin(tmp_path, "slow", "1" * 64, ["send"])
    _fake_list(monkeypatch, {rp.path: TimeoutError()})
    td.bind_loop(asyncio.get_running_loop())
    with caplog.at_level(logging.INFO):
        td.schedule_fill(_resolution(rp))
        await asyncio.sleep(0)
        await _drain()
    assert td.get("1" * 64, "send") is None
    assert any("slow" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_fill_is_idempotent_per_artifact_id(monkeypatch, tmp_path):
    rp = _plugin(tmp_path, "mail", "2" * 64, ["send"])
    seen = _fake_list(monkeypatch, {rp.path: [
        {"name": "send", "description": "d", "inputSchema": {}}]})
    td.bind_loop(asyncio.get_running_loop())
    for _ in range(3):
        td.schedule_fill(_resolution(rp))
    await asyncio.sleep(0)
    await _drain()
    td.schedule_fill(_resolution(rp))
    await asyncio.sleep(0)
    await _drain()
    assert seen == [rp.path]


@pytest.mark.asyncio
async def test_a_failed_fill_may_be_tried_again_later(monkeypatch, tmp_path):
    rp = _plugin(tmp_path, "mail", "3" * 64, ["send"])
    seen = _fake_list(monkeypatch, {rp.path: RuntimeError("down")})
    td.bind_loop(asyncio.get_running_loop())
    td.schedule_fill(_resolution(rp))
    await asyncio.sleep(0)
    await _drain()
    td.schedule_fill(_resolution(rp))                  # inside the cooldown: no retry
    await asyncio.sleep(0)
    await _drain()
    assert len(seen) == 1
    td._failed_at["3" * 64] -= td.RETRY_AFTER_S + 1    # the cooldown has passed
    _fake_list(monkeypatch, {rp.path: [{"name": "send", "description": "up",
                                        "inputSchema": {}}]})
    td.schedule_fill(_resolution(rp))
    await asyncio.sleep(0)
    await _drain()
    assert td.get("3" * 64, "send")["description"] == "up"


@pytest.mark.asyncio
async def test_a_failing_plugin_is_listed_at_most_three_times(monkeypatch, tmp_path, caplog):
    rp = _plugin(tmp_path, "mail", "8" * 64, ["send"])
    seen = _fake_list(monkeypatch, {rp.path: RuntimeError("down")})
    td.bind_loop(asyncio.get_running_loop())
    with caplog.at_level(logging.INFO):
        for _ in range(4):
            td.schedule_fill(_resolution(rp))
            await asyncio.sleep(0)
            await _drain()
            td._failed_at["8" * 64] -= td.RETRY_AFTER_S + 1   # the cooldown has passed
    assert len(seen) == td.MAX_ATTEMPTS == 3
    assert td.get("8" * 64, "send") is None
    assert len([r for r in caplog.records if "giving up" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_a_plugin_with_a_malformed_declaration_or_none_is_skipped(monkeypatch, tmp_path):
    bad = _plugin(tmp_path, "bad", "4" * 64, "not-a-list")
    none = _plugin(tmp_path, "none", "5" * 64, [])
    seen = _fake_list(monkeypatch, {})
    td.bind_loop(asyncio.get_running_loop())
    td.schedule_fill(_resolution(bad, none))
    await asyncio.sleep(0)
    await _drain()
    assert seen == []


@pytest.mark.asyncio
async def test_in_flight_tasks_are_held_until_done(monkeypatch, tmp_path):
    rp = _plugin(tmp_path, "mail", "6" * 64, ["send"])
    gate = {rp.path: asyncio.Event()}
    _fake_list(monkeypatch, {rp.path: [
        {"name": "send", "description": "d", "inputSchema": {}}]}, gate=gate)
    td.bind_loop(asyncio.get_running_loop())
    td.schedule_fill(_resolution(rp))
    await asyncio.sleep(0)
    assert len(td._tasks) == 1
    gate[rp.path].set()
    await _drain()
    assert td._tasks == set()
