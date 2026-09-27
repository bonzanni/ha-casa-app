"""#1046: the erase episode — Casa dispatches a turn that runs the plugin's
eraser, pre-authorizes a protected eraser for exactly that call, waits for the
captured result, and records what it reported. Nothing is removed here; the
record is what the finishing uninstall call consumes."""
from __future__ import annotations

import asyncio
import json

import pytest

import authz_grants
import plugin_erasure as pe
from authz_grants import GrantKey, GrantStore, canonical_args_hash
from test_authz_grants_setup_identity import live_operator  # noqa: F401

ART = "a" * 64
TOOL = "mcp__plugin_bank-feed_bank-feed__delete_all_data"
OP = (42, 42)       # (chat_id, user_id)
Q = "question-1"
SUBJ = "specialist:finance"


def _spec(targets=("specialist:finance",), protected=True, name="finance.bank-feed"):
    return pe.EraseSpec(name=name, artifact_id=ART, targets=tuple(targets),
                        tool_names=(TOOL,), protected=protected)


@pytest.fixture
def fresh(monkeypatch):
    watch, records, grants = pe.EraseWatch(), pe.ErasureRecords(), GrantStore()
    monkeypatch.setattr(pe, "WATCH", watch)
    monkeypatch.setattr(pe, "RECORDS", records)
    monkeypatch.setattr(authz_grants, "GRANTS", grants)
    return watch, records, grants


def _dispatcher(result_text=None, error=None, accept=True, end_turn=True):
    """A dispatch seam that, like the real turn, resolves the watch from the
    eraser's result and then ends the turn."""
    sent: list[tuple[str, str, dict]] = []

    async def dispatch(role, text, context):
        sent.append((role, text, dict(context)))
        if not accept:
            return False

        async def turn():
            await asyncio.sleep(0)
            if result_text is not None or error is not None:
                pe.WATCH.resolve(context["plugin_erase_episode"], TOOL,
                                 text=result_text, error=error)
            if end_turn:
                pe.turn_ended(context)
        asyncio.get_running_loop().create_task(turn())
        return True
    return dispatch, sent


@pytest.mark.asyncio
async def test_a_courier_turn_carries_the_stamps_and_a_complete_result_is_recorded(fresh):
    _watch, records, grants = fresh
    dispatch, sent = _dispatcher(json.dumps({"erasure": "complete", "report": "gone"}))
    pe.configure(dispatch=dispatch)
    [out] = await pe.run_erase_episode([_spec()], operator=OP, question=Q, subject=SUBJ)
    assert out == pe.ErasureOutcome("finance.bank-feed", ART, "complete", "gone")
    role, text, ctx = sent[0]
    assert role == "assistant"                                  # the courier
    assert "Delegate to the specialist 'finance'" in text and TOOL in text
    run = ctx.pop("plugin_erase_episode")
    assert isinstance(run, str) and len(run) == 32
    assert ctx == {"synthetic": "plugin_erase", "plugin_erase_target": "finance",
                   "plugin_erase_artifact": ART, "plugin_erase_subject": SUBJ,
                   "plugin_erase_question": Q}
    assert records.take_complete("plugin:finance.bank-feed", ART, "other-q") is None
    assert records.take_complete("plugin:finance.bank-feed", ART, Q) == "gone"


@pytest.mark.asyncio
async def test_a_protected_eraser_is_pre_authorized_for_exactly_its_no_arg_call(fresh):
    _w, _r, grants = fresh
    seen = {}

    async def dispatch(role, text, context):
        key = GrantKey(operator_id=42, chat_id=42, enforcement_role="finance",
                       artifact_id=ART, tool_name=TOOL,
                       args_hash=canonical_args_hash({}), engagement_id="")
        other_args = GrantKey(**{**key.__dict__, "args_hash": canonical_args_hash({"x": 1})})
        seen["other"] = grants.consume(other_args)
        seen["exact"] = grants.consume(key)
        seen["again"] = grants.consume(key)
        pe.turn_ended(context)
        return True
    pe.configure(dispatch=dispatch)
    await pe.run_erase_episode([_spec()], operator=OP, question=Q, subject=SUBJ)
    assert seen == {"other": False, "exact": True, "again": False}


