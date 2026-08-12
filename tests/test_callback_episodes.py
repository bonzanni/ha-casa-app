"""v0.147.0 — the attempt-driven delivery worker (redelivery until receipt).

The private episode store and its consumed-key tombstones are gone: the
durable state is the spool's per-flow attempt ledger, and this module owns
only the delivery half of it. Every test therefore drives a REAL
:class:`callback_spool.CallbackSpool` on a real filesystem — a fake ledger
would pin nothing, since the whole point of the rework is that the ledger and
the artifacts are the same truth.

Two injected seams keep it deterministic: ``ce._sleep`` (the retry backoff)
and ``ce._now`` (the schedule clock), mirroring ``callback_spool``'s ``now=``
injection style. A global ``asyncio.sleep`` is NEVER patched — that is the
repo's memory-cage rule.
"""

from __future__ import annotations

import asyncio
import json
import os

import callback_attempts
import callback_episodes as ce
import callback_spool
import pytest

PLUGIN = "demo"
HASH = "a" * 64            # sha256(state) hex — the flow handle

T0 = 1_000_000.0           # the fixture clock's origin


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class _Wired:
    """A real spool + a recording dispatch/notify double + a driven clock."""

    def __init__(self, spool: callback_spool.CallbackSpool) -> None:
        self.spool = spool
        self.clock = T0
        self.entry: dict | None = {"targets": ["resident:assistant"]}
        self.dispatches: list[tuple[str, str, dict]] = []
        self.dispatch_ok = True
        self.notes: list[str] = []
        self.note_error: Exception | None = None
        self.sleeps: list[float] = []
        self.fired = asyncio.Event()

    # -- clock ----------------------------------------------------------
    def now(self) -> float:
        return self.clock

    def advance(self, seconds: float) -> float:
        self.clock += seconds
        return self.clock

    # -- artifacts ------------------------------------------------------
    def result_path(self, h: str, plugin: str = PLUGIN):
        return (self.spool.root / plugin / callback_spool.RESULTS_DIR
                / f"{h}.json")

    def attempt(self, h: str, plugin: str = PLUGIN) -> dict | None:
        for name, rec in self.spool.list_attempts(plugin):
            if name == h:
                return rec
        return None

    def seed_result(self, h: str = HASH, *, plugin: str = PLUGIN,
                    mtime: float | None = None, meta=None) -> float:
        """A published result plus the ``result_ready`` attempt the publish
        would have written, both stamped on the driven clock. Returns the
        result's mtime — the anchor the worker must schedule against."""
        self.spool.ensure_plugin_dirs(plugin)
        path = self.result_path(h, plugin)
        path.write_text(json.dumps({"v": 1, "plugin": plugin}),
                        encoding="utf-8")
        stamp = self.clock if mtime is None else mtime
        os.utime(path, (stamp, stamp))
        rec = callback_attempts.new_attempt(
            state_hash=h, minted_ts=stamp, status="result_ready", meta=meta,
            now=stamp)
        assert self.spool.write_attempt(plugin, h, rec)
        return stamp

    def seed_terminal(self, h: str = HASH, outcome: str = "expired", *,
                      plugin: str = PLUGIN, ended_ts: float | None = None,
                      nudges: int = 0) -> dict:
        """A terminal attempt with NO surviving artifact (the state the sweep
        leaves behind when a flow dies unread)."""
        self.spool.ensure_plugin_dirs(plugin)
        end = self.clock if ended_ts is None else ended_ts
        rec = callback_attempts.new_attempt(
            state_hash=h, minted_ts=end - 100.0, status="result_ready",
            now=end - 100.0)
        rec["nudges"] = nudges
        rec = callback_attempts.terminalize(rec, outcome, now=end)
        assert self.spool.write_attempt(plugin, h, rec)
        return rec


