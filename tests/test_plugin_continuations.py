"""Browser executor §5.4–§5.6 (Plan 3 C4, C8, C11): continuation turns rebuilt
from a binding, the relay of their answer up the requester chain, and the driver
sweep that calls a plugin's declared tool directly."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent as agent_mod
import plugin_bindings
import plugin_continuations as pc
import specialist_desk
import tools as tools_mod
from authz_grants import GrantIdentity
from config import AgentConfig, CharacterConfig, MemoryConfig
from plugin_registry import ResolutionResult, ResolvedPlugin
from tools import DelegatedOutput

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

ORIGIN = {"role": "assistant", "channel": "telegram", "chat_id": 42, "user_id": 100,
          "user_name": "N", "_origin_route": "telegram", "_origin_clearance": "private",
          "user_text": "collect them", "_operator_turn": True}


def _cfg(role, name, kind="specialist", budget=4000):
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role, model="x",
                       character=CharacterConfig(name=name), system_prompt="x",
                       memory=MemoryConfig(token_budget=budget), kind=kind)


@pytest.fixture
def roles(monkeypatch, tmp_path):
    m = {"assistant": _cfg("assistant", "Ellen", "resident"),
         "finance": _cfg("finance", "Alex"), "browser": _cfg("browser", "Fido", budget=0)}
    monkeypatch.setattr(tools_mod, "_agent_role_map", m, raising=False)
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "b.json")))
    return m


def _binding(chain=("assistant", "browser"), plugin="browser"):
    ident = GrantIdentity(operator_id=100, chat_id=42, enforcement_role=chain[-1],
                          artifact_id="1" * 64, engagement_id="")
    return plugin_bindings.BINDINGS.for_turn(plugin=plugin, turn_key="d-1", identity=ident,
                                             chain=chain, origin=ORIGIN)


def test_a_continuation_origin_is_rebuilt_from_the_binding_alone(roles):
    b = _binding(("assistant", "finance", "browser"))
    o = pc.continuation_origin(b)
    assert o["synthetic"] == "continuation" and o["user_text"] == ""
    assert "_operator_turn" not in o
    assert o["_handoff_chain"] == ("assistant", "finance")
    assert o["execution_role"] == "finance" and o["role"] == "assistant"
    assert (o["chat_id"], o["user_id"], o["_binding_ref"]) == (42, 100, b.ref)
    relay = pc.continuation_origin(b, executing="finance")
    assert relay["_handoff_chain"] == ("assistant",) and relay["synthetic"] == "continuation"
    import handoff
    assert handoff.operator_words({**o, "execution_role": "browser"}) is None


@pytest.mark.asyncio
async def test_one_pending_continuation_per_run_until_it_starts(roles, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake(pending, *, on_start):
        on_start()
        started.set()
        await release.wait()

    monkeypatch.setattr(pc, "run_continuation", _fake)
    q = pc.Continuations()
    b = _binding()
    # first request queues; a second before the turn starts is a duplicate
    assert q.request(b, "r-1", "operator_answer") == "queued"
    assert q.request(b, "r-1", "sweep") == "already_queued"
    assert q.request(b, "r-2", "sweep") == "queued"
    await started.wait()
    # once a turn has started, a tap for the same run queues the next one
    assert q.request(b, "r-1", "operator_answer") == "queued"
    release.set()
    await q.drain()


@pytest.fixture
def desks(monkeypatch):
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (42, 100))
    monkeypatch.setattr(specialist_desk, "DESKS", specialist_desk.DeskRegistry())
    monkeypatch.setattr(tools_mod, "_specialist_limiter", None, raising=False)
    monkeypatch.setattr(pc, "RETRY_EVERY_S", 0.01)
    return specialist_desk.DESKS


class _Reg:
    def __init__(self):
        self.records = []

    async def register_delegation(self, record):
        self.records.append(record)


@pytest.fixture
def announce(monkeypatch):
    reg = _Reg()
    announced: list = []
    monkeypatch.setattr(tools_mod, "_specialist_registry", reg, raising=False)

    def _attach(task, record, **kw):
        announced.append((task, record))
        quiet.append(kw.get("quiet_if"))

    quiet: list = []
    reg.quiet = quiet

    monkeypatch.setattr(tools_mod, "_attach_completion_callback", _attach)
    return reg, announced


def _recording_runner(monkeypatch, seen, gate=None, says=None):
    async def _bounded(cfg, task_text, context_text, resolution=None, output_format=None):
        o = agent_mod.origin_var.get(None) or {}
        seen.append((cfg.role, task_text, o.get("synthetic"), o.get("_handoff_chain"),
                     tools_mod._delegation_quota_key.get()))
        if gate is not None and cfg.role == "browser":
            await gate.wait()
        return DelegatedOutput(text=(says or {}).get(cfg.role, f"{cfg.role} says done"))
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _bounded)


@pytest.mark.asyncio
async def test_a_request_queued_while_a_worker_finishes_is_not_lost(roles, monkeypatch):
    """p3r2c: the finishing worker removes only its own entry — a tap landing
    as its turn returns, before the worker's cleanup, still gets a turn."""
    runs: list = []
    q = pc.Continuations()
    b = _binding()

    async def _fake(pending, *, on_start):
        on_start()
        runs.append(pending)
        if len(runs) == 1:
            assert q.request(b, "r-1", "operator_answer") == "queued"

    monkeypatch.setattr(pc, "run_continuation", _fake)
    assert q.request(b, "r-1", "operator_answer") == "queued"
    for _ in range(50):
        await asyncio.sleep(0)
    assert len(runs) == 2
    await q.drain()


