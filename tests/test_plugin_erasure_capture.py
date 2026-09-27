"""#1046: the erase result convention, the capture watch, and the binding
check on an erase-marked turn.

The eraser returns ``{"erasure": "complete"|"incomplete", "report": str}``;
anything else is not complete. The result broker's hooks capture that result
for the exact (artifact, tool) an erase episode armed — before the ``safe``
early return — and, on an erase-marked turn, refuse to run an armed eraser
whose session binding carries another artifact than the tap named."""
from __future__ import annotations

import asyncio
import json

import pytest

import plugin_erasure as pe
import result_broker as rb
from test_result_broker import (
    ARTIFACT, DM, LIST, OTHER_ARTIFACT, _Origin, _deny_reason, _map, _post, _pre,
    _store,
)

ERASE = LIST        # a safe tool of the adopting probe plugin
RUN = "run-1"
QID = "question-1"
ERASE_TURN = {**DM, "synthetic": "plugin_erase", "plugin_erase_target": "finance",
              "plugin_erase_artifact": ARTIFACT, "plugin_erase_episode": RUN,
              "plugin_erase_subject": "plugin:probe", "plugin_erase_question": QID}


# --- parse_erase_result ------------------------------------------------------------

def test_parse_complete_and_incomplete():
    assert pe.parse_erase_result(json.dumps(
        {"erasure": "complete", "report": "All gone."})) == ("complete", "All gone.")
    assert pe.parse_erase_result(json.dumps(
        {"erasure": "incomplete", "report": "One bank kept its consent."})) == (
        "incomplete", "One bank kept its consent.")


@pytest.mark.parametrize("text", [
    "Done. Everything was erased.",                       # prose
    json.dumps({"report": "x"}),                          # missing key
    json.dumps({"erasure": "COMPLETE", "report": "x"}),   # another value
    json.dumps({"erasure": "complete", "report": 7}),     # report not text
    json.dumps(["complete"]),                             # not an object
    "",
])
def test_anything_else_is_unreadable(text):
    verdict, report = pe.parse_erase_result(text)
    assert verdict == "unreadable"
    assert report == text[:pe.MAX_REPORT_CHARS]


def test_none_is_unreadable_with_empty_report():
    assert pe.parse_erase_result(None) == ("unreadable", "")


def test_parse_bounds_report_and_rejects_non_object():
    long = "x" * (pe.MAX_REPORT_CHARS + 50)
    verdict, report = pe.parse_erase_result(json.dumps(
        {"erasure": "complete", "report": long}))
    assert verdict == "complete"
    assert report == long[:pe.MAX_REPORT_CHARS] + pe.TRUNCATED
    verdict, report = pe.parse_erase_result(long)
    assert verdict == "unreadable" and report.endswith(pe.TRUNCATED)


# --- the watch ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watch_resolves_once_for_its_exact_key():
    w = pe.EraseWatch()
    fut = w.arm(RUN, ERASE)
    assert w.is_armed(RUN, ERASE) and not w.is_armed("run-2", ERASE)
    assert w.resolve("run-2", ERASE, text="x") is False
    assert w.resolve(RUN, ERASE, text="t") is True
    assert (await fut) == {"text": "t", "error": None}
    assert w.resolve(RUN, ERASE, text="again") is False
    w.disarm(RUN, ERASE)
    assert not w.is_armed(RUN, ERASE)


# --- the hooks ---------------------------------------------------------------------

@pytest.fixture
def watch(monkeypatch):
    w = pe.EraseWatch()
    monkeypatch.setattr(pe, "WATCH", w)
    qs = pe.QuestionIds()
    qs._current["plugin:probe"] = QID          # ERASE_TURN's question is open
    monkeypatch.setattr(pe, "QUESTIONS", qs)
    return w


@pytest.mark.asyncio
async def test_result_hook_captures_an_armed_safe_eraser_on_an_erase_turn(watch):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    fut = watch.arm(RUN, ERASE)
    body = json.dumps({"erasure": "complete", "report": "ok"})
    with _Origin(ERASE_TURN):
        assert await hook(_post(ERASE, body), "t", {}) == {}     # passes unchanged
    assert fut.done() and fut.result() == {"text": body, "error": None}


@pytest.mark.asyncio
async def test_result_hook_ignores_the_tool_on_an_unmarked_turn(watch):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    fut = watch.arm(RUN, ERASE)
    with _Origin(DM):
        assert await hook(_post(ERASE, "{}"), "t", {}) == {}
    assert not fut.done()


@pytest.mark.asyncio
async def test_failure_hook_resolves_the_watch_as_an_error(watch):
    store, _ = _store()
    hook = rb.make_failure_hook(_map(), client_id="c1", store=store)
    fut = watch.arm(RUN, ERASE)
    with _Origin(ERASE_TURN):
        await hook({"hook_event_name": "PostToolUseFailure", "tool_name": ERASE,
                    "tool_input": {}, "error": "boom"}, "t", {})
    assert fut.result() == {"text": None, "error": "boom"}


@pytest.mark.asyncio
async def test_admission_denies_an_armed_eraser_bound_to_another_artifact(watch):
    """Design rev 2 (r1 Astra S1): a publish between the tap and the session
    build must not run another version's eraser — refused before execution."""
    store, _ = _store()
    hook = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    watch.arm(RUN, ERASE)                       # armed, but the tap named
    turn = {**ERASE_TURN, "plugin_erase_artifact": OTHER_ARTIFACT}  # another version
    with _Origin(turn):
        out = await hook(_pre(ERASE), "t", {})
    assert "erase" in _deny_reason(out)


