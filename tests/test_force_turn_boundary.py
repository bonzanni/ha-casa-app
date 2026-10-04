"""A2b — verified group-wide ``force_turn_boundary`` backstop (v0.83.0).

On the 2nd ``operator_away`` refusal in an away-episode the driver force-ends
the CLI turn so a doctrine-defying agent cannot loop ask→refusal at token
speed. The kill is a VERIFIED, GROUP-WIDE ladder in ``s6_rc``:

  * STRICT tri-state entry probe (down / up / unknown) — ``unknown`` never
    reads as success ("not suspending blind");
  * ``os.killpg(pid, SIGTERM)`` on the whole group (leader + MCP/tool children);
  * bounded verification of GROUP EXTINCTION (``os.killpg(pgid, 0)`` →
    ``ProcessLookupError``), NOT leader turnover;
  * timeout → ``os.killpg(RECORDED pgid, SIGKILL)`` → re-poll the same probe;
  * truthful bool — a False is WARN-logged, never falsely reported suspended.

Fakes inject the probe / pid / getpgid / killpg / sleep seams; no real signals,
no real s6. The ladder tests assert EXACT call sequences so a mis-ordered ladder
fails meaningfully. The recorded pgid comes from ``os.getpgid`` (NOT an assumed
pid — the run template never calls setsid), so a leader that is not its own
group leader still gets its whole group signalled on the RECORDED pgid.
"""

from __future__ import annotations

import logging
import signal
import types

import pytest

from drivers import s6_rc

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers: install the injectable seams on the s6_rc module
# ---------------------------------------------------------------------------


def _install(monkeypatch, *, probe, pid=4242, killpg, getpgid=None):
    async def _fake_probe(scandir):
        # ONE atomic snapshot: status + pid together (Sol A2 review).
        status = probe() if callable(probe) else probe
        return status, pid

    def _fake_getpgid(p):
        # Default: the leader IS its own group leader (pgid == pid). Cases that
        # need a divergent group or a vanished leader pass an explicit callable.
        return p if getpgid is None else getpgid(p)

    monkeypatch.setattr(s6_rc, "_probe_status_and_pid", _fake_probe)
    monkeypatch.setattr(s6_rc, "_getpgid", _fake_getpgid)
    monkeypatch.setattr(s6_rc, "_killpg", killpg)


async def _no_sleep_recorder():
    calls: list[float] = []

    async def _sleep(d):
        calls.append(d)

    return calls, _sleep


# ---------------------------------------------------------------------------
# 1. unknown entry probe → False, NO signals, WARN
# ---------------------------------------------------------------------------