@pytest.mark.asyncio
async def test_the_answer_climbs_the_chain_one_requester_at_a_time(roles, desks, announce,
                                                                   monkeypatch):
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-7", "operator_answer", 0.0), on_start=lambda: None)
    task, record = announced[0]
    out = await task
    assert [x[0] for x in seen] == ["browser", "finance"]
    assert seen[0][1] == "Continue browsing run r-7." and seen[0][4] == record.id
    assert "Fido has finished part of the work" in seen[1][1]
    assert "browser says done" in seen[1][1]
    assert seen[1][2] == "continuation" and seen[1][3] == ("assistant",)
    assert out.text == "finance says done" and record.agent == "finance"
    assert record.origin["role"] == "assistant" and record.origin["chat_id"] == 42


@pytest.mark.asyncio
async def test_an_unloaded_requester_fails_the_relay_instead_of_passing_the_answer_on(
        roles, desks, announce, monkeypatch):
    seen: list = []
    _recording_runner(monkeypatch, seen)
    del roles["finance"]
    b = _binding(("assistant", "finance", "browser"))
    out = DelegatedOutput(text="browser secret answer")
    with pytest.raises(pc.RequesterUnavailable) as exc:
        await pc._relay_up(b, "r-1", out, deadline=1e18)
    assert "browser secret answer" not in str(exc.value)


@pytest.mark.asyncio
async def test_a_continuation_waits_for_the_desk_and_lets_it_go_after_its_own_turn(
        roles, desks, announce, monkeypatch):
    seen: list = []
    gate = asyncio.Event()
    _recording_runner(monkeypatch, seen, gate=gate)
    reg, announced = announce
    fido_desk = desks.get_or_create(42, "browser")
    finance_desk = desks.get_or_create(42, "finance")
    b = _binding(("assistant", "finance", "browser"))
    starts: list = []
    async with fido_desk.use(fido_desk.reserve()):           # a Fido turn still drives the run
        worker = asyncio.create_task(pc.run_continuation(
            pc.Pending(b, "r-1", "operator_answer", 0.0), on_start=lambda: starts.append(1)))
        for _ in range(20):
            await asyncio.sleep(0)
        assert starts == [] and reg.records == []
    async with finance_desk.use(finance_desk.reserve()):     # a finance turn holds its desk
        for _ in range(50):
            await asyncio.sleep(0)
        gate.set()                                            # Fido's turn ends
        for _ in range(200):
            await asyncio.sleep(0)
            if not fido_desk.lock.locked():
                break
        assert not fido_desk.lock.locked()                    # Fido's desk is free again
        assert [x[0] for x in seen] == ["browser"]            # the relay waits for finance's desk
    await worker
    await announced[0][0]
    assert [x[0] for x in seen] == ["browser", "finance"] and starts == [1]