@pytest.fixture
def wired(tmp_path, monkeypatch):
    spool = callback_spool.CallbackSpool(tmp_path / "callbacks")
    spool.ensure_plugin_dirs(PLUGIN)
    state = _Wired(spool)

    monkeypatch.setattr(ce, "_worker_task", None)
    monkeypatch.setattr(ce, "_kick", None)
    monkeypatch.setattr(ce, "_now", state.now)
    ce._pending_hints.clear()

    async def dispatch(role, text, context):
        state.dispatches.append((role, text, context))
        state.fired.set()
        return state.dispatch_ok

    async def notify(text):
        state.notes.append(text)
        if state.note_error is not None:
            raise state.note_error

    async def fake_sleep(s):
        state.sleeps.append(s)

    def _wire(*, notify_operator=notify):
        ce.configure(
            dispatch=dispatch,
            resolve_registry_entry=lambda plugin: state.entry,
            get_spool=lambda: spool,
            notify_operator=notify_operator,
            sleep=fake_sleep,
        )

    state.wire = _wire       # type: ignore[attr-defined]
    _wire()
    try:
        yield state
    finally:
        spool.close()


# ---------------------------------------------------------------------------
# INV-CB-008 — redelivery until receipt (the tombstone regression)
# ---------------------------------------------------------------------------


async def test_surviving_result_gets_a_second_nudge_on_schedule(wired):
    """RED CASE for INV-CB-008. v0.146's consumed-key tombstone was written
    atomically with the bus accept and suppressed every further nudge while
    the result lived — a correctly-approved authorization then died of TTL,
    unread, with exactly one turn spent on it. A result that survives its
    first ACCEPTED nudge must get a second."""
    mtime = wired.seed_result()
    await ce._worker_pass()
    assert len(wired.dispatches) == 1
    rec = wired.attempt(HASH)
    assert rec["nudges"] == 1
    assert rec["next_nudge_ts"] == mtime + 60.0

    # Not yet due: the schedule is a schedule, not a spin.
    await ce._worker_pass()
    assert len(wired.dispatches) == 1

    # +60 s and the result is still sitting there — nudge again.
    wired.advance(60.0)
    await ce._worker_pass()
    assert len(wired.dispatches) == 2
    assert wired.attempt(HASH)["nudges"] == 2


async def test_result_gone_stops_the_nudges(wired):
    wired.seed_result()
    await ce._worker_pass()
    assert len(wired.dispatches) == 1
    # The consumer collected it: the result is gone, so the ledger's own pass
    # infers receipt and nothing further is owed.
    wired.result_path(HASH).unlink()
    wired.advance(60.0)
    await ce._worker_pass()
    assert len(wired.dispatches) == 1
    rec = wired.attempt(HASH)
    assert (rec["status"], rec["outcome"]) == ("done", "collected")


async def test_collected_attempt_never_nudges(wired):
    rec = callback_attempts.new_attempt(
        state_hash=HASH, minted_ts=T0, status="result_ready", now=T0)
    rec = callback_attempts.terminalize(rec, "collected", now=T0)
    assert rec["next_nudge_ts"] is None
    assert wired.spool.write_attempt(PLUGIN, HASH, rec)
    wired.advance(100_000.0)
    await ce._worker_pass()
    assert wired.dispatches == []


# ---------------------------------------------------------------------------
# anchoring (plan amendment 9) — the RESULT INODE'S MTIME
# ---------------------------------------------------------------------------


async def test_result_phase_anchors_on_the_result_mtime(wired):
    """The publish clock is the result inode's mtime, not "now": a nudge
    dispatched late (a restart, a deferral) must not slide the whole cadence
    forward with it."""
    mtime = wired.seed_result(mtime=T0 - 500.0)
    wired.advance(0.0)
    await ce._worker_pass()
    assert len(wired.dispatches) == 1
    rec = wired.attempt(HASH)
    assert wired.spool.result_mtime(PLUGIN, HASH) == mtime
    # +60 from the PUBLISH time, floored at now (never scheduled in the past).
    assert rec["next_nudge_ts"] == max(wired.clock, mtime + 60.0)


async def test_result_mtime_is_three_state_safe(wired):
    assert wired.spool.result_mtime(PLUGIN, HASH) is None      # absent
    mtime = wired.seed_result()
    assert wired.spool.result_mtime(PLUGIN, HASH) == mtime
    assert wired.spool.result_mtime("no-such-plugin", HASH) is None
    assert wired.spool.result_mtime(PLUGIN, "not-a-hash") is None


