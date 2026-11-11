"""The Stop: a browsing request stopped at once, by a mechanical tap or by the resident's own
tool, with no model turn and no desk (``plugin_stop``). Every test runs the plugin's stop tool
as a real stdio server that records the calls it receives (the driver sweep's pattern), and
asserts counts."""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import agent as agent_mod
import plugin_bindings
import plugin_continuations as pc
import plugin_stop
import plugin_store
import result_broker as rb
import run_admission
import specialist_desk as sd
import tools as tools_mod
import verdict_broker as vb
from authz_grants import GrantIdentity
from bus import MessageBus
from channels.tg_address import TgAddress
from channels.telegram import TelegramChannel, _parse_callback_data
from config import AgentConfig, CharacterConfig, MemoryConfig
from plugin_registry import ResolutionResult, ResolvedPlugin
from telegram_channel_double import make_transport
from two_endpoints import CASA_BOT

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

OPERATOR = 42
BOT = CASA_BOT
ARTIFACT = "1" * 64
CID = "conf-" + "c" * 32
CARD_MID = 777
CARD_TEXT = "📊 Fido\nStart this browsing run?\n• RunPod — invoice\nAsked for by Ellen.\n\n☑ Start"
ORIGIN = {"role": "assistant", "channel": "telegram", "chat_id": OPERATOR,
          "user_id": OPERATOR, "user_name": "N", "_origin_route": "telegram",
          "_origin_clearance": "private", "user_text": "get my invoice", "_operator_turn": True,
          "_tg_bot": BOT}

STOP_SERVER = r'''
import json, os, sys, time
calls = os.environ["CALLS_OUT"]
for line in sys.stdin:
    msg = json.loads(line)
    mid = msg.get("id")
    if msg.get("method") == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "browser", "version": "0"}}
    elif msg.get("method") == "tools/list":
        res = {"tools": [{"name": "stop_runs", "inputSchema": {"type": "object"}}]}
    elif msg.get("method") == "tools/call":
        args = msg["params"].get("arguments") or {}
        with open(calls, "a") as fh:
            fh.write(json.dumps({"tool": msg["params"]["name"], "args": args,
                                 "at": time.time()}) + "\n")
        time.sleep(float(os.environ.get("DELAY", "0")))
        if "ref" in args:
            answer = os.environ["ANSWER"]
        else:
            answer = os.environ["LISTING"]
        res = {"content": [{"type": "text", "text": answer}]}
    elif mid is not None:
        res = {}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
time.sleep(float(os.environ.get("EXIT_DELAY", "0")))
'''

STOPPED = {"outcome": "stopped", "tasks": ["invoice: get the RunPod invoice"],
           "refs": ["request:k1", "run_abc", "intent:r1"]}


def _cfg(role, name, kind="specialist"):
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role, model="x",
                       character=CharacterConfig(name=name), system_prompt="x",
                       memory=MemoryConfig(token_budget=0), kind=kind)


class _FakeBot:
    def __init__(self):
        self.sent, self.edited, self.markups = [], [], []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=900 + len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return True

    async def edit_message_reply_markup(self, **kwargs):
        self.markups.append(kwargs)
        return True


async def _noop(*a, **k):
    return None


def _channel():
    bus = MessageBus()
    bus.register("assistant", _noop)
    ch = TelegramChannel(bot_token="T", chat_id=str(OPERATOR), default_agent="assistant", bus=bus,
                         is_casa=True, known_bot_id=BOT)
    ch._start_typing = lambda *a, **k: None
    ch._app = types.SimpleNamespace(bot=_FakeBot())
    ch._session_registry = None
    return ch


def _plugin(root: Path, *, answer=None, listing=None, delay=0.0, exit_delay=0.0,
            name="browser", artifact=ARTIFACT, declare=True):
    root.mkdir(parents=True, exist_ok=True)
    (root / "srv.py").write_text(STOP_SERVER)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"browser": {
        "command": sys.executable, "args": ["${CLAUDE_PLUGIN_ROOT}/srv.py"],
        "env": {"CALLS_OUT": str(root / "calls.jsonl"),
                "ANSWER": json.dumps(answer if answer is not None else STOPPED),
                "LISTING": (listing if isinstance(listing, str)
                            else json.dumps(listing if listing is not None else {"requests": []})),
                "DELAY": str(delay), "EXIT_DELAY": str(exit_delay)}}}}))
    casa = {"resultContract": {"version": 1, "tools": {"stop_runs": {"result": "safe"}}}}
    if declare:
        casa["stopTool"] = "stop_runs"
    return ResolvedPlugin(name=name, artifact_id=artifact, path=str(root), version="0.1.0",
                          manifest={"name": name, "casa": casa})


