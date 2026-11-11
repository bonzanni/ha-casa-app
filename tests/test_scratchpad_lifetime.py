"""Where a pad is attached and when it goes: the real delegated runner over the CLI double, the
real binding store and continuation worker, and the pad reached through the SDK's own bridge."""
from __future__ import annotations

import pytest

import agent as agent_mod
import operator_calls
import plugin_bindings
import plugin_continuations as pc
import plugin_stop
import run_admission
import scratchpad
import tools as tools_mod
from authz_grants import GrantIdentity
from error_kinds import ApiErrorTurn
from scratchpad import GRANT, IDLE_S, PROMPT_LINE, SERVER
from test_memory_capture import mem  # noqa: F401 — fixture
from test_plugin_continuations import (  # noqa: F401 — fixtures
    ORIGIN, SID, _binding, _continue, _kept, announce, conv, desks, roles)
from test_plugin_stop import CARD_MID, _calls, _cq, _plugin, _quiet, _tap, env  # noqa: F401
from test_scratchpad import _body, _call, _client, pads  # noqa: F401

SID2 = "55555555-5555-4555-8555-555555555555"
DAY = 24 * 3600.0


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


async def _delegate(conv, role="browser", origin=None):  # noqa: F811
    tok = agent_mod.origin_var.set(dict(origin or ORIGIN))
    try:
        return await tools_mod._run_delegated_agent(conv.roles[role], "go", "")
    finally:
        agent_mod.origin_var.reset(tok)


def _writes(conv, text):  # noqa: F811
    async def _write():
        await _call(conv.h.launches[-1], op="append", text=text)
    conv.h.on_receive = _write


def _ident():
    return GrantIdentity(operator_id=100, bot_id=111, chat_id=42, enforcement_role="browser",
                         artifact_id="1" * 64, engagement_id="")


def _mint(store, *, turn="d-1", plugin="browser", session=SID):
    return store.for_turn(plugin=plugin, turn_key=turn, identity=_ident(),
                          chain=("assistant", "browser"),
                          origin={"_delegated_session_id": session})


# --- attached to one surface -------------------------------------------------------------


async def test_a_delegated_client_holds_a_pad_under_its_own_session(conv, pads):  # noqa: F811
    seen: list = []

    async def _during():
        opts = conv.h.launches[-1]
        await _call(opts, op="write", text="note")
        seen.append((opts.session_id, pads.keys()))
    conv.h.on_receive = _during
    await _delegate(conv)
    (opts,) = conv.h.launches
    assert GRANT in opts.allowed_tools and SERVER in opts.mcp_servers
    assert opts.system_prompt == f"x\n\n{PROMPT_LINE}"
    assert seen == [(opts.session_id, [opts.session_id])]


async def test_two_delegated_runs_have_two_pads(conv, pads):  # noqa: F811
    keys: list = []

    async def _during():
        await _call(conv.h.launches[-1], op="write", text="note")
        keys.append(pads.keys())
    conv.h.on_receive = _during
    await _delegate(conv)
    await _delegate(conv)
    a, b = conv.h.launches
    assert a.session_id != b.session_id
    assert keys == [[a.session_id], [b.session_id]]


async def test_a_resumed_client_is_told_as_well(conv):  # noqa: F811
    b = _binding()
    _kept(conv, b)
    await (await _continue(conv, b))
    (opts,) = conv.h.launches
    assert opts.resume == SID and PROMPT_LINE in opts.system_prompt


async def test_a_stored_call_has_no_tool(conv, pads):  # noqa: F811
    origin = {**ORIGIN, "stored_call": {"run_id": "r", "runtime_name": "x", "canonical": "{}",
                                        "label": "go"}}
    await _delegate(conv, origin=origin)
    (opts,) = conv.h.launches
    assert SERVER not in opts.mcp_servers
    assert GRANT not in opts.allowed_tools
    assert opts.system_prompt == "x" and PROMPT_LINE not in opts.system_prompt
    assert pads.keys() == []


@pytest.mark.parametrize("long_runs, always", [(False, None), (True, True)])
async def test_only_a_long_conversation_role_loads_it_up_front(conv, long_runs, always):  # noqa: F811
    conv.roles["browser"].session.long_runs = long_runs
    await _delegate(conv)
    (opts,) = conv.h.launches
    assert opts.mcp_servers[SERVER].get("alwaysLoad") is always