# ---------------------------------------------------------------------------
# budget and deferral (spec §8)
# ---------------------------------------------------------------------------


async def test_budget_stops_at_exactly_six_accepted_dispatches(wired):
    """Four result-phase slots (0, 60, 180, 480 from the publish time), then
    — once the flow ends unread — two outcome-phase slots (30 m, 2 h from
    ``ended_ts``). Six accepted dispatches, then silence."""
    mtime = wired.seed_result()
    for offset in callback_attempts.RESULT_PHASE_OFFSETS:
        wired.clock = mtime + offset
        await ce._worker_pass()
    assert len(wired.dispatches) == 4
    rec = wired.attempt(HASH)
    assert rec["nudges"] == 4
    assert rec["next_nudge_ts"] is None       # result phase exhausted

    # The result times out unread and the sweep terminalizes it: the outcome
    # phase opens on ended_ts.
    wired.result_path(HASH).unlink()
    ended = wired.advance(900.0)
    assert wired.spool.write_attempt(
        PLUGIN, HASH,
        callback_attempts.terminalize(rec, "expired_unread", now=ended))

    for offset in callback_attempts.OUTCOME_PHASE_OFFSETS:
        wired.clock = ended + offset
        await ce._worker_pass()
    assert len(wired.dispatches) == callback_attempts.MAX_NUDGES
    assert wired.attempt(HASH)["nudges"] == callback_attempts.MAX_NUDGES
    assert wired.attempt(HASH)["next_nudge_ts"] is None

    # Budget spent: no further dispatch, ever.
    wired.advance(1_000_000.0)
    await ce._worker_pass()
    assert len(wired.dispatches) == callback_attempts.MAX_NUDGES


async def test_rejected_pass_consumes_no_budget_and_defers(wired):
    """A bus outage must never spend the consumer's redelivery allowance —
    it escalates the deferral instead, capped so it can never spin."""
    wired.dispatch_ok = False
    wired.seed_result()
    expected = [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0]
    for i, delay in enumerate(expected):
        due = wired.clock
        await ce._worker_pass()
        rec = wired.attempt(HASH)
        assert rec["nudges"] == 0, f"budget spent on rejected pass {i}"
        assert rec["deferrals"] == i + 1
        assert rec["next_nudge_ts"] == due + delay
        wired.clock = due + delay
    # Every pass burned its in-pass retries (3 tries ⇒ 2 backoff sleeps) and
    # every submission was REJECTED — the bus was offered the turn each time.
    assert wired.sleeps == [1.0, 5.0] * len(expected)
    assert len(wired.dispatches) == 3 * len(expected)


async def test_accept_after_deferral_resets_the_streak(wired):
    wired.dispatch_ok = False
    mtime = wired.seed_result()
    await ce._worker_pass()
    assert wired.attempt(HASH)["deferrals"] == 1
    wired.dispatch_ok = True
    wired.advance(60.0)
    await ce._worker_pass()
    rec = wired.attempt(HASH)
    assert rec["deferrals"] == 0
    assert rec["nudges"] == 1
    # The deferral moved the schedule but never the ANCHOR.
    assert rec["next_nudge_ts"] == max(wired.clock, mtime + 60.0)


# ---------------------------------------------------------------------------
# terminal attempts — outcome-phase nudges anchored on ended_ts
# ---------------------------------------------------------------------------


async def test_terminal_expired_nudges_outcome_phase_from_ended_ts(wired):
    rec = wired.seed_terminal(outcome="expired")
    ended = rec["ended_ts"]
    assert rec["next_nudge_ts"] == ended + callback_attempts.OUTCOME_PHASE_OFFSETS[0]

    wired.clock = ended + callback_attempts.OUTCOME_PHASE_OFFSETS[0]
    await ce._worker_pass()
    assert len(wired.dispatches) == 1
    _role, text, _ctx = wired.dispatches[0]
    assert text == (f"Authorization attempt for '{PLUGIN}' ended without "
                    f"collection (handle {HASH}) — check the plugin's "
                    "attempt list.")
    after = wired.attempt(HASH)
    assert after["next_nudge_ts"] == (
        ended + callback_attempts.OUTCOME_PHASE_OFFSETS[1])