def _calls(rp) -> list[dict]:
    path = Path(rp.path) / "calls.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def env(monkeypatch, tmp_path):
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (OPERATOR, OPERATOR))
    roles = {"assistant": _cfg("assistant", "Ellen", "resident"), "browser": _cfg("browser", "Fido")}
    monkeypatch.setattr(tools_mod, "_agent_role_map", roles, raising=False)
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "b.json")))
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    monkeypatch.setattr(sd, "DESKS", sd.DeskRegistry())
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))
    monkeypatch.setattr(pc, "CONTINUATIONS", pc.Continuations())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    plugins: dict[str, list] = {"browser": []}
    monkeypatch.setattr(tools_mod, "_delegated_resolution", lambda cfg: ResolutionResult(
        registry_valid=True, plugins=list(plugins.get(cfg.role, []))))
    ch = _channel()
    monkeypatch.setattr(tools_mod, "_channel_manager", make_transport(ch), raising=False)
    desk_uses: list = []
    real_get = sd.DESKS.get_or_create

    def _get_or_create(*key):
        desk_uses.append(key[-2:])          # (chat, role); the bot is asserted elsewhere
        return real_get(*key)
    monkeypatch.setattr(sd.DESKS, "get_or_create", _get_or_create)
    turns: list = []

    async def _turn(*a, **k):
        turns.append(a)
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _turn)
    taps: list = []

    async def _handle_tap(**kwargs):
        taps.append(kwargs)
        if kwargs.get("reservation") is not None:
            kwargs["reservation"].release()
    monkeypatch.setattr(sd, "handle_tap", _handle_tap, raising=False)

    def use(rp, role="browser"):
        plugins.setdefault(role, []).append(rp)
        return rp

    def admit(*, status="admitted", card=True, request_key="k1", cid=CID, plugin="browser",
              operator=OPERATOR):
        ident = GrantIdentity(operator_id=operator, chat_id=OPERATOR, enforcement_role="browser",
                              artifact_id=ARTIFACT, engagement_id="", bot_id=BOT,
                          delivery=TgAddress(BOT, OPERATOR))
        b = plugin_bindings.BINDINGS.for_turn(plugin=plugin, turn_key="d-" + cid, identity=ident,
                                              chain=("assistant", "browser"), origin=ORIGIN)
        c = run_admission.Confirmation(
            cid=cid, plugin=plugin, bot_id=BOT, operator_id=operator, binding_ref=b.ref,
            request_key=request_key, status=status, admissions=[], created_at=time.time(),
            card=[BOT, OPERATOR, CARD_MID] if card else None,
            card_text=CARD_TEXT if card else "")
        run_admission.CONFIRMATIONS.add(c)
        return c, b

    return types.SimpleNamespace(ch=ch, bot=ch._app.bot, use=use, admit=admit, tmp=tmp_path,
                                 desk_uses=desk_uses, turns=turns, taps=taps, plugins=plugins)


def _cq(*, cid=CID, chat_id=OPERATOR, user_id=OPERATOR, message_id=CARD_MID):
    cq = types.SimpleNamespace(
        id="cq1", data=f"v1|run_stop|{cid}|0",
        message=types.SimpleNamespace(message_id=message_id, chat=types.SimpleNamespace(id=chat_id),
                                      message_thread_id=None),
        answer=AsyncMock(return_value=None), from_user=types.SimpleNamespace(id=user_id))
    return types.SimpleNamespace(callback_query=cq)


async def _tap(env, update=None):
    update = update or _cq()
    await env.ch._on_inline_callback(update, None)
    return update.callback_query