@pytest.mark.asyncio
async def test_a_desk_that_faults_while_the_continuation_waits_runs_nothing(
        roles, desks, announce, monkeypatch):
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, _ = announce
    desk = desks.get_or_create(42, "browser")
    async with desk.use(desk.reserve()):
        worker = asyncio.create_task(pc.run_continuation(
            pc.Pending(_binding(), "r-1", "sweep", 0.0), on_start=lambda: None))
        for _ in range(20):
            await asyncio.sleep(0)
        desk.faulted = "unconfirmed"
    await worker
    assert seen == [] and reg.records == []


@pytest.mark.asyncio
async def test_a_binding_of_a_former_operator_is_never_continued(roles, desks, announce,
                                                                 monkeypatch):
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (42, 999))
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, _ = announce
    await pc.run_continuation(pc.Pending(_binding(), "r-1", "sweep", 0.0), on_start=lambda: None)
    assert seen == [] and reg.records == []


@pytest.mark.asyncio
async def test_shutdown_cancels_a_running_continuation_turn(roles, desks, announce, monkeypatch):
    cancelled = asyncio.Event()
    entered = asyncio.Event()

    async def _bounded(cfg, task_text, context_text, resolution=None, output_format=None):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _bounded)
    q = pc.Continuations()
    q.request(_binding(), "r-1", "operator_answer")
    await asyncio.wait_for(entered.wait(), 2)
    await q.drain()
    assert cancelled.is_set()
    assert not desks.get_or_create(42, "browser").lock.locked()


@pytest.mark.asyncio
async def test_a_relayed_answer_is_not_banked_as_the_requesters_task(roles, monkeypatch):
    """INV-MEM-024: finance's relay turn banks its own answer, never Fido's."""
    banked: list = []

    async def _retain(sem, *, origin_channel, turns, **kw):
        banked.extend(t.text for t in turns)

    monkeypatch.setattr(tools_mod, "retain_delegated", _retain)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", object(), raising=False)

    class _Client:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return None
        async def query(self, prompt): return None
        async def receive_response(self):
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
            yield AssistantMessage(content=[TextBlock(text="finance answer")], model="x")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="s")

    monkeypatch.setattr(tools_mod, "ClaudeSDKClient", _Client)
    b = _binding(("assistant", "finance", "browser"))
    origin = pc.continuation_origin(b, executing="finance")
    tok = agent_mod.origin_var.set(origin)
    try:
        await tools_mod._run_delegated_agent(roles["finance"], "Fido's secret answer", "")
    finally:
        agent_mod.origin_var.reset(tok)
    await asyncio.gather(*list(tools_mod._specialist_bg_tasks), return_exceptions=True)
    assert "Fido's secret answer" not in banked
    assert "finance answer" in banked


# -- the driver sweep ----------------------------------------------------------------------

def test_the_plugin_data_dir_follows_the_clis_rule():
    env = {"CLAUDE_CONFIG_DIR": "/data/claude", "HOME": "/root"}
    assert pc.plugin_data_dir("browser", environ=env) == "/data/claude/plugins/data/browser-inline"
    assert pc.plugin_data_dir("my.plugin", environ={"HOME": "/root"}) == (
        "/root/.claude/plugins/data/my-plugin-inline")


SERVER = r'''
import json, os, sys
open(os.environ["PROBE_OUT"], "w").write(json.dumps({
    "data": os.environ.get("CLAUDE_PLUGIN_DATA"), "root": os.environ.get("CLAUDE_PLUGIN_ROOT"),
    "token": os.environ.get("X_TOKEN")}))
for line in sys.stdin:
    msg = json.loads(line)
    mid = msg.get("id")
    if msg.get("method") == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "browser", "version": "0"}}
    elif msg.get("method") == "tools/list":
        res = {"tools": [{"name": "runs_needing_driver", "inputSchema": {"type": "object"}}]}
    elif msg.get("method") == "tools/call":
        res = {"content": [{"type": "text", "text": json.dumps({"runs": json.loads(os.environ["RUNS"])})}]}
    elif mid is not None:
        res = {}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
'''


