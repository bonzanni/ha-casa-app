"""#1303: a proposal button may store a ``capability`` tool whose ONLY provided
slot delivers ``operator_file`` — the ``More`` exception's file sibling. The
tap runs it in the same pinned one-call turn; the file goes through the
existing delivered-slot path; the landed file is the receipt, and a call that
ran with arguments an installed hook changed is told on every outcome
(INV-PROP-007).
"""
from __future__ import annotations

import json
import logging

import pytest

import pinned_run as pr
import result_broker as rb
import specialist_desk as sd
import stored_calls as sc
import tools as tools_mod
from test_desk_tap import ART, LABEL, OPERATOR, _build, _echo, _tap, _tool, env  # noqa: F401
from test_operator_file_delivery import (DATA, EXPORT, SLOT, _identity, _map, _open, _put,  # noqa: F401
                                         names, outbox, recorder)
from test_stored_calls import _map as _sc_map
from test_stored_calls import _runtime as _sc_runtime
from test_stored_calls import _tool as _sc_tool

FILE = "mcp__plugin_probe_probe__package"


# --- which calls a button may store ------------------------------------------------

def test_a_capability_whose_only_slot_delivers_a_file_is_a_stored_call():
    tool = _sc_tool("package", "capability", provides=("package",),
                    delivers={"package": "operator_file"})
    call, reason = sc.resolve_stored_call("package", contract_map=_sc_map([tool]), protected={},
                                          seg="probe", server="probe")
    assert reason is None and call.runtime_name == _sc_runtime("package")
    assert sc.stored_call_still_ok(call.runtime_name, contract_map=_sc_map([tool]),
                                   protected={}) is None


@pytest.mark.parametrize("tool, reason", [
    (_sc_tool("x", "capability", provides=("package", "other"),
              delivers={"package": "operator_file"}), "capability"),
    (_sc_tool("x", "capability", provides=("package", "card"),
              delivers={"package": "operator_file", "card": "operator_proposal"}), "capability"),
    (_sc_tool("x", "capability", provides=("package",), delivers={"package": "operator_message"}),
     "capability"),
    (_sc_tool("x", "capability", provides=("package",)), "capability"),
    (_sc_tool("x", "capability", provides=("package",), consumes={"d": "s"},
              delivers={"package": "operator_file"}), "consumes"),
])
def test_every_other_file_shape_is_still_refused(tool, reason):
    call, why = sc.resolve_stored_call("x", contract_map=_sc_map([tool]), protected={},
                                       seg="probe", server="probe")
    assert call is None and why == reason


# --- the pinned turn's capture -----------------------------------------------------

async def test_a_landed_file_is_captured_as_delivered_with_its_kind(recorder, names, outbox):
    path = _put(outbox, "q3.zip.csv")
    store = rb.ReferenceStore(now=lambda: 1000.0)
    owner = pr.PinnedRun(run_id="run-1", runtime_name=EXPORT, canonical="{}", label="Get package",
                         build_input=None)
    hook = rb.make_result_hook(_map(), client_id="c1", store=store, owner=owner)
    _open(store)
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text", caption="Q3")
    assert err is None
    await hook({"hook_event_name": "PostToolUse", "tool_name": EXPORT, "tool_input": {},
                "tool_response": json.dumps({SLOT: ref})}, "call-1", {})
    assert len(recorder.deliveries) == 1 and recorder.deliveries[0][1] == DATA
    assert owner.captured == pr.Capture("delivered", "operator_file")


async def test_a_failed_call_with_rewritten_arguments_is_told(caplog):
    owner = pr.PinnedRun(run_id="run-2", runtime_name=EXPORT, canonical='{"batch":1}',
                         label="Get package", build_input=None)
    hook = rb.make_failure_hook(_map(), client_id="c1", store=rb.ReferenceStore(), owner=owner)
    with caplog.at_level(logging.ERROR, logger="result_broker"):
        await hook({"hook_event_name": "PostToolUseFailure", "tool_name": EXPORT,
                    "tool_input": {"batch": 2}, "error": "boom"}, "call-1", {})
    assert owner.captured == pr.Capture("error", "tool_error", rewritten=True)
    assert any("changed by an installed hook" in r.getMessage() for r in caplog.records)