# ---------------------------------------------------------------------------
# timed wake — the schedule fires without any kick
# ---------------------------------------------------------------------------


async def test_timed_wake_fires_a_due_nudge_with_no_kick(wired, monkeypatch):
    """A kick-only worker cannot generate scheduled work. With NO kick at all,
    a ``next_nudge_ts`` that comes due must still fire."""
    monkeypatch.setattr(ce, "_MIN_WAKE_S", 0.01)
    mtime = wired.seed_result(mtime=T0 + 300.0)   # due in the future
    await ce._worker_pass()                       # establishes the wake only
    assert wired.dispatches == []
    assert ce._next_due == mtime

    wired.clock = mtime                            # the slot comes due
    assert ce._kick is not None and not ce._kick.is_set()
    task = asyncio.get_running_loop().create_task(ce._worker())
    try:
        await asyncio.wait_for(wired.fired.wait(), timeout=5.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(wired.dispatches) == 1


async def test_wake_timeout_tracks_the_schedule_and_floors(wired):
    # Nothing scheduled: the worker sleeps on the kick alone.
    await ce._worker_pass()
    assert ce._next_due is None
    assert ce._wake_timeout() is None

    mtime = wired.seed_result()
    await ce._worker_pass()                       # accepted; next slot at +60
    assert ce._next_due == mtime + 60.0
    assert ce._wake_timeout() == 60.0

    # An overdue slot floors at the minimum rather than spinning.
    wired.advance(600.0)
    assert ce._wake_timeout() == ce._MIN_WAKE_S


# ---------------------------------------------------------------------------
# exhaustion note — MARK then notify, once (plan amendment 13)
# ---------------------------------------------------------------------------


async def _spend_budget(wired) -> None:
    """Drive a terminal attempt through all six accepted dispatches."""
    rec = wired.seed_terminal(outcome="expired",
                              nudges=callback_attempts.MAX_NUDGES - 1)
    ended = rec["ended_ts"]
    assert rec["nudges"] == callback_attempts.MAX_NUDGES - 1
    wired.clock = ended + callback_attempts.OUTCOME_PHASE_OFFSETS[0]
    await ce._worker_pass()


async def test_exhaustion_note_fires_once_in_the_accept_pass(wired):
    await _spend_budget(wired)
    rec = wired.attempt(HASH)
    assert rec["nudges"] == callback_attempts.MAX_NUDGES
    assert rec["noted"] is True
    assert rec["next_nudge_ts"] is None
    assert len(wired.notes) == 1
    assert HASH in wired.notes[0]

    # Same pass, same process: no second note on any later pass.
    wired.advance(100_000.0)
    await ce._worker_pass()
    assert len(wired.notes) == 1


async def test_noted_survives_a_restart(wired):
    await _spend_budget(wired)
    assert len(wired.notes) == 1
    # Simulated crash-restart: the module's in-memory state is rebuilt from
    # scratch; `noted` lives in the durable attempt file, so no duplicate.
    wired.notes.clear()
    ce._pending_hints.clear()
    wired.wire()
    wired.advance(100_000.0)
    await ce._worker_pass()
    assert wired.notes == []
    assert wired.attempt(HASH)["noted"] is True


async def test_failed_exhaustion_mark_suppresses_the_note(wired, monkeypatch):
    """Mark-then-notify: an unmarked exhaustion emits nothing, because an
    un-suppressible duplicate note is worse than a lost advisory one."""
    real = wired.spool.update_attempt_nudge
    calls: list[dict] = []

    def flaky(plugin, h, **fields):
        calls.append(fields)
        if "noted" in fields:
            return False
        return real(plugin, h, **fields)

    monkeypatch.setattr(wired.spool, "update_attempt_nudge", flaky)
    await _spend_budget(wired)
    assert any("noted" in f for f in calls)
    assert wired.notes == []
    assert wired.attempt(HASH)["noted"] is False


# ---------------------------------------------------------------------------
# duplicate-nudge tolerance — the at-least-once boundary
# ---------------------------------------------------------------------------


async def test_lost_accept_mark_redelivers(wired, monkeypatch):
    """A crash between the bus accept and the attempt-file write costs one
    duplicate nudge — idempotent for the consumer, whose collect against an
    emptied directory is a no-op. It must never cost the nudge itself."""
    wired.seed_result()
    monkeypatch.setattr(wired.spool, "update_attempt_nudge",
                        lambda *a, **k: False)
    await ce._worker_pass()
    assert len(wired.dispatches) == 1
    rec = wired.attempt(HASH)
    assert rec["nudges"] == 0                  # the mark never landed
    monkeypatch.undo()
    await ce._worker_pass()
    assert len(wired.dispatches) == 2          # redelivered, same slot
    assert wired.attempt(HASH)["nudges"] == 1


# ---------------------------------------------------------------------------
# removal records — NOTIFY then mark (at-least-once, spec §10)
# ---------------------------------------------------------------------------


def _removal_records(spool) -> list[tuple[str, dict]]:
    return spool.list_removal_records()


async def test_removal_note_marks_the_record_on_success(wired):
    wired.seed_result()
    assert wired.spool.remove_plugin(PLUGIN) is True
    records = _removal_records(wired.spool)
    assert len(records) == 1 and records[0][1]["noted"] is False

    await ce._worker_pass()
    assert len(wired.notes) == 1
    assert PLUGIN in wired.notes[0]
    assert _removal_records(wired.spool)[0][1]["noted"] is True

    # Noted once: a later pass must not re-notify.
    await ce._worker_pass()
    assert len(wired.notes) == 1


async def test_removal_note_failure_leaves_it_unnoted_and_retries(wired):
    wired.seed_result()
    assert wired.spool.remove_plugin(PLUGIN) is True
    wired.note_error = RuntimeError("bus down")

    await ce._worker_pass()
    assert len(wired.notes) == 1                      # attempted
    assert _removal_records(wired.spool)[0][1]["noted"] is False

    # The NEXT pass retries and succeeds — at-least-once, never at-most-once.
    wired.note_error = None
    await ce._worker_pass()
    assert len(wired.notes) == 2
    assert _removal_records(wired.spool)[0][1]["noted"] is True


async def test_removal_note_send_success_mark_failure_never_resends(
        wired, monkeypatch):
    """#532 (Sol diff r1): a DM that SENT whose mark keeps failing must
    retry only the mark — with un-noted records now never age-pruned, an
    ignored mark failure would otherwise resend the same DM on every pass
    forever, without a crash."""
    wired.seed_result()
    assert wired.spool.remove_plugin(PLUGIN) is True

    fail = {"on": True}
    real_mark = wired.spool.mark_removal_noted

    def flaky_mark(*args, **kwargs):
        if fail["on"]:
            return False
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(wired.spool, "mark_removal_noted", flaky_mark)
    await ce._worker_pass()
    assert len(wired.notes) == 1                      # sent once
    assert _removal_records(wired.spool)[0][1]["noted"] is False

    await ce._worker_pass()                           # mark retried, no resend
    assert len(wired.notes) == 1

    fail["on"] = False
    await ce._worker_pass()
    assert _removal_records(wired.spool)[0][1]["noted"] is True
    assert len(wired.notes) == 1


async def test_removal_note_without_a_notifier_is_never_marked(wired):
    wired.seed_result()
    assert wired.spool.remove_plugin(PLUGIN) is True
    wired.wire(notify_operator=None)

    await ce._worker_pass()
    assert _removal_records(wired.spool)[0][1]["noted"] is False


async def test_removal_records_are_pruned_by_the_pass(wired):
    wired.seed_result()
    assert wired.spool.remove_plugin(PLUGIN) is True
    await ce._worker_pass()
    assert _removal_records(wired.spool)[0][1]["noted"] is True
    # A week after the note, the record is spent.
    wired.advance(callback_spool.REMOVAL_RECORD_PRUNE_S + 1.0)
    await ce._worker_pass()
    assert _removal_records(wired.spool) == []


# ---------------------------------------------------------------------------
# target selection (copied verbatim from plugin_setup_episodes._compose)
# ---------------------------------------------------------------------------


async def test_target_prefers_assistant_when_present(wired):
    wired.entry = {"targets": ["resident:zeta", "resident:assistant"]}
    wired.seed_result()
    await ce._worker_pass()
    role, _text, ctx = wired.dispatches[0]
    assert role == "assistant"
    assert ctx["synthetic"] == "callback_nudge"


async def test_target_first_sorted_resident_fallback(wired):
    wired.entry = {"targets": ["resident:mars", "resident:aqua"]}
    wired.seed_result()
    await ce._worker_pass()
    assert wired.dispatches[0][0] == "aqua"


async def test_specialist_only_delegates_via_assistant(wired):
    wired.entry = {"targets": ["specialist:finance"]}
    wired.seed_result()
    await ce._worker_pass()
    role, text, _ctx = wired.dispatches[0]
    assert role == "assistant"
    assert "'finance'" in text
    assert "Delegate" in text


async def test_no_target_defers_and_notes_once_per_streak(wired):
    wired.entry = {"targets": []}
    wired.seed_result()
    await ce._worker_pass()
    assert wired.dispatches == []
    rec = wired.attempt(HASH)
    assert rec["nudges"] == 0 and rec["deferrals"] == 1
    assert len(wired.notes) == 1
    # A second failing pass defers again but does not re-notify.
    wired.clock = rec["next_nudge_ts"]
    await ce._worker_pass()
    assert wired.attempt(HASH)["deferrals"] == 2
    assert len(wired.notes) == 1


async def test_unresolved_registry_leaves_the_schedule_alone(wired):
    wired.entry = None
    mtime = wired.seed_result()
    await ce._worker_pass()
    assert wired.dispatches == []
    rec = wired.attempt(HASH)
    assert rec["nudges"] == 0 and rec["deferrals"] == 0
    assert rec["next_nudge_ts"] == mtime      # still due — retried next pass


# ---------------------------------------------------------------------------
# message wording + request-path discipline
# ---------------------------------------------------------------------------


async def test_result_nudge_is_the_fixed_v0146_wording(wired):
    wired.seed_result()
    await ce._worker_pass()
    _role, text, _ctx = wired.dispatches[0]
    assert text == (f"Authorization result for '{PLUGIN}' is waiting "
                    f"(handle {HASH}) — collect it now.")


def test_kick_is_o1_and_touches_no_spool(wired, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("kick touched the spool")

    monkeypatch.setattr(ce, "_get_spool", _boom)
    ce.kick(PLUGIN, HASH)
    assert (PLUGIN, HASH) in ce._pending_hints
    assert ce._kick is not None and ce._kick.is_set()


async def test_recovery_runs_a_boot_attempts_pass_and_kicks(wired):
    seen: list[dict] = []
    real = wired.spool.attempts_pass

    def record(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    wired.spool.attempts_pass = record       # type: ignore[method-assign]
    ce._kick.clear()
    await ce.recovery(wired.spool)
    assert seen == [{"now": T0, "boot": True}]
    assert ce._kick.is_set()


async def test_recovery_never_raises(wired):
    def boom(**_kwargs):
        raise OSError("spool on fire")

    wired.spool.attempts_pass = boom          # type: ignore[method-assign]
    await ce.recovery(wired.spool)            # must not propagate
    assert ce._kick.is_set()


async def test_worker_pass_without_a_spool_is_a_no_op(wired):
    ce.configure(dispatch=lambda *a: None,
                 resolve_registry_entry=lambda p: None,
                 get_spool=lambda: None)
    await ce._worker_pass()
    assert ce._next_due is None


def test_worker_owns_no_episode_store():
    """The v0.146 episode store is fully retired — the worker module keeps
    no store path and none of the old store API."""
    assert not hasattr(ce, "STORE_PATH")
    assert not hasattr(ce, "LEGACY_STORE_PATH")
    for gone in ("_load", "_save", "episodes", "_enqueue_locked",
                 "_reconcile_locked", "_mark_dispatched", "_update_episode",
                 "_any_tombstone", "_has_key", "_empty"):
        assert not hasattr(ce, gone), gone