@pytest.mark.asyncio
async def test_admission_allows_the_armed_eraser_on_its_own_artifact(watch):
    store, _ = _store()
    hook = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    watch.arm(RUN, ERASE)
    with _Origin(ERASE_TURN):
        assert await hook(_pre(ERASE), "t", {}) == {}
    with _Origin(DM):                           # unmarked turns are untouched
        assert await hook(_pre(ERASE), "t", {}) == {}


# --- the records -------------------------------------------------------------------

def test_records_take_complete_once_for_the_same_artifact():
    r = pe.ErasureRecords()
    r.put("plugin:p", ARTIFACT, "complete", "gone", "q1")
    assert r.take_complete("plugin:p", OTHER_ARTIFACT, "q1") is None
    assert r.take_complete("plugin:p", ARTIFACT, "q2") is None     # other question
    assert r.take_complete("plugin:p", ARTIFACT, "q1") == "gone"
    assert r.take_complete("plugin:p", ARTIFACT, "q1") is None
    r.put("plugin:p", ARTIFACT, "incomplete", "kept", "q1")
    assert r.take_complete("plugin:p", ARTIFACT, "q1") is None


def test_questions_replace_and_close():
    q = pe.QuestionIds()
    a = q.open("plugin:p")
    b = q.open("plugin:p")
    assert a != b and q.current("plugin:p") == b
    q.close("plugin:p", a)                       # a stale close changes nothing
    assert q.current("plugin:p") == b
    q.close("plugin:p", b)
    assert q.current("plugin:p") is None


@pytest.mark.asyncio
async def test_admission_denies_any_plugin_tool_on_an_erase_turn_nothing_waits_for(watch):
    """Diff r1 (Astra S1): an erase turn that runs after its episode timed out
    (the watch disarmed) must not run the eraser — on any artifact."""
    store, _ = _store()
    hook = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    with _Origin(ERASE_TURN):                   # nothing armed at all
        assert "erase" in _deny_reason(await hook(_pre(ERASE), "t", {}))
    turn = {**ERASE_TURN, "plugin_erase_artifact": OTHER_ARTIFACT}
    watch.arm(RUN, ERASE)                       # armed, but not this session's
    with _Origin(turn):
        assert "erase" in _deny_reason(await hook(_pre(ERASE), "t", {}))
    watch.arm(RUN, ERASE)                       # another tool of the plugin
    with _Origin(ERASE_TURN):
        from test_result_broker import FETCH
        assert "erase" in _deny_reason(await hook(_pre(FETCH), "t", {}))


@pytest.mark.asyncio
async def test_a_late_result_of_one_run_never_answers_another(watch):
    """Diff r4 (Astra S2): the watch is keyed by the erase run, so run A's late
    result cannot answer run B for the same artifact and tool, and a turn of A
    cannot run the eraser B is waiting for."""
    store, _ = _store()
    result = rb.make_result_hook(_map(), client_id="c1", store=store)
    admit = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    fut_b = watch.arm("run-B", ERASE)
    turn_a = {**ERASE_TURN, "plugin_erase_episode": "run-A"}
    body = json.dumps({"erasure": "complete", "report": "A"})
    with _Origin(turn_a):
        await result(_post(ERASE, body), "t", {})
        assert "erase" in _deny_reason(await admit(_pre(ERASE), "t", {}))
    assert not fut_b.done()
    with _Origin({**ERASE_TURN, "plugin_erase_episode": "run-B"}):
        assert await admit(_pre(ERASE), "t", {}) == {}
        await result(_post(ERASE, body), "t", {})
    assert fut_b.result()["text"] == body


@pytest.mark.asyncio
async def test_an_erase_turn_of_a_voided_question_runs_nothing(watch, monkeypatch):
    """Diff r5 (Astra S1, Terra S1; operator ruling: Cancel stops it): a queued
    erase turn whose question was replaced or cancelled before it ran is
    refused before the eraser executes."""
    qs = pe.QuestionIds()
    monkeypatch.setattr(pe, "QUESTIONS", qs)
    store, _ = _store()
    admit = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    q1 = qs.open("plugin:probe")
    watch.arm(RUN, ERASE)
    turn = {**ERASE_TURN, "plugin_erase_subject": "plugin:probe",
            "plugin_erase_question": q1}
    with _Origin(turn):
        assert await admit(_pre(ERASE), "t", {}) == {}           # still open
    qs.open("plugin:probe")                                      # asked again
    with _Origin(turn):
        assert "erase" in _deny_reason(await admit(_pre(ERASE), "t", {}))
    qs.close("plugin:probe")                                     # and cancelled
    with _Origin(turn):
        assert "erase" in _deny_reason(await admit(_pre(ERASE), "t", {}))
    with _Origin({**turn, "plugin_erase_question": ""}):         # no stamp
        assert "erase" in _deny_reason(await admit(_pre(ERASE), "t", {}))



@pytest.mark.asyncio
async def test_the_broker_admits_a_protected_eraser_only_on_its_own_run(watch):
    """Diff r6 (Astra S1): the Erase tap is the approval, enforced by the
    broker's own run checks — a protected eraser on its run's turn skips the
    authorization challenge; the same tool on an ordinary turn still meets it."""
    store, _ = _store()
    calls = []

    async def authz(input_data, tool_use_id, context):
        calls.append(tool_use_id)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": "challenge"}}
    protected = {ERASE: {"artifact_id": ARTIFACT, "summary": None}}
    admit = rb.make_plugin_admission_hook("finance", _map(), client_id="c1",
                                          store=store, authz_hook=authz,
                                          protected=protected)
    watch.arm(RUN, ERASE)
    with _Origin(ERASE_TURN):
        assert await admit(_pre(ERASE), "erase-turn", {}) == {}
    with _Origin(DM):
        assert "challenge" in _deny_reason(await admit(_pre(ERASE), "dm", {}))
    assert calls == ["dm"]