@pytest.mark.parametrize("deny", [GRANT, f"mcp__{SERVER}"])
async def test_a_role_that_disallows_it_has_no_tool_and_is_told_nothing(conv, deny):  # noqa: F811
    conv.roles["browser"].tools.disallowed = [deny]
    await _delegate(conv)
    (opts,) = conv.h.launches
    assert deny in opts.disallowed_tools
    assert SERVER not in opts.mcp_servers and GRANT not in opts.allowed_tools
    assert opts.system_prompt == "x"


# --- the brief ---------------------------------------------------------------------------


def _sees_its_pad(conv, pads):  # noqa: F811
    seen: list = []

    async def _during():
        opts = conv.h.launches[-1]
        seen.append(list(pads.lines(opts.session_id or opts.resume) or []))
    conv.h.on_receive = _during
    return seen


async def test_a_fresh_conversation_opens_its_pad_with_its_brief(conv, pads):  # noqa: F811
    seen = _sees_its_pad(conv, pads)
    await _delegate(conv, origin={**ORIGIN, "_operator_words": "add these:\n## x\nalpha"})
    await _delegate(conv)
    assert seen == [["## brief", "Task:", "go", "The operator's words:", "add these:", " ## x",
                     "alpha"],
                    ["## brief", "Task:", "go"]]


async def test_a_continuation_leaves_the_brief_as_it_is(conv, pads):  # noqa: F811
    b = _binding()
    _kept(conv, b)
    pads.put(SID, ["## brief", "edited by the model"])
    seen = _sees_its_pad(conv, pads)
    await (await _continue(conv, b))
    assert seen == [["## brief", "edited by the model"]]


async def test_a_fresh_continuation_takes_the_brief_along_unchanged(conv, pads):  # noqa: F811
    b = _binding()
    plugin_bindings.BINDINGS.adopt(b.ref, SID)      # no transcript on disk: the next is fresh
    pads.put(SID, ["## brief", "Task:", "the first request's"])
    seen = _sees_its_pad(conv, pads)
    await (await _continue(conv, b))
    assert seen == [["## brief", "Task:", "the first request's"]]


async def test_a_continuation_after_a_restart_writes_no_brief(conv, pads):  # noqa: F811
    b = _binding()
    _kept(conv, b)
    seen = _sees_its_pad(conv, pads)
    await (await _continue(conv, b))
    assert seen == [[]] and pads.keys() == []


async def test_a_runs_plugin_code_reaches_the_runs_own_pad_through_the_route(  # noqa: F811
        conv, pads, monkeypatch):
    """The real runner: the client id is the one its options builder put in the env, and the
    route reaches the pad of the run that client belongs to, and nothing once it ended."""
    import json
    from dataclasses import replace

    from aiohttp.test_utils import make_mocked_request

    import result_broker as rb
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    built, ids = tools_mod._build_specialist_options, iter(["c-run-1", "c-run-2"])

    def _with_broker(cfg, **kw):
        options = built(cfg, **kw)
        return replace(options, env={**options.env, **rb.broker_env(next(ids))})
    monkeypatch.setattr(tools_mod, "_build_specialist_options", _with_broker)

    async def _post(body):
        req = make_mocked_request("POST", "/internal/broker/pad")

        async def _json():
            return body
        req.json = _json
        return json.loads((await operator_calls.build_pad_handler()(req)).body)

    seen: list = []

    async def _during():
        opts = conv.h.launches[-1]
        client = opts.env[rb.ENV_CLIENT]
        operator_calls.CALLS.open(client_id=client, tool_use_id="tu", tool_name="x",
                                  role="browser", plugin="browser", identity=_ident(),
                                  origin=agent_mod.origin_var.get())
        await _post({"client": client, "op": "write", "section": "page", "text": client})
        seen.append((client, _body((await _call(opts, op="read"))[0]),
                     await _post({"client": "c-run-1", "op": "read", "section": "page"})))
        operator_calls.CALLS.close(client, "tu")
    conv.h.on_receive = _during
    await _delegate(conv)
    await _delegate(conv)
    (c1, pad1, read1), (c2, pad2, read2) = seen
    assert (c1, c2) == ("c-run-1", "c-run-2")
    assert pad1 == "## brief\nTask:\ngo\n## page\nc-run-1"
    assert pad2 == "## brief\nTask:\ngo\n## page\nc-run-2"
    assert read1 == {"text": "c-run-1", "next": None}
    assert read2["error"] == "no_call_in_flight"     # the first run's call, and pad, are gone
    assert pads.keys() == [] and pads._clients == {}


