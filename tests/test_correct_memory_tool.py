"""``correct_memory``: the operator says something memory holds is wrong,
and the tool marks the message he said it in — the operator event this turn
opened with — through ``memory.corrections``, queues the correction job and
returns ``queued`` at once. The mark re-opens the event (INV-MEM-027); a
retried call changes nothing (#1185); a turn no message of his opened marks
nothing."""
from __future__ import annotations

from unittest.mock import patch

import pytest

import agent as agent_mod
import tools
from memory import capture, occurrence
from memory_tools_support import (  # noqa: F401 — fixtures
    mem, payload, store_event, turn)
from test_agent_process import FakeClient, _make_agent
from test_memory_capture import CHAT, _operator_msg, _system_msg

pytestmark = [pytest.mark.asyncio]


async def _correct(**args) -> dict:
    return payload(await tools.correct_memory.handler(
        {"what": "his sister's name", **args}))


def _marks(store) -> list[tuple]:
    with store.transaction() as conn:
        return conn.execute("SELECT seq, mark_no FROM correction_marks "
                            "ORDER BY seq").fetchall()


def _jobs(store) -> list[tuple]:
    with store.transaction() as conn:
        return conn.execute("SELECT ledger, kind, state FROM jobs").fetchall()


def _operator_event(store, text: str, key: str) -> int:
    return store_event(store, text, source_key=key)


async def test_it_marks_his_message_and_queues_the_correction(mem, turn):
    key = occurrence.provider_key("telegram", CHAT, "700")
    seq = _operator_event(mem, "no, my sister is called Anna", key)
    turn("private", **{agent_mod.MEMORY_OPENING_KEY: key})
    out = await _correct()
    assert out["status"] == "queued"
    assert _marks(mem) == [(seq, 1)]
    assert _jobs(mem) == [("private", "correction", "queued")]

    again = await _correct()
    assert again["status"] == "queued"
    assert _marks(mem) == [(seq, 1)] and len(_jobs(mem)) == 1


async def test_a_turn_without_his_message_marks_nothing(mem, turn):
    key = occurrence.provider_key("telegram", CHAT, "701")
    _operator_event(mem, "no, my sister is called Anna", key)
    turn("private")
    out = await _correct()
    assert out["status"] == "error" and out["kind"] == "no_message_of_his"
    assert _marks(mem) == [] and _jobs(mem) == []


async def test_only_an_operator_event_is_marked(mem, turn):
    key = occurrence.event_key()
    store_event(mem, "the reminder text", speaker="casa", source_key=key)
    turn("private", **{agent_mod.MEMORY_OPENING_KEY: key})
    out = await _correct()
    assert out["status"] == "unavailable"
    assert _marks(mem) == []


async def test_an_engagement_turn_marks_nothing(mem, turn):
    key = occurrence.provider_key("telegram", CHAT, "702")
    _operator_event(mem, "no, it is Anna", key)
    turn("private", **{agent_mod.MEMORY_OPENING_KEY: key})
    token = tools.engagement_var.set(object())
    try:
        out = await _correct()
    finally:
        tools.engagement_var.reset(token)
    assert out["status"] == "error" and _marks(mem) == []


async def test_a_spooled_message_is_marked_once_the_drain_stores_it(
        mem, turn, monkeypatch):
    """His message went to the spool while the store was busy: the tool
    does not drain it on the turn's path. It answers ``unavailable``,
    marks nothing and asks for the background drain; once that has stored
    his message, the next call marks it."""
    monkeypatch.setattr(capture, "_background", set())
    monkeypatch.setattr(capture, "_drain", [None])
    key = occurrence.provider_key("telegram", CHAT, "703")
    item = capture.event(role="assistant", channel="telegram",
                         speaker_kind="operator", origin_kind="operator",
                         text="no, it is Anna", eligibility="write_trusted_turn",
                         origin_tier="private", source_key=key)
    mem._unavailable_reason = "busy"
    try:
        assert capture.append(mem, item).outcome == capture.SPOOLED
    finally:
        mem._unavailable_reason = None
    turn("private", **{agent_mod.MEMORY_OPENING_KEY: key})
    first = await _correct()
    assert first["status"] == "unavailable" and _marks(mem) == []
    await capture.settle()
    assert (await _correct())["status"] == "queued"
    assert len(_marks(mem)) == 1


async def test_an_unavailable_store_reports_it(mem, turn):
    turn("private", **{agent_mod.MEMORY_OPENING_KEY: "provider:x"})
    mem._unavailable_reason = "a simulated outage"
    try:
        out = await _correct()
    finally:
        mem._unavailable_reason = None
    assert out["status"] == "unavailable"


class _CorrectingClient(FakeClient):
    """A turn whose model calls ``correct_memory`` once."""

    results: list[dict] = []

    async def query(self, text):
        await super().query(text)
        _CorrectingClient.results.append(payload(
            await tools.correct_memory.handler({"what": "the name"})))


async def test_a_resident_turn_hands_the_tool_its_opening(mem, tmp_path):
    """Through the real turn: the operator message a DM opens with is the
    one the tool marks; a turn no person typed hands it nothing."""
    FakeClient.reset()
    _CorrectingClient.results = []
    agent = _make_agent(tmp_path)
    with patch("sdk_client_pool._default_make_client", _CorrectingClient):
        await agent._process(_operator_msg("no, she is called Anna", "704"))
        await agent._process(_system_msg("the hourly check"))
    FakeClient.reset()
    marked, refused = _CorrectingClient.results
    assert marked["status"] == "queued"
    assert refused["kind"] == "no_message_of_his"
    with mem.transaction() as conn:
        rows = conn.execute(
            "SELECT e.text FROM correction_marks AS m JOIN events AS e "
            "ON e.seq = m.seq").fetchall()
    assert rows == [("no, she is called Anna",)]


async def test_a_busy_store_answers_at_once_and_marks_nothing(mem, turn):
    """Another connection holds the store's write lock (a writer commit, a
    drain): the tool waits no longer than the turn-path bound, answers
    `unavailable` without claiming an update, and marks nothing."""
    import sqlite3
    import time

    from memory import corrections
    key = occurrence.provider_key("telegram", CHAT, "705")
    _operator_event(mem, "no, it is Anna", key)
    turn("private", **{agent_mod.MEMORY_OPENING_KEY: key})
    holder = sqlite3.connect(mem.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        out = await _correct()
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert elapsed < 2.0, elapsed
    assert corrections.TOOL_BUSY_TIMEOUT_S <= 0.05
    assert out["status"] == "unavailable"
    assert "Do NOT say memory was updated" in out["message"]
    assert _marks(mem) == [] and _jobs(mem) == []
    assert (await _correct())["status"] == "queued"