def _plugin(tmp_path, runs, *, sweep="runs_needing_driver"):
    root = tmp_path / "browser"
    root.mkdir()
    (root / "srv.py").write_text(SERVER)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"browser": {
        "command": sys.executable, "args": ["${CLAUDE_PLUGIN_ROOT}/srv.py"],
        "env": {"X_TOKEN": "${CASA_PLUGIN_BROWSER_TOKEN:-none}",
                "RUNS": json.dumps(runs), "PROBE_OUT": str(tmp_path / "probe.json")}}}}))
    manifest = {"name": "browser", "casa": {
        "driverSweep": sweep,
        "resultContract": {"version": 1, "tools": {"runs_needing_driver": {"result": "safe"}}}}}
    return ResolvedPlugin(name="browser", artifact_id="1" * 64, path=str(root),
                          version="0.1.0", manifest=manifest)


@pytest.mark.asyncio
async def test_the_sweep_calls_the_declared_tool_and_queues_its_runs(roles, monkeypatch, tmp_path):
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (42, 100))
    mine = _binding()
    other = _binding(plugin="bank-feed")
    former = plugin_bindings.BINDINGS.for_turn(
        plugin="browser", turn_key="old", chain=("assistant", "browser"), origin=ORIGIN,
        identity=GrantIdentity(operator_id=999, chat_id=42, enforcement_role="browser",
                               artifact_id="1" * 64, engagement_id=""))
    rp = _plugin(tmp_path, [{"run_ref": "r-1", "binding_ref": mine.ref},
                            {"run_ref": "r-2", "binding_ref": other.ref},
                            {"run_ref": "r-4", "binding_ref": former.ref},
                            {"run_ref": "r-3", "binding_ref": "casa-bind-unknown"},
                            {"run_ref": "x" * 80, "binding_ref": mine.ref}])
    monkeypatch.setattr(tools_mod, "_delegated_resolution",
                        lambda cfg: ResolutionResult(registry_valid=True,
                                                     plugins=[rp] if cfg.role == "browser" else []))
    queued: list = []

    class _Q:
        def request(self, binding, run_ref, reason):
            queued.append((binding.ref, run_ref, reason))
            return "queued"

    monkeypatch.setattr(pc, "CONTINUATIONS", _Q())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CASA_PLUGIN_BROWSER_TOKEN", "t0k")
    assert await pc.sweep_once() == 1
    assert queued == [(mine.ref, "r-1", "sweep")]          # never another plugin's binding
    probe = json.loads((tmp_path / "probe.json").read_text())
    assert probe["data"] == str(tmp_path / "cfg" / "plugins" / "data" / "browser-inline")
    assert probe["root"] == rp.path and probe["token"] == "t0k"


@pytest.mark.asyncio
async def test_an_invalid_sweep_declaration_sweeps_nothing(roles, monkeypatch, tmp_path):
    rp = _plugin(tmp_path, [], sweep="missing_tool")
    monkeypatch.setattr(tools_mod, "_delegated_resolution",
                        lambda cfg: ResolutionResult(registry_valid=True, plugins=[rp]))
    assert await pc.sweep_once() == 0
    assert not (tmp_path / "probe.json").exists()          # the server was never started


@pytest.mark.asyncio
async def test_the_sweep_calls_only_a_tool_the_server_lists(roles, monkeypatch, tmp_path):
    """p3r2c: a declared tool the server does not list is never called."""
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (42, 100))
    rp = _plugin(tmp_path, [{"run_ref": "r-1", "binding_ref": _binding().ref}],
                 sweep="runs_needing_driver")
    srv = Path(rp.path) / "srv.py"
    srv.write_text(srv.read_text().replace('"name": "runs_needing_driver"', '"name": "other"'))
    monkeypatch.setattr(tools_mod, "_delegated_resolution",
                        lambda cfg: ResolutionResult(registry_valid=True, plugins=[rp]))
    queued: list = []
    monkeypatch.setattr(pc, "CONTINUATIONS", SimpleNamespace(
        request=lambda *a: queued.append(a) or "queued"))
    assert await pc.sweep_once() == 0 and queued == []


