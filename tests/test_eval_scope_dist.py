"""Unit tests for scripts/eval_scope_dist.py — parser only.

The SSH / log-fetch side is exercised by hand on the live production box; these
tests cover the log-line parser, the per-channel bucketing, the
histogram binning, and the threshold-cluster flag.
"""

from __future__ import annotations

import importlib.util
import os
import pytest


SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "eval_scope_dist.py",
)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location(
        "eval_scope_dist", SCRIPT_PATH,
    )
    m = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(m)
    return m


class TestParseScopeRouteLine:
    def test_parses_json_log_with_scope_route_marker(self, mod):
        line = (
            '{"msg": "scope_route", "channel": "telegram", '
            '"winner": "finance", "winner_score": 0.52, '
            '"second_score": 0.31, "threshold": 0.35}'
        )
        rec = mod.parse_line(line)
        assert rec is not None
        assert rec["channel"] == "telegram"
        assert rec["winner"] == "finance"
        assert rec["winner_score"] == pytest.approx(0.52)

    def test_ignores_unrelated_lines(self, mod):
        assert mod.parse_line("not JSON at all") is None
        assert mod.parse_line('{"msg": "other_event"}') is None
        assert mod.parse_line("") is None
        assert mod.parse_line('{"msg": "scope_route"') is None  # truncated


class TestBucketAndHistogram:
    def test_buckets_by_channel(self, mod):
        records = [
            {"channel": "telegram", "winner_score": 0.42, "winner": "p"},
            {"channel": "telegram", "winner_score": 0.51, "winner": "f"},
            {"channel": "voice", "winner_score": 0.38, "winner": "h"},
        ]
        buckets = mod.bucket_by_channel(records)
        assert set(buckets.keys()) == {"telegram", "voice"}
        assert len(buckets["telegram"]) == 2
        assert len(buckets["voice"]) == 1

    def test_histogram_bins_around_threshold(self, mod):
        scores = [0.20, 0.28, 0.35, 0.38, 0.42, 0.55]
        hist = mod.score_histogram(scores, threshold=0.35, bins=10)
        assert sum(hist.values()) == len(scores)
        # At least one non-zero bin near the threshold.
        assert any(v > 0 for v in hist.values())

    def test_flags_clusters_near_threshold(self, mod):
        # 3 of 5 (60%) within +-0.05 of threshold -> flag
        near_cluster = [0.32, 0.36, 0.38, 0.50, 0.60]
        assert mod.is_clustered_near_threshold(
            near_cluster, threshold=0.35,
        ) is True
        # Only 1 of 5 (20%) — ties the 20% cut; implementation uses '>20%'
        # so this should NOT flag.
        assert mod.is_clustered_near_threshold(
            [0.38, 0.50, 0.60, 0.70, 0.80], threshold=0.35,
        ) is False