async def _quiet(env, seconds=8.0):
    """Wait for every tracked tap task (the stop, its line) to end."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        busy = [t for t in env.ch._turn_tasks if not t.done()]
        if not busy:
            return
        await asyncio.wait(busy, timeout=0.1)
    raise AssertionError("the stop's task did not end")


def _lines(env) -> list[dict]:
    """The reply lines sent under a card (the status card and any other send excluded)."""
    return [m for m in env.bot.sent if "reply_to_message_id" in m]


def _card_edits(env) -> list[dict]:
    return [e for e in env.bot.edited if e.get("message_id") == CARD_MID]


def _buttons(edit) -> list:
    return [b for row in edit["reply_markup"].inline_keyboard for b in row]


# -- 1. the declaration -----------------------------------------------------------------

def _manifest(stop, tools):
    return {"casa": {"stopTool": stop, "resultContract": {"version": 1, "tools": tools}}}


def test_an_absent_stop_tool_declares_nothing():
    assert plugin_store.manifest_stop_tool({"casa": {}}) is None
    assert plugin_store.manifest_stop_tool({}) is None


def test_a_declared_stop_tool_is_its_name():
    assert plugin_store.manifest_stop_tool(
        _manifest("stop_runs", {"stop_runs": {"result": "safe"}})) == "stop_runs"


@pytest.mark.parametrize("bad", [
    _manifest("missing", {"stop_runs": {"result": "safe"}}),
    _manifest(7, {"stop_runs": {"result": "safe"}}),
    _manifest("stop_runs", {"stop_runs": {"result": "safe", "callsCasa": True}}),
])
def test_an_undeclared_or_calling_stop_tool_is_refused(bad):
    with pytest.raises(plugin_store.StoreError) as err:
        plugin_store.manifest_stop_tool(bad)
    assert err.value.reason_code == "stop_tool_invalid"


# -- 2. the direct call -------------------------------------------------------------------

async def test_the_direct_call_passes_the_arguments_byte_for_byte(env):
    rp = env.use(_plugin(env.tmp / "p"))
    out = await plugin_stop.call_stop(rp, "stop_runs", {"ref": "request:k1"})
    assert [c["args"] for c in _calls(rp)] == [{"ref": "request:k1"}]
    assert [c["tool"] for c in _calls(rp)] == ["stop_runs"]
    assert out["outcome"] == "stopped"


async def test_the_direct_call_parses_the_answer(env):
    rp = env.use(_plugin(env.tmp / "p", listing={"requests": [
        {"ref": "request:k1", "tasks": ["invoice: RunPod"], "state": "running"}, "junk"]}))
    assert await plugin_stop.call_stop(rp, "stop_runs", {"ref": "request:k1"}) == STOPPED
    assert await plugin_stop.call_stop(rp, "stop_runs", {}) == {"requests": [
        {"ref": "request:k1", "tasks": ["invoice: RunPod"], "state": "running"}]}
    odd = env.use(_plugin(env.tmp / "q", answer={"outcome": "maybe"}))
    assert await plugin_stop.call_stop(odd, "stop_runs", {"ref": "request:k1"}) == {
        "outcome": "not_taken"}


async def test_the_direct_call_times_out_to_not_taken(env, monkeypatch):
    monkeypatch.setattr(plugin_stop, "STOP_CALL_TIMEOUT_S", 0.5)
    rp = env.use(_plugin(env.tmp / "p", delay=5))
    t0 = time.monotonic()
    assert await plugin_stop.call_stop(rp, "stop_runs", {"ref": "request:k1"}) == {
        "outcome": "not_taken"}
    assert time.monotonic() - t0 < 3
    await plugin_stop.drain()


async def test_the_line_is_sent_before_the_servers_teardown_ends(env):
    """The answer is handed over from inside the client session: the tap's line goes out while
    the plugin process is still exiting (it lingers 3 s after its stdin closes)."""
    rp = env.use(_plugin(env.tmp / "p", exit_delay=3))
    env.admit()
    t0 = time.monotonic()
    await _tap(env)
    while not _lines(env) and time.monotonic() - t0 < 10:
        await asyncio.sleep(0.05)
    assert len(_lines(env)) == 1 and time.monotonic() - t0 < 2.5
    assert len(plugin_stop._TASKS) == 1           # its stdio teardown still running, tracked
    await plugin_stop.drain()
    await _quiet(env)


# -- 5–9. the Stop tap ---------------------------------------------------------------------

def test_the_parser_accepts_the_run_stop_namespace():
    data = f"v1|run_stop|{CID}|0"
    assert len(data.encode()) <= 64
    assert _parse_callback_data(data) == ("run_stop", CID, 0, None)


async def test_the_operators_stop_tap_stops_it_directly_and_tells_him_once(env):
    rp = env.use(_plugin(env.tmp / "p"))
    env.admit()
    cq = await _tap(env)
    await _quiet(env)
    cq.answer.assert_awaited_once_with("stopping…")
    assert [c["args"] for c in _calls(rp)] == [{"ref": "request:k1"}]
    assert env.desk_uses == [] and env.turns == [] and env.taps == []
    (edit,) = _card_edits(env)
    assert edit["text"].endswith("☑ Stopped") and _buttons(edit) == []
    (line,) = _lines(env)
    assert line["reply_to_message_id"] == CARD_MID and line["text"].endswith("\nStopped.")


@pytest.mark.parametrize("case", ["sender", "chat", "unknown", "declined", "not_operator"])
async def test_a_stop_tap_that_is_not_his_calls_nothing(env, monkeypatch, case):
    rp = env.use(_plugin(env.tmp / "p"))
    env.admit(status="declined" if case == "declined" else "admitted")
    update = _cq(user_id=7 if case == "sender" else OPERATOR,
                 chat_id=8 if case == "chat" else OPERATOR,
                 cid=("conf-" + "d" * 32) if case == "unknown" else CID)
    if case == "not_operator":
        import authz_grants
        monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (OPERATOR, 99))
    cq = await _tap(env, update)
    await _quiet(env)
    assert _calls(rp) == [] and env.bot.sent == [] and env.bot.edited == []
    cq.answer.assert_awaited_once()
    assert cq.answer.await_args.args[0] in ("expired", "not for you")


async def test_two_taps_at_once_stop_once_and_tell_once(env):
    rp = env.use(_plugin(env.tmp / "p", delay=0.3))
    env.admit()
    await _tap(env)
    await asyncio.sleep(0.01)
    await _tap(env)
    await _quiet(env)
    assert len(_calls(rp)) == 1 and len(_lines(env)) == 1 and len(_card_edits(env)) == 1


async def test_the_flight_is_held_until_the_card_and_line_are_done(env):
    """§10: the path that got the outcome holds the request's flight through its card mark and
    its line, so a tap arriving while that telling is still going out stops nothing again and
    tells nothing."""
    rp = env.use(_plugin(env.tmp / "p"))
    env.admit()
    gate = asyncio.Event()
    real_edit = env.bot.edit_message_text

    async def slow_edit(**kwargs):
        await gate.wait()
        return await real_edit(**kwargs)
    env.bot.edit_message_text = slow_edit
    await _tap(env)
    for _ in range(200):
        if _calls(rp):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)               # the answer is in; the card mark is held
    await _tap(env)
    await asyncio.sleep(0.3)
    gate.set()
    await _quiet(env)
    assert len(_calls(rp)) == 1 and len(_lines(env)) == 1 and len(_card_edits(env)) == 1


async def test_a_later_tap_on_a_stopped_request_marks_the_card_and_says_nothing(env):
    rp = env.use(_plugin(env.tmp / "p", answer={"outcome": "already_stopped"}))
    env.admit()
    await _tap(env)
    await _quiet(env)
    assert len(_calls(rp)) == 1 and _lines(env) == []
    (edit,) = _card_edits(env)
    assert edit["text"].endswith("☑ Stopped")


async def test_a_tap_and_ellen_at_once_tell_him_once(env):
    """§10: two Stop paths at once — the one holding the request's flight gets ``stopped`` and
    tells him; the other answers ``in_flight`` and tells nothing (Ellen is told to add
    nothing)."""
    rp = env.use(_plugin(env.tmp / "p", delay=0.3))
    env.admit()
    await _tap(env)
    await asyncio.sleep(0.05)
    typed = await _stop_work(env, {"ref": "request:k1"})
    await _quiet(env)
    assert len(_calls(rp)) == 1 and len(_lines(env)) == 1 and len(_card_edits(env)) == 1
    assert "already stopped and the person has been told" in typed


async def test_the_start_tap_ordering_is_admitted_before_the_stop_shows(env, monkeypatch):
    """7b: his Start settles the Confirmation — admitted, saved with its card's message id —
    BEFORE the edit that shows the Stop button."""
    order: list = []
    ch = types.SimpleNamespace(default_agent="assistant", bot_id=BOT, has_bot_id=True)

    async def deliver_operator_question(chat_id, text, labels, rid, *, post=None):
        return CARD_MID

    async def edit_dm_message(chat_id, message_id, text, buttons=None):
        c = run_admission.CONFIRMATIONS.peek(cid_box[0])
        order.append(("edit", c.status, c.card, [d for _, d in (buttons or [])]))
        return True
    ch.deliver_operator_question, ch.edit_dm_message = deliver_operator_question, edit_dm_message
    monkeypatch.setattr(tools_mod, "_channel_manager", make_transport(ch), raising=False)
    cid_box = await _pending_card(env, monkeypatch)
    await _answer_card(0)
    assert order == [("edit", "admitted", [BOT, OPERATOR, CARD_MID],
                      [f"v1|run_stop|{cid_box[0]}|0"])]
    c = run_admission.CONFIRMATIONS.peek(cid_box[0])
    reread = run_admission.Confirmations(str(env.tmp / "admissions.json")).peek(cid_box[0])
    assert reread.card_address() == TgAddress(BOT, OPERATOR, CARD_MID)
    assert reread.status == "admitted" == c.status


@pytest.mark.parametrize("outcome, mark, line", [
    ("stopped", "☑ Stopped", "Stopped."),
    ("ended", "☑ Ended", "It had already ended."),
    ("not_taken", "☑ Stop asked",
     "Casa Browser isn't answering; the browsing stops as soon as it does."),
    ("unknown", None, None),
    ("unavailable", None, "I couldn't stop it: Fido's browser isn't set up any more."),
])
async def test_each_outcome_has_its_mark_and_line(env, outcome, mark, line):
    if outcome != "unavailable":            # unavailable: no plugin resolved for the role
        env.use(_plugin(env.tmp / "p", answer={"outcome": outcome}))
    env.admit()
    await _tap(env)
    await _quiet(env)
    edits = _card_edits(env)
    if mark is None:
        assert edits == []                  # the card keeps its Stop
    else:
        assert len(edits) == 1 and edits[0]["text"] == f"{CARD_TEXT}\n{mark}"
    lines = _lines(env)
    if line is None:
        assert lines == []
    else:
        assert [m["text"] for m in lines] == [f"📊 Fido\n{line}"]


async def test_a_stop_tap_after_a_restart_still_stops(env, monkeypatch):
    rp = env.use(_plugin(env.tmp / "p"))
    env.admit()
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(env.tmp / "admissions.json")))
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(env.tmp / "b.json")))
    env.ch = _channel()
    env.bot = env.ch._app.bot
    monkeypatch.setattr(tools_mod, "_channel_manager", make_transport(env.ch), raising=False)
    await _tap(env)
    await _quiet(env)
    assert len(_calls(rp)) == 1 and len(_lines(env)) == 1


# -- 10. no desk ---------------------------------------------------------------------------

@pytest.fixture
async def held_desk(env):
    desk = sd.DESKS.get_or_create(BOT, OPERATOR, "browser")
    env.desk_uses.clear()
    reservation = desk.reserve()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def _hold():
        async with desk.use(reservation):
            entered.set()
            await release.wait()
    task = asyncio.create_task(_hold())
    await entered.wait()
    yield types.SimpleNamespace(desk=desk, release=release.set)
    release.set()
    await task


async def test_the_stop_tap_does_not_wait_for_the_running_desk_use(env, held_desk):
    rp = env.use(_plugin(env.tmp / "p"))
    env.admit()
    t0 = time.time()
    await _tap(env)
    await _quiet(env, seconds=5)
    (call,) = _calls(rp)
    assert call["at"] - t0 < 1.0 and env.desk_uses == []


async def test_stop_work_does_not_wait_for_the_running_desk_use(env, held_desk):
    rp = env.use(_plugin(env.tmp / "p"))
    env.admit()
    t0 = time.time()
    await asyncio.wait_for(_stop_work(env, {"ref": "request:k1"}), 5)
    (call,) = _calls(rp)
    assert call["at"] - t0 < 1.0 and env.desk_uses == []


# -- 11. the drop ---------------------------------------------------------------------------

def _binding():
    ident = GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR, enforcement_role="browser",
                          artifact_id=ARTIFACT, engagement_id="", bot_id=BOT,
                          delivery=TgAddress(BOT, OPERATOR))
    return plugin_bindings.BINDINGS.for_turn(plugin="browser", turn_key="d-x", identity=ident,
                                             chain=("assistant", "browser"), origin=ORIGIN)


@pytest.fixture
def held_queue(env, monkeypatch):
    """Continuations waiting for Fido's held desk; each turn that starts is recorded."""
    monkeypatch.setattr(pc, "RETRY_EVERY_S", 0.01)
    monkeypatch.setattr(tools_mod, "_specialist_limiter", None, raising=False)
    started: list = []

    async def _turn(cfg, task, origin, *, quota_key):
        started.append(task)
        return types.SimpleNamespace(run_aborted=False, text="<silent/>")
    monkeypatch.setattr(pc, "_run_turn", _turn)

    class _Reg:
        async def register_delegation(self, record):
            pass
    monkeypatch.setattr(tools_mod, "_specialist_registry", _Reg(), raising=False)
    monkeypatch.setattr(tools_mod, "_attach_completion_callback", lambda *a, **k: None)
    return started


