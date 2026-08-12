"""``event_episodes`` — the plugin-event delivery worker.

Every test drives a REAL :class:`event_spool.EventSpool` on a real
filesystem (mirrors ``test_callback_episodes.py``'s ``wired`` harness) —
the whole point of the pre-send gate is that the ledger and the live
resolver/ack-store are the same truth a fake would paper over. Two
injected seams keep it deterministic: ``ee._sleep`` (the retry backoff) and
``ee._now`` (the schedule clock). The shared ``asyncio.sleep`` is NEVER
patched (the memory-cage rule).
"""
from __future__ import annotations

import asyncio

import pytest

import event_attempts
import event_episodes as ee
import event_spool
from plugin_events import ack_identity, subscribe_declaration_digest

EMITTER = "gmail"
EVENT = "mail_in"
SUBSCRIBER = "finance"
ARTIFACT = "art-1"
T0 = 1_000_000.0


def _manifest(subscribes):
    return {"name": "x", "casa": {
        "subscribes": [{"plugin": e, "event": ev} for e, ev in subscribes]}}


def _identity(subscriber, artifact_id, emitter, event, targets):
    digest = subscribe_declaration_digest({"plugin": emitter, "event": event})
    return ack_identity(subscriber, artifact_id, emitter, event, digest,
                        sorted(targets))


def _snapshot(subscriber, artifact_id, emitter, event, targets):
    return {"subscriber": subscriber, "artifact_id": artifact_id,
            "targets": sorted(targets),
            "ack_identity": _identity(subscriber, artifact_id, emitter,
                                      event, targets)}


class _AckStub:
    def __init__(self):
        self.acked: set = set()

    def get(self, identity):
        return {"identity": identity} if identity in self.acked else None


class _Wired:
    def __init__(self, spool: event_spool.EventSpool) -> None:
        self.spool = spool
        self.clock = T0
        self.entry = {
            "targets": ["resident:assistant"], "artifact_id": ARTIFACT,
            "manifest": _manifest([(EMITTER, EVENT)])}
        self.installed = {SUBSCRIBER}
        self.registry_valid = True
        self.acks = _AckStub()
        self.routed = {(EMITTER, EVENT): {
            SUBSCRIBER: _snapshot(SUBSCRIBER, ARTIFACT, EMITTER, EVENT,
                                  ["resident:assistant"])}}
        self.acks.acked.add(self.routed[(EMITTER, EVENT)][SUBSCRIBER]["ack_identity"])
        self.dispatches: list[tuple[str, str, dict]] = []
        self.dispatch_ok = True
        self.notes: list[str] = []
        self.note_error: Exception | None = None
        self.sleeps: list[float] = []
        self.fired = asyncio.Event()

    def now(self) -> float:
        return self.clock

    def advance(self, seconds: float) -> float:
        self.clock += seconds
        return self.clock

    def seed(self, *, emitter=EMITTER, event=EVENT, subscriber=SUBSCRIBER,
             now=None) -> dict:
        now = self.clock if now is None else now
        self.spool.ensure_emitter_dirs(emitter)
        event_spool.emit(self.spool.root / emitter, event)
        self.spool.fold_pass({(emitter, event): {subscriber}}, now)
        return self.rec(emitter, event, subscriber)

    def rec(self, emitter=EMITTER, event=EVENT, subscriber=SUBSCRIBER):
        return self.spool.read_delivery(emitter, event, subscriber)

    def wire(self, **overrides) -> None:
        async def dispatch(role, text, context):
            self.dispatches.append((role, text, context))
            self.fired.set()
            return self.dispatch_ok

        async def notify(text):
            self.notes.append(text)
            if self.note_error is not None:
                raise self.note_error

        async def fake_sleep(s):
            self.sleeps.append(s)

        def resolve_registry_entry(name):
            if name == SUBSCRIBER:
                return self.entry
            # SOL-P2b's live emitter re-check calls this SAME seam for the
            # EMITTER side of a pair too — synthesize a manifest declaring
            # emits for whatever event(s) the current subscriber's own
            # casa.subscribes associates with this emitter name (good
            # enough for the harness; a test needing a specific emitter
            # shape overrides resolve_registry_entry directly).
            subs = ((self.entry.get("manifest") or {}).get("casa") or {}
                   ).get("subscribes") or []
            events = sorted({s["event"] for s in subs
                            if s.get("plugin") == name})
            if not events:
                return None
            return {"targets": [], "artifact_id": f"emitter-art-{name}",
                   "manifest": {"name": name,
                                "casa": {"emits": [{"name": e}
                                                   for e in events]}}}

        kwargs = dict(
            dispatch=dispatch,
            resolve_registry_entry=resolve_registry_entry,
            get_routed=lambda: self.routed,
            get_installed=lambda: self.installed,
            get_registry_valid=lambda: self.registry_valid,
            get_acks=lambda: self.acks,
            get_spool=lambda: self.spool,
            notify_operator=notify,
            sleep=fake_sleep,
        )
        kwargs.update(overrides)
        ee.configure(**kwargs)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    spool = event_spool.EventSpool(tmp_path / "events")
    state = _Wired(spool)

    monkeypatch.setattr(ee, "_worker_task", None)
    monkeypatch.setattr(ee, "_kick", None)
    monkeypatch.setattr(ee, "_now", state.now)
    ee._pending_hints.clear()

    state.wire()
    try:
        yield state
    finally:
        spool.close()


