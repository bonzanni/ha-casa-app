"""#1003: a released setup obligation is consumed by evidence that its setup
tool ran — in a session built on its artifact, after its release — in ANY turn
of the executing resident, not only the Casa-dispatched one; and once settled
that way nothing re-dispatches or reopens it.

Measured on production 2026-09-16: the dispatched turn ran toolless (cold
session, MCP server still connecting), the row went back to ``pending``; the
assistant's next ordinary turn ran the setup tool successfully; the row stayed
``pending`` and the next reload would have asked for the setup again.
"""
from __future__ import annotations

import math

import pytest

import plugin_setup_episodes as pse
from test_plugin_setup_episodes import (  # noqa: F401 — fixtures
    _COURIER, _courier_dispatched, _decide, _dispatched, _drain_pending,
    _prompt, wired,
)

pytestmark = pytest.mark.asyncio

_NS = "mcp__plugin_elevenlabs_elevenlabs__setup_elevenlabs_voicemail"
_BINDING = {"elevenlabs": "art-1"}


async def _released_pending(wired):
    """A released obligation returned to ``pending`` by a toolless dispatched
    turn — the production shape."""
    ep = await _dispatched(wired)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted=set(),
        available_tools={"Read"})
    row = pse.episodes()[0]
    assert row["status"] == "pending" and row["gate"] == "released"
    return row


def _after(row):
    return row["released_ts"] + 1.0


# --- the release stamp ------------------------------------------------------

async def test_release_stamps_a_finite_released_ts(wired):
    _prompt()
    await _decide()
    row = pse.episodes()[0]
    assert row["gate"] == "released"
    assert isinstance(row["released_ts"], float)
    assert math.isfinite(row["released_ts"])


async def test_release_captures_the_composed_setup_tool(wired):
    """The prefilter is a set of names carried by the rows: the release
    captures the same name the dispatch composes."""
    _prompt()
    await _decide()
    assert pse.episodes()[0]["expected_tool"] == _NS
    assert pse._WATCH[1] == frozenset({_NS})


async def test_release_without_a_composable_tool_leaves_the_row_unwatched(wired):
    wired["entry"] = dict(wired["entry"], setup_tool=None)
    _prompt()
    await _decide()
    assert "expected_tool" not in pse.episodes()[0]
    assert pse._WATCH[1] == frozenset()


# --- settlement from an ordinary turn's tool evidence -----------------------

async def test_evidence_after_release_settles_the_pending_row(wired):
    row = await _released_pending(wired)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row), binding=_BINDING)
    row = pse.episodes()[0]
    assert row["status"] == "dispatched"
    assert row["settled_by"] == "turn_evidence"
    assert row["expected_tool"] == _NS
    assert row["last_error"] == ""
    assert math.isfinite(row["settled_ts"])


async def test_evidence_settles_a_dispatched_row_too(wired):
    """The Casa-dispatched turn is still in flight (row ``dispatched``) when an
    ordinary turn runs the tool: the obligation is met either way."""
    ep = await _dispatched(wired)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(ep), binding=_BINDING)
    assert pse.episodes()[0]["settled_by"] == "turn_evidence"


async def test_a_second_evidence_is_a_no_op(wired, monkeypatch):
    row = await _released_pending(wired)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row), binding=_BINDING)
    first = dict(pse.episodes()[0])
    # on the watch-unknown path, so the INNER prior-settlement guard decides
    # (diff round 2, Terra S2) — the watch alone would already skip it
    monkeypatch.setattr(pse, "_WATCH", None)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row) + 5, binding=_BINDING)
    assert pse.episodes()[0] == first


