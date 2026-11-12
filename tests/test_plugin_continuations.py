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
import agent_lane
import plugin_bindings
import plugin_continuations as pc
import tools as tools_mod
from authz_grants import GrantIdentity
from config import AgentConfig, CharacterConfig, MemoryConfig
from plugin_registry import ResolutionResult, ResolvedPlugin
from tools import DelegatedOutput
from error_kinds import ApiErrorTurn, ErrorKind
from output_boundary import SILENCE_SENTINEL
from test_delegated_transcript_retention import TRANSCRIPT, _Harness, _project, _write_session
from test_memory_capture import captured_events, mem  # noqa: F401 — fixture
from transcript_runs_support import rows
from two_endpoints import CASA_BOT, build_two_endpoints

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

BOT = CASA_BOT
ORIGIN = {"role": "assistant", "channel": "telegram", "chat_id": 42, "user_id": 100,
          "user_name": "N", "_origin_route": "telegram", "_origin_clearance": "private",
          "user_text": "collect them", "_operator_turn": True, "_tg_bot": BOT}


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
    monkeypatch.setattr(tools_mod, "_channel_manager", build_two_endpoints().manager,
                        raising=False)
    return m


def _binding(chain=("assistant", "browser"), plugin="browser", origin=ORIGIN, turn_key="d-1"):
    ident = GrantIdentity(operator_id=100, chat_id=42, enforcement_role=chain[-1],
                          artifact_id="1" * 64, engagement_id="", bot_id=BOT)
    return plugin_bindings.BINDINGS.for_turn(plugin=plugin, turn_key=turn_key, identity=ident,
                                             chain=chain, origin=origin)


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


def test_a_binding_carries_the_task_proof_into_its_continuation_and_relay_turns(roles):
    chain = ("assistant", "finance", "browser")
    b = _binding(chain, origin={**ORIGIN, "_operator_task": True, "_owner_pass": 2})
    assert (b.origin["_operator_task"], b.origin["_owner_pass"]) == (True, 2)
    for o in (pc.continuation_origin(b), pc.continuation_origin(b, executing="finance")):
        assert o["_operator_task"] is True and o["_owner_pass"] == 2
    # it survives a restart with the binding
    reloaded = plugin_bindings.BindingStore(plugin_bindings.BINDINGS._path).peek(b.ref)
    assert pc.continuation_origin(reloaded)["_operator_task"] is True
    plain = _binding(chain, turn_key="d-2")
    for o in (pc.continuation_origin(plain), pc.continuation_origin(plain, executing="finance")):
        assert "_operator_task" not in o and "_owner_pass" not in o
    # a value that is not the literal proof is not carried
    odd = _binding(chain, turn_key="d-3",
                   origin={**ORIGIN, "_operator_task": "yes", "_owner_pass": True})
    assert not {"_operator_task", "_owner_pass"} & set(pc.continuation_origin(odd))


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
def lanes(monkeypatch):
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (42, 100))
    agent_lane.reset()
    monkeypatch.setattr(tools_mod, "_specialist_limiter", None, raising=False)
    monkeypatch.setattr(pc, "RETRY_EVERY_S", 0.01)
    return agent_lane.LANES


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
        said = (says or {}).get(cfg.role, f"{cfg.role} says done")
        return said if isinstance(said, DelegatedOutput) else DelegatedOutput(text=said)
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
async def test_the_answer_climbs_the_chain_one_requester_at_a_time(roles, lanes, announce,
                                                                   monkeypatch):
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-7", "operator_answer", 0.0, bot_id=BOT), on_start=lambda: None)
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
async def test_a_chain_started_in_a_specialists_own_chat_answers_that_specialist(
        roles, lanes, announce, monkeypatch):
    """A specialist asked in its own chat handed work on: the helper's finished
    run is announced to that specialist's conversation, as a resident's is, and
    no relay turn runs — the root is told by the announcement."""
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, announced = announce
    b = _binding(("finance", "browser"), origin={**ORIGIN, "role": "finance"})
    await pc.run_continuation(pc.Pending(b, "r-8", "operator_answer", 0.0, bot_id=BOT),
                              on_start=lambda: None)
    task, record = announced[0]
    out = await task
    assert [x[0] for x in seen] == ["browser"]
    assert seen[0][3] == ("finance",)             # asked by finance, from its own turn
    assert out.text == "browser says done"
    assert record.agent == "browser" and record.requester.role == "finance"
    assert record.origin["role"] == "finance" and record.origin["chat_id"] == 42