@pytest.mark.asyncio
async def test_a_turn_still_unwinding_keeps_the_desk_refused(roles, desks, announce, monkeypatch):
    """p3r3c: #1197's guard — a run the bounded runner gave up on refuses the
    desk until it ends, for a continuation as for any desk use."""
    leftover = asyncio.Event()

    async def _bounded(cfg, task_text, context_text, resolution=None, output_format=None):
        sink = tools_mod._desk_run_sink.get()
        assert sink is not None
        sink.append(asyncio.ensure_future(leftover.wait()))
        return DelegatedOutput(text="done")

    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _bounded)
    await pc.run_continuation(pc.Pending(_binding(), "r-1", "sweep", 0.0), on_start=lambda: None)
    desk = desks.get_or_create(42, "browser")
    assert desk.faulted is not None
    leftover.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert desk.faulted is None


@pytest.mark.asyncio
async def test_a_stop_during_the_relay_tells_the_resident_instead_of_going_silent(
        roles, desks, announce, monkeypatch):
    """p3r3c: Casa stops while the answer waits for finance's desk — the chain
    task fails with Casa's own message, so the resident is told durably."""
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, announced = announce
    finance_desk = desks.get_or_create(42, "finance")
    q = pc.Continuations()
    async with finance_desk.use(finance_desk.reserve()):
        q.request(_binding(("assistant", "finance", "browser")), "r-9", "operator_answer")
        for _ in range(200):
            await asyncio.sleep(0)
            if announced and seen:
                break
        await q.drain()
    task, record = announced[0]
    exc = task.exception()
    assert isinstance(exc, pc.RelayInterrupted) and "browser says done" not in str(exc)
    assert [x[0] for x in seen] == ["browser"]


@pytest.mark.asyncio
async def test_a_relay_whose_author_is_no_longer_loaded_is_not_banked(roles, monkeypatch):
    """p3r3c: the relayed task's author unloaded — kept out, like its notice."""
    import delegated_memory
    del roles["browser"]
    assert delegated_memory.role_writes_memory("browser") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("composed", [True, False], ids=["inside-relay", "plain-hand-on"])
async def test_no_task_inside_a_relay_is_banked_however_deep(roles, monkeypatch, composed):
    """p3r4c: a hand-off made from inside a relay turn (Fido → a memory-writing
    specialist) banks the target's answer, never the task — the rule reads the
    chain's shape and Casa's marker, nothing a nested run could carry over."""
    banked: list = []

    async def _retain(sem, *, origin_channel, turns, **kw):
        banked.extend(t.text for t in turns)

    monkeypatch.setattr(tools_mod, "retain_delegated", _retain)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", object(), raising=False)

    class _Client:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return None
        async def query(self, prompt): return None
        async def receive_response(self):
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
            yield AssistantMessage(content=[TextBlock(text="target answer")], model="x")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="s")

    monkeypatch.setattr(tools_mod, "ClaudeSDKClient", _Client)
    b = _binding(("assistant", "finance", "browser"))
    relay = pc.continuation_origin(b, executing="browser")
    # Fido's relay turn, handing on to finance (a nested delegated run)
    nested = {**relay, "execution_role": "browser",
              "_handoff_chain": ("assistant", "browser"), "delegation_depth": 1}
    if not composed:
        nested.pop("synthetic")      # a specialist handing on outside any relay
    tok = agent_mod.origin_var.set(nested)
    try:
        await tools_mod._run_delegated_agent(roles["finance"], "FIDO CANARY", "")
    finally:
        agent_mod.origin_var.reset(tok)
    await asyncio.gather(*list(tools_mod._specialist_bg_tasks), return_exceptions=True)
    assert "FIDO CANARY" not in banked and "target answer" in banked


