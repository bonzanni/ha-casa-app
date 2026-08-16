"""#603: assertions over one `quiesce_probe.py` run. Reads PROBE_RESULT on stdin.

Split out of the shell driver on purpose — the assertions carry the reasoning
about what would make a green run meaningless, and that does not survive being
squeezed through shell quoting.

Usage: quiesce_assert.py <label> <finalize_budget_seconds>
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    label = sys.argv[1]
    budget = float(sys.argv[2])
    data = json.load(sys.stdin)
    fails: list[str] = []

    # --- setup integrity ---------------------------------------------------
    # Checked FIRST and as loudly as the guarantee itself: a probe whose
    # descendants never started would report an empty uid set after the kill
    # and look like a pass while proving nothing at all.
    want = {"leader", "foreground", "pgroup", "setsid"}
    running = set(data["writers_running_before"])
    missing = sorted(want - running)
    if missing:
        fails.append(f"setup: these writers never ran: {missing}")

    shapes = {int(p): tuple(v) for p, v in data["shapes_before"].items()}
    if len(data["live_pids_before"]) < 4:
        fails.append("setup: expected at least 4 live pids under the uid, saw "
                     f"{data['live_pids_before']}")
    sessions = {sid for (_pgid, sid) in shapes.values()}
    groups = {pgid for (pgid, _sid) in shapes.values()}
    if len(sessions) < 2:
        fails.append(f"setup: no descendant escaped the session, so the setsid "
                     f"shape was never exercised — shapes {shapes}")
    if len(groups) < 2:
        fails.append(f"setup: no descendant left the process group — {shapes}")

    # --- the guarantee -----------------------------------------------------
    if data["live_pids_after"]:
        fails.append("SURVIVOR under the engagement uid after finalize: "
                     f"{data['live_pids_after']} shapes={data['shapes_after']}")
    if data["record_status"] != "completed":
        fails.append(f"record status {data['record_status']!r}, expected completed")
    if data["quiesce_pending"]:
        fails.append("the durable obligation was not discharged "
                     "(quiesce_pending still set) despite an extinct uid set")
    if data.get("finalize_timed_out"):
        fails.append("the funnel did not terminate within the probe's hard "
                     "bound — a wedge, which is worse than the defect #599 fixed")
    if data["finalize_seconds"] > budget:
        fails.append(f"finalize took {data['finalize_seconds']}s, over the "
                     f"{budget}s budget — a wedge is worse than the defect")

    # --- the measurement, reported and bounded -----------------------------
    overrun = data["last_write_overrun_ms"]
    worst = max(overrun.values()) if overrun else None
    if worst is not None and worst > budget * 1000:
        fails.append(f"a descendant wrote {worst}ms past the terminal timestamp")

    print(f"[{label}] finalize={data['finalize_seconds']}s "
          f"result={data['finalize_result']} "
          f"pids before={len(data['live_pids_before'])} "
          f"after={len(data['live_pids_after'])}")
    print(f"[{label}] shapes before the kill (pid: pgid, sid): {shapes}")
    print(f"[{label}] last write per shape, ms after the terminal timestamp: "
          f"{overrun}")
    for f in fails:
        print("  FAIL: " + f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