# --- a run's end -------------------------------------------------------------------------


async def test_a_run_with_no_binding_takes_its_pad_with_it(conv, pads):  # noqa: F811
    _writes(conv, "note")
    await _delegate(conv)
    assert pads.keys() == []


async def test_a_run_that_raises_takes_its_pad_with_it(conv, pads):  # noqa: F811
    _writes(conv, "note")
    conv.h.mode = "raise"
    with pytest.raises(ApiErrorTurn):
        await _delegate(conv)
    assert pads.keys() == []


async def test_a_binding_minted_during_the_run_keeps_its_pad(conv, pads):  # noqa: F811
    minted: list = []

    async def _during():
        await _call(conv.h.launches[-1], op="write", text="note")
        call = operator_calls.OperatorCalls().open(
            client_id="c", tool_use_id="t", tool_name="x", role="browser", plugin="browser",
            identity=_ident(), origin=agent_mod.origin_var.get())
        minted.append(operator_calls.binding_for(call))
    conv.h.on_receive = _during
    await _delegate(conv)
    (opts,) = conv.h.launches
    assert minted[0].session_id == opts.session_id
    assert pads.keys() == [opts.session_id] and pads.lines(opts.session_id) == ["note"]


# --- a continuation ----------------------------------------------------------------------


async def test_a_resumed_turn_reads_its_requests_pad(conv, pads):  # noqa: F811
    b = _binding()
    _kept(conv, b)
    await _call(_client(SID), op="write", text="the list")
    read: list = []

    async def _during():
        read.append(_body((await _call(conv.h.launches[-1], op="read"))[0]))
    conv.h.on_receive = _during
    await (await _continue(conv, b))
    await (await _continue(conv, b))
    assert [o.resume for o in conv.h.launches] == [SID, SID]
    assert [o.session_id for o in conv.h.launches] == [None, None]
    assert read == ["the list", "the list"]
    assert pads.keys() == [SID]


async def test_a_fresh_session_takes_the_requests_pad_with_it(conv, pads):  # noqa: F811
    b = _binding()
    plugin_bindings.BINDINGS.adopt(b.ref, SID)      # no transcript on disk: the next is fresh
    await _call(_client(SID), op="write", text="the list")
    read: list = []

    async def _during():
        read.append(_body((await _call(conv.h.launches[-1], op="read"))[0]))
    conv.h.on_receive = _during
    await (await _continue(conv, b))
    (opts,) = conv.h.launches
    assert opts.resume is None and opts.session_id != SID
    assert b.session_id == opts.session_id
    assert read == ["the list"]
    assert pads.keys() == [opts.session_id]


async def test_a_relay_turn_never_sees_the_workers_pad(conv, pads):  # noqa: F811
    b = _binding(("assistant", "finance", "browser"))
    _kept(conv, b)
    await _call(_client(SID), op="write", text="the worker's")
    read: list = []

    async def _during():
        read.append(await _call(conv.h.launches[-1], op="read"))
    conv.h.on_receive = _during
    await (await _continue(conv, b))
    worker, relay = conv.h.launches
    assert worker.resume == SID and relay.session_id not in (None, SID)
    assert _body(read[0][0]) == "the worker's"
    # the relay is a conversation of its own: its pad is its own brief, never the worker's
    assert "the worker's" not in read[1][0] and _body(read[1][0]).startswith("## brief\nTask:")
    assert pads.keys() == [SID]


async def test_a_restart_leaves_a_binding_with_an_empty_pad_that_says_so(  # noqa: F811
        conv, pads, monkeypatch):
    b = _binding()
    _kept(conv, b)
    await _call(_client(SID), op="write", text="gone")
    monkeypatch.setattr(scratchpad, "PADS", scratchpad.PadStore())
    read: list = []

    async def _during():
        read.append(await _call(conv.h.launches[-1], op="read"))
    conv.h.on_receive = _during
    await (await _continue(conv, b))
    assert conv.h.launches[0].resume == SID
    assert read[0][1] is True and "restarted at " in read[0][0]


