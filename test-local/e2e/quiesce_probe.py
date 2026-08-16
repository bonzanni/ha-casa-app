"""#603: settling evidence for the terminal uid quiesce (#599, v0.213.0).

Runs INSIDE the app container, against real s6 supervision, a real `setpriv`
uid drop and a real `/proc`. The Claude CLI is the mock — deliberately: what
this probe needs from the CLI is that it be a uid-dropped process which spawns
descendants in three specific shapes and mutates the filesystem continuously,
which the mock does deterministically and the real CLI would not. Everything
the ladder actually touches — s6, the uid, the process tree, pidfd signalling —
is real.

The question it answers is the one the unit ladder cannot: **does anything under
the engagement's uid keep writing after the engagement goes terminal?**

Reported, not merely asserted: the measured gap between the registry's terminal
timestamp and the last byte any descendant wrote. INV-CONT-006 promises a
bounded, observed kill before the operator-visible effects — not an instantaneous
one — so the honest artifact is the number, next to the bound it must respect.

One mode today: the COMPLETION path with the CLI still alive and mutating,
which is the case #599 could not scope away — a single model turn may emit
parallel tool calls, so a completing engagement is not necessarily idle.

A `hung-s6` mode was built and withdrawn; see the driver script for why, and
for what covers that ground instead.
"""
from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import signal
import sys
import time

sys.path.insert(0, "/opt/casa")

faulthandler.register(signal.SIGUSR1)

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
WRITER_DIR = "/tmp/casa-quiesce-writers"
GRANT = "mcp__casa-framework__list_engagement_workspaces"
MCP_URL = "http://127.0.0.1:8100/mcp/casa-framework"


def _writer_last_ms(name: str) -> int | None:
    """Last epoch-ms this writer recorded, or None if it never ran."""
    path = os.path.join(WRITER_DIR, name + ".log")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            last = None
            for line in fh:
                parts = line.split()
                if len(parts) == 2 and parts[1].isdigit():
                    last = int(parts[1])
            return last
    except OSError:
        return None


def _proc_shapes(uid: int) -> dict:
    """pid -> (pgid, sid) for every live process under *uid* — the evidence
    that the three shapes really were distinct before the kill."""
    out = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status", encoding="utf-8") as fh:
                ruid = None
                for line in fh:
                    if line.startswith("Uid:"):
                        ruid = int(line.split()[1])
                        break
            if ruid != uid:
                continue
            stat = open(f"/proc/{entry}/stat", encoding="utf-8").read()
            tail = stat[stat.rindex(")") + 1:].split()
            out[int(entry)] = (int(tail[2]), int(tail[3]))   # pgrp, session
        except (OSError, ValueError, IndexError):
            continue
    return out


