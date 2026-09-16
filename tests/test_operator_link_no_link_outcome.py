"""#1015 addendum (v0.319.0): a delivered-link tool's explicit "no link this
time" result, and the in-flight cap sweep that must not strand a deposit.

Every case opens the call through the REAL admission hook (so `delivers`
comes from the contract map, never a hand-built `_InFlight`) and closes it
through the REAL result hook. The channel is an in-process recorder.
"""
from __future__ import annotations

import json

import pytest

import result_broker as rb
from plugin_grants import PluginContract, ResultContractMap, ToolContract
from test_operator_link_delivery import (ARTIFACT, LINK, FETCH, PLUGIN, SETUP_CAP,
                                         SLOT, URL, _map, _post, _pre,
                                         _replacement, _store, recorder)  # noqa: F401
from test_result_broker import DM, _Origin

MULTI = "mcp__plugin_probe_api__multi"      # provides [a, b], delivers {a}


def _multi_map() -> ResultContractMap:
    base = _map()
    tools = dict(base.tools)
    tools[MULTI] = ToolContract(ARTIFACT, PLUGIN, "capability", ("a", "b"), {},
                                {"a": "operator_link"})
    return ResultContractMap(tools=tools, plugins=base.plugins)


async def _admit(pre, tool, call):
    with _Origin(DM):
        assert await pre(_pre(tool), call, {}) == {}


def _hooks(contract=None):
    store, clock = _store()
    contract = contract or _map()
    pre = rb.make_plugin_admission_hook("finance", contract, client_id="c1", store=store)
    post = rb.make_result_hook(contract, client_id="c1", store=store)
    return store, clock, pre, post


def _withheld_bad_capability(out) -> dict:
    parsed = json.loads(_replacement(out))
    assert parsed["casa_result_withheld"] is True
    assert parsed["reason"] == rb._REASON_BAD_CAPABILITY
    return parsed


NO_LINK = {SLOT: None, "status": "already_connected",
           "instructions": "Already connected; no link was created."}


@pytest.mark.asyncio
async def test_an_explicit_no_link_result_passes_unchanged_with_nothing_sent(recorder):
    store, _, pre, post = _hooks()
    await _admit(pre, LINK, "n-1")
    assert len(store._inflight) == 1
    out = await post(_post(LINK, json.dumps(NO_LINK)), "n-1", {})
    assert out == {}
    assert recorder.deliveries == [] and recorder.other_sends == []
    assert store._inflight == {} and store._refs == {}


@pytest.mark.asyncio
async def test_a_null_slot_after_a_deposit_is_withheld_and_the_deposit_dropped(recorder):
    store, _, pre, post = _hooks()
    await _admit(pre, LINK, "n-2")
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=URL)
    assert err is None and ref in store._refs
    out = await post(_post(LINK, json.dumps(NO_LINK)), "n-2", {})
    _withheld_bad_capability(out)
    assert recorder.deliveries == []
    assert ref not in store._refs                         # RAW membership
    assert store._inflight == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", False, 0, [], {}, "null"])
async def test_only_json_null_is_a_no_link_statement(recorder, value):
    store, _, pre, post = _hooks()
    await _admit(pre, LINK, "n-3")
    out = await post(_post(LINK, json.dumps({SLOT: value, "status": "x"})), "n-3", {})
    _withheld_bad_capability(out)
    assert recorder.deliveries == [] and store._inflight == {}


@pytest.mark.asyncio
async def test_a_missing_slot_member_is_not_a_no_link_statement(recorder):
    store, _, pre, post = _hooks()
    await _admit(pre, LINK, "n-4")
    out = await post(_post(LINK, json.dumps({"status": "already_connected"})), "n-4", {})
    _withheld_bad_capability(out)
    assert recorder.deliveries == [] and store._inflight == {}


@pytest.mark.asyncio
async def test_a_non_delivering_capability_call_keeps_0_318_behaviour(recorder):
    store, _, pre, post = _hooks()
    await _admit(pre, FETCH, "n-5")
    out = await post(_post(FETCH, json.dumps({"token": None, "status": "x"})), "n-5", {})
    _withheld_bad_capability(out)
    assert store._inflight == {}


@pytest.mark.asyncio
async def test_a_multi_slot_call_passes_only_when_every_slot_is_null(recorder):
    store, _, pre, post = _hooks(_multi_map())
    await _admit(pre, MULTI, "m-1")
    out = await post(_post(MULTI, json.dumps({"a": None, "b": None, "t": "none"})), "m-1", {})
    assert out == {} and store._inflight == {}
    await _admit(pre, MULTI, "m-2")
    out = await post(_post(MULTI, json.dumps({"a": None, "b": rb.new_reference()})), "m-2", {})
    _withheld_bad_capability(out)
    assert recorder.deliveries == [] and store._inflight == {}