async def test_a_dropped_continuation_runs_no_turn(env, held_desk, held_queue):
    b = _binding()
    assert pc.CONTINUATIONS.request(b, "run_abc", "sweep") == "queued"
    await asyncio.sleep(0.05)
    assert pc.CONTINUATIONS.drop("browser", BOT, OPERATOR, ["run_abc"]) == 1
    held_desk.release()
    await _drain_queue()
    assert held_queue == []


async def test_a_stopped_requests_queued_continuations_are_dropped(env, held_desk, held_queue):
    """A Stop that answers ``stopped`` drops every continuation still queued for the refs it
    names, and the start card's own ``admission:<cid>``: none of them runs a turn."""
    env.use(_plugin(env.tmp / "p"))
    env.admit()
    b = _binding()
    for ref in ("run_abc", "intent:r1", f"admission:{CID}"):
        pc.CONTINUATIONS.request(b, ref, "sweep")
    await asyncio.sleep(0.05)
    await _tap(env)
    await _quiet(env)
    held_desk.release()
    await _drain_queue()
    assert held_queue == []


@pytest.mark.parametrize("outcome", ["not_taken", "already_stopped"])
async def test_a_stop_that_keeps_the_seal_drops_the_queued_continuations(
        env, held_desk, held_queue, outcome):
    """Astra r1 (diff): a Stop that did not reach the app yet (the plugin retries it) or found it
    already stopped still keeps the plugin from driving the request, so the continuations queued
    before it are dropped too: none starts a turn."""
    env.use(_plugin(env.tmp / "p", answer={"outcome": outcome, "tasks": [],
                                           "refs": ["request:k1", "run_abc", "intent:r1"]}))
    env.admit()
    b = _binding()
    for ref in ("run_abc", "intent:r1", f"admission:{CID}"):
        pc.CONTINUATIONS.request(b, ref, "sweep")
    await asyncio.sleep(0.05)
    await _tap(env)
    await _quiet(env)
    held_desk.release()
    await _drain_queue()
    assert held_queue == []


