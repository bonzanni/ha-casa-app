"""S7a §3.8 (INV-PLUG-047): an ``operator_file`` deposit may carry the name the
operator sees, separate from the staged file's basename — only when its tool's
result-contract entry declares ``"filename": true`` (a member a Casa without
this slice refuses at load), judged at deposit by ``send_media``'s own
predicate before any reference is minted, and never deciding which staged file
is claimed, read or removed.
"""
from __future__ import annotations

import json

import pytest

import result_broker as rb
from plugin_grants import ToolContract
from plugin_store import StoreError, manifest_result_contract
from test_operator_file_delivery import (  # noqa: F401  (fixtures and helpers)
    ARTIFACT, DATA, EXPORT, LABEL, PLUGIN, SLOT, _identity, _map, _outbox_is_empty,
    _post, _put, _replacement, _store, names, outbox, recorder,
)
from plugin_grants import ResultContractMap


def _entry(*, filename: bool) -> ToolContract:
    return ToolContract(ARTIFACT, PLUGIN, "capability", (SLOT,), {},
                        {SLOT: "operator_file"}, filename=filename)


def _named_map(*, filename=True) -> ResultContractMap:
    base = _map()
    return ResultContractMap(tools={EXPORT: _entry(filename=filename)}, plugins=base.plugins)


def _open(store, *, filename: bool, call="call-1"):
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=EXPORT,
                    tool_use_id=call, identity=_identity(), provides=(SLOT,),
                    delivers={SLOT: "operator_file"}, entry=_entry(filename=filename))


# --- the declaration ----------------------------------------------------------

def _manifest(tool: dict) -> dict:
    return {"name": PLUGIN, "casa": {"resultContract": {"version": 1, "tools": {"export": tool}}}}


FILE_TOOL = {"result": "capability", "provides": [SLOT], "delivers": {SLOT: "operator_file"}}


def test_the_declaration_is_accepted_on_a_tool_delivering_a_file():
    out = manifest_result_contract(_manifest({**FILE_TOOL, "filename": True}))
    assert out["tools"]["export"]["filename"] is True
    assert "filename" not in manifest_result_contract(_manifest(FILE_TOOL))["tools"]["export"]


@pytest.mark.parametrize("tool", [
    {**FILE_TOOL, "filename": False},
    {**FILE_TOOL, "filename": "yes"},
    {**FILE_TOOL, "filename": 1},
    {"result": "capability", "provides": [SLOT], "delivers": {SLOT: "operator_message"},
     "filename": True},
    {"result": "capability", "provides": [SLOT], "filename": True},
    {"result": "safe", "filename": True},
], ids=["false", "string", "int", "message-slot", "no-delivery", "safe"])
def test_the_declaration_is_refused_anywhere_else(tool):
    with pytest.raises(StoreError) as exc:
        manifest_result_contract(_manifest(tool))
    assert exc.value.reason_code == "result_contract_invalid"


# --- the deposit --------------------------------------------------------------

def test_a_declared_tool_keeps_a_valid_name(names):
    store, _ = _store()
    _open(store, filename=True)
    ref, err = store.deposit(client_id="c1", slot=SLOT, value="/x/stage-91.csv",
                             kind="text", filename="Q3 export.csv")
    assert err is None
    assert store.take_for_delivery(ref)[6] == "Q3 export.csv"


@pytest.mark.parametrize("filename", ["a/b.csv", "../x.csv", "x.exe", "bad\x01.csv",
                                      "x" * 256 + ".csv", 7, ["x.csv"]],
                         ids=["slash", "dotdot", "extension", "control", "too-long",
                              "int", "list"])
def test_a_declared_tool_refuses_a_bad_name_before_minting(filename, names):
    store, _ = _store()
    _open(store, filename=True)
    assert store.deposit(client_id="c1", slot=SLOT, value="/x/stage.csv", kind="text",
                         filename=filename) == (None, "bad_filename")
    assert store._refs == {}