@pytest.mark.parametrize("case", [
    "before_release", "unreleased_row", "other_role", "other_tool",
    "tool_empty", "binding_absent", "binding_other_artifact",
    "binding_not_a_dict", "registry_superseded", "registry_unavailable",
    "two_grants", "no_setup_tool", "row_refused", "row_artifact_missing",
    "invoked_at_nan", "invoked_at_inf", "invoked_at_none",
    "released_ts_missing", "released_ts_nan",
])
async def test_evidence_that_does_not_prove_the_run_is_ignored(
        wired, monkeypatch, case):
    """Every case is built so that ONLY the predicate it names rejects the
    evidence (diff round 1, Terra S2): an awaiting row carries a finite
    release stamp so the gate check decides, a missing setup tool is paired
    with the name that check would otherwise derive, and so on. The store's
    in-memory watch is a PRE-filter duplicating several of these predicates;
    each case runs on the watch-unknown path (first evidence in a process
    before any read — a real path) so the inner predicate is the one that
    decides."""
    if case == "unreleased_row":
        _prompt()                       # round open, verdict not sealed
        row = pse.episodes()[0]
        assert row["gate"] != "released"
        # a stamp the time fence accepts, so the GATE predicate is decisive
        pse._update_episode(row["id"], released_ts=pse._now() - 10)
        row = pse.episodes()[0]
        invoked_at = pse._now()
    else:
        row = await _released_pending(wired)
        invoked_at = _after(row)
    role, tool, binding = "assistant", _NS, dict(_BINDING)
    if case == "before_release":
        invoked_at = row["released_ts"] - 0.001
    elif case == "tool_empty":
        tool = ""
    elif case == "binding_not_a_dict":
        class _MappingLike:                 # answers .get, is not a dict
            def get(self, key, default=None):
                return "art-1"
        binding = _MappingLike()
    elif case == "row_refused":
        # #1052: `failed`/`stale` rows are clearable now; `refused` never is
        pse._update_episode(row["id"], status="refused")
    elif case == "row_artifact_missing":
        data = pse._load()
        for e in data["episodes"]:
            e.pop("artifact_id", None)
        pse._save(data)
        binding = {"elevenlabs": None}
        wired["entry"] = dict(wired["entry"], artifact_id=None)
    elif case == "other_role":
        role = "butler"
    elif case == "other_tool":
        tool = "mcp__plugin_elevenlabs_elevenlabs__list_voices"
    elif case == "binding_absent":
        binding = {}
    elif case == "binding_other_artifact":
        binding = {"elevenlabs": "art-0"}
    elif case == "registry_superseded":
        wired["entry"] = dict(wired["entry"], artifact_id="art-2")
    elif case == "registry_unavailable":
        wired["entry"] = None
    elif case == "two_grants":
        wired["entry"] = dict(wired["entry"], granted_tools=[
            "mcp__plugin_elevenlabs_elevenlabs", "mcp__plugin_elevenlabs_x"])
    elif case == "no_setup_tool":
        wired["entry"] = dict(wired["entry"], setup_tool=None)
        tool = "mcp__plugin_elevenlabs_elevenlabs__None"   # the derived name
    elif case == "invoked_at_nan":
        invoked_at = float("nan")
    elif case == "invoked_at_inf":
        invoked_at = float("inf")
    elif case == "invoked_at_none":
        invoked_at = None
    elif case == "released_ts_missing":
        pse._update_episode(row["id"], released_ts=None)
        data = pse._load()
        for e in data["episodes"]:
            e.pop("released_ts", None)
        pse._save(data)
    elif case == "released_ts_nan":
        pse._update_episode(row["id"], released_ts=float("nan"))
    before = pse.episodes()[0]["status"]
    monkeypatch.setattr(pse, "_WATCH", None)
    pse.settle_from_tool_evidence(
        role=role, tool=tool, invoked_at=invoked_at, binding=binding)
    row = pse.episodes()[0]
    assert "settled_by" not in row
    assert row["status"] == before


# --- the common path does no I/O ---------------------------------------------

def _count_loads(monkeypatch):
    calls = []
    real = pse._load

    def counting():
        calls.append(1)
        return real()
    monkeypatch.setattr(pse, "_load", counting)
    return calls