@pytest.mark.asyncio
async def test_a_capability_setup_tool_with_no_link_passes_through_both_real_hooks(recorder):
    store, _, pre, post = _hooks()
    await _admit(pre, SETUP_CAP, "s-1")
    body = {"auth_url": None, "status": "already_connected", "account": "a@example.com"}
    out = await post(_post(SETUP_CAP, json.dumps(body)), "s-1", {})
    assert out == {}
    assert recorder.deliveries == [] and store._inflight == {} and store._refs == {}


def _report_into_a_real_row(monkeypatch, evidence: dict) -> dict:
    """Feed setup-outcome evidence to the REAL `report_dispatch_outcome` over
    one dispatched resident-target row; return the row as it was left."""
    import plugin_setup_episodes as pse
    row = {"id": "ep-521", "plugin": "gmail", "artifact_id": ARTIFACT,
           "status": "dispatched", "gate": "released",
           "expected_tool": SETUP_CAP, "execution_retries": 0}
    monkeypatch.setattr(pse, "_load", lambda: {"episodes": [row]})
    monkeypatch.setattr(pse, "_save", lambda data: None)
    pse.report_dispatch_outcome("ep-521", **evidence)
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("as_error", [False, True])
async def test_a_no_link_setup_run_counts_as_run(tmp_path, monkeypatch, recorder,
                                                 as_error):
    """Through every in-process seam: the REAL result hook passes the no-link
    result (returns `{}`, so the CLI hands the original, non-error result to
    the model); that result, as the ToolResultBlock the SDK then yields, runs
    through the REAL `Agent._process` setup-outcome accounting; and the
    evidence it reports, fed to the REAL `report_dispatch_outcome`, leaves the
    dispatched row consumed. Control (`as_error`): the same answer sent as an
    MCP error, the only route 0.318.0 offered, returns the row to pending."""
    from unittest.mock import patch
    from test_agent_process import (ScriptedToolClient, _capture_reports,
                                    _make_agent, _mk_assistant, _mk_init,
                                    _mk_result, _mk_tool_result, _mk_tool_use,
                                    _setup_msg)
    body = json.dumps({"auth_url": None, "status": "already_connected"})
    store, _, pre, post = _hooks()
    await _admit(pre, SETUP_CAP, "t1")
    assert await post(_post(SETUP_CAP, body), "t1", {}) == {}
    assert recorder.deliveries == [] and store._inflight == {}

    agent = _make_agent(tmp_path)
    ScriptedToolClient.reset([
        _mk_init("sid-nl", ["Read", SETUP_CAP]),
        _mk_tool_use("t1", SETUP_CAP),
        _mk_tool_result("t1", is_error=as_error, text=body),
        _mk_assistant("Gmail is already connected."),
        _mk_result("sid-nl"),
    ])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            _capture_reports() as calls:
        await agent._process(_setup_msg("op-nl"))
    assert [episode for episode, _ in calls] == ["ep-521"]
    row = _report_into_a_real_row(monkeypatch, calls[0][1])
    if as_error:
        assert row["status"] == "pending" and row["execution_retries"] == 1
    else:
        assert row["status"] == "dispatched" and row["execution_retries"] == 0


@pytest.mark.asyncio
async def test_the_cap_sweep_drops_a_swept_calls_deposits(recorder):
    """§3b: a call swept for outliving INFLIGHT_CAP_S takes its deposits with
    it, so the withheld result leaves nothing redeemable behind."""
    store, clock, pre, post = _hooks()
    await _admit(pre, LINK, "w-1")
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=URL)
    assert err is None
    clock.t += rb.INFLIGHT_CAP_S + 1
    await _admit(pre, FETCH, "w-2")                      # open_call runs the real sweep
    assert ref not in store._refs                        # RAW membership
    out = await post(_post(LINK, json.dumps({SLOT: ref})), "w-1", {})
    _withheld_bad_capability(out)
    assert recorder.deliveries == [] and ref not in store._refs


@pytest.mark.asyncio
async def test_the_cap_sweep_drops_a_non_delivering_calls_deposits_too(recorder):
    """Diff round 1 (Astra): the sweep's drop is not scoped to delivering calls
    — a plain capability call swept by the cap takes its deposit with it."""
    store, clock, pre, post = _hooks()
    await _admit(pre, FETCH, "w-3")
    ref, err = store.deposit(client_id="c1", slot="token", value="tok-1")
    assert err is None
    clock.t += rb.INFLIGHT_CAP_S + 1
    await _admit(pre, LINK, "w-4")                       # open_call runs the real sweep
    assert ref not in store._refs                        # RAW membership
    out = await post(_post(FETCH, json.dumps({"token": ref})), "w-3", {})
    _withheld_bad_capability(out)


def test_both_residents_are_told_a_null_link_field_means_no_link():
    """§4: without it, a resident holding a no-link result (no receipt)
    would report a link as unconfirmed that never existed."""
    import pathlib
    root = pathlib.Path(rb.__file__).resolve().parent / "defaults" / "agents"
    for role in ("assistant", "butler"):
        text = " ".join((root / role / "prompts" / "system.md").read_text().split())
        assert ("A result whose link field is `null` means the tool created no "
                "link: report what its text says, and do not describe a link as "
                "unconfirmed.") in text, role