@pytest.mark.asyncio
async def test_a_run_started_in_an_engagement_is_continued_as_its_specialist(
        roles, lanes, announce, monkeypatch):
    """Phone round 7: the call made inside an interactive engagement runs under
    the launching resident's turn origin; the sweep's continuation of its run
    must be a turn of the specialist that runs it, never of the resident."""
    import operator_calls
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    engagement_turn = {**ORIGIN, "cid": "ellen-turn", "delegation_depth": 1,
                       "_operator_words": "order the pizza"}
    ident = GrantIdentity(operator_id=100, chat_id=42, enforcement_role="browser",
                          artifact_id="1" * 64, engagement_id="", bot_id=BOT)
    call = operator_calls.CALLS.open(
        client_id="c1", tool_use_id="tu", tool_name="mcp__plugin_browser_api__start_runs",
        role="browser", plugin="browser", identity=ident, origin=engagement_turn)
    ref = operator_calls.requester_facts(call)["binding_ref"]
    binding = plugin_bindings.BINDINGS.peek(ref)
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, announced = announce
    await pc.run_continuation(pc.Pending(binding, "r-1", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    task, record = announced[0]
    await task
    assert [x[0] for x in seen] == ["browser"]
    assert seen[0][1] == "Continue browsing run r-1." and record.agent == "browser"


@pytest.mark.asyncio
async def test_a_continuation_addressed_to_a_resident_runs_nothing(roles, lanes, announce,
                                                                  monkeypatch, caplog):
    """A binding whose chain ends with a resident (one minted before the chain
    fix, still on disk) is dropped with one warning: a resident has no plugin
    run to continue, and its answer would reach the operator every sweep."""
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, announced = announce
    b = _binding(("assistant",))
    with caplog.at_level("WARNING", logger="plugin_continuations"):
        await pc.run_continuation(pc.Pending(b, "r-1", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    assert seen == [] and announced == [] and reg.records == []
    assert [r.message for r in caplog.records if r.levelname == "WARNING"] == [
        "continuation: role assistant is not a specialist — dropped"]


@pytest.mark.asyncio
async def test_an_unloaded_requester_fails_the_relay_instead_of_passing_the_answer_on(
        roles, lanes, announce, monkeypatch):
    seen: list = []
    _recording_runner(monkeypatch, seen)
    del roles["finance"]
    b = _binding(("assistant", "finance", "browser"))
    out = DelegatedOutput(text="browser secret answer")
    with pytest.raises(pc.RequesterUnavailable) as exc:
        await pc._relay_up(b, "r-1", out, deadline=1e18)
    assert "browser secret answer" not in str(exc.value)


@pytest.mark.asyncio
async def test_a_continuation_waits_for_the_lane_and_lets_it_go_after_its_own_turn(
        roles, lanes, announce, monkeypatch):
    seen: list = []
    gate = asyncio.Event()
    _recording_runner(monkeypatch, seen, gate=gate)
    reg, announced = announce
    fido_lane = lanes.get("browser")
    finance_lane = lanes.get("finance")
    b = _binding(("assistant", "finance", "browser"))
    starts: list = []
    async with fido_lane.use(fido_lane.reserve()):           # a Fido turn still drives the run
        worker = asyncio.create_task(pc.run_continuation(
            pc.Pending(b, "r-1", "operator_answer", 0.0, bot_id=BOT), on_start=lambda: starts.append(1)))
        for _ in range(20):
            await asyncio.sleep(0)
        assert starts == [] and reg.records == []
    async with finance_lane.use(finance_lane.reserve()):     # a finance turn holds its lane
        for _ in range(50):
            await asyncio.sleep(0)
        gate.set()                                            # Fido's turn ends
        for _ in range(200):
            await asyncio.sleep(0)
            if not fido_lane.lock.locked():
                break
        assert not fido_lane.lock.locked()                    # Fido's lane is free again
        assert [x[0] for x in seen] == ["browser"]            # the relay waits for finance's lane
    await worker
    await announced[0][0]
    assert [x[0] for x in seen] == ["browser", "finance"] and starts == [1]


@pytest.mark.asyncio
async def test_an_agent_that_faults_while_the_continuation_waits_runs_nothing(
        roles, lanes, announce, monkeypatch):
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, _ = announce
    lane = lanes.get("browser")
    async with lane.use(lane.reserve()):
        worker = asyncio.create_task(pc.run_continuation(
            pc.Pending(_binding(), "r-1", "sweep", 0.0, bot_id=BOT), on_start=lambda: None))
        for _ in range(20):
            await asyncio.sleep(0)
        agent_lane.FAULTS.mark("browser", "r-0", "browser", reason="unconfirmed")
    await worker
    assert seen == [] and reg.records == []


@pytest.mark.asyncio
async def test_a_binding_of_a_former_operator_is_never_continued(roles, lanes, announce,
                                                                 monkeypatch):
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (42, 999))
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, _ = announce
    await pc.run_continuation(pc.Pending(_binding(), "r-1", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    assert seen == [] and reg.records == []


@pytest.mark.asyncio
async def test_shutdown_cancels_a_running_continuation_turn(roles, lanes, announce, monkeypatch):
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
    assert not lanes.get("browser").lock.locked()


@pytest.mark.asyncio
async def test_a_relayed_answer_is_not_banked_as_the_requesters_task(roles, monkeypatch,
                                                                    mem):
    """INV-MEM-032: finance's relay turn captures its own answer, never Fido's."""

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
    # What was captured, wherever it waits: on a loaded machine a capture
    # that met a busy store is durable in the spool (a canary there counts).
    banked = [e["text"] for e in await captured_events(mem)]
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
        with open(os.environ["PROBE_OUT"] + ".calls", "a") as fh:
            fh.write("call\n")
        answer = {"runs": json.loads(os.environ["RUNS"]), **json.loads(os.environ.get("EXTRA", "{}"))}
        res = {"content": [{"type": "text", "text": json.dumps(answer)}]}
    elif mid is not None:
        res = {}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
'''


def _plugin(tmp_path, runs, *, sweep="runs_needing_driver", extra=None):
    root = tmp_path / "browser"
    root.mkdir()
    (root / "srv.py").write_text(SERVER)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"browser": {
        "command": sys.executable, "args": ["${CLAUDE_PLUGIN_ROOT}/srv.py"],
        "env": {"X_TOKEN": "${CASA_PLUGIN_BROWSER_TOKEN:-none}",
                "RUNS": json.dumps(runs), "EXTRA": json.dumps(extra or {}),
                "PROBE_OUT": str(tmp_path / "probe.json")}}}}))
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
                               artifact_id="1" * 64, engagement_id="", bot_id=BOT))
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


# -- slice 5k: a hand-over Casa delivered holds the run; a live view is looked at again soon ----

def _sweep_setup(monkeypatch, tmp_path, runs, extra=None):
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (42, 100))
    rp = _plugin(tmp_path, runs, extra=extra)
    monkeypatch.setattr(tools_mod, "_delegated_resolution",
                        lambda cfg: ResolutionResult(registry_valid=True,
                                                     plugins=[rp] if cfg.role == "browser" else []))
    queued: list = []
    monkeypatch.setattr(pc, "CONTINUATIONS", SimpleNamespace(
        request=lambda binding, run_ref, reason: queued.append(run_ref) or "queued"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    return rp, queued


def _outcome(monkeypatch, ref, delivered=True, *, plugin="browser", operator_id=100,
             artifact_id="1" * 64):
    import result_broker
    outcomes = result_broker.DeliveryOutcomes()
    monkeypatch.setattr(result_broker, "DELIVERY_OUTCOMES", outcomes)
    outcomes.record(ref, plugin=plugin, operator_id=operator_id, delivered=delivered,
                    repeat=False, artifact_id=artifact_id)
    return outcomes


REF_A = "casa-cap-" + "a" * 32
REF_B = "casa-cap-" + "b" * 32


@pytest.mark.asyncio
async def test_a_run_held_by_a_delivered_hand_over_is_not_continued(roles, monkeypatch, tmp_path):
    """Slice 5k: the plugin names the deposits whose delivery it has not confirmed yet; Casa,
    which delivered them, skips the run when every one of them reached the operator."""
    b = _binding()
    _rp, queued = _sweep_setup(monkeypatch, tmp_path, [
        {"run_ref": "r-held", "binding_ref": b.ref, "unless_delivered": [REF_A]},
        {"run_ref": "r-free", "binding_ref": b.ref}])
    _outcome(monkeypatch, REF_A)
    assert await pc.sweep_once() == 1
    assert queued == ["r-free"]


@pytest.mark.parametrize("entry_refs, recorded", [
    ([REF_A, REF_B], {REF_A: True}),                 # one of them not known delivered
    ([REF_A], {REF_A: False}),                       # withheld
    ([REF_A], {}),                                   # Casa forgot it (a restart): unknown
    ("casa-cap-" + "a" * 32, {REF_A: True}),         # not a list
    ([], {}),                                        # empty
    ([REF_A] * 5, {REF_A: True}),                    # too many
    (["not-a-reference"], {}),
])
@pytest.mark.asyncio
async def test_a_hand_over_not_known_delivered_still_continues_the_run(
        roles, monkeypatch, tmp_path, entry_refs, recorded):
    b = _binding()
    _rp, queued = _sweep_setup(monkeypatch, tmp_path, [
        {"run_ref": "r-1", "binding_ref": b.ref, "unless_delivered": entry_refs}])
    import result_broker
    outcomes = result_broker.DeliveryOutcomes()
    monkeypatch.setattr(result_broker, "DELIVERY_OUTCOMES", outcomes)
    for ref, delivered in recorded.items():
        outcomes.record(ref, plugin="browser", operator_id=100, delivered=delivered,
                        repeat=False, artifact_id="1" * 64)
    assert await pc.sweep_once() == 1 and queued == ["r-1"]


@pytest.mark.parametrize("who", [{"plugin": "bank-feed"}, {"operator_id": 7},
                                 {"artifact_id": "2" * 64}])
@pytest.mark.asyncio
async def test_only_the_plugins_own_delivery_to_this_operator_holds_a_run(
        roles, monkeypatch, tmp_path, who):
    b = _binding()
    _rp, queued = _sweep_setup(monkeypatch, tmp_path, [
        {"run_ref": "r-1", "binding_ref": b.ref, "unless_delivered": [REF_A]}])
    _outcome(monkeypatch, REF_A, **who)
    assert await pc.sweep_once() == 1 and queued == ["r-1"]


@pytest.fixture
def extra_pass(monkeypatch):
    monkeypatch.setattr(pc, "SWEEP_AGAIN_MIN_S", 0.05)
    monkeypatch.setattr(pc, "_stopping", False)
    pc.cancel_extra_pass()
    yield
    pc.cancel_extra_pass()


def _calls(tmp_path) -> int:
    path = tmp_path / "probe.json.calls"
    return len(path.read_text().splitlines()) if path.exists() else 0


async def _settle(seconds: float) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_a_plugin_that_asks_is_swept_again_soon(roles, monkeypatch, tmp_path, extra_pass):
    """Slice 5k: the operator's Done in a live view reaches no one, so while a view is open the
    plugin asks to be swept again in a few seconds; each pass arms at most one more."""
    _rp, queued = _sweep_setup(monkeypatch, tmp_path, [], extra={"sweep_again_s": 1})
    await pc.sweep_job()
    assert _calls(tmp_path) == 1
    await _settle(1.4)
    assert _calls(tmp_path) == 2                 # looked at again after its second
    pc.cancel_extra_pass()                       # Casa's stop
    await _settle(1.4)
    assert _calls(tmp_path) == 2                 # nothing more runs
    assert queued == []


@pytest.mark.asyncio
async def test_an_extra_pass_comes_no_sooner_than_five_seconds(roles, monkeypatch, tmp_path):
    rp = _plugin(tmp_path, [], extra={"sweep_again_s": 1})
    from plugin_continuations import sweep_server
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    runs, again, ended = await pc.call_sweep_tool(sweep_server(rp), "runs_needing_driver")
    assert runs == [] and again == 5.0 and ended == []


@pytest.mark.parametrize("value, expected", [
    ([{"ref": "request:k1", "binding_ref": "b"}, "x", 3],
     [{"ref": "request:k1", "binding_ref": "b"}]),
    ([{"ref": f"request:k{i}"} for i in range(80)],
     [{"ref": f"request:k{i}"} for i in range(50)]),
    ("request:k1", []), (None, []), ({"ref": "request:k1"}, [])])
@pytest.mark.asyncio
async def test_the_sweep_reads_the_requests_that_ended(roles, monkeypatch, tmp_path, value,
                                                       expected):
    """v0.347.6: the plugin names the requests that ended on their own (``ended``); only a
    list's objects are read, at most fifty."""
    rp = _plugin(tmp_path, [], extra={"ended": value})
    from plugin_continuations import sweep_server
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    _runs, _again, ended = await pc.call_sweep_tool(sweep_server(rp), "runs_needing_driver")
    assert ended == expected


@pytest.mark.asyncio
async def test_no_extra_pass_unless_asked(roles, monkeypatch, tmp_path, extra_pass):
    _sweep_setup(monkeypatch, tmp_path, [])
    await pc.sweep_job()
    await _settle(0.4)
    assert _calls(tmp_path) == 1


@pytest.mark.parametrize("value", [True, "5", None, 0, -3, 60, 600, 5.5])
@pytest.mark.asyncio
async def test_a_bad_or_minute_long_request_arms_nothing(roles, monkeypatch, tmp_path, extra_pass,
                                                        value):
    _sweep_setup(monkeypatch, tmp_path, [], extra={"sweep_again_s": value})
    await pc.sweep_job()
    assert pc._extra["handle"] is None            # nothing armed
    await _settle(0.3)
    assert _calls(tmp_path) == 1


@pytest.mark.asyncio
async def test_a_pass_never_overlaps_another(roles, monkeypatch, tmp_path, extra_pass):
    """The minute pass and an extra pass never run at once: the later one returns."""
    gate = asyncio.Event()
    started: list = []

    async def _slow_pass():
        started.append(1)
        await gate.wait()
        return 0, None

    monkeypatch.setattr(pc, "_sweep_pass", _slow_pass)
    first = asyncio.ensure_future(pc.sweep_job())
    await asyncio.sleep(0)
    await asyncio.wait_for(pc.sweep_job(), 2)    # returns at once
    assert started == [1]
    gate.set()
    await first
    await pc.sweep_job()
    assert started == [1, 1]


@pytest.mark.asyncio
async def test_a_turn_still_unwinding_keeps_the_lane_refused(roles, lanes, announce, monkeypatch):
    """#1197's guard — a run the bounded runner gave up on refuses the lane
    until it ends, for a continuation as for any lane use."""
    leftover = asyncio.Event()

    async def _bounded(cfg, task_text, context_text, resolution=None, output_format=None):
        sink = tools_mod._lane_run_sink.get()
        assert sink is not None
        sink.append(asyncio.ensure_future(leftover.wait()))
        return DelegatedOutput(text="done")

    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _bounded)
    await pc.run_continuation(pc.Pending(_binding(), "r-1", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    lane = lanes.get("browser")
    assert lane.faulted is not None
    leftover.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert lane.faulted is None


@pytest.mark.asyncio
async def test_a_stop_during_the_relay_tells_the_resident_instead_of_going_silent(
        roles, lanes, announce, monkeypatch):
    """Casa stops while the answer waits for finance's lane — the chain
    task fails with Casa's own message, so the resident is told durably."""
    seen: list = []
    _recording_runner(monkeypatch, seen)
    reg, announced = announce
    finance_lane = lanes.get("finance")
    q = pc.Continuations()
    async with finance_lane.use(finance_lane.reserve()):
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
async def test_no_task_inside_a_relay_is_banked_however_deep(roles, monkeypatch, composed,
                                                             mem):
    """p3r4c: a hand-off made from inside a relay turn (Fido → a memory-writing
    specialist) captures the target's answer, never the task — the rule reads
    the chain's shape and Casa's marker, nothing a nested run could carry over."""

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
    # What was captured, wherever it waits: on a loaded machine a capture
    # that met a busy store is durable in the spool (a canary there counts).
    banked = [e["text"] for e in await captured_events(mem)]
    assert "FIDO CANARY" not in banked and "target answer" in banked


@pytest.mark.asyncio
async def test_a_progress_only_answer_climbs_no_further(roles, lanes, announce, monkeypatch):
    """Operator ruling Q3 (C4a): a continuation turn that answers only
    ``<silent/>`` declared progress — no relay turn, and its record is attached
    quietly, so the resident hears only results and questions."""
    seen: list = []
    _recording_runner(monkeypatch, seen, says={"browser": "<silent/>"})
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-9", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser"] and out.text == "<silent/>"
    assert reg.quiet[0](out) is True


@pytest.mark.asyncio
async def test_a_closing_silence_after_notes_declares_progress(roles, lanes, announce,
                                                               monkeypatch):
    """A turn that writes working notes and then closes with ``<silent/>``
    declared progress: what decides is the message it closed with."""
    seen: list = []
    _recording_runner(monkeypatch, seen, says={
        "browser": DelegatedOutput(text="note\n\n<silent/>", final_message="<silent/>")})
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-5", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser"]
    assert reg.quiet[0](out) is True


@pytest.mark.asyncio
async def test_a_closing_message_that_only_starts_with_silence_climbs(roles, lanes, announce,
                                                                      monkeypatch):
    """The closing message is read whole: one that opens with the marker and
    carries an answer far past the result cap is an answer, and climbs."""
    seen: list = []
    closing = "<silent/>" + "\n" * 20000 + "EUR/USD is 1.08."
    _recording_runner(monkeypatch, seen, says={
        "browser": DelegatedOutput(text=closing, final_message=closing)})
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-4", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser", "finance"]
    assert reg.quiet[0](out) is False


def test_the_knowledge_line_reads_like_the_agents_own_memory(roles):
    """The note an agent keeps of a continuation it did not start says who
    asked, what, and how it went, with no sizes or plumbing."""
    assert pc._knowledge_line("assistant", "check the rate", DelegatedOutput(text="x" * 400)) \
        == "Ellen asked you: check the rate — you answered"
    assert pc._knowledge_line("assistant", "check the rate", failed="timeout") \
        == "Ellen asked you: check the rate — you couldn't finish it (timeout)"
    aborted = DelegatedOutput(text="", run_subtype="error_max_turns")
    assert pc._knowledge_line("assistant", "check the rate", aborted) \
        == "Ellen asked you: check the rate — you couldn't finish it (error_max_turns)"


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["", "   ", "<silent/><silent/>"])
async def test_only_the_exact_sentinel_declares_progress(roles, lanes, announce, monkeypatch,
                                                         said):
    """p3r5x: an empty answer is not a declaration — it climbs and is announced."""
    seen: list = []
    _recording_runner(monkeypatch, seen, says={"browser": said})
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-6", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser", "finance"]
    assert reg.quiet[0](out) is False


@pytest.mark.asyncio
async def test_a_requester_that_closes_silent_ends_the_relay_as_progress(roles, lanes, announce,
                                                                         monkeypatch):
    """A specialist requester that closes its relay turn with exactly
    ``<silent/>`` has more work coming back to it: the record completes quiet
    and the requester above it is told nothing."""
    seen: list = []
    _recording_runner(monkeypatch, seen, says={"finance": "<silent/>"})
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-8", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser", "finance"] and out.text == "<silent/>"
    assert reg.quiet[0](out) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("said", [
    "done <silent/> more",
    DelegatedOutput(text="<silent/>", run_subtype="error_max_turns"),
], ids=["marker-inside-an-answer", "aborted-turn"])
async def test_a_requesters_answer_is_announced_unless_it_closes_silent(roles, lanes, announce,
                                                                        monkeypatch, said):
    """Only a completed relay turn whose closing message is the marker alone is
    progress: an answer that merely contains it, and an aborted turn, are
    announced."""
    seen: list = []
    _recording_runner(monkeypatch, seen, says={"finance": said})
    reg, announced = announce
    b = _binding(("assistant", "finance", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-8", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser", "finance"]
    assert reg.quiet[0](out) is False


@pytest.mark.asyncio
async def test_a_silent_requester_stops_the_relay_below_the_requesters_above_it(
        roles, lanes, announce, monkeypatch):
    """With two specialist requesters in the chain, the nearer one closing
    silent means the farther one gets no turn at all."""
    roles["legal"] = _cfg("legal", "Lena")
    seen: list = []
    _recording_runner(monkeypatch, seen, says={"legal": "<silent/>"})
    reg, announced = announce
    b = _binding(("assistant", "finance", "legal", "browser"))
    await pc.run_continuation(pc.Pending(b, "r-8", "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    out = await announced[0][0]
    assert [x[0] for x in seen] == ["browser", "legal"]
    assert reg.quiet[0](out) is True


def test_the_relay_task_says_when_to_close_silent():
    assert ("If you have just started more work whose end will come back to you, close "
            "with exactly <silent/> instead; otherwise answer.") in pc.RELAY_TASK


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


@pytest.mark.asyncio
async def test_a_pass_in_flight_at_casas_stop_arms_nothing(roles, monkeypatch, tmp_path,
                                                          extra_pass):
    """r1 Astra S2: the minute pass still running when Casa stops must not arm another; no
    pass starts after the stop."""
    gate = asyncio.Event()
    passes: list = []

    async def _pass():
        passes.append(1)
        await gate.wait()
        return 0, 1.0

    monkeypatch.setattr(pc, "_sweep_pass", _pass)
    first = asyncio.ensure_future(pc.sweep_job())
    await asyncio.sleep(0)
    pc.stop_sweep()                               # Casa stops while that pass runs
    gate.set()
    await first
    assert pc._extra["handle"] is None
    await pc.sweep_job()                          # the scheduler's next tick
    assert passes == [1]


# -- one conversation per request (INV-PLUG-058) --------------------------------------
#
# The real runner over the CLI double, the SDK's real session lookup over real files.

SID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def conv(roles, lanes, announce, monkeypatch, tmp_path, mem):  # noqa: F811 — the fixture
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    for role, cfg in roles.items():
        cfg.cwd = str(tmp_path / "home" / role)
        Path(cfg.cwd).mkdir(parents=True)
    h = _Harness()
    h.body = TRANSCRIPT
    monkeypatch.setattr(tools_mod, "ClaudeSDKClient", h.client_cls())
    monkeypatch.setattr(tools_mod, "_specialist_telemetry", None, raising=False)
    reg, announced = announce
    return SimpleNamespace(h=h, reg=reg, announced=announced, store=mem, roles=roles)


def _kept(conv, binding, sid=SID):
    """The binding keeps *sid*, whose transcript is on disk."""
    plugin_bindings.BINDINGS.adopt(binding.ref, sid)
    _write_session(_project(conv.roles[binding.chain[-1]].cwd), sid, TRANSCRIPT)


async def _continue(conv, binding, run_ref="r-1"):
    """One continuation; returns its announced task, not yet awaited past the turn."""
    n = len(conv.announced)
    await pc.run_continuation(pc.Pending(binding, run_ref, "sweep", 0.0, bot_id=BOT), on_start=lambda: None)
    return conv.announced[n][0]


async def test_a_continuation_resumes_its_bindings_conversation(conv):
    b = _binding()
    _kept(conv, b)
    await (await _continue(conv, b))
    (opts,) = conv.h.launches
    assert opts.resume == SID and opts.session_id is None
    assert opts.cwd == conv.roles["browser"].cwd
    await pc.asyncio.sleep(0)
    from memory import transcript_runs
    await transcript_runs.settle()
    assert [r["session_id"] for r in rows(conv.store)] == [SID]
    assert b.session_id == SID


async def test_the_first_turn_gives_the_binding_its_session(conv):
    """The binding minted during a delegated turn names that turn's session, through the
    real call record, whose origin keeps only plain values."""
    import operator_calls
    minted: list = []

    async def _ask():
        ident = GrantIdentity(operator_id=100, chat_id=42, enforcement_role="browser",
                              artifact_id="1" * 64, engagement_id="", bot_id=BOT)
        call = operator_calls.OperatorCalls().open(
            client_id="c", tool_use_id="t", tool_name="x", role="browser", plugin="browser",
            identity=ident, origin=agent_mod.origin_var.get())
        minted.append(operator_calls.binding_for(call))

    conv.h.on_receive = _ask
    tok = agent_mod.origin_var.set(dict(ORIGIN))
    try:
        await tools_mod._run_delegated_agent(conv.roles["browser"], "go", "")
    finally:
        agent_mod.origin_var.reset(tok)
    (opts,) = conv.h.launches
    assert opts.resume is None and opts.session_id
    assert minted[0].session_id == opts.session_id
    # and it is not an origin marker a continuation is rebuilt from
    assert "_delegated_session_id" not in minted[0].origin


async def test_relay_turns_stay_fresh(conv):
    b = _binding(("assistant", "finance", "browser"))
    _kept(conv, b)
    await (await _continue(conv, b))
    browser, finance = conv.h.launches
    assert browser.resume == SID and browser.cwd == conv.roles["browser"].cwd
    assert finance.resume is None and finance.session_id and finance.session_id != SID
    assert b.session_id == SID       # a relay's session is nobody's conversation


async def test_a_turn_nested_in_a_resumed_one_stays_fresh(conv):
    seen: list = []

    async def _nested():
        seen.append(tools_mod._conversation.get())
        conv.h.on_receive = None
        await tools_mod._run_delegated_agent(conv.roles["finance"], "nested", "")

    conv.h.on_receive = _nested
    b = _binding()
    _kept(conv, b)
    await (await _continue(conv, b))
    assert seen == [None]
    outer, inner = conv.h.launches
    assert outer.resume == SID
    assert inner.resume is None and inner.session_id and inner.session_id != SID
    assert b.session_id == SID


async def test_a_missing_transcript_starts_fresh_and_the_binding_adopts_it(conv):
    b = _binding()
    plugin_bindings.BINDINGS.adopt(b.ref, SID)      # kept, but no transcript on disk
    await (await _continue(conv, b))
    (opts,) = conv.h.launches
    assert opts.resume is None and opts.session_id and opts.session_id != SID
    assert b.session_id == opts.session_id
    # the transcript it wrote is what the next one resumes
    await (await _continue(conv, b))
    assert conv.h.launches[1].resume == opts.session_id and conv.h.launches[1].session_id is None


async def test_a_binding_with_no_session_starts_fresh_and_adopts_it(conv):
    b = _binding()
    assert b.session_id is None
    await (await _continue(conv, b))
    (opts,) = conv.h.launches
    assert opts.resume is None and b.session_id == opts.session_id


async def test_two_raised_resumed_turns_mean_the_next_is_fresh(conv):
    b = _binding()
    _kept(conv, b)
    h = conv.h
    h.mode = "raise"
    await (await _continue(conv, b))
    assert h.launches[-1].resume == SID and b.resume_failures == 1
    h.mode = "return"                       # a success between them resets the count
    await (await _continue(conv, b))
    assert h.launches[-1].resume == SID and b.resume_failures == 0
    h.mode = "raise"
    await (await _continue(conv, b))
    await (await _continue(conv, b))
    assert [o.resume for o in h.launches[-2:]] == [SID, SID] and b.resume_failures == 2
    h.mode = "return"
    await (await _continue(conv, b))
    fresh = h.launches[-1]
    assert fresh.resume is None and fresh.session_id != SID
    assert b.session_id == fresh.session_id and b.resume_failures == 0


async def test_a_raised_resumed_turn_is_quiet_and_the_sweep_goes_on(conv):
    b = _binding()
    _kept(conv, b)
    conv.h.mode = "raise"
    out = await (await _continue(conv, b))
    assert out.text == SILENCE_SENTINEL and conv.reg.quiet[0](out) is True
    assert not out.run_aborted
    # nothing is left pending: the sweep's next pass queues it
    q = pc.Continuations()
    assert q.request(b, "r-1", "sweep") == "queued"
    await q.drain()


async def test_a_raised_fresh_turn_is_announced_as_always(conv):
    b = _binding()
    conv.h.mode = "raise"
    task = await _continue(conv, b)
    with pytest.raises(ApiErrorTurn):
        await task
    assert b.resume_failures == 0


async def test_a_cancelled_resumed_turn_is_cancelled_not_quiet(conv):
    """A Casa stop is no failed resume: the count stands and nothing reads as progress."""
    b = _binding()
    _kept(conv, b)
    conv.h.mode = "cancel"
    run = asyncio.ensure_future(_continue(conv, b))
    await conv.h.entered_receive.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert b.resume_failures == 0 and conv.announced[0][0].cancelled()


def test_a_binding_keeps_its_conversation_across_a_restart(tmp_path, monkeypatch):
    path = tmp_path / "b.json"
    store = plugin_bindings.BindingStore(str(path))
    ident = GrantIdentity(operator_id=100, chat_id=42, enforcement_role="browser",
                          artifact_id="1" * 64, engagement_id="", bot_id=BOT)
    b = store.for_turn(plugin="browser", turn_key="d-1", identity=ident,
                       chain=("assistant", "browser"), origin={"_delegated_session_id": SID})
    assert b.session_id == SID
    store.turn_ended(b.ref, raised=True)
    assert store.note_cost(SID, 2.0) == 2.0
    again = plugin_bindings.BindingStore(str(path)).peek(b.ref)
    assert (again.session_id, again.resume_failures, again.cost_seen) == (SID, 1, 2.0)
    # a row written before these fields loads
    rows_ = json.loads(path.read_text())
    for field in ("session_id", "resume_failures", "cost_seen"):
        del rows_[0][field]
    path.write_text(json.dumps(rows_))
    old = plugin_bindings.BindingStore(str(path)).peek(b.ref)
    assert (old.session_id, old.resume_failures, old.cost_seen) == (None, 0, 0.0)


async def test_cost_counts_each_resumed_turn_once(conv, monkeypatch):
    recorded: list = []
    monkeypatch.setattr(tools_mod, "_specialist_telemetry", SimpleNamespace(
        record_cost=lambda role, cost_usd, usage: recorded.append(cost_usd)), raising=False)
    conv.h.costs = [2.0, 3.5]
    b = _binding()
    _kept(conv, b)
    await (await _continue(conv, b))
    await (await _continue(conv, b))
    assert recorded == [2.0, 1.5]
    # a session no binding holds reports its whole value
    conv.h.costs = [0.7]
    tok = agent_mod.origin_var.set(dict(ORIGIN))
    try:
        await tools_mod._run_delegated_agent(conv.roles["finance"], "go", "")
    finally:
        agent_mod.origin_var.reset(tok)
    assert recorded[-1] == 0.7


async def test_a_fresh_conversation_counts_its_cost_from_zero(conv, monkeypatch):
    recorded: list = []
    monkeypatch.setattr(tools_mod, "_specialist_telemetry", SimpleNamespace(
        record_cost=lambda role, cost_usd, usage: recorded.append(cost_usd)), raising=False)
    b = _binding()
    plugin_bindings.BINDINGS.adopt(b.ref, SID)
    assert plugin_bindings.BINDINGS.note_cost(SID, 9.0) == 9.0
    conv.h.costs = [0.5]
    await (await _continue(conv, b))        # no transcript: a new session, a new total
    assert recorded == [0.5]


async def test_the_compaction_window_is_set_only_for_a_long_runs_role(conv):
    from config import SessionConfig
    browser = conv.roles["browser"]
    browser.session = SessionConfig(strategy="ephemeral", idle_timeout=0, long_runs=True)
    tok = agent_mod.origin_var.set(dict(ORIGIN))
    try:
        await tools_mod._run_delegated_agent(browser, "go", "")
        await tools_mod._run_delegated_agent(conv.roles["finance"], "go", "")
    finally:
        agent_mod.origin_var.reset(tok)
    long_one, ordinary = conv.h.launches
    assert long_one.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in ordinary.env