# --- the binding store -------------------------------------------------------------------


def test_adopt_moves_the_pad(tmp_path, pads):
    store = plugin_bindings.BindingStore(str(tmp_path / "b.json"))
    b = _mint(store)
    pads.put(SID, ["note"])
    store.adopt(b.ref, SID2)
    assert pads.keys() == [SID2] and pads.lines(SID2) == ["note"]
    assert store.holds_session(SID2) and not store.holds_session(SID)


def test_adopting_the_session_it_has_keeps_the_pad(tmp_path, pads):
    store = plugin_bindings.BindingStore(str(tmp_path / "b.json"))
    b = _mint(store)
    pads.put(SID, ["note"])
    store.adopt(b.ref, SID)
    assert pads.keys() == [SID] and pads.lines(SID) == ["note"]


def test_adopt_with_no_pad_or_no_binding_moves_nothing(tmp_path, pads):
    store = plugin_bindings.BindingStore(str(tmp_path / "b.json"))
    b = _mint(store)
    store.adopt(b.ref, SID2)
    assert pads.keys() == []
    pads.put(SID, ["other"])
    store.adopt("casa-bind-none", SID2)
    assert pads.keys() == [SID]


def test_adopt_leaves_a_copy_for_another_binding_of_the_session(tmp_path, pads):
    store = plugin_bindings.BindingStore(str(tmp_path / "b.json"))
    a, _other = _mint(store, plugin="browser"), _mint(store, plugin="other")
    pads.put(SID, ["note"])
    store.adopt(a.ref, SID2)
    assert sorted(pads.keys()) == sorted([SID, SID2])
    assert pads.lines(SID) == ["note"] and pads.lines(SID2) == ["note"]
    pads.lines(SID2).append("mine")
    assert pads.lines(SID) == ["note"]


def test_a_binding_past_its_life_takes_its_pad(tmp_path, pads):
    clock = Clock()
    store = plugin_bindings.BindingStore(str(tmp_path / "b.json"), now=clock)
    _mint(store)
    pads.put(SID, ["note"])
    clock.t += plugin_bindings.BINDING_TTL_S + 1
    _mint(store, turn="d-2", session=SID2)
    assert not store.holds_session(SID) and pads.keys() == []


def test_a_binding_past_the_store_limit_takes_its_pad(tmp_path, pads, monkeypatch):
    monkeypatch.setattr(plugin_bindings, "MAX_BINDINGS", 1)
    clock = Clock()
    store = plugin_bindings.BindingStore(str(tmp_path / "b.json"), now=clock)
    _mint(store)
    pads.put(SID, ["note"])
    pads.put(SID2, ["kept"])
    clock.t += 1
    _mint(store, turn="d-2", session=SID2)
    assert pads.keys() == [SID2]


def test_a_dropped_binding_leaves_the_pad_of_a_binding_that_stays(tmp_path, pads):
    clock = Clock()
    store = plugin_bindings.BindingStore(str(tmp_path / "b.json"), now=clock)
    _mint(store, plugin="browser")
    clock.t += plugin_bindings.BINDING_TTL_S - 10
    late = _mint(store, plugin="other")
    pads.put(SID, ["note"])
    clock.t += 20
    _mint(store, turn="d-2", session=SID2)          # the prune drops the first alone
    assert store.peek(late.ref) is not None
    assert pads.keys() == [SID]


# --- expiry ------------------------------------------------------------------------------


@pytest.fixture
def clocked(monkeypatch):
    clock = Clock()
    store = scratchpad.PadStore(now=clock)
    monkeypatch.setattr(scratchpad, "PADS", store)
    return store, clock


def test_expiry_takes_an_idle_pad_nothing_owns(clocked):
    store, clock = clocked
    store.put("old", ["x"])
    clock.t += DAY - 1
    store.put("new", ["y"])
    assert store.expire(lambda key: False) == 0
    assert sorted(store.keys()) == ["new", "old"]
    clock.t += 1
    assert store.expire(lambda key: False) == 1
    assert store.keys() == ["new"]