# ---------------------------------------------------------------------------
# basic accept/defer/ack ladder
# ---------------------------------------------------------------------------


async def test_due_scan_order_is_deterministic(wired):
    wired.spool.ensure_emitter_dirs("zeta")
    wired.entry = {"targets": ["resident:assistant"], "artifact_id": ARTIFACT,
                   "manifest": _manifest([(EMITTER, EVENT), ("zeta", "e")])}
    wired.routed[("zeta", "e")] = {SUBSCRIBER: _snapshot(
        SUBSCRIBER, ARTIFACT, "zeta", "e", ["resident:assistant"])}
    wired.acks.acked.add(wired.routed[("zeta", "e")][SUBSCRIBER]["ack_identity"])
    wired.seed(emitter="zeta", event="e")
    wired.seed()
    wired.wire()
    await ee._worker_pass()
    emitters_seen = [d[2]["emitter"] for d in wired.dispatches]
    assert emitters_seen == sorted(emitters_seen)


async def test_accept_advances_the_ladder(wired):
    wired.seed()
    await ee._worker_pass()
    assert len(wired.dispatches) == 1
    rec = wired.rec()
    assert rec["nudges"] == 1
    assert rec["next_nudge_ts"] == T0 + event_attempts.PHASE_OFFSETS[1]

    # Not yet due.
    await ee._worker_pass()
    assert len(wired.dispatches) == 1

    wired.advance(event_attempts.PHASE_OFFSETS[1])
    await ee._worker_pass()
    assert len(wired.dispatches) == 2
    assert wired.rec()["nudges"] == 2


async def test_concurrent_ack_beats_bookkeeping(wired):
    wired.seed()
    identity = wired.routed[(EMITTER, EVENT)][SUBSCRIBER]["ack_identity"]
    ok, sub = wired.spool.ack(EMITTER, EVENT, wired.rec()["ack_token"], now=wired.clock)
    assert (ok, sub) == ("acked", SUBSCRIBER)
    before = wired.rec()
    assert before["status"] == "done" and before["outcome"] == "acked"

    await ee._worker_pass()
    # No dispatch (not nudgeable — status done) and the record stays acked.
    assert wired.dispatches == []
    assert wired.rec() == before


async def test_concurrent_ack_beats_bookkeeping_on_the_advance_branch(wired):
    """The race that actually matters (Important-4e, review round 1): the
    PREVIOUS pin acked BEFORE the pass even started, so the record was
    never nudgeable and ``_accept`` was never reached at all — unreachable
    in production, since due-scan only ever selects a still-pending
    record. The real race is the ack landing AFTER due-scan selected the
    (still pending) record and dispatch was accepted, but BEFORE the
    accept's own ``update_delivery_nudge`` write lands — simulated here by
    acking from WITHIN the dispatch callable itself (an agent that calls
    ``ack_event`` before casa's own bookkeeping write completes)."""
    rec = wired.seed()
    token = rec["ack_token"]

    async def dispatch_then_ack(role, text, context):
        wired.dispatches.append((role, text, context))
        ok, sub = wired.spool.ack(EMITTER, EVENT, token, now=wired.clock)
        assert (ok, sub) == ("acked", SUBSCRIBER)
        return True

    wired.wire(dispatch=dispatch_then_ack)
    await ee._worker_pass()

    assert len(wired.dispatches) == 1        # the bus WAS offered the turn
    after = wired.rec()
    assert after["status"] == "done" and after["outcome"] == "acked"
    # The accept's own conditional write was REFUSED (status no longer
    # "pending") and skipped SILENTLY — never overwrote the ack, and never
    # spent budget/advanced the schedule on top of it.
    assert after["nudges"] == 0
    assert after["last_nudge_ts"] is None


async def test_reject_defers_and_spends_no_budget(wired):
    wired.dispatch_ok = False
    wired.seed()
    await ee._worker_pass()
    rec = wired.rec()
    assert rec["nudges"] == 0
    assert rec["deferrals"] == 1
    assert rec["next_nudge_ts"] == T0 + event_attempts.DEFERRAL_BASE_S
    assert wired.sleeps == [1.0, 5.0]
    assert len(wired.dispatches) == 3