async def test_no_store_read_when_no_released_obligation_exists(wired, monkeypatch):
    """The path runs in on_message for every successful plugin-tool result:
    with the store read once this process (any read publishes the watch) and
    no released unsettled row, evidence touches no file."""
    _prompt()                                   # a row exists, not released
    assert pse.episodes()                       # one read: the watch is known
    loads = _count_loads(monkeypatch)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=pse._now(), binding=_BINDING)
    pse.settle_from_tool_evidence(
        role="assistant", tool="mcp__plugin_other_other__x",
        invoked_at=pse._now(), binding=_BINDING)
    assert loads == []


async def test_no_store_read_for_another_plugins_tool(wired, monkeypatch):
    row = await _released_pending(wired)        # a candidate exists
    loads = _count_loads(monkeypatch)
    pse.settle_from_tool_evidence(
        role="assistant", tool="mcp__plugin_other_other__setup_other",
        invoked_at=_after(row), binding=_BINDING)
    assert loads == []
    assert "settled_by" not in pse.episodes()[0]


async def test_the_prefilter_never_calls_the_resolver(wired, monkeypatch):
    """Diff round 2 (both reviewers): the production resolver reads manifests
    on the loop, so the common path may not consult it at all — not for an
    unrelated tool, and not for the same plugin's other tools."""
    row = await _released_pending(wired)        # a candidate exists
    def boom(_plugin):
        raise AssertionError("resolver consulted on the common path")
    monkeypatch.setattr(pse, "_resolve_registry_entry", boom)
    loads = _count_loads(monkeypatch)
    for tool in ("mcp__plugin_other_other__setup_other",
                 "mcp__plugin_elevenlabs_elevenlabs__list_voices", "Read"):
        pse.settle_from_tool_evidence(
            role="assistant", tool=tool, invoked_at=_after(row),
            binding=_BINDING)
    assert loads == []


async def test_a_background_thread_read_does_not_republish_the_watch(wired):
    """Diff round 2 (Astra S1): a status/health reader on a worker thread
    holds a snapshot that may predate a release the loop has since written;
    its read must not overwrite the loop's watch."""
    import asyncio
    row = await _released_pending(wired)
    assert pse._WATCH[1] == frozenset({_NS})
    # a stale snapshot: what a thread read before the release would hold
    data = pse._load()
    for e in data["episodes"]:
        e["gate"] = "awaiting_verdict"
    await asyncio.to_thread(pse._publish_watch, data)
    assert pse._WATCH[1] == frozenset({_NS})    # unchanged by the thread
    read = await asyncio.to_thread(pse._read_store)  # a real thread read
    assert read.data["episodes"][0]["id"] == row["id"]
    assert pse._WATCH[1] == frozenset({_NS})
    pse._publish_watch(data)                    # the loop may publish
    assert pse._WATCH[1] == frozenset()


async def test_the_watch_follows_release_and_settlement(wired, monkeypatch):
    row = await _released_pending(wired)
    loads = _count_loads(monkeypatch)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row), binding=_BINDING)
    assert loads == [1]                         # the candidate path read once
    assert pse.episodes()[0]["settled_by"] == "turn_evidence"
    loads.clear()
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row) + 1,
        binding=_BINDING)
    assert loads == []                          # settled: no longer watched


async def test_a_save_publishes_the_watch_without_a_read(wired):
    """A release lands through `_save`; the very next evidence, with no read
    in between, must see the candidate — so the save itself republishes."""
    _prompt()
    await _decide()
    data = pse._load()
    pse._publish_watch({"episodes": []})        # a stale, empty watch
    assert pse._WATCH[1] == frozenset()
    pse._save(data)                             # the save alone refreshes it
    assert _NS in pse._WATCH[1]


async def test_an_unknown_watch_reads_once_then_knows(wired, monkeypatch):
    _prompt()
    monkeypatch.setattr(pse, "_WATCH", None)    # nothing read this process
    loads = _count_loads(monkeypatch)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=pse._now(), binding=_BINDING)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=pse._now(), binding=_BINDING)
    assert loads == [1]