async def test_using_a_pad_restarts_its_clock(clocked):
    store, clock = clocked
    client = _client("S1")
    await _call(client, op="write", text="x")
    clock.t += DAY - 1
    await _call(client, op="read")
    clock.t += DAY - 1
    assert store.expire(lambda key: False) == 0
    clock.t += 1
    assert store.expire(lambda key: False) == 1


def test_expiry_leaves_a_pad_a_retained_binding_owns_and_takes_it_after_release(
        clocked, tmp_path):
    store, clock = clocked
    bindings = plugin_bindings.BindingStore(str(tmp_path / "b.json"), now=clock)
    _mint(bindings)
    store.put(SID, ["notes for later"])
    clock.t += 25 * 3600
    assert store.expire(bindings.holds_session) == 0
    assert store.lines(SID) == ["notes for later"]
    clock.t += 40 * DAY
    assert store.expire(bindings.holds_session) == 0    # still in the store, so still resumable
    assert store.keys() == [SID]
    _mint(bindings, turn="d-2", session=SID2)           # any write prunes the lapsed one
    assert not bindings.holds_session(SID)
    assert store.keys() == []


def test_expiry_takes_a_pad_whose_owner_went_without_a_delete(clocked, tmp_path):
    """A session named by no binding any more, whatever took it: only the clock is left."""
    store, clock = clocked
    bindings = plugin_bindings.BindingStore(str(tmp_path / "b.json"), now=clock)
    b = _mint(bindings)
    store.put(SID, ["x"])
    b.session_id = SID2
    clock.t += IDLE_S
    assert store.expire(bindings.holds_session) == 1
    assert store.keys() == []


async def test_opening_a_client_expires_old_pads_but_not_a_requests(  # noqa: F811
        conv, clocked):
    store, clock = clocked
    b = _binding()
    _kept(conv, b)
    store.put(SID, ["the request's"])
    store.put("nobody", ["old"])
    clock.t += 25 * 3600
    await _delegate(conv)
    assert store.keys() == [SID]


# --- a request's end is not the conversation's --------------------------------------------


def _second_request(env, binding):  # noqa: F811
    c = run_admission.Confirmation(
        cid="conf-" + "d" * 32, plugin="browser", bot_id=111, operator_id=42,
        binding_ref=binding.ref, request_key="k2", status="admitted", admissions=[],
        created_at=0.0, card=[111, 42, CARD_MID + 1], card_text="card")
    run_admission.CONFIRMATIONS.add(c)
    return c


async def test_stopping_one_of_two_requests_on_a_binding_keeps_the_pad(env, pads):  # noqa: F811
    rp = env.use(_plugin(env.tmp / "p"))
    first, binding = env.admit()
    second = _second_request(env, binding)
    plugin_bindings.BINDINGS.adopt(binding.ref, SID)
    await _call(_client(SID), op="write", text="both requests'")
    await _tap(env, _cq())
    await _quiet(env)
    await plugin_stop.drain()
    assert [c["args"] for c in _calls(rp)] == [{"ref": plugin_stop.REF_PREFIX + "k1"}]
    assert run_admission.CONFIRMATIONS.peek(second.cid).status == "admitted"
    assert plugin_bindings.BINDINGS.holds_session(SID)
    assert pads.lines(SID) == ["both requests'"]


async def test_one_of_two_requests_ending_on_its_own_keeps_the_pad(env, pads):  # noqa: F811
    first, binding = env.admit()
    second = _second_request(env, binding)
    plugin_bindings.BINDINGS.adopt(binding.ref, SID)
    await _call(_client(SID), op="write", text="both requests'")
    ref = plugin_stop.REF_PREFIX + first.request_key
    assert await plugin_stop.settle_ended("browser", 111, 42, ref) is True
    assert run_admission.CONFIRMATIONS.peek(second.cid).status == "admitted"
    assert pads.lines(SID) == ["both requests'"]


def test_no_request_level_code_touches_a_pad():
    """Nothing about a request's end deletes a pad: only the binding store and a run's end do."""
    import ast
    from pathlib import Path
    for module in (pc, plugin_stop, run_admission):
        tree = ast.parse(Path(module.__file__).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "scratchpad" not in names and "PADS" not in names