async def test_a_continuation_for_another_ref_still_runs(env, held_desk, held_queue):
    b = _binding()
    pc.CONTINUATIONS.request(b, "run_abc", "sweep")
    pc.CONTINUATIONS.request(b, "run_other", "sweep")
    await asyncio.sleep(0.05)
    assert pc.CONTINUATIONS.drop("browser", BOT, OPERATOR, ["run_abc"]) == 1
    held_desk.release()
    await _drain_queue()
    assert held_queue == ["Continue browsing run run_other."]


async def test_a_started_continuation_is_not_dropped(env, held_queue, monkeypatch):
    b = _binding()
    gate = asyncio.Event()

    async def _turn(cfg, task, origin, *, quota_key):
        held_queue.append(task)
        await gate.wait()
        return types.SimpleNamespace(run_aborted=False, text="<silent/>")
    monkeypatch.setattr(pc, "_run_turn", _turn)
    pc.CONTINUATIONS.request(b, "run_abc", "sweep")
    for _ in range(100):
        if held_queue:
            break
        await asyncio.sleep(0.01)
    assert pc.CONTINUATIONS.drop("browser", BOT, OPERATOR, ["run_abc"]) == 0
    gate.set()
    await _drain_queue()
    assert held_queue == ["Continue browsing run run_abc."]


async def _drain_queue():
    for _ in range(300):
        if not pc.CONTINUATIONS._tasks:
            return
        await asyncio.sleep(0.01)
    await pc.CONTINUATIONS.drain()


# -- 12. the 20-minute card ------------------------------------------------------------------

def _proposal_meta(wire_name="stop_runs", arguments=None):
    return {"deadline": 10_000.0, "chat_id": OPERATOR, "bot_id": BOT, "operator_id": OPERATOR,
            "role": "browser",
            "artifact_id": ARTIFACT, "plugin_seg": "browser", "label": "📊 Fido",
            "text": "📊 Fido\nStill going.", "options": ["Close", "Stop"], "revision": "",
            "message_id": 601, "owner": "d-1", "_scope": f"proposal:{BOT}:{OPERATOR}",
            "kinds": ["close", "call"],
            "calls": [None, {"server": "browser", "wire_name": wire_name,
                             "runtime_name": f"mcp__plugin_browser_browser__{wire_name}",
                             "proposal": False,
                             "arguments": arguments or {"ref": "request:k1"},
                             "canonical": json.dumps(arguments or {"ref": "request:k1"})}]}


async def test_the_twenty_minute_cards_stop_is_called_directly(env):
    rp = env.use(_plugin(env.tmp / "p"))
    env.admit()
    assert await env.ch._dispatch_proposal_tap(meta=_proposal_meta(), idx=1, request_id="r" * 32)
    await _quiet(env)
    assert [c["args"] for c in _calls(rp)] == [{"ref": "request:k1"}]
    assert env.desk_uses == [] and env.taps == [] and env.turns == []
    marks = [e for e in env.bot.edited if e.get("message_id") == 601]
    assert [e["text"] for e in marks] == ["📊 Fido\nStill going.\n☑ Stopped"]
    assert len(_card_edits(env)) == 1               # the start card is marked too
    (line,) = _lines(env)
    assert line["reply_to_message_id"] == 601