async def test_six_accepts_exhaust_via_one_atomic_update_then_note(wired):
    rec = wired.seed()
    for i in range(event_attempts.MAX_NUDGES):
        due = wired.rec()["next_nudge_ts"]
        wired.clock = due
        await ee._worker_pass()
    final = wired.rec()
    assert final["nudges"] == event_attempts.MAX_NUDGES
    assert final["status"] == "done"
    assert final["outcome"] == "exhausted"
    assert final["noted"] is True
    assert len(wired.notes) == 1

    # crash-after-mark: no second note, no further mutation possible.
    wired.advance(1_000_000.0)
    await ee._worker_pass()
    assert len(wired.notes) == 1
    assert wired.rec() == final


async def test_failed_exhaustion_write_sends_no_note(wired, monkeypatch):
    """#532: the note follows the durable terminal write (notify-after-mark)
    — when the exhaustion write itself will not go durable, no note fires
    off an unproven state."""
    wired.seed()
    real = wired.spool.update_delivery_nudge

    def flaky(emitter, event, subscriber, gen, mutator):
        # Probe what the mutator WOULD write; refuse the terminal
        # (status=done) write outright — it never lands on disk.
        probe = mutator(dict(wired.rec(emitter, event, subscriber)))
        if probe.get("status") == "done":
            return False
        return real(emitter, event, subscriber, gen, mutator)

    monkeypatch.setattr(wired.spool, "update_delivery_nudge", flaky)
    for i in range(event_attempts.MAX_NUDGES):
        due = wired.rec()["next_nudge_ts"]
        wired.clock = due
        await ee._worker_pass()
    assert [n for n in wired.notes if "went unanswered" in n] == []


# ---------------------------------------------------------------------------
# #532 — exhaustion notice is at-least-once (notify-after-mark, durable retry)
# ---------------------------------------------------------------------------


def _observed_notify(wired):
    """Rewire notify as an OBSERVED seam: raises while ``down``, records
    deliveries only on success (the honest _setup_notify contract)."""
    state = {"down": False}
    delivered: list[str] = []

    async def notify(text):
        if state["down"]:
            raise RuntimeError("telegram channel not ready")
        delivered.append(text)

    wired.wire(notify_operator=notify)
    return state, delivered


async def _drive_to_exhaustion(wired):
    for _ in range(event_attempts.MAX_NUDGES):
        due = wired.rec()["next_nudge_ts"]
        wired.clock = due
        await ee._worker_pass()


async def test_exhaustion_notice_survives_a_boot_window_send_failure(wired):
    """THE #532 red case: the 6th dispatch lands while the channel cannot
    deliver — the record terminalizes un-noted, and the FIRST pass with a
    working channel delivers exactly one notice and marks it."""
    state, delivered = _observed_notify(wired)
    state["down"] = True
    wired.seed()
    await _drive_to_exhaustion(wired)

    rec = wired.rec()
    assert rec["status"] == "done" and rec["outcome"] == "exhausted"
    assert rec["noted"] is False                 # owed, not lost
    assert delivered == []

    state["down"] = False
    await ee._worker_pass()
    assert [n for n in delivered if "went unanswered" in n] != []
    assert wired.rec()["noted"] is True

    # Settled: a later pass neither re-sends nor re-marks.
    count = len(delivered)
    wired.advance(1_000_000.0)
    await ee._worker_pass()
    assert len(delivered) == count


async def test_exhaustion_send_success_mark_failure_never_resends(wired,
                                                                  monkeypatch):
    """Terra design r1 / Sol design r2-r3: a send that succeeded whose mark
    keeps failing must retry ONLY the mark — the DM stream stays bounded
    without a crash, and the sent-key clears only on a confirmed mark."""
    state, delivered = _observed_notify(wired)
    wired.seed()

    fail = {"on": True}
    mark_calls: list[tuple] = []
    real_mark = getattr(wired.spool, "mark_delivery_noted", None)

    def flaky_mark(*args, **kwargs):
        mark_calls.append(args)
        if fail["on"]:
            return False
        assert real_mark is not None
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(wired.spool, "mark_delivery_noted", flaky_mark,
                        raising=False)
    await _drive_to_exhaustion(wired)

    assert len([n for n in delivered if "went unanswered" in n]) == 1
    assert wired.rec()["noted"] is False

    await ee._worker_pass()                      # mark retried, send NOT
    assert len([n for n in delivered if "went unanswered" in n]) == 1
    assert len(mark_calls) >= 2

    fail["on"] = False
    await ee._worker_pass()
    assert wired.rec()["noted"] is True
    assert len([n for n in delivered if "went unanswered" in n]) == 1