async def test_a_failed_call_with_its_stored_arguments_is_not_told():
    owner = pr.PinnedRun(run_id="run-3", runtime_name=EXPORT, canonical='{"batch":1}',
                         label="Get package", build_input=None)
    hook = rb.make_failure_hook(_map(), client_id="c1", store=rb.ReferenceStore(), owner=owner)
    await hook({"hook_event_name": "PostToolUseFailure", "tool_name": EXPORT,
                "tool_input": {"batch": 1}, "error": "boom"}, "call-1", {})
    assert owner.captured == pr.Capture("error", "tool_error", rewritten=False)


# --- the desk ----------------------------------------------------------------------

@pytest.fixture
def file_env(env):
    env.build = _build(tools=[_tool("apply"),
                              _tool("package", "capability", provides=("package",),
                                    delivers={"package": "operator_file"})])
    return env


def _file_meta():
    return {"options": ["Get package"], "calls": [
        {"server": "probe", "wire_name": "package", "runtime_name": FILE, "proposal": True,
         "arguments": {}, "canonical": "{}"}]}


def _respond(capture):
    async def respond(call):
        call.owner.resolve(capture)
        return tools_mod.DelegatedOutput(text="")
    return respond


async def test_a_landed_file_is_the_sole_receipt(file_env):
    env = file_env
    env.respond = _respond(pr.Capture("delivered", "operator_file"))
    await _tap(env, meta=_file_meta())
    assert env.channel.replies == [] and env.channel.notices == [] and env.channel.marks == []
    assert [(e.who, e.text) for e in env.desk.log] == [("operator", "[tapped: Get package]"),
                                                       ("specialist", sd.POSTED_FILE)]
    assert _echo() == [f"{LABEL} applied your tap (Get package)."]
    (call,) = env.calls
    assert call.owner.runtime_name == FILE


async def test_a_landed_file_with_rewritten_arguments_is_followed_by_one_tell(file_env):
    env = file_env
    env.respond = _respond(pr.Capture("delivered", "operator_file", rewritten=True))
    await _tap(env, meta=_file_meta())
    assert env.channel.notices == [(OPERATOR, f"{LABEL} {sd.TELL_LINE}")]
    assert _echo() == [f"{LABEL} applied your tap (Get package) — the CLI reported arguments "
                       "changed by an installed hook."]


async def test_a_withheld_file_is_the_refusal_notice(file_env):
    env = file_env
    env.respond = _respond(pr.Capture("withheld", "not delivered"))
    await _tap(env, meta=_file_meta())
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not apply your tap (not delivered).")]
    assert env.channel.marks == ["✖ failed"]


async def test_a_refused_call_with_rewritten_arguments_tells_before_the_refusal(file_env):
    env = file_env
    env.respond = _respond(pr.Capture("withheld", "not delivered", rewritten=True))
    await _tap(env, meta=_file_meta())
    assert env.channel.notices == [(OPERATOR, f"{LABEL} {sd.TELL_LINE}"),
                                   (OPERATOR, f"{LABEL} could not apply your tap (not delivered).")]


async def test_a_more_proposal_with_rewritten_arguments_still_tells_only_in_its_message(env):
    env.respond = _respond(pr.Capture("delivered", "operator_proposal", rewritten=True))
    await _tap(env, idx=1)
    assert env.channel.notices == []
    assert [(e.who, e.text) for e in env.desk.log][-1] == ("specialist", sd.POSTED_PROPOSAL)


async def test_a_failed_call_with_rewritten_arguments_tells_before_the_refusal_at_the_desk(file_env):
    """Diff r1 (Terra): the failure hook's own capture, driven through the
    desk — the operator sees the tell, then the refusal."""
    env = file_env

    async def respond(call):
        hook = rb.make_failure_hook(env.build.contract_map, client_id="c1",
                                    store=rb.ReferenceStore(), owner=call.owner)
        await hook({"hook_event_name": "PostToolUseFailure", "tool_name": FILE,
                    "tool_input": {"batch": 2}, "error": "boom"}, "call-1", {})
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env, meta=_file_meta())
    assert env.channel.notices == [(OPERATOR, f"{LABEL} {sd.TELL_LINE}"),
                                   (OPERATOR, f"{LABEL} could not apply your tap (tool_error).")]