async def test_the_twenty_minute_cards_stop_starts_before_any_card_edit(env):
    """Astra e1: the finish hook calls the declared stop tool before it edits the card, so a
    slow or flood-limited Telegram edit never keeps the run going; no ⏳ line is written, the
    stop's outcome mark is the card's one edit."""
    rp = env.use(_plugin(env.tmp / "p"))
    env.admit()
    gate = asyncio.Event()
    real_edit = env.bot.edit_message_text

    async def slow_edit(**kwargs):
        await gate.wait()
        return await real_edit(**kwargs)
    env.bot.edit_message_text = slow_edit
    meta = _proposal_meta()
    finish = env.ch.proposal_finish_hook(rid="r" * 32, req=types.SimpleNamespace(meta=meta))
    hook = asyncio.ensure_future(finish({"outcome": "answered", "option_index": 1}))
    for _ in range(200):
        if _calls(rp):
            break
        await asyncio.sleep(0.05)
    assert [c["args"] for c in _calls(rp)] == [{"ref": "request:k1"}]      # edits still held
    gate.set()
    await hook
    await _quiet(env)
    marks = [e["text"] for e in env.bot.edited if e.get("message_id") == 601]
    assert marks == ["📊 Fido\nStill going.\n☑ Stopped"]                   # no ⏳ line
    assert env.desk_uses == [] and env.taps == []


@pytest.mark.parametrize("wire_name, arguments", [
    ("apply_tap", {"t": "tok"}),
    ("continue_run", {"ref": "request:k1"}),       # a ref-shaped call of another tool
])
async def test_a_stored_call_of_another_tool_still_goes_to_the_desk(env, wire_name, arguments):
    rp = env.use(_plugin(env.tmp / "p"))
    meta = _proposal_meta(wire_name=wire_name, arguments=arguments)
    assert await env.ch._dispatch_proposal_tap(meta=meta, idx=1, request_id="r" * 32)
    await _quiet(env)
    assert _calls(rp) == [] and len(env.taps) == 1 and env.desk_uses == [(OPERATOR, "browser")]


# -- 13. Ellen's stop_work ---------------------------------------------------------------------

async def _stop_work(env, args, origin=None):
    tok = agent_mod.origin_var.set(dict(origin if origin is not None else ORIGIN))
    try:
        out = await tools_mod.stop_work.handler(args)
    finally:
        agent_mod.origin_var.reset(tok)
    return out["content"][0]["text"]


@pytest.mark.parametrize("origin", [
    {k: v for k, v in ORIGIN.items() if k != "_operator_turn"},       # not his live turn
    {**ORIGIN, "delegation_depth": 1},                                  # a delegated turn
    "engagement",
])
async def test_stop_work_is_refused_off_his_own_live_turn(env, monkeypatch, origin):
    rp = env.use(_plugin(env.tmp / "p"))
    if origin == "engagement":
        token = tools_mod.engagement_var.set(types.SimpleNamespace(kind="executor", origin={}))
        try:
            text = await _stop_work(env, {"ref": "request:k1"})
        finally:
            tools_mod.engagement_var.reset(token)
    else:
        text = await _stop_work(env, {"ref": "request:k1"}, origin)
    assert "Only the person's own message can stop work." in text and _calls(rp) == []


async def test_stop_work_lists_every_stop_plugins_requests(env):
    env.use(_plugin(env.tmp / "p", listing={"requests": [
        {"ref": "request:k1", "tasks": ["invoice: RunPod"], "state": "running"}]}))
    env.plugins["finance"] = [_plugin(env.tmp / "q", name="ledger", artifact="2" * 64, listing={
        "requests": [{"ref": "request:k2", "tasks": ["statement: bank"], "state": "starting"}]})]
    tools_mod._agent_role_map["finance"] = _cfg("finance", "Alex")
    env.admit()
    env.admit(plugin="ledger", request_key="k2", cid="conf-" + "e" * 32)
    text = await _stop_work(env, {})
    assert text.splitlines() == ["- request:k1: Fido — invoice: RunPod (running)",
                                 "- request:k2: Alex — statement: bank (starting)"]


async def test_a_listing_the_plugin_could_not_make_is_said_so(env):
    env.use(_plugin(env.tmp / "p", listing="Casa Browser is not reachable (unreachable)"))
    assert await _stop_work(env, {}) == "- Fido: what it is doing could not be listed right now."


async def test_stop_work_by_a_listed_ref_stops_it_and_marks_the_card(env):
    rp = env.use(_plugin(env.tmp / "p"))
    env.admit()
    text = await _stop_work(env, {"ref": "request:k1"})
    assert [c["args"] for c in _calls(rp)] == [{"ref": "request:k1"}]
    assert text == ("Stopped: invoice: get the RunPod invoice. Tell the person in one plain line "
                    "what stopped, in his words, and nothing about how you found it.")
    (edit,) = _card_edits(env)
    assert edit["text"].endswith("☑ Stopped") and _lines(env) == []      # her reply is the line


async def test_stop_work_neither_lists_nor_stops_another_operators_request(env):
    """Astra + Terra r1 (diff): after the configured operator changes, a request admitted for
    the earlier operator is not his — not listed, not stopped, no plugin call."""
    rp = env.use(_plugin(env.tmp / "p", listing={"requests": [
        {"ref": "request:k1", "tasks": ["invoice: RunPod"], "state": "running"}]}))
    env.admit(operator=7)
    assert await _stop_work(env, {}) == "Nothing is running."
    text = await _stop_work(env, {"ref": "request:k1"})
    assert text.startswith("Error: nothing running has that ref")
    assert [c["args"] for c in _calls(rp)] == [{}]          # the listing only, no stop


def _admit_on(bot, *, chain, request_key, cid):
    """A request admitted on *bot* (an agent's own chat), for work *chain*'s
    root asked for. No card, so a stop marks nothing."""
    ident = GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR, enforcement_role="browser",
                          artifact_id=ARTIFACT, engagement_id="", bot_id=bot,
                          delivery=TgAddress(bot, OPERATOR))
    b = plugin_bindings.BINDINGS.for_turn(plugin="browser", turn_key="d-" + cid, identity=ident,
                                          chain=chain, origin={**ORIGIN, "_tg_bot": bot})
    run_admission.CONFIRMATIONS.add(run_admission.Confirmation(
        cid=cid, plugin="browser", bot_id=bot, operator_id=OPERATOR, binding_ref=b.ref,
        request_key=request_key, status="admitted", admissions=[], created_at=time.time()))