async def test_specialist_target_obligations_are_never_evidence_settled(wired):
    wired["entry"] = dict(wired["entry"], targets=["specialist:finance"])
    _prompt()
    await _decide()
    row = pse.episodes()[0]
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row), binding=_BINDING)
    assert "settled_by" not in pse.episodes()[0]


async def test_settlement_never_raises_on_an_unreadable_store(wired, monkeypatch):
    def boom():
        raise OSError("store gone")
    monkeypatch.setattr(pse, "_load", boom)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=pse._now(), binding=_BINDING)


# --- a settled row is authoritative ----------------------------------------

async def test_a_later_negative_dispatch_report_cannot_reopen_a_settled_row(wired):
    """The older synthetic turn finishes toolless AFTER the ordinary turn ran
    the tool: its report is a no-op."""
    ep = await _dispatched(wired)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(ep), binding=_BINDING)
    pse.report_dispatch_outcome(
        ep["id"], tools_used_ok=set(), tools_attempted=set(),
        available_tools={"Read"})
    row = pse.episodes()[0]
    assert row["status"] == "dispatched"
    assert row["settled_by"] == "turn_evidence"
    assert row.get("execution_retries", 0) == 0


async def test_the_worker_does_not_dispatch_a_row_settled_after_its_snapshot(wired):
    """M5: the worker's pending snapshot predates the settlement; the pre-send
    re-read refuses the dispatch."""
    row = await _released_pending(wired)
    snapshot = dict(row)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row), binding=_BINDING)
    sent_before = len(wired["dispatches"])
    await pse._run_episode(snapshot)
    assert len(wired["dispatches"]) == sent_before
    assert pse.episodes()[0]["settled_by"] == "turn_evidence"


async def test_a_settled_row_is_not_redispatched_by_a_later_pass(wired):
    row = await _released_pending(wired)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row), binding=_BINDING)
    sent_before = len(wired["dispatches"])
    await _drain_pending(wired)
    assert len(wired["dispatches"]) == sent_before


async def test_dispatch_still_owed_reads_only_a_settled_row_as_not_owed(wired):
    ep = await _dispatched(wired)
    assert pse.dispatch_still_owed(ep["id"]) is True
    assert pse.dispatch_still_owed("no-such-row") is True
    assert pse.dispatch_still_owed("") is True
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(ep), binding=_BINDING)
    assert pse.dispatch_still_owed(ep["id"]) is False


async def test_dispatch_still_owed_reads_an_unreadable_store_as_owed(
        wired, monkeypatch):
    def boom():
        raise OSError("store gone")
    monkeypatch.setattr(pse, "_load", boom)
    assert pse.dispatch_still_owed("any") is True


async def test_removal_keeps_the_settlement_mark(wired):
    row = await _released_pending(wired)
    pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row), binding=_BINDING)
    pse.retire_for_removed("elevenlabs")
    row = pse.episodes()[0]
    assert row["status"] == "stale"
    assert row["settled_by"] == "turn_evidence"


# --- the operator-facing sentence -------------------------------------------

async def test_status_sentence_reports_an_evidence_settled_row_as_ran():
    from tools import _episode_sentence
    line = _episode_sentence({
        "plugin": "gmail", "status": "dispatched",
        "settled_by": "turn_evidence", "execution_retries": 1})
    assert "setup ran" in line
    assert "running" not in line


async def test_status_sentence_keeps_the_settlement_after_removal():
    from tools import _episode_sentence
    line = _episode_sentence({
        "plugin": "gmail", "status": "stale", "settled_by": "turn_evidence",
        "last_error": "plugin removed", "removed_ts": 1.0})
    assert "setup ran" in line
    assert "stopped partway" not in line
    assert "removed since" in line


async def test_status_sentence_plain_dispatched_still_reads_running():
    from tools import _episode_sentence
    assert "setup is running" in _episode_sentence(
        {"plugin": "gmail", "status": "dispatched"})


