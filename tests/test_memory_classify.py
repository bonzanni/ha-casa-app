"""Tier classification in the ``private`` job (``memory/classify.py``).

Each provisional event the job files is classified before the commit, and
the commit writes the verdict: never below the event's ``origin_tier``. A
classifier fallback leaves the event provisional, pending in no lower
ledger. A run that does not commit writes no verdict.
"""
from __future__ import annotations

import asyncio

from memory import classify, pending, writer
from memory_writer_support import (
    ScriptedPort, classifier_of, keep_provisional, new_store, new_writer,
    no_change_all, rows, run_new, seed,
)


def _tiers(store):
    return [(r["seq"], r["tier"], r["tier_status"]) for r in rows(
        store, "SELECT seq, tier, tier_status FROM events ORDER BY seq")]


async def test_the_verdict_is_written_in_the_commit_never_below_origin(
        tmp_path):
    store = new_store(tmp_path)
    a = seed(store, "the weather is nice", origin_tier=None)
    b = seed(store, "a reply in the DM", speaker="resident",
             origin_tier="family")
    w = new_writer(store, ScriptedPort(no_change_all),
                   classifier=classifier_of("public"))
    await run_new(w)
    assert _tiers(store) == [(a, "public", "classified"),
                             (b, "family", "classified")]
    with store.transaction() as conn:
        assert pending.pending_seqs(conn, "public") == [a]
        assert pending.pending_seqs(conn, "family") == [a, b]
        assert pending.pending_seqs(conn, "private") == []


async def test_a_fallback_leaves_the_event_provisional(tmp_path):
    store = new_store(tmp_path)
    a = seed(store, "something")
    await run_new(new_writer(store, ScriptedPort(no_change_all),
                             classifier=keep_provisional))
    assert _tiers(store) == [(a, "private", "provisional")]
    with store.transaction() as conn:
        assert [pending.pending_seqs(conn, t) for t in pending.TIERS] == [
            [], [], [], []]


async def test_a_raising_classifier_counts_as_a_fallback(tmp_path):
    store = new_store(tmp_path)
    a = seed(store, "something")

    async def broken(text):
        raise RuntimeError("backend down")

    _, outcome = await run_new(new_writer(store, ScriptedPort(no_change_all),
                                          classifier=broken))
    assert outcome == writer.COMMITTED
    assert _tiers(store) == [(a, "private", "provisional")]


async def test_a_run_that_does_not_commit_writes_no_verdict(tmp_path):
    store = new_store(tmp_path)
    a = seed(store, "something")
    _, outcome = await run_new(new_writer(
        store, ScriptedPort([RuntimeError("model down")]),
        classifier=classifier_of("public")))
    assert outcome == writer.RETRY
    assert _tiers(store) == [(a, "private", "provisional")]


async def test_only_provisional_events_are_classified(tmp_path):
    store = new_store(tmp_path)
    seed(store, "already", tier="friends", tier_status="classified")
    calls = []

    async def counting(text):
        calls.append(text)
        return "public"

    await run_new(new_writer(store, ScriptedPort(no_change_all),
                             classifier=counting))
    assert calls == []


def test_floor():
    assert classify.floor("public", None) == "public"
    assert classify.floor("public", "family") == "family"
    assert classify.floor("private", "friends") == "private"


async def test_the_default_classifier_adapts_casas_classifier(monkeypatch):
    """A real verdict passes through; the classifier's fallback is
    ``None``."""
    import tier_classifier

    async def verdict(text):
        return "friends"

    async def fallback(text):
        return tier_classifier.FallbackTier("private")

    monkeypatch.setattr(tier_classifier, "classify_tier", verdict)
    assert await classify.default_classifier("x") == "friends"
    monkeypatch.setattr(tier_classifier, "classify_tier", fallback)
    assert await classify.default_classifier("x") is None


async def test_a_stalled_classifier_leaves_the_event_provisional(
        tmp_path, monkeypatch):
    """A classification call that never completes counts as a fallback once
    its bound passes: the event stays provisional ``private`` at its floor,
    the run goes on to the model and commits, and the worker is free.
    (Unbounded, the run would hold the one worker forever.)"""
    monkeypatch.setattr(classify, "TIMEOUT_S", 0.05)
    store = new_store(tmp_path)
    a = seed(store, "the alarm is set to 6", origin_tier="family")

    async def stalled(text):
        await asyncio.Event().wait()

    port = ScriptedPort(no_change_all)
    job = writer.enqueue(store, "private", writer.ORDINARY)
    outcome = await asyncio.wait_for(
        new_writer(store, port, classifier=stalled).run_job(job), 10)
    assert outcome == writer.COMMITTED
    assert len(port.prompts) == 1
    assert _tiers(store) == [(a, "private", "provisional")]
    assert rows(store, "SELECT outcome FROM dispositions") == [
        {"outcome": "no_change"}]