async def test_unknown_probe_returns_false_no_signals(monkeypatch, caplog):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    _install(monkeypatch, probe="unknown", killpg=killpg)
    with caplog.at_level(logging.WARNING, logger="drivers.s6_rc"):
        result = await s6_rc.force_turn_boundary(engagement_id="e1")

    assert result is False
    assert seq == []
    assert any("not suspending blind" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. down entry probe → True immediately, NO signals
# ---------------------------------------------------------------------------


async def test_down_probe_returns_true_no_signals(monkeypatch):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    _install(monkeypatch, probe="down", killpg=killpg)
    result = await s6_rc.force_turn_boundary(engagement_id="e1")

    assert result is True
    assert seq == []


# ---------------------------------------------------------------------------
# 3. up → SIGTERM group → extinction on poll N → True; exact sequence; no KILL
# ---------------------------------------------------------------------------


async def test_up_sigterm_then_extinct_no_kill(monkeypatch):
    seq: list[tuple] = []
    state = {"zero": 0}

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == 0:
            state["zero"] += 1
            if state["zero"] >= 3:      # extinct on the 3rd emptiness probe
                raise ProcessLookupError

    _install(monkeypatch, probe="up", pid=777, killpg=killpg)
    calls, sleep = await _no_sleep_recorder()
    result = await s6_rc.force_turn_boundary(engagement_id="e1", sleep=sleep)

    assert result is True
    # SIGTERM to the group first, then emptiness probes on the RECORDED pgid.
    assert seq[0] == (777, signal.SIGTERM)
    assert seq[1:] == [(777, 0), (777, 0), (777, 0)]
    assert all(sig != signal.SIGKILL for _, sig in seq)


# ---------------------------------------------------------------------------
# 4. SIGTERM ignored → SIGKILL to the RECORDED pgid → extinct → True
# ---------------------------------------------------------------------------


async def test_sigterm_ignored_escalates_to_sigkill(monkeypatch):
    seq: list[tuple] = []
    state = {"killed": False}

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == signal.SIGKILL:
            state["killed"] = True
            return
        if sig == 0 and state["killed"]:
            raise ProcessLookupError
        # SIGTERM, or emptiness-probe before the kill → still alive.

    _install(monkeypatch, probe="up", pid=555, killpg=killpg)
    calls, sleep = await _no_sleep_recorder()
    result = await s6_rc.force_turn_boundary(engagement_id="e1", sleep=sleep)

    assert result is True
    signals = [sig for _, sig in seq]
    # SIGTERM once, then 20 alive emptiness probes, then SIGKILL, then extinct.
    assert seq[0] == (555, signal.SIGTERM)
    assert signals.count(0) == 20 + 1          # 20 pre-kill alive + 1 post-kill
    assert (555, signal.SIGKILL) in seq        # kill on the RECORDED pgid
    # every emptiness probe used the recorded pgid, never a re-read pid.
    assert all(pgid == 555 for pgid, _ in seq)


# ---------------------------------------------------------------------------
# 5. still alive after SIGKILL re-poll → truthful False + WARN
# ---------------------------------------------------------------------------


async def test_never_extinct_returns_false(monkeypatch, caplog):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        # never raises ProcessLookupError → group refuses to die.

    _install(monkeypatch, probe="up", pid=999, killpg=killpg)
    calls, sleep = await _no_sleep_recorder()
    with caplog.at_level(logging.WARNING, logger="drivers.s6_rc"):
        result = await s6_rc.force_turn_boundary(engagement_id="e1", sleep=sleep)

    assert result is False
    assert (999, signal.SIGKILL) in seq
    assert any("NOT verified" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. ProcessLookupError at the FIRST killpg (SIGTERM) → already extinct → True
# ---------------------------------------------------------------------------


async def test_processlookup_on_sigterm_is_extinct(monkeypatch):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == signal.SIGTERM:
            raise ProcessLookupError

    _install(monkeypatch, probe="up", pid=111, killpg=killpg)
    result = await s6_rc.force_turn_boundary(engagement_id="e1")

    assert result is True
    # SIGTERM raised extinct → no emptiness probing needed.
    assert seq == [(111, signal.SIGTERM)]


# ---------------------------------------------------------------------------
# 7. leader NOT a group leader → all signals/probes use the RECORDED pgid
#    (os.getpgid), never the pid. Guards against the false verified-suspended:
#    killpg(pid) would raise ProcessLookupError → misread as "already extinct".
# ---------------------------------------------------------------------------


async def test_leader_not_group_leader_uses_recorded_pgid(monkeypatch):
    seq: list[tuple] = []
    state = {"zero": 0}

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == 0:
            state["zero"] += 1
            if state["zero"] >= 2:          # extinct on the 2nd emptiness probe
                raise ProcessLookupError

    # Leader pid 777, but its process GROUP is 700 (run template never setsid).
    _install(
        monkeypatch, probe="up", pid=777, killpg=killpg,
        getpgid=lambda p: 700,
    )
    calls, sleep = await _no_sleep_recorder()
    result = await s6_rc.force_turn_boundary(engagement_id="e1", sleep=sleep)

    assert result is True
    # SIGTERM first, then emptiness probes — ALL on the recorded pgid 700.
    assert seq[0] == (700, signal.SIGTERM)
    assert seq[1:] == [(700, 0), (700, 0)]
    # The pid 777 is NEVER signalled directly (that is the whole bug).
    assert all(pgid == 700 for pgid, _ in seq)


# ---------------------------------------------------------------------------
# 8. getpgid raises ProcessLookupError (leader vanished in the probe→getpgid
#    window) → cannot identify the group → truthful False, WARN, ZERO signals.
# ---------------------------------------------------------------------------


async def test_getpgid_vanished_returns_false_no_signals(monkeypatch, caplog):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    def _vanished(p):
        raise ProcessLookupError

    _install(
        monkeypatch, probe="up", pid=888, killpg=killpg, getpgid=_vanished,
    )
    with caplog.at_level(logging.WARNING, logger="drivers.s6_rc"):
        result = await s6_rc.force_turn_boundary(engagement_id="e1")

    assert result is False
    assert seq == []               # never guess a pgid → never signal
    assert any(
        "cannot verify suspension" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Driver-side trigger (record_away_refusal → force-end once per episode)
# ---------------------------------------------------------------------------


def _driver(tmp_path, *, monotonic=None):
    from unittest.mock import AsyncMock

    from drivers.claude_code_driver import ClaudeCodeDriver

    kw = {} if monotonic is None else {"monotonic": monotonic}
    return ClaudeCodeDriver(
        engagements_root=str(tmp_path), send_to_topic=AsyncMock(),
        casa_framework_mcp_url="x", **kw)


async def _drain():
    # let the create_task'd force-end coroutine run.
    import asyncio
    for _ in range(5):
        await asyncio.sleep(0)


async def test_second_away_refusal_fires_force_end_once(tmp_path):
    from unittest.mock import AsyncMock

    eid = "eng00000000000001"
    drv = _driver(tmp_path)
    fake = AsyncMock(return_value=True)
    drv._force_turn_boundary = fake
    drv._operator_away[eid] = True
    drv._epoch_pending[eid] = 7

    assert drv.record_away_refusal(eid) == 1
    await _drain()
    fake.assert_not_awaited()

    assert drv.record_away_refusal(eid) == 2
    await _drain()
    fake.assert_awaited_once()
    # the current epoch was marked expected-terminated BEFORE signalling AND is
    # passed to the kill as the expected-epoch guard, with the workspace dir.
    kwargs = fake.await_args.kwargs
    assert kwargs["engagement_id"] == eid
    assert kwargs["expected_epoch"] == 7
    assert kwargs["workspace_dir"].endswith(eid)
    assert drv._forced_suspend_epochs[eid] == 7

    # 3rd refusal in the SAME episode does NOT re-fire.
    assert drv.record_away_refusal(eid) == 3
    await _drain()
    fake.assert_awaited_once()


async def test_new_episode_can_fire_again_after_clear(tmp_path):
    from unittest.mock import AsyncMock

    eid = "eng00000000000002"
    drv = _driver(tmp_path)
    fake = AsyncMock(return_value=True)
    drv._force_turn_boundary = fake
    drv._operator_away[eid] = True

    drv.record_away_refusal(eid)
    drv.record_away_refusal(eid)
    await _drain()
    assert fake.await_count == 1

    # operator returns → away clears (resets the fired flag + counter).
    await drv._clear_operator_away(eid)

    # a NEW away-episode.
    drv._operator_away[eid] = True
    drv.record_away_refusal(eid)
    drv.record_away_refusal(eid)
    await _drain()
    assert fake.await_count == 2


# ---------------------------------------------------------------------------
# Sol A2 wave-3, Finding 1 + F3 (whole-branch gate): the once-per-episode
# backstop RE-ARMS on an unverified (False/unknown) outcome so a transient probe
# failure does not permanently disable it — but a monotonic COOLDOWN must elapse
# before it re-fires (F3), so a doctrine-defying ask→refusal loop cannot churn
# probes/subprocesses at token speed. A VERIFIED (True) outcome latches.
# ---------------------------------------------------------------------------


async def test_unverified_outcome_rearms_after_cooldown(tmp_path):
    from unittest.mock import AsyncMock

    eid = "eng0000000000000a"
    clock = {"t": 1000.0}
    drv = _driver(tmp_path, monotonic=lambda: clock["t"])
    # First force-end returns False (unknown probe); the second returns True.
    fake = AsyncMock(side_effect=[False, True])
    drv._force_turn_boundary = fake
    drv._operator_away[eid] = True

    drv.record_away_refusal(eid)          # 1 — below the threshold
    drv.record_away_refusal(eid)          # 2 — fires; returns False → re-arm+cooldown
    await _drain()
    assert fake.await_count == 1
    assert eid not in drv._away_suspend_fired          # unverified → re-armed
    # A cooldown deadline in the future was recorded.
    assert drv._away_force_cooldown_until[eid] > clock["t"]

    # 3 — still WITHIN the cooldown window → does NOT re-fire (F3 pacing).
    drv.record_away_refusal(eid)
    await _drain()
    assert fake.await_count == 1

    # Advance the injected clock past the cooldown → the next refusal re-fires.
    clock["t"] += 61.0
    drv.record_away_refusal(eid)          # 4 — re-fires; returns True → latch
    await _drain()
    assert fake.await_count == 2
    assert eid in drv._away_suspend_fired               # verified True → latched

    drv.record_away_refusal(eid)          # 5 — latched, NO re-fire
    await _drain()
    assert fake.await_count == 2


# ---------------------------------------------------------------------------
# Sol A2 wave-3, Finding 2: the post-SIGTERM cleanup handoff is CANCEL-EXEMPT
# and leak-free — tracked in a dedicated ``_force_cleanups`` set (never
# ``_tasks[eng_id]``), retired on completion, never cancelled by teardown, and
# recreating no stale ``_tasks`` entry after teardown popped it.
# ---------------------------------------------------------------------------


async def test_force_cleanup_tracked_in_dedicated_set_not_tasks(tmp_path):
    import asyncio

    eid = "eng0000000000000b"
    drv = _driver(tmp_path)

    done = asyncio.Event()

    async def _cleanup():
        await done.wait()
        return True

    t = asyncio.ensure_future(_cleanup())
    drv._register_force_cleanup(eid, t)

    # Tracked in the dedicated set, NOT under _tasks (which teardown cancels).
    assert t in drv._force_cleanups
    assert eid not in drv._tasks

    done.set()
    await asyncio.wait_for(t, timeout=1.0)
    await _drain()
    # Retired via add_done_callback — no reference lingers.
    assert t not in drv._force_cleanups
    assert not drv._force_cleanups


async def test_handoff_after_teardown_leaves_no_stale_tasks_entry(tmp_path):
    import asyncio

    eid = "eng0000000000000c"
    drv = _driver(tmp_path)
    assert eid not in drv._tasks           # teardown already popped it

    async def _cleanup():
        return True

    t = asyncio.ensure_future(_cleanup())
    drv._register_force_cleanup(eid, t)
    # The handoff must NOT recreate a stale _tasks[eng_id] entry (Finding 2c).
    assert eid not in drv._tasks

    await asyncio.wait_for(t, timeout=1.0)
    await _drain()
    assert not drv._force_cleanups


async def test_teardown_does_not_cancel_handed_off_cleanup(tmp_path, monkeypatch):
    import asyncio

    from drivers import s6_rc as _s6

    eid = "eng0000000000000d"
    drv = _driver(tmp_path)

    gate = asyncio.Event()
    killed = {"done": False}

    async def _cleanup():
        await gate.wait()          # simulate the still-running SIGKILL escalation
        killed["done"] = True
        return True

    t = asyncio.ensure_future(_cleanup())
    drv._register_force_cleanup(eid, t)

    # Neutralise the s6/service teardown so cancel() exercises only task handling.
    async def _anoop(*a, **k):
        return None

    monkeypatch.setattr(_s6, "stop_service", _anoop)
    monkeypatch.setattr(_s6, "stop_log_service", _anoop)
    monkeypatch.setattr(_s6, "remove_service_dir", lambda *a, **k: None)
    monkeypatch.setattr(_s6, "_compile_and_update_locked", _anoop)

    await drv.cancel(types.SimpleNamespace(id=eid))
    await _drain()

    # Teardown must NOT cancel the handed-off cleanup — it must finish its kill.
    assert not t.cancelled()
    gate.set()
    assert await asyncio.wait_for(t, timeout=1.0) is True
    assert killed["done"] is True


async def test_abnormal_exit_log_annotated_for_marked_epoch(tmp_path, caplog):
    eid = "eng00000000000003"
    drv = _driver(tmp_path)
    engagement = types.SimpleNamespace(id=eid)

    drv._forced_suspend_epochs[eid] = 42
    with caplog.at_level(logging.INFO, logger="drivers.claude_code_driver"):
        drv._log_abnormal_exit(engagement, 42)
    assert any(
        "forced suspend (operator away)" in r.message
        and r.levelno == logging.INFO
        for r in caplog.records
    )
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


async def test_abnormal_exit_unmarked_epoch_keeps_warn(tmp_path, caplog):
    eid = "eng00000000000004"
    drv = _driver(tmp_path)
    engagement = types.SimpleNamespace(id=eid)

    # a marked DIFFERENT epoch must not annotate this one.
    drv._forced_suspend_epochs[eid] = 42
    with caplog.at_level(logging.INFO, logger="drivers.claude_code_driver"):
        drv._log_abnormal_exit(engagement, 99)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert not any(
        "forced suspend" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Finding 4a: expected-epoch guard. The atomic status+pid probe closes the
# turn-ended/respawn race; if .spawn_epoch no longer equals the armed epoch the
# ladder aborts with False and ZERO signals (never kills the fresh spawn).
# ---------------------------------------------------------------------------


async def test_epoch_mismatch_aborts_with_no_signals(monkeypatch, tmp_path, caplog):
    import signal as _sig

    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    _install(monkeypatch, probe="up", pid=333, killpg=killpg)
    # The workspace epoch advanced past what the driver armed → turn respawned.
    (tmp_path / ".spawn_epoch").write_text("9\n")
    calls, sleep = await _no_sleep_recorder()

    with caplog.at_level(logging.INFO, logger="drivers.s6_rc"):
        result = await s6_rc.force_turn_boundary(
            engagement_id="e1", workspace_dir=str(tmp_path),
            expected_epoch=7, sleep=sleep)

    assert result is False
    assert seq == []              # ZERO signals — the fresh spawn is never killed
    assert _sig.SIGKILL not in [s for _, s in seq]
    assert any("turn already ended" in r.message for r in caplog.records)


async def test_epoch_match_proceeds_to_signal(monkeypatch, tmp_path):
    import signal as _sig

    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == _sig.SIGTERM:
            raise ProcessLookupError   # group already empty on the group signal

    _install(monkeypatch, probe="up", pid=333, killpg=killpg)
    (tmp_path / ".spawn_epoch").write_text("7\n")   # still the armed epoch

    result = await s6_rc.force_turn_boundary(
        engagement_id="e1", workspace_dir=str(tmp_path), expected_epoch=7)

    assert result is True
    assert seq == [(333, _sig.SIGTERM)]   # guard passed → signalled the group


# ---------------------------------------------------------------------------
# Finding 4b: away-clear cancels an in-flight force task; force_turn_boundary
# tolerates mid-poll cancellation cleanly (no SIGKILL after the cancel).
# ---------------------------------------------------------------------------


async def test_clear_operator_away_cancels_inflight_force_task(tmp_path):
    import asyncio

    eid = "eng00000000000005"
    drv = _driver(tmp_path)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking(**kwargs):
        started.set()
        await release.wait()      # simulate a long group-extinction verify
        return True

    drv._force_turn_boundary = _blocking
    drv._operator_away[eid] = True

    drv.record_away_refusal(eid)
    drv.record_away_refusal(eid)      # 2nd refusal fires the force task
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task = drv._force_tasks[eid]
    assert not task.done()

    # Operator returns → away clears → the in-flight kill is cancelled.
    await drv._clear_operator_away(eid)
    await _drain()

    assert task.cancelled()
    assert eid not in drv._force_tasks


async def test_cancellation_before_sigterm_sends_nothing(monkeypatch):
    """Finding 3: a PRE-signal cancel (during the entry probe / epoch guard /
    re-probe) aborts cleanly — asyncio delivers CancelledError only at an await,
    and nothing has been signalled yet."""
    import asyncio

    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    started = asyncio.Event()
    release = asyncio.Event()

    async def _probe(scandir):
        started.set()
        await release.wait()          # wedge the ENTRY probe (pre-signal)
        return ("up", 100)

    monkeypatch.setattr(s6_rc, "_probe_status_and_pid", _probe)
    monkeypatch.setattr(s6_rc, "_getpgid", lambda p: p)
    monkeypatch.setattr(s6_rc, "_killpg", killpg)

    task = asyncio.ensure_future(s6_rc.force_turn_boundary(engagement_id="e1"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seq == []                  # cancelled pre-signal → ZERO signals


async def test_cancellation_after_sigterm_completes_cleanup_via_track_task(
    monkeypatch,
):
    """Finding 3 (the RED): once SIGTERM is sent, a cancel of the caller must NOT
    suppress the extinction poll + SIGKILL escalation. force_turn_boundary hands
    the shielded post-signal cleanup to ``track_task``; the caller re-raises
    CancelledError but the cleanup runs to completion (SIGKILL + verified)."""
    import asyncio

    seq: list[tuple] = []
    state = {"killed": False}

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == signal.SIGKILL:
            state["killed"] = True
            return
        if sig == 0 and state["killed"]:
            raise ProcessLookupError      # extinct only AFTER the SIGKILL
        # SIGTERM + every pre-kill emptiness probe → still alive.

    started = asyncio.Event()
    gate = asyncio.Event()

    async def _sleep(_d):
        started.set()
        await gate.wait()                 # Event-gated fake sleep wedges the poll

    captured: list = []

    _install(monkeypatch, probe="up", pid=444, killpg=killpg)
    task = asyncio.ensure_future(s6_rc.force_turn_boundary(
        engagement_id="e1", sleep=_sleep, track_task=captured.append))

    # SIGTERM delivered + first emptiness probe + the poll wedged on the sleep.
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert (444, signal.SIGTERM) in seq

    # Operator returned → the caller task is cancelled mid-poll.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cleanup was HANDED OFF (not cancelled) and is still running.
    assert len(captured) == 1
    cleanup = captured[0]
    assert not cleanup.done()

    # Release the wedge → the cleanup escalates to SIGKILL and completes truthful.
    gate.set()
    result = await asyncio.wait_for(cleanup, timeout=1.0)
    assert result is True
    assert (444, signal.SIGKILL) in seq   # escalation still ran after the cancel


# ---------------------------------------------------------------------------
# Finding 2: pid-stability re-probe closes the epoch TOCTOU. A respawn can be
# supervisor-visible before .spawn_epoch republishes, so the epoch guard can
# pass against a FRESH group. The re-probe requires the pid UNCHANGED; a changed
# pid → abort False, ZERO signals.
# ---------------------------------------------------------------------------


async def test_pid_change_on_reprobe_aborts_no_signals(monkeypatch, caplog):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    calls = {"n": 0}

    async def _probe(scandir):
        calls["n"] += 1
        # entry probe: up pid 100; re-probe: a FRESH spawn already owns pid 200.
        return ("up", 100 if calls["n"] == 1 else 200)

    monkeypatch.setattr(s6_rc, "_probe_status_and_pid", _probe)
    monkeypatch.setattr(s6_rc, "_getpgid", lambda p: p)
    monkeypatch.setattr(s6_rc, "_killpg", killpg)
    calls_sleep, sleep = await _no_sleep_recorder()

    with caplog.at_level(logging.INFO, logger="drivers.s6_rc"):
        result = await s6_rc.force_turn_boundary(engagement_id="e1", sleep=sleep)

    assert result is False
    assert seq == []                       # pid changed → never signal the group
    assert any("turn already ended" in r.message for r in caplog.records)


async def test_reprobe_unknown_aborts_no_signals(monkeypatch):
    """A re-probe that reads unknown (query failure / malformed) also aborts — we
    do not signal blind once the second read is not a clean ``up`` at the same
    pid."""
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    calls = {"n": 0}

    async def _probe(scandir):
        calls["n"] += 1
        return ("up", 100) if calls["n"] == 1 else ("unknown", None)

    monkeypatch.setattr(s6_rc, "_probe_status_and_pid", _probe)
    monkeypatch.setattr(s6_rc, "_getpgid", lambda p: p)
    monkeypatch.setattr(s6_rc, "_killpg", killpg)

    result = await s6_rc.force_turn_boundary(engagement_id="e1")
    assert result is False
    assert seq == []


# ---------------------------------------------------------------------------
# Finding 1: _probe_status_and_pid returns a strict tri-state — only the two
# coherent shapes (up+live-pid, down+no-pid) are trusted; every contradictory or
# malformed snapshot reads ("unknown", None), never a definite status.
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


@pytest.mark.parametrize("stdout,rc,expected", [
    # Sol A2 wave-3, Finding 1: the REAL ``s6-svstat -o pid`` contract — an UP
    # service publishes its live leader pid (> 0); a DOWN service publishes
    # EXACTLY ``-1``. Only those two coherent shapes are trusted.
    ("true true 4242\n", 0, ("up", 4242)),      # up WITH a live pid → up
    ("true false 4242\n", 0, ("up", 4242)),     # up (wantedup irrelevant) → up
    ("false false -1\n", 0, ("down", None)),     # down sentinel → down
    ("false true -1\n", 0, ("down", None)),      # transitional: leader ALREADY
                                                 #   gone (pid -1), wanted-up →
                                                 #   down for turn-boundary
    ("false false 0\n", 0, ("unknown", None)),   # pid 0 is NOT the down sentinel
    ("false true 0\n", 0, ("unknown", None)),    # pid 0 → not a definite status
    ("false false -2\n", 0, ("unknown", None)),  # negative but NOT -1
    ("false false 4242\n", 0, ("unknown", None)),   # down BUT pid present
    ("true true 0\n", 0, ("unknown", None)),        # up BUT no live pid (<= 0)
    ("true true -1\n", 0, ("unknown", None)),       # up BUT down-sentinel pid
    ("true garbage 4242\n", 0, ("unknown", None)),  # malformed wantedup
    ("garbage true 4242\n", 0, ("unknown", None)),  # malformed up
    ("true true\n", 0, ("unknown", None)),          # missing pid field
    ("true true garbage\n", 0, ("unknown", None)),  # unparseable pid
    ("true true 4242 extra\n", 0, ("unknown", None)),  # too many fields
    ("", 1, ("unknown", None)),                      # query failure (rc != 0)
])
async def test_probe_status_and_pid_strict_shapes(
    monkeypatch, stdout, rc, expected,
):
    # Only the two COHERENT shapes are trusted: an ``up`` service WITH a live pid
    # (> 0), or a ``down`` service publishing the exact ``-1`` sentinel. Every
    # contradictory / malformed / non-sentinel-pid snapshot (including the
    # ``false * 0`` pid-zero reads and the ``false true -1`` case, which reads
    # DOWN because the leader is already gone) is validated against the REAL
    # s6-svstat pid contract so force_turn_boundary never signals blind.
    monkeypatch.setattr(s6_rc.os.path, "isdir", lambda p: True)

    def _run(argv, capture_output=True, text=True, env=None):
        return _FakeCompleted(rc, stdout)

    monkeypatch.setattr(s6_rc.subprocess, "run", _run)
    result = await s6_rc._probe_status_and_pid(
        "/run/service/engagement-e1")
    assert result == expected


async def test_probe_status_and_pid_scandir_absent_is_down(monkeypatch):
    monkeypatch.setattr(s6_rc.os.path, "isdir", lambda p: False)
    assert await s6_rc._probe_status_and_pid("/nope") == ("down", None)


# ---------------------------------------------------------------------------
# F1 (whole-branch gate): bounded shutdown drain of the cancel-exempt
# post-SIGTERM force-suspend cleanups (``drain_force_cleanups``). A gated
# in-flight cleanup is AWAITED before the drain returns True; a cleanup that
# outruns the timeout returns a truthful False (and is NOT cancelled — it is
# cancel-exempt) so shutdown proceeds.
# ---------------------------------------------------------------------------


async def test_drain_force_cleanups_no_pending_returns_true(tmp_path):
    drv = _driver(tmp_path)
    assert await drv.drain_force_cleanups() is True


async def test_drain_force_cleanups_awaits_inflight_then_true(tmp_path):
    import asyncio

    eid = "eng0000000000000d"
    drv = _driver(tmp_path)
    gate = asyncio.Event()
    finished = []

    async def _cleanup():
        await gate.wait()
        finished.append(True)
        return True

    t = asyncio.ensure_future(_cleanup())
    drv._register_force_cleanup(eid, t)

    # Release the cleanup, then drain: the drain must AWAIT it to completion
    # before returning True (it is not yet done when the drain starts).
    async def _release() -> None:
        await asyncio.sleep(0)
        gate.set()

    releaser = asyncio.ensure_future(_release())
    drained = await asyncio.wait_for(drv.drain_force_cleanups(timeout=1.0), 1.5)
    await releaser
    assert drained is True
    assert finished == [True]          # the cleanup actually completed
    assert t.done()


async def test_drain_force_cleanups_timeout_returns_false_and_does_not_cancel(
    tmp_path, caplog,
):
    import asyncio
    import logging

    eid = "eng0000000000000e"
    drv = _driver(tmp_path)
    gate = asyncio.Event()

    async def _slow_cleanup():
        await gate.wait()          # never released within the timeout
        return True

    t = asyncio.ensure_future(_slow_cleanup())
    drv._register_force_cleanup(eid, t)

    with caplog.at_level(logging.WARNING):
        drained = await drv.drain_force_cleanups(timeout=0.05)

    # Truthful False on timeout; the cleanup is cancel-exempt (still running).
    assert drained is False
    assert not t.done()
    assert any("did not finish" in r.message for r in caplog.records)

    # Cleanup after the drain so the test leaves no pending task.
    gate.set()
    await asyncio.wait_for(t, timeout=1.0)


# ---------------------------------------------------------------------------
# F1 (whole-branch gate WAVE 2): the drain must LOOP until STABLE and cover
# BOTH surfaces — an ACTIVE ``_force_tasks`` entry (a post-SIGTERM cleanup
# normally lives INSIDE a still-running force-suspend task, shield-awaited
# there; it only migrates to ``_force_cleanups`` when the owner is cancelled)
# AND a handoff that occurs DURING the drain (caught by the loop's re-snapshot).
# ---------------------------------------------------------------------------


async def test_drain_awaits_active_force_task(tmp_path):
    """An in-flight ``_force_tasks`` entry (the shield-awaiting owner) is drained
    even though nothing has migrated to ``_force_cleanups`` yet."""
    import asyncio

    eid = "engactive00000001"
    drv = _driver(tmp_path)
    gate = asyncio.Event()
    finished: list[bool] = []

    async def _force_task():
        await gate.wait()
        finished.append(True)
        return None

    ft = asyncio.ensure_future(_force_task())
    drv._force_tasks[eid] = ft

    async def _release() -> None:
        await asyncio.sleep(0)
        gate.set()

    releaser = asyncio.ensure_future(_release())
    drained = await asyncio.wait_for(drv.drain_force_cleanups(timeout=1.0), 1.5)
    await releaser
    assert drained is True
    assert finished == [True]          # the active force task actually completed
    assert ft.done()


async def test_drain_loops_until_stable_catches_handoff(tmp_path):
    """A handoff (owner cancelled → cleanup registered) that lands AFTER the
    drain's first snapshot is caught by the loop's re-snapshot, not orphaned."""
    import asyncio

    eid = "enghandoff0000001"
    drv = _driver(tmp_path)
    gate = asyncio.Event()
    cleanup_done: list[bool] = []

    async def _late_cleanup():
        cleanup_done.append(True)
        return True

    async def _force_task():
        await gate.wait()
        # Simulate the cancel→handoff: a NEW cleanup is registered as the force
        # task unwinds, AFTER the drain already snapshotted _force_cleanups.
        drv._register_force_cleanup(eid, asyncio.ensure_future(_late_cleanup()))
        return None

    ft = asyncio.ensure_future(_force_task())
    drv._force_tasks[eid] = ft

    async def _release() -> None:
        await asyncio.sleep(0)
        gate.set()

    releaser = asyncio.ensure_future(_release())
    drained = await asyncio.wait_for(drv.drain_force_cleanups(timeout=1.0), 1.5)
    await releaser
    assert drained is True
    assert cleanup_done == [True]      # the late-handoff cleanup was drained
    assert not drv._force_cleanups


async def test_drain_budget_exhaustion_false_with_counts(tmp_path, caplog):
    """Budget exhausted while BOTH a force task and a cleanup are pending →
    truthful False, neither is cancelled, and the WARN carries both counts."""
    import asyncio
    import logging

    eid = "engbudget00000001"
    drv = _driver(tmp_path)
    gate = asyncio.Event()

    async def _slow():
        await gate.wait()
        return True

    ft = asyncio.ensure_future(_slow())
    ct = asyncio.ensure_future(_slow())
    drv._force_tasks[eid] = ft
    drv._register_force_cleanup(eid, ct)

    with caplog.at_level(logging.WARNING):
        drained = await drv.drain_force_cleanups(timeout=0.05)

    assert drained is False
    assert not ft.done() and not ct.done()      # cancel-exempt — still running
    assert any("did not finish" in r.message for r in caplog.records)

    gate.set()
    await asyncio.gather(ft, ct)


# ---------------------------------------------------------------------------
# Whole-branch gate r3: cancel-in-place visibility + single-flight trigger.
# The Sol repro: pop-then-cancel made a cancelled-but-not-yet-done owner
# invisible to BOTH drain surfaces (its shielded cleanup only registers when
# the cancellation runs, one loop turn later), so the drain returned True with
# SIGKILL/extinction work still pending.
# ---------------------------------------------------------------------------


async def test_cancelled_owner_stays_visible_until_done(tmp_path):
    """``_clear_operator_away`` cancels IN PLACE: the owner stays in
    ``_force_tasks`` (drain-visible) until its done callback retires it, and
    the drain awaits it through the cancellation-time handoff."""
    import asyncio

    eid = "engr3visible00001"
    drv = _driver(tmp_path)
    handed_off: list[bool] = []
    started = asyncio.Event()

    async def _cleanup():
        handed_off.append(True)
        return True

    async def _parked_force_suspend(engagement_id):
        started.set()
        try:
            await asyncio.Event().wait()  # park until cancelled
        except asyncio.CancelledError:
            # Mirror the real path: the shielded post-SIGTERM cleanup is handed
            # off exactly when the owner's cancellation runs.
            drv._register_force_cleanup(
                eid, asyncio.ensure_future(_cleanup()))
            raise

    drv._run_force_suspend = _parked_force_suspend
    drv._trigger_force_suspend(eid)  # installs task + done-callback retirement
    await started.wait()
    ft = drv._force_tasks.get(eid)
    assert ft is not None and not ft.done()

    drv._operator_away[eid] = True
    await drv._clear_operator_away(eid)

    # THE r3 PIN (pre-fix this fails: the owner was popped before cancel).
    assert drv._force_tasks.get(eid) is ft
    drained = await asyncio.wait_for(drv.drain_force_cleanups(timeout=1.0), 1.5)
    assert drained is True
    assert handed_off == [True]
    await asyncio.sleep(0)  # let the done callback retire the handle
    assert drv._force_tasks.get(eid) is None


async def test_trigger_single_flight_defers_to_inflight_kill(tmp_path):
    """A second trigger while an owner is live (e.g. cancelled-but-not-done)
    never overwrites the visible handle — it defers to the in-flight kill."""
    import asyncio

    eid = "engr3single000001"
    drv = _driver(tmp_path)
    started = asyncio.Event()
    calls: list[int] = []

    async def _parked_force_suspend(engagement_id):
        calls.append(1)
        started.set()
        await asyncio.Event().wait()

    drv._run_force_suspend = _parked_force_suspend
    drv._trigger_force_suspend(eid)
    await started.wait()
    first = drv._force_tasks.get(eid)

    drv._trigger_force_suspend(eid)  # single-flight: must defer, not replace
    assert drv._force_tasks.get(eid) is first
    await asyncio.sleep(0)
    assert calls == [1]  # no second coroutine ever started

    first.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await first
