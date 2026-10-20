"""Browser executor (Plan 3 C1, C8, C9): a plugin's ``callsCasa`` calls — the
declaration, the record the admission hook makes, the bindings behind C1, and
the requester, continue and delivery-status routes."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

import agent as agent_mod
import operator_calls
import plugin_bindings
import plugin_continuations
import plugin_store
import result_broker as rb
import tools as tools_mod
from authz_grants import GrantIdentity
from plugin_grants import PluginContract, ResultContractMap, ToolContract

ARTIFACT = "1" * 64
START = "mcp__plugin_browser_api__start_runs"
PLAIN = "mcp__plugin_browser_api__observe"

FIDO_RUN = {"role": "assistant", "execution_role": "browser", "channel": "telegram",
            "chat_id": "42", "user_id": 100, "cid": "c", "message_type": "channel_in",
            "source": "telegram", "_operator_turn": True, "_origin_route": "telegram",
            "_origin_clearance": "private",
            "user_text": "download my latest RunPod invoice",
            "_operator_words": "download my latest RunPod invoice", "_delegation_id": "d-1",
            "delegation_depth": 1, "_handoff_chain": ("assistant", "browser")}


def _map() -> ResultContractMap:
    return ResultContractMap(
        tools={START: ToolContract(ARTIFACT, "browser", "safe", (), {}, calls_casa=True),
               PLAIN: ToolContract(ARTIFACT, "browser", "safe", (), {})},
        plugins={"browser": PluginContract(ARTIFACT, True, frozenset())})


class _Origin:
    def __init__(self, origin):
        self._origin = origin

    def __enter__(self):
        self._o = agent_mod.origin_var.set(self._origin)
        self._e = tools_mod.engagement_var.set(None)

    def __exit__(self, *exc):
        agent_mod.origin_var.reset(self._o)
        tools_mod.engagement_var.reset(self._e)


def _pre(tool):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    import authz_grants
    live = {"id": (42, 100)}
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: live["id"])
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "b.json")))
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(character=SimpleNamespace(name="Ellen"), kind="resident"),
        "finance": SimpleNamespace(character=SimpleNamespace(name="Alex"), kind="specialist"),
        "browser": SimpleNamespace(character=SimpleNamespace(name="Fido"), kind="specialist"),
    }, raising=False)


def _identity(operator_id=100):
    return GrantIdentity(operator_id=operator_id, chat_id=42, enforcement_role="browser",
                         artifact_id=ARTIFACT, engagement_id="")


def _open(origin=FIDO_RUN, client="c1", tu="tu"):
    return operator_calls.CALLS.open(client_id=client, tool_use_id=tu, tool_name=START,
                                     role="browser", plugin="browser",
                                     identity=_identity(), origin=dict(origin))


# -- the declaration ----------------------------------------------------------

def _manifest(entry: dict, extra: dict | None = None) -> dict:
    return {"name": "browser", "casa": {"resultContract": {
        "version": 1, "tools": {"start_runs": entry,
                                "runs_needing_driver": {"result": "safe"}}}, **(extra or {})}}


def test_calls_casa_declaration_is_the_literal_true():
    got = plugin_store.manifest_result_contract(_manifest({"result": "safe", "callsCasa": True}))
    assert got["tools"]["start_runs"]["calls_casa"] is True
    for bad in (False, "yes", 1, None):
        with pytest.raises(plugin_store.StoreError):
            plugin_store.manifest_result_contract(_manifest({"result": "safe", "callsCasa": bad}))


def test_driver_sweep_names_a_safe_tool_that_calls_nothing():
    ok = _manifest({"result": "safe", "callsCasa": True}, {"driverSweep": "runs_needing_driver"})
    assert plugin_store.manifest_driver_sweep(ok) == "runs_needing_driver"
    assert plugin_store.manifest_driver_sweep(_manifest({"result": "safe"})) is None
    for name in ("start_runs", "nope", 7):
        bad = _manifest({"result": "safe", "callsCasa": True}, {"driverSweep": name})
        with pytest.raises(plugin_store.StoreError):
            plugin_store.manifest_driver_sweep(bad)


# -- the record ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_admission_records_the_call_with_the_turns_facts():
    hook = rb.make_plugin_admission_hook("browser", _map(), client_id="c1",
                                         store=rb.ReferenceStore())
    with _Origin(FIDO_RUN):
        assert await hook(_pre(START), "tu-1", {}) == {}
        assert await hook(_pre(PLAIN), "tu-2", {}) == {}
    call, err = operator_calls.CALLS.find("c1")
    assert err is None and call.tool_use_id == "tu-1" and call.plugin == "browser"
    assert call.operator_words == "download my latest RunPod invoice"
    assert call.chain == ("assistant", "browser") and call.turn_key == "d-1"
    result = rb.make_result_hook(_map(), client_id="c1", store=rb.ReferenceStore())
    await result({"hook_event_name": "PostToolUse", "tool_name": START,
                  "tool_input": {}, "tool_response": "ok"}, "tu-1", {})
    assert operator_calls.CALLS.find("c1") == (None, "no_call_in_flight")


@pytest.mark.asyncio
async def test_failure_hook_forgets_the_call():
    hook = rb.make_plugin_admission_hook("browser", _map(), client_id="c1",
                                         store=rb.ReferenceStore())
    with _Origin(FIDO_RUN):
        await hook(_pre(START), "tu-1", {})
    failure = rb.make_failure_hook(_map(), client_id="c1", store=rb.ReferenceStore())
    await failure({"hook_event_name": "PostToolUseFailure", "tool_name": START,
                   "tool_input": {}, "error": "x"}, "tu-1", {})
    assert operator_calls.CALLS.find("c1") == (None, "no_call_in_flight")


@pytest.mark.asyncio
async def test_a_turn_with_no_operator_to_ask_is_denied_and_records_nothing():
    hook = rb.make_plugin_admission_hook("browser", _map(), client_id="c1",
                                         store=rb.ReferenceStore())
    webhook = {**FIDO_RUN, "message_type": "webhook", "source": "webhook",
               "channel": "webhook"}
    with _Origin(webhook):
        out = await hook(_pre(START), "tu-1", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert operator_calls.CALLS.find("c1") == (None, "no_call_in_flight")


@pytest.mark.asyncio
async def test_a_continuation_turn_has_the_operators_identity_and_no_words():
    binding = plugin_bindings.BINDINGS.for_turn(
        plugin="browser", turn_key="d-1", identity=_identity(),
        chain=("assistant", "browser"), origin=FIDO_RUN)
    parent = plugin_continuations.continuation_origin(binding)
    child = {**parent, "execution_role": "browser",
             "_handoff_chain": ("assistant", "browser"), "delegation_depth": 1}
    hook = rb.make_plugin_admission_hook("browser", _map(), client_id="c9",
                                         store=rb.ReferenceStore())
    with _Origin(child):
        assert await hook(_pre(START), "tu-9", {}) == {}
    call, _ = operator_calls.CALLS.find("c9")
    assert call.operator_words is None and call.binding_ref == binding.ref
    assert (call.identity.chat_id, call.identity.operator_id) == (42, 100)


@pytest.mark.asyncio
async def test_a_sender_who_is_not_the_configured_operator_is_denied(monkeypatch):
    """Accept-all mode lets another sender in with an identity of their own;
    a callsCasa call acts for the operator, so it never runs for them."""
    import authz_grants
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: None)
    hook = rb.make_plugin_admission_hook("browser", _map(), client_id="c1",
                                         store=rb.ReferenceStore())
    with _Origin(FIDO_RUN):
        out = await hook(_pre(START), "tu-1", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (42, 555))
    with _Origin(FIDO_RUN):
        out = await hook(_pre(START), "tu-2", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert operator_calls.CALLS.find("c1") == (None, "no_call_in_flight")


@pytest.mark.asyncio
async def test_a_call_denied_after_it_was_recorded_leaves_no_record():
    """p3r4b: a callsCasa consumer whose reference is stale is denied, and its
    record goes with the denial, so the next valid call is not ambiguous."""
    CONSUME = "mcp__plugin_browser_api__use"
    cmap = ResultContractMap(
        tools={START: ToolContract(ARTIFACT, "browser", "safe", (), {}, calls_casa=True),
               CONSUME: ToolContract(ARTIFACT, "browser", "safe", (), {"r": "card"},
                                     calls_casa=True)},
        plugins={"browser": PluginContract(ARTIFACT, True, frozenset())})
    hook = rb.make_plugin_admission_hook("browser", cmap, client_id="c1",
                                         store=rb.ReferenceStore())
    with _Origin(FIDO_RUN):
        out = await hook({"hook_event_name": "PreToolUse", "tool_name": CONSUME,
                          "tool_input": {"r": "casa-cap-" + "9" * 32}}, "tu-1", {})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert await hook(_pre(START), "tu-2", {}) == {}
    call, err = operator_calls.CALLS.find("c1")
    assert err is None and call.tool_use_id == "tu-2"


def test_two_calls_in_flight_are_ambiguous():
    _open(tu="a")
    _open(tu="b")
    assert operator_calls.CALLS.find("c1") == (None, "ambiguous_call")


def test_a_call_expires_after_the_cap():
    now = [0.0]
    calls = operator_calls.OperatorCalls(now=lambda: now[0])
    calls.open(client_id="c1", tool_use_id="a", tool_name=START, role="browser",
               plugin="browser", identity=_identity(), origin=FIDO_RUN)
    now[0] = operator_calls.OPERATOR_CALL_CAP_S
    assert calls.find("c1") == (None, "no_call_in_flight")


# -- bindings (C1) -----------------------------------------------------------------

def test_one_binding_per_plugin_and_turn_and_it_survives_a_restart(tmp_path):
    path = str(tmp_path / "bindings.json")
    store = plugin_bindings.BindingStore(path)
    a = store.for_turn(plugin="browser", turn_key="d-1", identity=_identity(),
                       chain=("assistant", "finance", "browser"), origin=FIDO_RUN)
    assert store.for_turn(plugin="browser", turn_key="d-1", identity=_identity(),
                          chain=("assistant", "finance", "browser"), origin=FIDO_RUN) == a
    assert a.requester == "finance" and a.requester_kind == "specialist"
    assert "user_text" not in a.origin and "_operator_turn" not in a.origin
    again = plugin_bindings.BindingStore(path)
    b = again.get(a.ref, plugin="browser", operator_id=100)
    assert b is not None and b.chain == ["assistant", "finance", "browser"]
    assert again.get(a.ref, plugin="other", operator_id=100) is None
    assert again.get(a.ref, plugin="browser", operator_id=999) is None
    import os
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_bindings_age_out():
    now = [1000.0]
    store = plugin_bindings.BindingStore("/nonexistent-dir-for-test/b.json", now=lambda: now[0])
    b = store.for_turn(plugin="browser", turn_key="t", identity=_identity(),
                       chain=("assistant", "browser"), origin=FIDO_RUN)
    now[0] += plugin_bindings.BINDING_TTL_S + 1
    store.for_turn(plugin="browser", turn_key="t2", identity=_identity(),
                   chain=("assistant", "browser"), origin=FIDO_RUN)
    assert store.peek(b.ref) is None


async def _post(handler, body):
    req = make_mocked_request("POST", "/x")

    async def _json():
        return body
    req.json = _json
    resp = await handler(req)
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_requester_route_reads_the_call_never_the_body():
    _open({**FIDO_RUN, "_handoff_chain": ("assistant", "finance", "browser"),
           "execution_role": "browser"})
    out = await _post(operator_calls.build_requester_handler(),
                      {"client": "c1", "requester_id": "assistant"})
    assert out["requester_id"] == "finance" and out["requester_kind"] == "specialist"
    assert out["chain"] == ["assistant", "finance", "browser"]
    assert out["binding_ref"].startswith(plugin_bindings.REF_PREFIX)
    assert await _post(operator_calls.build_requester_handler(), {"client": "zz"}) == {
        "error": "no_call_in_flight"}


# -- continue (C8) -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continue_queues_once_per_run_for_the_bindings_own_plugin(monkeypatch):
    queued: list = []

    class _Q:
        def request(self, binding, run_ref, reason):
            key = (binding.plugin, run_ref)
            if key in queued:
                return "already_queued"
            queued.append(key)
            return "queued"

    monkeypatch.setattr(plugin_continuations, "CONTINUATIONS", _Q())
    _open()
    facts = await _post(operator_calls.build_requester_handler(), {"client": "c1"})
    h = operator_calls.build_continue_handler()
    body = {"client": "c1", "binding_ref": facts["binding_ref"], "run_ref": "r-1",
            "reason": "operator_answer"}
    assert await _post(h, body) == {"status": "queued"}
    assert await _post(h, body) == {"status": "already_queued"}
    assert await _post(h, {**body, "reason": "because"}) == {"error": "bad_reason"}
    assert await _post(h, {**body, "run_ref": "x" * 65}) == {"error": "bad_run_ref"}
    assert await _post(h, {**body, "binding_ref": "casa-bind-" + "0" * 32}) == {
        "error": "unknown_binding"}


# -- delivery status (C9) -------------------------------------------------------------

def test_delivery_status_only_for_the_plugin_and_operator_it_was_posted_for():
    outcomes = rb.DeliveryOutcomes()
    store = rb.ReferenceStore()
    ref = rb.new_reference()
    outcomes.record(ref, plugin="browser", operator_id=100, delivered=True, repeat=False)
    assert outcomes.status(ref, plugin="browser", operator_id=100, store=store) == {
        "status": "delivered", "repeat": False}
    versioned = rb.new_reference()
    outcomes.record(versioned, plugin="browser", operator_id=100, artifact_id="old",
                    delivered=True, repeat=False)
    assert outcomes.status(versioned, plugin="browser", operator_id=100, artifact_id="new",
                           store=store)["status"] == "unknown"           # p3r3b
    assert outcomes.status(ref, plugin="finance", operator_id=100, store=store)["status"] == "unknown"
    assert outcomes.status(ref, plugin="browser", operator_id=7, store=store)["status"] == "unknown"
    other = rb.new_reference()
    outcomes.record(other, plugin="browser", operator_id=100, delivered=False, repeat=False)
    assert outcomes.status(other, plugin="browser", operator_id=100, store=store)["status"] == "withheld"
    assert outcomes.status("nope", plugin="browser", operator_id=100, store=store)["status"] == "unknown"


def test_a_still_escrowed_deposit_reads_pending_only_for_its_owner():
    store = rb.ReferenceStore()
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=START, tool_use_id="t",
                    identity=_identity(), provides=("file",))
    ref, err = store.deposit(client_id="c1", slot="file", value="v")
    assert err is None
    outcomes = rb.DeliveryOutcomes()
    assert outcomes.status(ref, plugin="browser", operator_id=100, artifact_id=ARTIFACT,
                           store=store)["status"] == "pending"
    assert outcomes.status(ref, plugin="browser", operator_id=7, artifact_id=ARTIFACT,
                           store=store)["status"] == "unknown"
    assert outcomes.status(ref, plugin="bank", operator_id=100, artifact_id="2" * 64,
                           store=store)["status"] == "unknown"