def test_an_undeclared_tool_refuses_a_name(names):
    store, _ = _store()
    _open(store, filename=False)
    assert store.deposit(client_id="c1", slot=SLOT, value="/x/stage.csv", kind="text",
                         filename="Q3.csv") == (None, "filename_not_declared")
    assert store._refs == {}


@pytest.mark.parametrize("filename", [None, ""])
@pytest.mark.parametrize("declared", [True, False])
def test_no_name_is_today(filename, declared, names):
    store, _ = _store()
    _open(store, filename=declared)
    ref, err = store.deposit(client_id="c1", slot=SLOT, value="/x/stage.csv", kind="text",
                             filename=filename)
    assert err is None
    assert store.take_for_delivery(ref)[6] == ""


def test_the_member_is_ignored_on_any_other_slot(names):
    store, _ = _store()
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=EXPORT,
                    tool_use_id="call-1", identity=_identity(), provides=(SLOT,),
                    delivers={SLOT: "operator_message"})
    ref, err = store.deposit(client_id="c1", slot=SLOT, value="hello", filename="a/b")
    assert err is None and rb.is_reference(ref)


@pytest.mark.asyncio
async def test_the_deposit_route_passes_the_member_through(names):
    store, _ = _store()
    _open(store, filename=True)
    handler = rb.build_broker_deposit_handler(store)

    class _Req:
        async def json(self):
            return {"client": "c1", "slot": SLOT, "value": "/x/stage.csv", "kind": "text",
                    "filename": "../evil.csv"}
    resp = await handler(_Req())
    assert json.loads(resp.text) == {"error": "bad_filename"}


# --- the delivery -------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_declared_name_is_what_the_operator_receives(recorder, names, outbox):
    path = _put(outbox, "stage-7f3a.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_named_map(), client_id="c1", store=store)
    _open(store, filename=True)
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text",
                             filename="Q3 export.csv")
    assert err is None
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries == [(42, DATA, "text", "Q3 export.csv", LABEL)]
    assert json.loads(_replacement(out))["casa_delivery"]["status"] == "delivered"
    assert _outbox_is_empty(outbox)


@pytest.mark.asyncio
async def test_no_name_still_delivers_under_the_staged_basename(recorder, names, outbox):
    path = _put(outbox, "report.csv")
    store, _ = _store()
    hook = rb.make_result_hook(_named_map(), client_id="c1", store=store)
    _open(store, filename=True)
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text")
    await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries[0][3] == "report.csv"


@pytest.mark.asyncio
async def test_the_name_never_decides_which_staged_file_is_sent(recorder, names, outbox):
    """Two sends staging distinct files under one delivered name each deliver
    their own bytes and consume only their own file."""
    first, second = _put(outbox, "stage-a.csv", b"a,1\n"), _put(outbox, "stage-b.csv", b"b,2\n")
    store, _ = _store()
    for call, path in (("call-1", first), ("call-2", second)):
        hook = rb.make_result_hook(_named_map(), client_id="c1", store=store)
        _open(store, filename=True, call=call)
        ref, err = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text",
                                 filename="Q3.csv")
        assert err is None
        await hook(_post(json.dumps({SLOT: ref})), call, {})
    assert [(d[1], d[3]) for d in recorder.deliveries] == [(b"a,1\n", "Q3.csv"),
                                                          (b"b,2\n", "Q3.csv")]
    assert _outbox_is_empty(outbox)


@pytest.mark.asyncio
async def test_a_staged_file_whose_own_name_is_invalid_is_still_withheld(recorder, names, outbox):
    """The staged path is judged exactly as today, whatever the delivered name."""
    path = _put(outbox, "stage.exe", DATA)
    store, _ = _store()
    hook = rb.make_result_hook(_named_map(), client_id="c1", store=store)
    _open(store, filename=True)
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=path, kind="text",
                             filename="Q3.csv")
    assert err is None
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert recorder.deliveries == []
    assert json.loads(_replacement(out))["casa_result_withheld"] is True