@pytest.mark.asyncio
async def test_a_progress_only_answer_climbs_no_further(roles, desks, announce, monkeypatch):
    """Operator ruling Q3 (C4a): a continuation turn that answers only
    ``<silent/>`` declared progress — no relay turn, and its record is attached
    quietly, so the resident hears only results and questions."""
    seen: list = []
    _recording_runner(monkeypatch, seen, says={"browser": "<silent/>"})
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-9", "sweep", 0.0), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser"] and out.text == "<silent/>"
    assert reg.quiet[0](out) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["", "   ", "<silent/><silent/>"])
async def test_only_the_exact_sentinel_declares_progress(roles, desks, announce, monkeypatch,
                                                         said):
    """p3r5x: an empty answer is not a declaration — it climbs and is announced."""
    seen: list = []
    _recording_runner(monkeypatch, seen, says={"browser": said})
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-6", "sweep", 0.0), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser", "finance"]
    assert reg.quiet[0](out) is False


@pytest.mark.asyncio
async def test_a_requesters_own_silence_never_hides_the_result(roles, desks, announce,
                                                               monkeypatch):
    """p3r5c: quiet is the continuation turn's own declaration only — a relay
    requester answering ``<silent/>`` to a real result is still announced."""
    seen: list = []
    _recording_runner(monkeypatch, seen, says={"finance": "<silent/>"})
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-8", "sweep", 0.0), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser", "finance"] and out.text == "<silent/>"
    assert reg.quiet[0](out) is False


class _Registry:
    def __init__(self):
        self.completed: list = []
        self.job_registry = SimpleNamespace(ack_terminal_notification=self._ack)

    async def _ack(self, _id):
        return None

    async def complete_delegation(self, did, result="", *, announce_creator=False):
        self.completed.append((did, result, announce_creator))
        if self.fail_write:
            raise OSError("disk full")
        return None

    fail_write = False

    async def fail_compat(self, *a, **k):
        return None


class _Bus:
    def __init__(self):
        self.sent: list = []

    async def notify(self, msg):
        self.sent.append(msg)


@pytest.mark.asyncio
@pytest.mark.parametrize("quiet, aborted, told, fail_write", [
    (True, False, False, False),            # declared progress: completed, nobody told
    (True, False, False, True),             # …and a failed write is retried, still owing nothing
    (False, False, True, False),            # a result
    (True, True, True, False),              # an aborted run is always announced
], ids=["progress", "progress-write-fails", "result", "aborted"])
async def test_a_quiet_record_completes_without_a_notice(monkeypatch, quiet, aborted, told,
                                                         fail_write):
    from specialist_registry import DelegationRecord
    import time as _t
    reg, bus = _Registry(), _Bus()
    reg.fail_write = fail_write
    retried: list = []
    reg.job_registry.schedule_completion_reconciliation = (
        lambda did, result="", *, announce_creator=False: retried.append(announce_creator))
    monkeypatch.setattr(tools_mod, "_specialist_registry", reg, raising=False)
    monkeypatch.setattr(tools_mod, "_bus", bus, raising=False)
    failed: list = []

    async def _fail(did, failure, *, announce_creator):
        failed.append(did)
        return None

    monkeypatch.setattr(tools_mod, "_fail_delegation_durably", _fail)
    rec = DelegationRecord(id="d" * 32, agent="browser", started_at=_t.time(),
                           origin={"role": "assistant", "channel": "telegram",
                                   "chat_id": 42, "cid": "c"})

    async def _out():
        return DelegatedOutput(text="<silent/>" if quiet else "the result",
                               run_subtype="error_max_turns" if aborted else "success")

    task = asyncio.create_task(_out())
    tools_mod._attach_completion_callback(task, rec, quiet_if=lambda o: quiet)
    await task
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks), return_exceptions=True)
    assert bool(bus.sent) is told
    if not aborted:
        assert reg.completed == [(rec.id, reg.completed[0][1], told)]
    assert retried == ([False] if fail_write else [])