async def test_stop_work_reaches_work_she_asked_for_in_another_agents_chat(env):
    """A delegation to an agent with its own chat runs there, on that agent's
    bot: Ellen still lists and stops it from her chat. Work the agent's own
    conversation started stays the agent's — she neither lists nor stops it."""
    rp = env.use(_plugin(env.tmp / "p", listing={"requests": [
        {"ref": "request:k1", "tasks": ["invoice: RunPod"], "state": "running"},
        {"ref": "request:k2", "tasks": ["receipt: Hetzner"], "state": "running"}]}))
    _admit_on(222, chain=("assistant", "browser"), request_key="k1", cid="conf-" + "1" * 32)
    _admit_on(222, chain=("browser",), request_key="k2", cid="conf-" + "2" * 32)
    assert await _stop_work(env, {}) == "- request:k1: Fido — invoice: RunPod (running)"
    text = await _stop_work(env, {"ref": "request:k2"})
    assert text.startswith("Error: nothing running has that ref")
    text = await _stop_work(env, {"ref": "request:k1"})
    assert text.startswith("Stopped: invoice: get the RunPod invoice.")
    assert [c["args"] for c in _calls(rp)] == [{}, {"ref": "request:k1"}]


async def test_stop_work_by_an_unknown_ref_says_so(env):
    env.use(_plugin(env.tmp / "p", answer={"outcome": "unknown"}))
    text = await _stop_work(env, {"ref": "request:nope"})
    assert text.startswith("Error: nothing running has that ref")


# -- helpers for the start card (run_admission) ------------------------------------------------

async def _pending_card(env, monkeypatch):
    import operator_calls
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    ident = GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR, enforcement_role="browser",
                          artifact_id=ARTIFACT, engagement_id="", bot_id=BOT,
                          delivery=TgAddress(BOT, OPERATOR))
    origin = {**ORIGIN, "execution_role": "browser", "_delegation_id": "d-1",
              "delegation_depth": 1, "_handoff_chain": ("assistant", "browser"),
              "synthetic": "continuation", "user_text": ""}
    call = operator_calls.CALLS.open(client_id="c1", tool_use_id="tu", tool_name="x",
                                     role="browser", plugin="browser", identity=ident,
                                     origin=origin)
    monkeypatch.setattr(pc, "CONTINUATIONS", types.SimpleNamespace(request=lambda *a: "queued"))
    run = {"spec_digest": "sha256:aa", "mode": "account", "task_kind": "invoice",
           "task_text": "t", "sites": [{"site_id": "runpod", "access": "account",
                                        "name": "RunPod"}]}
    out = await run_admission.request_admission(call, [run], "k1")
    assert out["status"] == "confirmation_pending"
    return [out["confirmation_id"]]


async def _answer_card(index):
    for _ in range(200):
        reqs = vb.BROKER.pending(namespace="resident_ask", scope=f"ask:{BOT}:{OPERATOR}")
        if reqs:
            break
        await asyncio.sleep(0)
    (rid,) = reqs
    claim = vb.BROKER.claim(namespace="resident_ask", scope=f"ask:{BOT}:{OPERATOR}",
                            request_id=getattr(rid, "request_id", rid), option_index=index,
                            actor_id=OPERATOR)
    assert not isinstance(claim, str), claim
    assert vb.BROKER.commit(claim)
    for _ in range(200):
        await asyncio.sleep(0)


# -- a request that ended on its own: its card loses its Stop (the driver sweep's ``ended``) ----

_REAL_CALL_PLUGIN_TOOL = pc.call_plugin_tool


def _sweeping(env, monkeypatch, ended, *, plugin_name="browser"):
    """A plugin declaring both the sweep and the stop; its sweep answers *ended* (a list, or a
    callable giving one per pass). The plugin's stdio server is replaced by its answer text,
    so the pass runs Casa's own parsing and settling."""
    rp = ResolvedPlugin(name=plugin_name, artifact_id=ARTIFACT, path=str(env.tmp / "sweep"),
                        version="0.1.0", manifest={"name": plugin_name, "casa": {
                            "driverSweep": "runs_needing_driver", "stopTool": "stop_runs",
                            "resultContract": {"version": 1, "tools": {
                                "runs_needing_driver": {"result": "safe"},
                                "stop_runs": {"result": "safe"}}}}})
    (env.tmp / "sweep").mkdir(exist_ok=True)
    (env.tmp / "sweep" / ".mcp.json").write_text(json.dumps({"mcpServers": {"browser": {
        "command": sys.executable, "args": ["-c", "pass"]}}}))
    env.use(rp)
    passes: list = []

    async def _call(server, tool, args, *, handed=None):
        assert tool == "runs_needing_driver" and args == {}
        passes.append(1)
        return json.dumps({"runs": [], "ended": ended() if callable(ended) else ended})
    monkeypatch.setattr(pc, "call_plugin_tool", _call)
    return passes


def _ended_entry(b, key="k1"):
    return {"ref": f"request:{key}", "binding_ref": b.ref}


