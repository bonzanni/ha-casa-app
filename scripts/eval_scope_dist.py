#!/usr/bin/env python3
"""eval_scope_dist.py — audit live scope-routing distribution.

LIMITATION (as of v0.8.4)
=========================
This script expects JSON-structured `scope_route` log records with fields:
    msg="scope_route", channel, winner, winner_score, second_score, threshold

The live casa addon at v0.8.4 emits `scope_route` as a FORMATTED-STRING
log line (see agent.py:441) WITHOUT winner_score — so this script will
report "total records: 0" against unmodified production logs.

Two follow-ups unblock real use:
  1. (preferred) Add a JSON emission alongside the formatted line in
     agent.py so scope_route events carry winner_score + second_score.
  2. (fallback) Rewrite parse_line as a regex over the current format;
     only channel + active + write + t_ms + embed_cache will be
     extractable — no winner-score histogram possible without upstream
     field addition.

Until then the parser is validated against synthetic logs (see
tests/test_eval_scope_dist.py) and waits for the upstream emission.

Usage:
    eval_scope_dist.py --log-file /path/to/casa.log --threshold 0.35
    eval_scope_dist.py --ha-host <ha-host> --since 24h --threshold 0.35
    eval_scope_dist.py --log-file casa.log --threshold 0.35 --json > dist.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_line(line: str) -> dict[str, Any] | None:
    """Return the parsed event dict, or None if the line isn't a
    scope_route JSON record."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    if rec.get("msg") != "scope_route":
        return None
    return rec


def bucket_by_channel(
    records: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        out[r.get("channel", "unknown")].append(r)
    return dict(out)


def score_histogram(
    scores: list[float], threshold: float, bins: int = 10,
) -> dict[str, int]:
    """Bin scores from threshold-0.2 to threshold+0.2 into `bins` buckets.
    Scores outside the range are bucketed into 'below' / 'above'."""
    lo = threshold - 0.2
    hi = threshold + 0.2
    step = (hi - lo) / bins
    counts: dict[str, int] = defaultdict(int)
    for s in scores:
        if s < lo:
            counts["<{:.3f}".format(lo)] += 1
        elif s >= hi:
            counts[">={:.3f}".format(hi)] += 1
        else:
            idx = int((s - lo) / step)
            idx = min(idx, bins - 1)
            bucket_lo = lo + idx * step
            bucket_hi = bucket_lo + step
            counts["{:.3f}-{:.3f}".format(bucket_lo, bucket_hi)] += 1
    return dict(counts)


def is_clustered_near_threshold(
    scores: list[float], threshold: float, band: float = 0.05,
    ratio: float = 0.20,
) -> bool:
    if not scores:
        return False
    near = sum(1 for s in scores if abs(s - threshold) <= band)
    return (near / len(scores)) > ratio


# ---------------------------------------------------------------------------
# Log source
# ---------------------------------------------------------------------------


def read_log_file(path: str) -> Iterable[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        yield from fh


def read_ssh_logs(
    host: str, since: str, container: str = "addon_91d4d4c8_casa",
) -> Iterable[str]:
    """Pull recent container logs over SSH. Wraps `ssh <host> sudo -n
    docker logs --since <since> <container>` — `sudo -n` because the HA
    SSH addon runs unprivileged and docker-socket ops require root.
    """
    cmd = [
        "ssh", host, "sudo", "-n", "docker", "logs",
        "--since", since, container,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(
            f"ssh failed (returncode={proc.returncode}): {proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    yield from proc.stdout.splitlines()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _summarize(
    buckets: dict[str, list[dict[str, Any]]], threshold: float,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for channel, records in sorted(buckets.items()):
        scores = [r.get("winner_score", 0.0) for r in records]
        out[channel] = {
            "count": len(records),
            "mean": (sum(scores) / len(scores)) if scores else 0.0,
            "p50": sorted(scores)[len(scores) // 2] if scores else 0.0,
            "p95": (sorted(scores)[min(len(scores) - 1, int(0.95 * len(scores)))]
                    if scores else 0.0),
            "histogram": score_histogram(scores, threshold),
            "flag_near_threshold": is_clustered_near_threshold(
                scores, threshold,
            ),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--log-file", help="path to a JSON log file")
    source.add_argument("--ha-host", help="SSH host alias for live pull")
    parser.add_argument("--since", default="24h",
                        help="lookback window for --ha-host (default: 24h)")
    parser.add_argument("--threshold", type=float,
                        help="current scope_threshold (required)")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON to stdout")
    args = parser.parse_args(argv)

    if args.threshold is None:
        parser.error(
            "--threshold is required (read from "
            "`bashio::config 'scope_threshold'` on the live host)"
        )

    if args.log_file:
        lines = read_log_file(args.log_file)
    else:
        lines = read_ssh_logs(args.ha_host, args.since)

    records = [r for r in (parse_line(l) for l in lines) if r is not None]
    buckets = bucket_by_channel(records)
    summary = _summarize(buckets, args.threshold)

    if args.json:
        json.dump({"threshold": args.threshold, "channels": summary},
                  sys.stdout, sort_keys=True)
        return 0

    print(f"scope_route distribution (threshold={args.threshold})")
    print(f"total records: {sum(len(v) for v in buckets.values())}")
    for channel, stats in summary.items():
        print(f"\n[{channel}]  n={stats['count']}  "
              f"mean={stats['mean']:.3f}  p50={stats['p50']:.3f}  "
              f"p95={stats['p95']:.3f}"
              f"{'  [!] near-threshold cluster' if stats['flag_near_threshold'] else ''}")
        for bucket, cnt in sorted(stats["histogram"].items()):
            bar = "#" * min(40, cnt)
            print(f"  {bucket:20s}  {cnt:4d}  {bar}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