@pytest.mark.asyncio
async def test_an_unprotected_eraser_mints_no_grant(fresh):
    _w, _r, grants = fresh
    dispatch, _ = _dispatcher(json.dumps({"erasure": "complete", "report": "ok"}))
    pe.configure(dispatch=dispatch)
    await pe.run_erase_episode([_spec(targets=("resident:assistant",),
                                      protected=False)], operator=OP, question=Q, subject=SUBJ)
    key = GrantKey(42, 42, "assistant", ART, TOOL, canonical_args_hash({}), "")
    assert grants.consume(key) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kw,verdict,report", [
    ({"result_text": json.dumps({"erasure": "incomplete", "report": "bank kept it"})},
     "incomplete", "bank kept it"),
    ({"result_text": "Done, all erased."}, "unreadable", "Done, all erased."),
    ({"error": "boom"}, "error", "boom"),
    ({}, "no_call", pe.NO_CALL_REPORT),
    ({"accept": False}, "not_dispatched", pe.NOT_DISPATCHED_REPORT),
])
async def test_anything_but_complete_records_nothing_removable(fresh, kw, verdict, report):
    _w, records, _g = fresh
    dispatch, _ = _dispatcher(**kw)
    pe.configure(dispatch=dispatch)
    [out] = await pe.run_erase_episode([_spec()], operator=OP, question=Q, subject=SUBJ)
    assert (out.verdict, out.report) == (verdict, report)
    assert records.take_complete("plugin:finance.bank-feed", ART, Q) is None


@pytest.mark.asyncio
async def test_a_turn_that_never_reports_times_out(fresh, monkeypatch):
    monkeypatch.setattr(pe, "ERASE_WAIT_S", 0.05)
    dispatch, _ = _dispatcher(end_turn=False)
    pe.configure(dispatch=dispatch)
    [out] = await pe.run_erase_episode([_spec()], operator=OP, question=Q, subject=SUBJ)
    assert (out.verdict, out.report) == ("timed_out", pe.TIMED_OUT_REPORT)
    assert pe.WATCH._futures == {}                              # disarmed


@pytest.mark.asyncio
async def test_a_plugin_with_no_target_is_not_dispatched(fresh):
    dispatch, sent = _dispatcher()
    pe.configure(dispatch=dispatch)
    [out] = await pe.run_erase_episode([_spec(targets=())], operator=OP, question=Q, subject=SUBJ)
    assert out.verdict == "not_dispatched" and sent == []


@pytest.mark.asyncio
async def test_several_plugins_run_in_turn_and_stop_at_the_first_not_complete(fresh):
    results = iter([json.dumps({"erasure": "complete", "report": "one"}),
                    json.dumps({"erasure": "incomplete", "report": "two"})])
    sent = []

    async def dispatch(role, text, context):
        sent.append(context["plugin_erase_artifact"])
        pe.WATCH.resolve(context["plugin_erase_episode"],
                         TOOL if context["plugin_erase_artifact"] == ART else "t2",
                         text=next(results))
        pe.turn_ended(context)
        return True
    pe.configure(dispatch=dispatch)
    b = pe.EraseSpec("finance.b", "b" * 64, ("specialist:finance",), ("t2",), False)
    c = pe.EraseSpec("finance.c", "c" * 64, ("specialist:finance",), ("t3",), False)
    outs = await pe.run_erase_episode([_spec(), b, c], operator=OP, question=Q, subject=SUBJ)
    assert [o.verdict for o in outs] == ["complete", "incomplete"]
    assert sent == [ART, "b" * 64]                              # c never ran


def test_turn_ended_ignores_other_turns(fresh):
    assert pe.turn_ended({"synthetic": "plugin_setup"}) is None
    assert pe.turn_ended({}) is None


@pytest.mark.asyncio
async def test_the_real_agent_turn_end_answers_an_eraser_it_never_called(
        tmp_path, fresh, live_operator):
    """Through the real ``Agent._process``: an erase-marked turn that ends
    without the eraser's result resolves the armed key as "no call" from the
    turn's finally, so the episode does not wait out its bound."""
    from test_plugin_erase_identity import _erase_msg, _run
    watch, _r, _g = fresh
    fut = watch.arm("run-1", TOOL)
    await _run(tmp_path, _erase_msg(context_extra={"plugin_erase_episode": "run-1"}))
    assert fut.done() and fut.result().get("no_call") is True


@pytest.mark.asyncio
async def test_an_eraser_with_no_server_is_not_dispatched(fresh):
    dispatch, sent = _dispatcher()
    pe.configure(dispatch=dispatch)
    spec = pe.EraseSpec("p", ART, ("resident:assistant",), (), False, tool="erase_all")
    [out] = await pe.run_erase_episode([spec], operator=OP, question=Q, subject=SUBJ)
    assert out.verdict == "not_dispatched" and sent == []



@pytest.mark.asyncio
async def test_a_pregrant_the_run_never_used_is_revoked_when_it_ends(fresh):
    """Diff r5 (Terra): a protected eraser's pre-authorization lives only as
    long as its run — a run that ended without the call leaves no grant a later
    ordinary call could consume without the operator's challenge."""
    _w, _r, grants = fresh
    dispatch, _ = _dispatcher()                       # the turn ends, no call
    pe.configure(dispatch=dispatch)
    await pe.run_erase_episode([_spec()], operator=OP, question=Q, subject=SUBJ)
    key = GrantKey(42, 42, "finance", ART, TOOL, canonical_args_hash({}), "")
    assert grants.consume(key) is False
