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
ERASE_TURN = {**DM, "synthetic": "plugin_erase", "plugin_erase_target": "finance",
              "plugin_erase_artifact": ARTIFACT}


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
    fut = w.arm(ARTIFACT, ERASE)
    assert w.is_armed(ARTIFACT, ERASE) and w.is_armed_name(ERASE)
    assert not w.is_armed(OTHER_ARTIFACT, ERASE)
    assert w.resolve(OTHER_ARTIFACT, ERASE, text="x") is False
    assert w.resolve(ARTIFACT, ERASE, text="t") is True
    assert (await fut) == {"text": "t", "error": None}
    assert w.resolve(ARTIFACT, ERASE, text="again") is False
    w.disarm(ARTIFACT, ERASE)
    assert not w.is_armed_name(ERASE)


# --- the hooks ---------------------------------------------------------------------

@pytest.fixture
def watch(monkeypatch):
    w = pe.EraseWatch()
    monkeypatch.setattr(pe, "WATCH", w)
    return w


@pytest.mark.asyncio
async def test_result_hook_captures_an_armed_safe_eraser_on_an_erase_turn(watch):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    fut = watch.arm(ARTIFACT, ERASE)
    body = json.dumps({"erasure": "complete", "report": "ok"})
    with _Origin(ERASE_TURN):
        assert await hook(_post(ERASE, body), "t", {}) == {}     # passes unchanged
    assert fut.done() and fut.result() == {"text": body, "error": None}


@pytest.mark.asyncio
async def test_result_hook_ignores_the_tool_on_an_unmarked_turn(watch):
    store, _ = _store()
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    fut = watch.arm(ARTIFACT, ERASE)
    with _Origin(DM):
        assert await hook(_post(ERASE, "{}"), "t", {}) == {}
    assert not fut.done()


@pytest.mark.asyncio
async def test_failure_hook_resolves_the_watch_as_an_error(watch):
    store, _ = _store()
    hook = rb.make_failure_hook(_map(), client_id="c1", store=store)
    fut = watch.arm(ARTIFACT, ERASE)
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
    watch.arm(OTHER_ARTIFACT, ERASE)            # the tap named another version
    turn = {**ERASE_TURN, "plugin_erase_artifact": OTHER_ARTIFACT}
    with _Origin(turn):
        out = await hook(_pre(ERASE), "t", {})
    assert "erase" in _deny_reason(out)


@pytest.mark.asyncio
async def test_admission_allows_the_armed_eraser_on_its_own_artifact(watch):
    store, _ = _store()
    hook = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    watch.arm(ARTIFACT, ERASE)
    with _Origin(ERASE_TURN):
        assert await hook(_pre(ERASE), "t", {}) == {}
    with _Origin(DM):                           # unmarked turns are untouched
        assert await hook(_pre(ERASE), "t", {}) == {}


# --- the records -------------------------------------------------------------------

def test_records_take_complete_once_for_the_same_artifact():
    r = pe.ErasureRecords()
    r.put("plugin:p", ARTIFACT, "complete", "gone")
    assert r.take_complete("plugin:p", OTHER_ARTIFACT) is None
    assert r.take_complete("plugin:p", ARTIFACT) == "gone"
    assert r.take_complete("plugin:p", ARTIFACT) is None
    r.put("plugin:p", ARTIFACT, "incomplete", "kept")
    assert r.take_complete("plugin:p", ARTIFACT) is None