async def main() -> int:
    import casa_core
    import private_state
    import tools
    from engagement_quiesce import live_pids_for_uid
    from engagement_registry import EngagementRegistry
    from engagement_uids import UID_BASE, UidAllocator
    from executor_registry import ExecutorRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from drivers.workspace import fifo_path

    private_state.enforce()

    alloc = UidAllocator("/data/engagement-uids.json")
    reg = EngagementRegistry(
        tombstone_path="/data/engagements.json", bus=None, uid_allocator=alloc)
    await reg.load()
    known, dir_owners = casa_core._gather_reconstruct_evidence(
        reg, data_dir="/data")
    alloc.reconstruct(known, dir_owners)

    exec_reg = ExecutorRegistry("/config/agents/executors")
    exec_reg.load()
    defn = exec_reg.definition_any("plugin-developer")
    assert defn is not None, f"no definition; have {exec_reg.list_types_any()}"

    rec = await reg.create(
        kind="executor", role_or_type="plugin-developer", driver="claude_code",
        task="#603 quiesce probe", topic_id=None,
        origin={"channel": "telegram", "chat_id": "1"},
        tools_allowed=(GRANT,),
    )
    assert rec.allocated_uid >= UID_BASE, f"no uid ({rec.allocated_uid})"
    uid = rec.allocated_uid

    async def _noop(*_a, **_kw):
        return None

    drv = ClaudeCodeDriver(
        engagements_root="/data/engagements", send_to_topic=_noop,
        casa_framework_mcp_url=MCP_URL, registry=reg,
        executor_defn_lookup=exec_reg.definition_any,
    )
    # casa_core wires this at boot; the probe stands in for casa_core.
    reg.set_quiesce_owner(drv.quiesce)
    tools.init_tools(channel_manager=None, bus=None, specialist_registry=None,
                     mcp_registry=None, trigger_registry=None,
                     engagement_registry=reg)

    await drv.start(rec, prompt="probe launch", options=defn)
    await asyncio.sleep(3.0)

    # Hold the FIFO OPEN for the rest of the probe. Closing it is an EOF: the
    # CLI leaves its read loop, exits, and s6 respawns it — which would leave
    # the writers orphaned and the leader dead before the measurement. A real
    # CLI stays alive through its turn, and that is the premise under test.
    fifo = fifo_path(rec.id)
    fifo_fh = open(fifo, "w", encoding="utf-8")
    fifo_fh.write(f"/mock spawn_writers {WRITER_DIR}\n")
    fifo_fh.flush()
    # The CLI emits its completion tool call and KEEPS RUNNING — the parallel
    # tool-call premise that makes this path non-idle.
    await asyncio.sleep(1.5)
    fifo_fh.write('/mock emit_completion_stay {"text": "done", "status": "ok"}\n')
    fifo_fh.flush()
    await asyncio.sleep(1.5)

    shapes_before = _proc_shapes(uid)
    live_before = live_pids_for_uid(uid)
    writers_running = [n for n in ("leader", "foreground", "pgroup", "setsid")
                       if _writer_last_ms(n) is not None]

    t_commit_wall = time.time()
    t0 = time.monotonic()
    # A HARD outer bound, so a wedged funnel is REPORTED rather than hanging the
    # probe. "The test timed out" and "the funnel does not terminate" look
    # identical from outside; only one of them is a finding, so the probe has to
    # be the thing that decides.
    timed_out = False
    try:
        result = await asyncio.wait_for(
            tools._finalize_engagement(
                rec, outcome="completed", text="probe completion", artifacts=[],
                next_steps=[], driver=drv, inbound_gate=False),
            timeout=90)
    except asyncio.TimeoutError:
        timed_out = True
        result = "TIMEOUT"
        faulthandler.dump_traceback()      # where, exactly
    elapsed = time.monotonic() - t0

    # The authoritative terminal instant, as the registry recorded it.
    terminal_ms = int((rec.completed_at or t_commit_wall) * 1000)

    await asyncio.sleep(0.5)          # let any survivor prove it survived
    live_after = live_pids_for_uid(uid)
    shapes_after = _proc_shapes(uid)
    last_writes = {n: _writer_last_ms(n)
                   for n in ("leader", "foreground", "pgroup", "setsid")}
    overruns = {n: (ms - terminal_ms)
                for n, ms in last_writes.items() if ms is not None}

    try:
        fifo_fh.close()
    except OSError:
        pass

    print("PROBE_RESULT=" + json.dumps({
        "mode": MODE,
        "engagement": rec.id,
        "uid": uid,
        "finalize_result": str(result),
        "finalize_seconds": round(elapsed, 3),
        "finalize_timed_out": timed_out,
        "record_status": rec.status,
        "quiesce_pending": rec.quiesce_pending,
        "writers_running_before": writers_running,
        "shapes_before": {str(p): list(v) for p, v in shapes_before.items()},
        "live_pids_before": live_before,
        "live_pids_after": live_after,
        "shapes_after": {str(p): list(v) for p, v in shapes_after.items()},
        "terminal_ms": terminal_ms,
        "last_write_overrun_ms": overruns,
    }))
    return 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.stdout.flush()
    sys.stderr.flush()
    # Deliberate: a `to_thread` worker abandoned by a fired timeout would
    # otherwise hold interpreter shutdown for the length of its own blocking
    # call, and the probe has already printed everything it measured.
    os._exit(rc)