async def test_a_request_that_ended_on_its_own_marks_its_card_once(env, monkeypatch):
    c, b = env.admit()
    passes = _sweeping(env, monkeypatch, lambda: [_ended_entry(b)])
    await pc.sweep_once()
    edits = _card_edits(env)
    assert len(edits) == 1 and edits[0]["text"] == f"{CARD_TEXT}\n☑ Ended"
    assert _buttons(edits[0]) == []                      # no Stop left on it
    assert _lines(env) == [] and env.turns == [] and env.desk_uses == []
    await pc.sweep_once()                                # named again on the next pass
    assert len(passes) == 2 and len(_card_edits(env)) == 1
    reloaded = run_admission.Confirmations(str(env.tmp / "admissions.json"))
    assert reloaded.peek(CID).card is None               # forgotten durably, text kept
    assert reloaded.peek(CID).card_text == CARD_TEXT


async def test_a_status_card_admitted_by_his_words_is_marked_too(env, monkeypatch):
    status_text = "📊 Fido\nBrowsing now:\n• RunPod — invoice\nAsked for by Ellen."
    c, b = env.admit()
    run_admission.CONFIRMATIONS.set_card(CID, TgAddress(BOT, OPERATOR, CARD_MID), status_text)
    _sweeping(env, monkeypatch, [_ended_entry(b)])
    await pc.sweep_once()
    assert [e["text"] for e in _card_edits(env)] == [f"{status_text}\n☑ Ended"]


@pytest.mark.parametrize("case", ["other_plugin", "unknown_binding", "not_a_request_ref",
                                  "too_long", "not_admitted", "no_card", "another_operator",
                                  "other_request", "not_a_dict"])
async def test_an_ended_entry_that_is_not_his_admitted_card_marks_nothing(env, monkeypatch,
                                                                          case):
    import authz_grants
    c, b = env.admit(status="pending" if case == "not_admitted" else "admitted",
                     card=case != "no_card")
    entry = _ended_entry(b)
    if case == "other_plugin":
        _, other = env.admit(plugin="bank-feed", cid="conf-" + "d" * 32)
        entry = _ended_entry(other)
    elif case == "unknown_binding":
        entry["binding_ref"] = "casa-bind-unknown"
    elif case == "not_a_request_ref":
        entry["ref"] = "k1"
    elif case == "too_long":
        entry["ref"] = "request:" + "k" * 80
    elif case == "another_operator":
        monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (OPERATOR, 7))
    elif case == "other_request":
        entry["ref"] = "request:k2"
    elif case == "not_a_dict":
        entry = "request:k1"
    _sweeping(env, monkeypatch, [entry])
    await pc.sweep_once()
    assert _card_edits(env) == []


async def test_a_stop_in_flight_keeps_the_card_for_its_own_mark(env, monkeypatch):
    c, b = env.admit()
    _sweeping(env, monkeypatch, [_ended_entry(b)])
    plugin_stop._FLIGHTS.add(("browser", BOT, "request:k1"))
    try:
        await pc.sweep_once()
    finally:
        plugin_stop._FLIGHTS.discard(("browser", BOT, "request:k1"))
    assert _card_edits(env) == []
    assert run_admission.CONFIRMATIONS.peek(CID).card == [BOT, OPERATOR, CARD_MID]


async def test_a_card_that_cannot_be_edited_is_tried_once(env, monkeypatch):
    from telegram.error import BadRequest
    tries: list = []

    async def _refuse(**kwargs):
        tries.append(kwargs)
        raise BadRequest("Message to edit not found")
    env.bot.edit_message_text = _refuse
    c, b = env.admit()
    _sweeping(env, monkeypatch, [_ended_entry(b)])
    await pc.sweep_once()
    await pc.sweep_once()
    assert len(tries) == 1                               # not retried every pass
    assert run_admission.CONFIRMATIONS.peek(CID).card is None


async def test_with_no_channel_yet_the_next_pass_marks_it(env, monkeypatch):
    c, b = env.admit()
    _sweeping(env, monkeypatch, [_ended_entry(b)])
    monkeypatch.setattr(tools_mod, "_channel_manager", None, raising=False)
    await pc.sweep_once()
    assert run_admission.CONFIRMATIONS.peek(CID).card == [BOT, OPERATOR, CARD_MID]
    monkeypatch.setattr(tools_mod, "_channel_manager", make_transport(env.ch), raising=False)
    await pc.sweep_once()
    assert [e["text"] for e in _card_edits(env)] == [f"{CARD_TEXT}\n☑ Ended"]


async def test_while_the_bot_is_rebuilt_the_next_pass_marks_it(env, monkeypatch):
    """r1 (Astra): during a Telegram rebuild the channel exists with no bot, so no edit can be
    tried; the card is kept for the next pass, not forgotten."""
    c, b = env.admit()
    _sweeping(env, monkeypatch, [_ended_entry(b)])
    app = env.ch._app
    env.ch._app = None                                   # what _rebuild's teardown leaves
    await pc.sweep_once()
    assert run_admission.CONFIRMATIONS.peek(CID).card == [BOT, OPERATOR, CARD_MID]
    env.ch._app = app
    await pc.sweep_once()
    assert [e["text"] for e in _card_edits(env)] == [f"{CARD_TEXT}\n☑ Ended"]


async def test_a_stop_tap_after_the_natural_end_still_says_it_had_ended(env, monkeypatch):
    """The card lost its Stop; a Stop from another path (Ellen's stop_work, a stale client)
    still gets the plugin's ``ended`` and its line, and the card is not edited again."""
    c, b = env.admit()
    _sweeping(env, monkeypatch, [_ended_entry(b)])
    await pc.sweep_once()
    monkeypatch.setattr(pc, "call_plugin_tool", _REAL_CALL_PLUGIN_TOOL)
    env.plugins["browser"] = [_plugin(env.tmp / "p", answer={"outcome": "ended"})]
    await _tap(env)
    await _quiet(env)
    assert len(_card_edits(env)) == 1
    assert [m["text"] for m in _lines(env)] == ["📊 Fido\nIt had already ended."]