async def test_removal_note_send_success_mark_failure_never_resends(
        wired, monkeypatch):
    """The removal-note twin of the bounded-duplicate rule (Sol design
    r2-3 / Terra r2-2): mark failures retry the mark, never the send."""
    _state, delivered = _observed_notify(wired)
    # A corrupt delivery file for an uninstalled plugin sweeps into one
    # removal record (same seeding as the spool's own removal tests).
    bad = wired.spool.root / "ghost" / "delivery"
    bad.mkdir(parents=True)
    (bad / "e--finance.json").write_text("{not json", encoding="utf-8")
    wired.spool.sweep({}, installed=set(), registry_valid=True,
                      now=wired.clock)
    assert len(wired.spool.list_removal_records()) == 1

    monkeypatch.setattr(wired.spool, "mark_removal_noted",
                        lambda *a, **k: False)
    await ee._worker_pass()
    removal_notes = [n for n in delivered if "was removed" in n]
    assert len(removal_notes) == 1

    await ee._worker_pass()                      # still exactly one send
    assert len([n for n in delivered if "was removed" in n]) == 1


async def test_notice_scans_run_under_routing_unavailable(wired):
    """Design R3-1: the removal and unnoted-exhaustion scans are
    notify/mark-only, so they run even under the ROUTING_UNAVAILABLE
    sentinel — an owed notice never waits on routing health."""
    state, delivered = _observed_notify(wired)
    state["down"] = True
    wired.seed()
    await _drive_to_exhaustion(wired)
    assert wired.rec()["noted"] is False

    state["down"] = False
    wired.routed = event_spool.ROUTING_UNAVAILABLE
    await ee._worker_pass()
    assert [n for n in delivered if "went unanswered" in n] != []
    assert wired.rec()["noted"] is True


# ---------------------------------------------------------------------------
# #534 — wake instruction closes with the silence sentinel
# ---------------------------------------------------------------------------


def test_wake_instruction_carries_the_silence_sentinel_contract():
    """#534: the reminder-delivery convention (#511) applied to event
    wakes — imperative, fail-noisy. The delegated-verbatim specialist text
    must NOT carry it (the specialist's turn is not the one that narrates
    into the operator chat); the assistant-directed postscript does."""
    plain = ee._wake_instruction(EMITTER, EVENT, SUBSCRIBER, "tok")
    assert "<silent/>" in plain

    delegated = ee._wake_instruction(EMITTER, EVENT, SUBSCRIBER, "tok",
                                     delegate_ack=True)
    assert "<silent/>" not in delegated
    post = ee._delegated_ack_postscript(EMITTER, EVENT, "tok")
    assert "<silent/>" in post


async def test_ack_stops_the_ladder(wired):
    wired.seed()
    await ee._worker_pass()
    ok, _ = wired.spool.ack(EMITTER, EVENT, wired.rec()["ack_token"], now=wired.clock)
    assert ok == "acked"
    wired.advance(1_000_000.0)
    await ee._worker_pass()
    assert len(wired.dispatches) == 1
    assert wired.rec()["outcome"] == "acked"


# ---------------------------------------------------------------------------
# recovery / kick / timed wake
# ---------------------------------------------------------------------------


async def test_recovery_runs_before_start_and_kicks(wired):
    wired.seed()
    ee._kick.clear()
    await ee.recovery(boot=True)
    assert ee._kick.is_set()
    assert wired.rec() is not None


async def test_recovery_never_raises(wired, monkeypatch):
    def boom(*a, **k):
        raise OSError("spool on fire")

    monkeypatch.setattr(wired.spool, "recovery_pass", boom)
    await ee.recovery(boot=True)          # must not propagate
    assert ee._kick.is_set()


