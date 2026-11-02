"""The view build is timed on a synthetic two-year ledger (p95 under 100 ms
on the target hardware).

About 50 000 ledger tokens a year: 2 000 current entries written over
6 000 revisions from 40 000 events, the whole history in ``revisions``. The
view is rendered at Ellen's budget (index mode at this size) and at the
butler's, and once as of an old commit, as a reconnecting session would.
Marked ``slow``: it runs on demand, and its timings are what the rollout
measures on the target hardware.
"""
from __future__ import annotations

import json
import random
import statistics
import time
from datetime import datetime, timedelta, timezone

import pytest

from memory import pending, view
from memory_serving_support import ledger_store

pytestmark = [pytest.mark.slow]

ENTRIES = 2_000
REVISIONS_PER_ENTRY = 3
EVENTS = 40_000
RUNS = 20
P95_MS = 100.0


def _build(tmp_path):
    store = ledger_store(tmp_path)
    rng = random.Random(7)
    start = datetime(2024, 11, 1, tzinfo=timezone.utc)
    topics = [f"Topic {i}" for i in range(24)]
    with store.transaction(write=True) as conn:
        conn.executemany(
            "INSERT INTO events (source_key, occurred_at, tz, local_day, "
            "recorded_at, role, channel, speaker_kind, origin_kind, text, "
            "tier, tier_status, provenance, eligibility) VALUES "
            "(?, ?, 'UTC', ?, ?, 'assistant', 'telegram', 'operator', "
            "'operator', ?, 'private', 'classified', 'captured', 'turn')",
            [(f"event:{i:032x}", (start + timedelta(minutes=26 * i)).isoformat(),
              (start + timedelta(minutes=26 * i)).date().isoformat(),
              (start + timedelta(minutes=26 * i)).isoformat(),
              "words " * 12) for i in range(EVENTS)])
        revisions = []
        commit_no = 0
        for i in range(ENTRIES):
            oid = f"o{i:016x}"
            topic = topics[i % len(topics)]
            for rev in range(1, REVISIONS_PER_ENTRY + 1):
                commit_no += 1
                body = (f"[{(start + timedelta(days=i // 3)).date()}] "
                        + " ".join(rng.choice(("value", "detail", "dated",
                                               "earlier", "current"))
                                   for _ in range(rng.randint(10, 60))))
                seqs = json.dumps([rng.randint(1, EVENTS) for _ in range(rev)])
                revisions.append((
                    oid, rev, topic, f"subject {i}", body, f"the need {i}",
                    json.dumps([f"cue{i}"]), seqs, commit_no))
        conn.executemany(
            "INSERT INTO revisions (ledger, object_id, revision, kind, "
            "entry_kind, topic, subject, body, line, cues, source_seqs, "
            "status, op, committed_at, commit_no) VALUES ('private', ?, ?, "
            "'entry', 'fact', ?, ?, ?, ?, ?, ?, 'current', 'add', "
            "'2026-11-01T00:00:00+00:00', ?)", revisions)
        conn.execute(
            "INSERT INTO objects (ledger, object_id, revision, kind, "
            "entry_kind, topic, subject, body, line, cues, source_seqs, "
            "status) SELECT ledger, object_id, revision, kind, entry_kind, "
            "topic, subject, body, line, cues, source_seqs, status FROM "
            "revisions AS r WHERE revision = (SELECT max(revision) FROM "
            "revisions WHERE object_id = r.object_id)")
    return store


def _p95_ms(store, fn) -> float:
    times = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        with store.transaction() as conn:
            fn(conn)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.quantiles(times, n=20)[-1]


def test_a_two_year_ledger_is_served_within_the_target(tmp_path):
    store = _build(tmp_path)
    with store.transaction() as conn:
        current = pending.current_commit_no(conn)
        tokens = sum(len(r[0]) // 4 for r in conn.execute(
            "SELECT body FROM objects WHERE status = 'current'"))
    assert tokens > 2 * 40_000
    timings = {
        "ellen": _p95_ms(store, lambda c: view.render(c, "private", 50_000)),
        "butler": _p95_ms(store, lambda c: view.render(c, "private", 10_000)),
        "as_of": _p95_ms(store, lambda c: view.render_as_of(
            c, "private", current // 2, 50_000)),
    }
    print(f"view build p95 ms on {ENTRIES} entries, {tokens} tokens: "
          f"{timings}")
    with store.transaction() as conn:
        assert view.render(conn, "private", 50_000).mode == view.INDEX
    assert max(timings.values()) < P95_MS, timings