# --- #1052: a later successful run clears a failed obligation ---------------
#
# Observed on v0.328.0 (#1051 round 1): the courier's retries were spent while
# the install was still wiring the delegation, the row went `failed`, the
# operator ran the setup by hand through the assistant and it succeeded — and
# plugin health still announced "could not finish setting up" afterwards.

_FIN_BINDING = {"elevenlabs": "art-1"}


async def _failed_courier_row(wired):
    """The production shape: a specialist-target row whose courier retries
    were exhausted."""
    ep = await _courier_dispatched(wired)
    for _ in range(3):
        pse.report_dispatch_outcome(
            ep["id"], tools_used_ok=set(), tools_attempted={_COURIER},
            available_tools={_COURIER})
        await _drain_pending(wired)
    row = pse.episodes()[0]
    assert row["status"] == "failed"
    assert [i["kind"] for i in pse.health_issues()] == ["setup_episode_failed"]
    return row


async def _failed_resident_row(wired):
    ep = await _dispatched(wired)
    for _ in range(3):
        pse.report_dispatch_outcome(
            ep["id"], tools_used_ok=set(), tools_attempted=set(),
            available_tools={"Read"})
        await _drain_pending(wired)
    row = pse.episodes()[0]
    assert row["status"] == "failed"
    return row


async def test_the_specialists_own_run_clears_a_failed_courier_row(wired):
    row = await _failed_courier_row(wired)
    cleared = pse.settle_from_tool_evidence(
        role="finance", tool=_NS, invoked_at=pse._now(),
        binding=_FIN_BINDING, delegated=True)
    assert cleared is True
    row = pse.episodes()[0]
    assert row["status"] == "dispatched"
    assert row["settled_by"] == "turn_evidence"
    assert row["settled_role"] == "finance"
    assert row["last_error"] == ""
    assert pse.health_issues() == []


async def test_the_residents_own_run_clears_a_failed_resident_row(wired):
    row = await _failed_resident_row(wired)
    assert pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=pse._now(),
        binding=_BINDING) is True
    assert pse.episodes()[0]["settled_role"] == "assistant"
    assert pse.health_issues() == []


async def test_a_stale_row_is_cleared_too(wired):
    row = await _released_pending(wired)
    pse._update_episode(row["id"], status="stale",
                        last_error="registry unresolvable after retries")
    assert pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row),
        binding=_BINDING) is True
    assert pse.health_issues() == []


async def test_a_cleared_row_is_not_redispatched_or_rearmed(wired):
    await _failed_courier_row(wired)
    pse.settle_from_tool_evidence(
        role="finance", tool=_NS, invoked_at=pse._now(),
        binding=_FIN_BINDING, delegated=True)
    sent_before = len(wired["dispatches"])
    await _drain_pending(wired)
    await pse._worker_pass()
    assert pse.ensure_obligation(plugin="elevenlabs", artifact_id="art-1") \
        is False
    assert len(wired["dispatches"]) == sent_before
    assert pse.episodes()[0]["settled_by"] == "turn_evidence"


@pytest.mark.parametrize("case", [
    "other_role", "assistant_role", "other_tool", "binding_other_artifact",
    "registry_superseded", "before_release", "removed",
])
async def test_evidence_that_does_not_prove_the_run_leaves_the_failed_row(
        wired, monkeypatch, case):
    row = await _failed_courier_row(wired)
    role, tool, binding = "finance", _NS, dict(_FIN_BINDING)
    invoked_at = pse._now()
    if case == "other_role":
        role = "butler"
    elif case == "assistant_role":
        # the courier itself: the assistant's session never ran the tool
        role = "assistant"
    elif case == "other_tool":
        tool = "mcp__plugin_elevenlabs_elevenlabs__list_voices"
    elif case == "binding_other_artifact":
        binding = {"elevenlabs": "art-0"}
    elif case == "registry_superseded":
        wired["entry"] = dict(wired["entry"], artifact_id="art-2")
    elif case == "before_release":
        invoked_at = row["released_ts"] - 0.001
    elif case == "removed":
        pse.retire_for_removed("elevenlabs")
    monkeypatch.setattr(pse, "_WATCH", None)
    assert pse.settle_from_tool_evidence(
        role=role, tool=tool, invoked_at=invoked_at, binding=binding,
        delegated=True) is False
    row = pse.episodes()[0]
    assert row["status"] == "failed"
    assert "settled_by" not in row