def test_kick_is_o1_and_touches_no_spool(wired, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("kick touched the spool")

    monkeypatch.setattr(ee, "_get_spool", _boom)
    ee.kick(EMITTER, EVENT)
    assert (EMITTER, EVENT) in ee._pending_hints
    assert ee._kick is not None and ee._kick.is_set()


def test_kick_all_sets_the_wake_event(wired):
    ee._kick.clear()
    ee.kick_all()
    assert ee._kick.is_set()


async def test_wake_timeout_tracks_the_schedule_and_floors(wired):
    await ee._worker_pass()
    assert ee._next_due is None
    assert ee._wake_timeout() is None

    wired.seed()
    await ee._worker_pass()
    assert ee._next_due == T0 + event_attempts.PHASE_OFFSETS[1]
    assert ee._wake_timeout() == event_attempts.PHASE_OFFSETS[1]

    wired.advance(1_000_000.0)
    assert ee._wake_timeout() == ee._MIN_WAKE_S


async def test_timed_wake_fires_nudge_two_with_no_kicks(wired, monkeypatch):
    monkeypatch.setattr(ee, "_MIN_WAKE_S", 0.01)
    wired.seed()
    await ee._worker_pass()                # nudge 1, establishes the wake
    assert len(wired.dispatches) == 1
    assert ee._next_due == T0 + event_attempts.PHASE_OFFSETS[1]

    wired.clock = ee._next_due              # nudge 2 comes due
    assert ee._kick is not None and not ee._kick.is_set()
    task = asyncio.get_running_loop().create_task(ee._worker())
    try:
        wired.fired.clear()
        await asyncio.wait_for(wired.fired.wait(), timeout=5.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(wired.dispatches) == 2


async def test_kick_shortens_the_wait(wired, monkeypatch):
    monkeypatch.setattr(ee, "_MIN_WAKE_S", 60.0)
    wired.seed()
    await ee._worker_pass()
    assert ee._wake_timeout() == event_attempts.PHASE_OFFSETS[1]  # far ahead

    task = asyncio.get_running_loop().create_task(ee._worker())
    try:
        await asyncio.sleep(0)              # let it start waiting
        ee.kick_all()
        await asyncio.wait_for(wired.fired.wait(), timeout=5.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# no target / unresolved registry / wake wording
# ---------------------------------------------------------------------------


async def test_no_target_defers_and_notes_once_per_streak(wired):
    wired.entry = {"targets": [], "artifact_id": ARTIFACT,
                   "manifest": _manifest([(EMITTER, EVENT)])}
    wired.seed()
    await ee._worker_pass()
    assert wired.dispatches == []
    rec = wired.rec()
    assert rec["nudges"] == 0 and rec["deferrals"] == 1
    assert len(wired.notes) == 1
    wired.clock = rec["next_nudge_ts"]
    await ee._worker_pass()
    assert wired.rec()["deferrals"] == 2
    assert len(wired.notes) == 1


async def test_unresolved_registry_leaves_the_schedule_alone(wired):
    wired.entry = None
    wired.seed()
    await ee._worker_pass()
    assert wired.dispatches == []
    rec = wired.rec()
    assert rec["nudges"] == 0 and rec["deferrals"] == 0
    assert rec["next_nudge_ts"] == T0


async def test_wake_instruction_and_context_exact(wired):
    wired.seed()
    await ee._worker_pass()
    role, text, ctx = wired.dispatches[0]
    token = wired.rec()  # already acked-advanced; re-read the minted token
    # recompute expected instruction from the ORIGINAL record's token —
    # re-derive it from the delivery record read before dispatch happened
    # is impossible post-hoc (nudges advanced it), so assert shape instead.
    assert text.startswith(f"Plugin '{EMITTER}' emitted the event '{EVENT}'.")
    assert f"headless wake for '{SUBSCRIBER}'" in text
    assert "do not ask" in text
    assert f"ack_event(emitter='{EMITTER}', event='{EVENT}', token='" in text
    assert ctx == {"synthetic": "event_wake", "emitter": EMITTER, "event": EVENT}


async def test_instruction_carries_that_records_token_same_role_isolation(wired):
    """Two subscribers on the SAME role, each dispatched with THEIR OWN
    record's token — never cross-wired."""
    other = "billing"
    wired.installed = {SUBSCRIBER, other}
    wired.entry_by_sub = {
        SUBSCRIBER: wired.entry,
        other: {"targets": ["resident:assistant"], "artifact_id": ARTIFACT,
                "manifest": _manifest([(EMITTER, EVENT)])},
        # SOL-P2b's live emitter re-check resolves EMITTER through this
        # SAME seam — must declare casa.emits for EVENT.
        EMITTER: {"targets": [], "artifact_id": "emitter-art",
                  "manifest": {"name": EMITTER,
                              "casa": {"emits": [{"name": EVENT}]}}},
    }
    wired.routed[(EMITTER, EVENT)][other] = _snapshot(
        other, ARTIFACT, EMITTER, EVENT, ["resident:assistant"])
    wired.acks.acked.add(wired.routed[(EMITTER, EVENT)][other]["ack_identity"])

    def resolve(subscriber):
        return wired.entry_by_sub.get(subscriber, wired.entry)

    wired.wire(resolve_registry_entry=resolve)
    wired.spool.ensure_emitter_dirs(EMITTER)
    event_spool.emit(wired.spool.root / EMITTER, EVENT)
    wired.spool.fold_pass({(EMITTER, EVENT): {SUBSCRIBER, other}}, wired.clock)

    await ee._worker_pass()
    assert len(wired.dispatches) == 2
    rec_sub = wired.rec(subscriber=SUBSCRIBER)
    rec_other = wired.rec(subscriber=other)
    by_subscriber = {}
    for role, text, ctx in wired.dispatches:
        assert role == "assistant"
        if f"'{SUBSCRIBER}'" in text:
            by_subscriber[SUBSCRIBER] = text
        else:
            by_subscriber[other] = text
    assert rec_sub["ack_token"] in by_subscriber[SUBSCRIBER]
    assert rec_other["ack_token"] in by_subscriber[other]
    assert rec_sub["ack_token"] not in by_subscriber[other]
    assert rec_other["ack_token"] not in by_subscriber[SUBSCRIBER]


async def test_specialist_only_target_never_asks_specialist_to_ack(wired):
    """Important-1 pin: a specialist-only target (no resident: entry) is
    ALWAYS delegated via assistant (plugin_dispatch.compose). Specialists
    are operator-installed content this repo does not control and are not
    guaranteed casa-framework tool access, so the text delegated verbatim
    to the specialist must never ask IT to call ack_event — the delegating
    agent (assistant) must, via a separate postscript outside that
    delegated text."""
    wired.entry = {"targets": ["specialist:noop"], "artifact_id": ARTIFACT,
                   "manifest": _manifest([(EMITTER, EVENT)])}
    # The pre-send gate recomputes the consent identity from entry's LIVE
    # targets — the routed snapshot + ack store must agree with the new
    # ["specialist:noop"] targets, not the fixture's default
    # ["resident:assistant"].
    snap = _snapshot(SUBSCRIBER, ARTIFACT, EMITTER, EVENT, ["specialist:noop"])
    wired.routed[(EMITTER, EVENT)][SUBSCRIBER] = snap
    wired.acks.acked.add(snap["ack_identity"])
    rec = wired.seed()
    await ee._worker_pass()

    assert len(wired.dispatches) == 1
    role, text, _ctx = wired.dispatches[0]
    assert role == "assistant"
    assert "Delegate to the specialist 'noop' with the instruction:" in text
    # the specialist's OWN delegated task text must not carry an ack_event
    # CALL — only the "do NOT call it yourself" refusal.
    ack_call = f"ack_event(emitter='{EMITTER}', event='{EVENT}', token='{rec['ack_token']}')"
    assert "Do NOT call ack_event yourself" in text
    # ...but the postscript, directed at the delegating agent, DOES.
    assert ack_call in text
    assert "the delegating agent, not the specialist" in text
    # and the ack call appears exactly once — only in the postscript.
    assert text.count(ack_call) == 1
    # Internal minor #1: the delegated instruction is lexically fenced —
    # quoted where compose relays it, and closed with an explicit marker
    # before the assistant-directed postscript, so the postscript can
    # never be misread as more of the specialist's own delegated text.
    assert 'with the instruction: "Plugin' in text
    assert '" Do not substitute another agent.' in text
    assert text.count("— end of delegated instruction.") == 1
    assert (text.index('" Do not substitute another agent.')
           < text.index("— end of delegated instruction.")
           < text.index(ack_call))


async def test_resident_target_never_carries_the_delegation_wording(wired):
    """Contrast case: a plain resident target must dispatch the ordinary
    (non-delegated) instruction — no "Delegate to the specialist" prose, no
    "Do NOT call ack_event" refusal, no postscript."""
    rec = wired.seed()
    await ee._worker_pass()
    role, text, _ctx = wired.dispatches[0]
    assert role == "assistant"
    assert "Delegate to the specialist" not in text
    assert "Do NOT call ack_event" not in text
    assert f"ack_event(emitter='{EMITTER}', event='{EVENT}', " \
           f"token='{rec['ack_token']}')" in text


async def test_compose_none_defers_with_one_note(wired):
    wired.entry = {"targets": ["specialist:noop-but-empty"],
                   "artifact_id": ARTIFACT,
                   "manifest": _manifest([(EMITTER, EVENT)])}
    # specialist target still routes via assistant delegation — force a
    # genuinely empty target set instead to hit compose's None branch.
    wired.entry["targets"] = []
    wired.seed()
    await ee._worker_pass()
    assert wired.dispatches == []
    assert len(wired.notes) == 1


# ---------------------------------------------------------------------------
# pre-send gate — revoke / artifact update / retarget / digest swap
# ---------------------------------------------------------------------------


async def test_gate_defers_on_revoke_mid_pass(wired):
    """Isolates the PRE-SEND GATE itself (not sweep's independent
    unroute-by-absence path — see decision 23): the delivery record
    already exists (as a real fold would leave it) and ``_run_nudge`` is
    invoked directly, so a bare ack-store revoke is the ONLY thing that
    can make the gate refuse."""
    rec = wired.seed()
    identity = wired.routed[(EMITTER, EVENT)][SUBSCRIBER]["ack_identity"]
    wired.acks.acked.discard(identity)
    await ee._run_nudge(EMITTER, EVENT, SUBSCRIBER, rec)
    assert wired.dispatches == []
    after = wired.rec()
    assert after["nudges"] == 0 and after["deferrals"] == 1


async def test_gate_defers_on_artifact_update(wired):
    rec = wired.seed()
    wired.entry = dict(wired.entry, artifact_id="art-2")
    await ee._run_nudge(EMITTER, EVENT, SUBSCRIBER, rec)
    assert wired.dispatches == []
    assert wired.rec()["deferrals"] == 1


async def test_gate_defers_on_retarget(wired):
    rec = wired.seed()
    wired.entry = dict(wired.entry, targets=["resident:ops"])
    await ee._run_nudge(EMITTER, EVENT, SUBSCRIBER, rec)
    assert wired.dispatches == []
    assert wired.rec()["deferrals"] == 1


async def test_gate_defers_on_pair_unrouted(wired):
    rec = wired.seed()
    wired.routed = {}
    await ee._run_nudge(EMITTER, EVENT, SUBSCRIBER, rec)
    assert wired.dispatches == []
    assert wired.rec()["deferrals"] == 1


async def test_gate_defers_on_declaration_digest_swap_same_artifact(wired,
                                                                    monkeypatch):
    rec = wired.seed()
    import plugin_store

    monkeypatch.setattr(
        plugin_store, "manifest_subscribes",
        lambda manifest, subscriber: [
            {"plugin": EMITTER, "event": EVENT, "digest": "0" * 64}])
    await ee._run_nudge(EMITTER, EVENT, SUBSCRIBER, rec)
    assert wired.dispatches == []
    assert wired.rec()["deferrals"] == 1


async def test_gate_defers_when_emitter_uninstalled_mid_pass(wired):
    """SOL-P2b pin: the EMITTER side of the pair is re-checked live too —
    an emitter uninstalled between fold and send must refuse the send,
    exactly like a stale subscriber-side identity does. The routed
    snapshot itself is left completely untouched here (no reconcile has
    run since the uninstall) — only the live resolve says "gone", proving
    the gate does not just trust the routed map."""
    rec = wired.seed()

    def resolve(name):
        if name == EMITTER:
            return None            # uninstalled — cannot resolve
        return wired.entry

    wired.wire(resolve_registry_entry=resolve)
    await ee._run_nudge(EMITTER, EVENT, SUBSCRIBER, rec)
    assert wired.dispatches == []
    assert wired.rec()["deferrals"] == 1


async def test_gate_defers_when_emitter_upgrade_drops_the_declaration(wired):
    """SOL-P2b pin, the sibling case: the emitter still resolves (still
    installed) but its LIVE manifest no longer declares this event — a
    routine upgrade that renamed or removed the casa.emits entry. Must
    refuse exactly like an outright uninstall does."""
    rec = wired.seed()

    def resolve(name):
        if name == EMITTER:
            return {"targets": [], "artifact_id": "emitter-art-2",
                   "manifest": {"name": EMITTER,
                               "casa": {"emits": [
                                   {"name": "a-different-event"}]}}}
        return wired.entry

    wired.wire(resolve_registry_entry=resolve)
    await ee._run_nudge(EMITTER, EVENT, SUBSCRIBER, rec)
    assert wired.dispatches == []
    assert wired.rec()["deferrals"] == 1


async def test_gate_resolves_subscriber_and_emitter_outside_the_dispatch_lock(
        wired):
    """Internal minor #3: resolving the subscriber AND the emitter — each
    a synchronous, blocking read of a live plugin.json off disk — must
    happen BEFORE ``DISPATCH_LOCK`` is acquired, once per dispatch
    attempt, never while the lock is held. A resolver that asserts the
    lock is free catches a regression that moves either resolve back
    inside the ``async with DISPATCH_LOCK`` block."""
    rec = wired.seed()
    calls = []

    def resolve(name):
        assert not ee.DISPATCH_LOCK.locked(), (
            f"resolve({name!r}) ran while DISPATCH_LOCK was held")
        calls.append(name)
        if name == SUBSCRIBER:
            return wired.entry
        return {"targets": [], "artifact_id": "emitter-art",
               "manifest": {"name": EMITTER,
                           "casa": {"emits": [{"name": EVENT}]}}}

    wired.wire(resolve_registry_entry=resolve)
    await ee._run_nudge(EMITTER, EVENT, SUBSCRIBER, rec)
    assert len(wired.dispatches) == 1
    # both sides were actually resolved (via the gate) at least once —
    # the assertion inside `resolve` is what proves it happened lock-free.
    assert SUBSCRIBER in calls and EMITTER in calls


async def test_gate_failure_kicks_reconcile(wired, monkeypatch):
    kicked = []
    fake_reconcile = type(
        "M", (), {"kick": staticmethod(lambda: kicked.append(1))})
    import sys
    monkeypatch.setitem(sys.modules, "event_reconcile", fake_reconcile)
    rec = wired.seed()
    wired.routed = {}
    await ee._run_nudge(EMITTER, EVENT, SUBSCRIBER, rec)
    assert kicked == [1]


# ---------------------------------------------------------------------------
# ROUTING_UNAVAILABLE — no dispatch, no destructive sweep, full resume
# ---------------------------------------------------------------------------


async def test_sentinel_suspends_dispatch_and_destructive_work(wired):
    wired.seed()
    # A SECOND, still-queued (unfolded) emission — exactly what an
    # authoritative sweep's watermark deletion would destroy for an
    # unrouted pair, and precisely what an authoritative EMPTY map (as
    # opposed to the sentinel) would licence deleting even for a ROUTED
    # pair with no pending record. Important-4d (review round 1): the
    # original test computed this and never asserted on it — a no-op
    # sweep call would have passed silently.
    event_spool.emit(wired.spool.root / EMITTER, EVENT)
    before_emissions = wired.spool.list_emissions(EMITTER, EVENT)
    assert before_emissions                  # sanity: something IS queued
    wired.routed = event_spool.ROUTING_UNAVAILABLE
    await ee._worker_pass()
    assert wired.dispatches == []
    # No fold/sweep destruction — the delivery record and the queued
    # emission both survive untouched, byte-for-byte.
    assert wired.rec() is not None
    assert wired.rec()["nudges"] == 0
    assert wired.spool.list_emissions(EMITTER, EVENT) == before_emissions
    assert ee._next_due is None


async def test_full_resume_after_recovery_when_routing_returns(wired):
    wired.seed()
    wired.routed = event_spool.ROUTING_UNAVAILABLE
    await ee._worker_pass()
    assert wired.dispatches == []

    # Routing recovers.
    wired.routed = {(EMITTER, EVENT): {
        SUBSCRIBER: _snapshot(SUBSCRIBER, ARTIFACT, EMITTER, EVENT,
                              ["resident:assistant"])}}
    await ee._worker_pass()
    assert len(wired.dispatches) == 1


async def test_invalid_registry_suspends_dispatch_and_destructive_work(wired):
    """Critical-2(c) pin: an AUTHORITATIVE routed map is not enough on its
    own — registry_valid=False must suspend dispatch/fold/destructive-sweep
    exactly like the ROUTING_UNAVAILABLE sentinel does, even though
    `wired.routed` here is a real (non-sentinel) mapping the whole time."""
    wired.seed()
    event_spool.emit(wired.spool.root / EMITTER, EVENT)
    before_emissions = wired.spool.list_emissions(EMITTER, EVENT)
    assert before_emissions
    wired.registry_valid = False
    await ee._worker_pass()
    assert wired.dispatches == []
    assert wired.rec() is not None
    assert wired.rec()["nudges"] == 0
    assert wired.spool.list_emissions(EMITTER, EVENT) == before_emissions
    assert ee._next_due is None


async def test_full_resume_after_recovery_when_registry_heals(wired):
    wired.seed()
    wired.registry_valid = False
    await ee._worker_pass()
    assert wired.dispatches == []

    wired.registry_valid = True
    await ee._worker_pass()
    assert len(wired.dispatches) == 1


async def test_worker_pass_without_a_spool_is_a_no_op(wired):
    wired.wire(get_spool=lambda: None)
    await ee._worker_pass()
    assert ee._next_due is None


# ---------------------------------------------------------------------------
# removal records — notify then mark (at-least-once)
# ---------------------------------------------------------------------------


async def test_removal_note_marks_the_record_on_success(wired):
    wired.seed()
    # A subscriber whose plugin was uninstalled is never still ROUTED
    # either (Critical-2(b): the drop path requires status=="done", so a
    # record that stayed routed while merely falling out of `installed`
    # must NOT be dropped while pending — only a genuinely unrouted-and-
    # uninstalled pair terminalizes-then-drops in one pass).
    wired.installed = set()
    wired.routed = {}
    # A single pass both (a) sweeps the now-uninstalled subscriber's
    # record into a durable removal record and (b) processes that SAME
    # removal record's notify-then-mark — both stages run inside one
    # `_worker_pass()`.
    await ee._worker_pass()
    records = wired.spool.list_removal_records()
    assert len(records) == 1 and records[0][1]["noted"] is True
    assert len(wired.notes) == 1

    # Noted once: a later pass must not re-notify.
    await ee._worker_pass()
    assert len(wired.notes) == 1


async def test_removal_note_failure_leaves_it_unnoted_and_retries(wired):
    wired.seed()
    wired.installed = set()
    wired.routed = {}
    wired.note_error = RuntimeError("bus down")
    await ee._worker_pass()          # creates the record AND attempts the note
    assert len(wired.notes) == 1
    assert wired.spool.list_removal_records()[0][1]["noted"] is False

    # The NEXT pass retries and succeeds — at-least-once, never at-most-once.
    wired.note_error = None
    await ee._worker_pass()
    assert len(wired.notes) == 2
    assert wired.spool.list_removal_records()[0][1]["noted"] is True