async def test_a_refused_row_is_never_cleared(wired):
    _prompt()
    await _decide(approved=False)
    row = pse.episodes()[0]
    assert row["status"] == "refused"
    pse._update_episode(row["id"], gate="released",
                        released_ts=pse._now() - 10)
    cleared = pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=pse._now(), binding=_BINDING)
    assert cleared is False
    assert pse.episodes()[0]["status"] == "refused"


async def test_delegated_evidence_never_settles_a_live_obligation(wired):
    """A delegated session holds no resident session gate, and a pending
    specialist row belongs to its courier (INV-PLUG-024): only a terminal row
    is a candidate for it — resident-target or specialist-target."""
    row = await _released_pending(wired)
    assert pse.settle_from_tool_evidence(
        role="assistant", tool=_NS, invoked_at=_after(row),
        binding=_BINDING, delegated=True) is False
    assert "settled_by" not in pse.episodes()[0]


async def test_delegated_evidence_never_settles_a_pending_courier_row(wired):
    wired["entry"] = dict(wired["entry"], targets=["specialist:finance"])
    _prompt()
    await _decide()
    row = pse.episodes()[0]
    assert row["status"] == "pending" and row["gate"] == "released"
    assert pse.settle_from_tool_evidence(
        role="finance", tool=_NS, invoked_at=_after(row),
        binding=_FIN_BINDING, delegated=True) is False
    assert "settled_by" not in pse.episodes()[0]


async def test_a_legacy_terminal_row_takes_its_failure_stamp_as_the_bound(
        wired):
    """A row released before `released_ts` existed: the run must follow the
    failure, since nothing else dates the release."""
    row = await _failed_courier_row(wired)
    data = pse._load()
    for e in data["episodes"]:
        e.pop("released_ts", None)
    pse._save(data)
    failed_at = pse.episodes()[0]["updated_ts"]
    assert pse.settle_from_tool_evidence(
        role="finance", tool=_NS, invoked_at=failed_at - 0.001,
        binding=_FIN_BINDING, delegated=True) is False
    assert pse.settle_from_tool_evidence(
        role="finance", tool=_NS, invoked_at=failed_at + 0.001,
        binding=_FIN_BINDING, delegated=True) is True


async def test_no_store_read_for_a_session_without_a_failed_plugin(
        wired, monkeypatch):
    await _failed_courier_row(wired)
    loads = _count_loads(monkeypatch)
    pse.settle_from_tool_evidence(
        role="finance", tool="mcp__plugin_other_other__setup_other",
        invoked_at=pse._now(), binding={"other": "art-9"}, delegated=True)
    assert loads == []
    pse.settle_from_tool_evidence(
        role="finance", tool=_NS, invoked_at=pse._now(),
        binding=_FIN_BINDING, delegated=True)
    assert loads == [1]
    loads.clear()
    pse.settle_from_tool_evidence(          # cleared: no longer watched
        role="finance", tool=_NS, invoked_at=pse._now(),
        binding=_FIN_BINDING, delegated=True)
    assert loads == []


async def test_status_sentence_names_the_specialist_that_ran_it():
    from tools import _episode_sentence
    line = _episode_sentence({
        "plugin": "bank-feed", "status": "dispatched",
        "settled_by": "turn_evidence", "settled_role": "finance"})
    assert "setup ran ('finance' ran the setup tool itself)" in line
    assert "assistant" not in _episode_sentence({
        "plugin": "bank-feed", "status": "dispatched",
        "settled_by": "turn_evidence", "settled_role": "finance"})
    assert "the assistant ran" in _episode_sentence({
        "plugin": "gmail", "status": "dispatched",
        "settled_by": "turn_evidence"})
