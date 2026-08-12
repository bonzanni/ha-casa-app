"""``event_spool.py`` — the ``/data/events`` spool protocol: emission,
casa-minted generations (reconstruct/repair/open), the conditional
delivery update, typed ack, sweep (watermark/valve/quarantine/tombstone/
removal), and the removal-record ledger.

Structure-mirrors ``tests/test_callback_spool.py`` where the protocols
overlap (fixtures, real-thread concurrency, ``os.utime``-driven TTL
determinism); the fold/generation machinery has no callback analogue and
is pinned fresh here against the reviewed spec's red-case list.
"""
import errno
import logging
import os
import threading
import time
from pathlib import Path

import pytest

import event_attempts as ea
import event_spool as es
from event_spool import (
    FOLD_BATCH_MAX,
    MAX_EMISSION_FILES,
    QUIESCENCE_S,
    TEMP_TTL_S,
    EventSpool,
    MarkerState,
    ROUTING_UNAVAILABLE,
)

E = "finance"              # emitter plugin name
EV = "invoice-created"     # event name
S1 = "reporting"           # subscriber plugin name
S2 = "audit"                # a second subscriber


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def spool(tmp_path):
    s = EventSpool(tmp_path / "events")
    s.ensure_emitter_dirs(E)
    try:
        yield s
    finally:
        s.close()


def _edir(spool, emitter=E) -> Path:
    return Path(spool.root) / emitter


def _emissions_dir(spool, emitter=E) -> Path:
    return _edir(spool, emitter) / "emissions"


def _state_dir(spool, emitter=E) -> Path:
    return _edir(spool, emitter) / "state"


def _delivery_dir(spool, emitter=E) -> Path:
    return _edir(spool, emitter) / "delivery"


def _utime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def R(*subs, emitter=E, event=EV) -> dict:
    """A one-pair routed map: ``{(emitter, event): {subs...}}``."""
    return {(emitter, event): set(subs)}


def _emit(spool, *, emitter=E, event=EV, when=None) -> Path:
    p = es.emit(_edir(spool, emitter), event)
    if when is not None:
        _utime(p, when)
    return p


def _emit_n(spool, n, *, emitter=E, event=EV, start=1000.0, step=1.0) -> list:
    return [_emit(spool, emitter=emitter, event=event, when=start + i * step)
            for i in range(n)]


def _delivery_path(spool, subscriber, *, emitter=E, event=EV) -> Path:
    return _delivery_dir(spool, emitter) / f"{event}--{subscriber}.json"


def _read_delivery(spool, subscriber, *, emitter=E, event=EV) -> dict:
    return spool.read_delivery(emitter, event, subscriber)


def _read_state(spool, *, emitter=E, event=EV) -> dict:
    return spool.read_state(emitter, event)


def _write_raw(path: Path, text: str, when: "float | None" = None) -> Path:
    path.write_text(text)
    if when is not None:
        _utime(path, when)
    return path


# ---------------------------------------------------------------------------
# ensure_emitter_dirs
# ---------------------------------------------------------------------------


def test_ensure_emitter_dirs_creates_0770_tree(spool):
    base = _edir(spool)
    assert base.is_dir()
    for sub in ("emissions", "state", "delivery"):
        assert (base / sub).is_dir()
    assert (base / ".dir-id").is_file()
    assert oct(base.stat().st_mode)[-3:] == "770"


def test_ensure_emitter_dirs_is_idempotent(spool):
    tok1 = (_edir(spool) / ".dir-id").read_text()
    spool.ensure_emitter_dirs(E)
    tok2 = (_edir(spool) / ".dir-id").read_text()
    assert tok1 == tok2


def test_ensure_emitter_dirs_refuses_unsafe_name(spool):
    with pytest.raises(ValueError):
        spool.ensure_emitter_dirs("../escape")
    with pytest.raises(ValueError):
        spool.ensure_emitter_dirs(".removals")


# ---------------------------------------------------------------------------
# emit() — the consumer-side reference
# ---------------------------------------------------------------------------


def test_emit_publishes_canonical_v1_envelope_0600(spool):
    p = es.emit(_edir(spool), EV)
    assert p.parent == _emissions_dir(spool)
    assert p.name.startswith(f"{EV}--") and p.name.endswith(".json")
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert p.read_bytes() == b'{"v":1}'


def test_emit_two_calls_never_collide(spool):
    p1 = es.emit(_edir(spool), EV)
    p2 = es.emit(_edir(spool), EV)
    assert p1 != p2
    assert p1.exists() and p2.exists()


def test_emit_refuses_unsafe_event_name(spool):
    with pytest.raises(ValueError):
        es.emit(_edir(spool), "bad--name")
    with pytest.raises(ValueError):
        es.emit(_edir(spool), "../escape")


def test_emit_leaves_no_part_residue_on_success(spool):
    es.emit(_edir(spool), EV)
    names = os.listdir(_emissions_dir(spool))
    assert all(not n.startswith(".part-") for n in names)


def test_emit_is_exactly_once_under_two_threads(spool):
    """Two real threads emitting concurrently must both land intact —
    the write-then-rename sequence is race-safe under contention, even
    though (unlike callback's claim) there is no winner to arbitrate:
    each call's random suffix is its own."""
    start = threading.Barrier(2)
    results = [None, None]

    def worker(i):
        start.wait()
        results[i] = es.emit(_edir(spool), EV)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results[0] is not None and results[1] is not None
    assert results[0] != results[1]
    assert results[0].read_bytes() == b'{"v":1}'
    assert results[1].read_bytes() == b'{"v":1}'


# ---------------------------------------------------------------------------
# fold_pass — happy path / open / idle / cycling
# ---------------------------------------------------------------------------


def test_fold_pass_noop_with_no_emissions_no_state(spool):
    changed = spool.fold_pass(R(S1), 1000.0)
    assert changed == []
    assert _read_state(spool) is None


def test_fold_pass_does_not_open_with_empty_routed_cohort(spool):
    _emit(spool, when=1000.0)
    changed = spool.fold_pass({}, 2000.0)
    assert changed == []
    assert _read_state(spool) is None


def test_fold_pass_opens_gen1_and_mints_pending_records(spool):
    _emit(spool, when=1000.0)
    changed = spool.fold_pass(R(S1, S2), 2000.0)
    assert set(changed) == {(E, EV, S1), (E, EV, S2)}
    state = _read_state(spool)
    assert state["gen"] == 1
    assert state["cohort"] == sorted([S1, S2])
    assert len(state["folded"]) == 1        # the one emission's token —
    # the FIELD records what was folded into this generation; the FILE
    # itself is what gets unlinked
    assert list(_emissions_dir(spool).glob(f"{EV}--*")) == []

    rec1 = _read_delivery(spool, S1)
    assert rec1["status"] == "pending" and rec1["gen"] == 1
    rec2 = _read_delivery(spool, S2)
    assert rec2["status"] == "pending" and rec2["gen"] == 1
    assert rec1["ack_token"] != rec2["ack_token"]


def test_fold_pass_not_idle_while_a_delivery_is_pending(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 2000.0)
    _emit(spool, when=2100.0)               # arrives mid-generation
    changed = spool.fold_pass(R(S1), 2200.0)
    assert changed == []                    # not idle: gen1 still pending
    state = _read_state(spool)
    assert state["gen"] == 1
    # the new emission is neither folded nor deleted — it survives
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))


def test_emit_during_in_flight_generation_is_never_lost(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 2000.0)
    new_emission = _emit(spool, when=2100.0)
    spool.fold_pass(R(S1), 2200.0)          # not idle — folds nothing
    assert new_emission.exists()

    # ack the gen-1 record -> idle
    rec = _read_delivery(spool, S1)
    outcome, sub = spool.ack(E, EV, rec["ack_token"], now=2300.0)
    assert (outcome, sub) == ("acked", S1)

    changed = spool.fold_pass(R(S1), 2400.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 2
    assert not new_emission.exists()        # now folded away


def test_second_cycle_after_terminal_opens_gen3(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    spool.ack(E, EV, rec["ack_token"], now=1200.0)

    _emit(spool, when=1300.0)
    spool.fold_pass(R(S1), 1400.0)
    rec = _read_delivery(spool, S1)
    assert rec["gen"] == 2
    spool.ack(E, EV, rec["ack_token"], now=1500.0)

    _emit(spool, when=1600.0)
    spool.fold_pass(R(S1), 1700.0)
    rec = _read_delivery(spool, S1)
    assert rec["gen"] == 3


def test_late_routed_subscriber_joins_the_next_generation_only(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)          # gen1, cohort={S1}
    assert _read_delivery(spool, S2) is None

    rec = _read_delivery(spool, S1)
    spool.ack(E, EV, rec["ack_token"], now=1200.0)   # idle
    _emit(spool, when=1300.0)
    changed = spool.fold_pass(R(S1, S2), 1400.0)      # S2 now routed
    assert set(changed) == {(E, EV, S1), (E, EV, S2)}
    state = _read_state(spool)
    assert state["gen"] == 2
    assert state["cohort"] == sorted([S1, S2])


def test_fold_batch_bound_65_folds_64_oldest_65th_survives(spool):
    paths = _emit_n(spool, 65, start=1000.0, step=1.0)
    changed = spool.fold_pass(R(S1), 2000.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 1
    assert len(state["folded"]) == FOLD_BATCH_MAX
    remaining = list(_emissions_dir(spool).glob(f"{EV}--*.json"))
    assert len(remaining) == 1
    assert remaining[0].name == paths[-1].name    # the newest one survives


def test_folded_remainder_folds_into_the_next_generation(spool):
    _emit_n(spool, 65, start=1000.0, step=1.0)
    spool.fold_pass(R(S1), 2000.0)
    rec = _read_delivery(spool, S1)
    spool.ack(E, EV, rec["ack_token"], now=2100.0)
    changed = spool.fold_pass(R(S1), 2200.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 2
    assert len(state["folded"]) == 1
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_unfoldable_emission_name_never_drives_repeated_opens(spool, caplog):
    """Round-2 NEW-2: a name that parses as ``<event>--<rest>`` but
    carries no valid u32hex token can never be folded — nothing ever
    unlinks it via ``state.folded``. Left in place it would keep
    ``emissions`` perpetually non-empty at OPEN, opening (and wasting) a
    fresh generation every idle pass forever with no real emission ever
    arriving. It must never drive OPEN at all — between sweeps too — and
    an authoritative sweep reaps it outright, in one log line."""
    junk = _emissions_dir(spool) / f"{EV}--not-8-hex.json"
    junk.write_bytes(b'{"v":1}')
    _utime(junk, 999.0)

    # The junk file alone must never open a generation.
    changed = spool.fold_pass(R(S1), 1000.0)
    assert changed == []
    assert _read_state(spool) is None

    real = _emit(spool, when=1001.0)
    changed = spool.fold_pass(R(S1), 1100.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 1
    assert not real.exists()          # the real emission WAS folded
    assert junk.exists()              # the junk one is inert to fold —
    # never counted, never touched by fold_pass at all

    # Ack + several more fold passes with no new REAL emission: gen must
    # never advance again just because the junk file is still sitting
    # there (previously: a fresh generation, and thus a wasted wake,
    # every single pass — gen 2, 3, 4, 5… forever).
    rec = _read_delivery(spool, S1)
    spool.ack(E, EV, rec["ack_token"], now=1200.0)
    for i in range(3):
        spool.fold_pass(R(S1), 1300.0 + i)
    assert _read_state(spool)["gen"] == 1

    # The first authoritative sweep reaps it, with one log line.
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        report = spool.sweep(R(S1), installed={S1}, registry_valid=True,
                             now=1400.0)
    assert report.deleted_unfoldable == 1
    assert not junk.exists()
    assert any("unfoldable" in r.message for r in caplog.records)

    spool.fold_pass(R(S1), 1500.0)
    assert _read_state(spool)["gen"] == 1


# ---------------------------------------------------------------------------
# fold_pass — non-resurrection (Sol-r3 #2 pin)
# ---------------------------------------------------------------------------


def test_removed_mid_generation_member_is_never_recreated(spool):
    """Genuine mutation-killer for REPAIR's ``cohort & routed_subs``
    filter: force the state to a gen ABOVE a member's own record while
    that member is simultaneously de-routed. Only the routing
    intersection — not the gen comparison alone — can be what excludes
    them; if `& routed_subs` were dropped, REPAIR's `rec["gen"] <
    state_gen` check by itself would still fire for the de-routed member
    too, and this test would catch that."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1, S2), 1100.0)      # gen1, cohort={S1,S2}, both
    # get a real gen1 record
    gen1 = _read_state(spool)["gen"]

    # Bump the state to a HIGHER generation directly (as a fold's own
    # OPEN would, but without touching either delivery record) — both
    # S1 and S2 now sit on a stale gen relative to state.
    bumped = dict(_read_state(spool), gen=gen1 + 1)
    _write_raw(_state_dir(spool) / f"{EV}.json",
              es.canonical_marker_bytes(bumped).decode())

    spool.fold_pass(R(S1), 1200.0)          # S2 now de-routed
    rec1 = _read_delivery(spool, S1)
    assert rec1["gen"] == bumped["gen"]     # routed + stale gen: backfilled
    rec2 = _read_delivery(spool, S2)
    assert rec2["gen"] == gen1              # de-routed + stale gen: the
    assert rec2["status"] == "pending"      # routing filter alone excludes
    # it — never touched, never recreated


def test_deroute_then_sweep_drop_then_reconsent_never_resurrects(spool):
    """The full sweep-driven non-resurrection story (Sol-r3 #2 /
    Important-1 pin): once a member is de-routed, terminalized by sweep,
    and its plugin is reinstalled + re-consented WITHOUT any new
    emission, REPAIR must not mint it a fresh record — that would
    reverse a settled terminal outcome and wake for emissions folded
    before the current consent ever existed."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1, S2), 1100.0)      # gen1, cohort={S1,S2}
    assert _read_delivery(spool, S2) is not None

    # S2 is de-routed mid-generation; a REPAIR pass must not touch it.
    spool.fold_pass(R(S1), 1200.0)          # S2 no longer routed
    rec2 = _read_delivery(spool, S2)
    assert rec2 is not None and rec2["status"] == "pending"   # untouched

    # S2's plugin is uninstalled — sweep terminalizes AND drops its
    # tombstone, durable removal record first.
    report = spool.sweep(R(S1), installed={S1}, registry_valid=True,
                         now=1300.0)
    assert report.dropped_records == 1
    assert _read_delivery(spool, S2) is None

    # S2 reinstalls and re-consents — routed again, with NO new emission.
    changed = spool.fold_pass(R(S1, S2), 1400.0)
    assert changed == []
    assert _read_delivery(spool, S2) is None    # never resurrected
    state = _read_state(spool)
    assert state["gen"] == 1                    # no new generation opened
    # either (S1 is still pending — not idle — and there was no new
    # emission to fold regardless)

    # A LEGITIMATE re-admission only ever happens through a fresh OPEN:
    # once S1 resolves and a new emission arrives, S2 rejoins as part of
    # the next generation's LIVE routed cohort.
    rec1 = _read_delivery(spool, S1)
    spool.ack(E, EV, rec1["ack_token"], now=1500.0)
    _emit(spool, when=1600.0)
    changed = spool.fold_pass(R(S1, S2), 1700.0)
    assert set(changed) == {(E, EV, S1), (E, EV, S2)}
    state = _read_state(spool)
    assert state["gen"] == 2
    assert state["cohort"] == sorted([S1, S2])


# ---------------------------------------------------------------------------
# fold_pass — reconstruction-first healing (Sol-r3 #3 / Terra-r3 #3 pins)
# ---------------------------------------------------------------------------


def test_reconstruction_rebuilds_state_at_max_record_gen_no_emission(spool):
    """Important-4(a) pin: TWO surviving records at DIFFERENT gens — the
    vacuous single-record version of this test could never distinguish
    "picks the max" from a bug that picked the min, the first, or the
    last record instead. S1 is a STALE gen-2 straggler (e.g. a member
    already terminalized/dropped and never resurrected); S2 is the
    CURRENT gen-4 member. Reconstruction must rebuild at gen 4 (the true
    max) with a cohort containing ONLY the member actually AT that gen —
    S1's stale gen-2 record must never be folded into the rebuilt
    cohort."""
    now = 5000.0
    stale = ea.new_record(E, EV, S1, 2, "tok-stale", now - 500)
    current = ea.new_record(E, EV, S2, 4, "tok-preserved", now - 100)
    spool.ensure_emitter_dirs(E)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(stale).decode())
    _write_raw(_delivery_path(spool, S2),
              es.canonical_marker_bytes(current).decode())
    assert _read_state(spool) is None

    changed = spool.fold_pass(R(S2), now)
    assert changed == []                     # nothing NEW minted — the
    # surviving current-gen record's ladder is preserved verbatim
    state = _read_state(spool)
    assert state is not None
    assert state["gen"] == 4                 # the MAX, not the min (2)
    assert state["cohort"] == [S2]            # only the max-gen member —
    # S1's stale gen-2 record never joins the rebuilt cohort
    assert state["folded"] == []
    rec_after = _read_delivery(spool, S2)
    assert rec_after["ack_token"] == "tok-preserved"
    # S1's stale record is untouched — reconstruction never rewrites a
    # record it did not choose as the cohort's current member.
    stale_after = _read_delivery(spool, S1)
    assert stale_after["ack_token"] == "tok-stale" and stale_after["gen"] == 2
    assert rec_after["gen"] == 4
    assert rec_after["status"] == "pending"


def test_reconstruction_with_queued_emission_also_repairs_and_stays_pending(spool):
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 4, "tok-preserved", now - 100)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())
    _emit(spool, when=now - 50)              # queued while state was lost

    changed = spool.fold_pass(R(S1), now)
    state = _read_state(spool)
    assert state["gen"] == 4                 # never re-minted below max
    # not idle (S1 still pending) — the queued emission must survive,
    # unfolded, until S1's gen-4 delivery resolves
    assert changed == []
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))


def test_reconstruction_quarantines_the_corrupt_original_and_surfaces_issue(spool):
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 2, "tok-x", now - 10)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())
    _write_raw(_state_dir(spool) / f"{EV}.json", "{not json")

    spool.fold_pass(R(S1), now)
    state = _read_state(spool)
    assert state["gen"] == 2

    corrupt = list(_state_dir(spool).glob(".corrupt-*"))
    assert len(corrupt) == 1
    issues = spool.spool_issues()
    assert any(i["kind"] == "corrupt_state" and i["emitter"] == E
              for i in issues)


def test_state_lost_with_no_records_silently_resets_to_gen1_no_issue(spool):
    now = 5000.0
    _emit(spool, when=now - 10)
    changed = spool.fold_pass(R(S1), now)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 1
    assert spool.spool_issues() == []


def test_reconstruction_repair_excludes_a_subscriber_consented_mid_crash(spool):
    """The subscriber consented AFTER the crash lost the state file must
    NOT join the reconstructed generation — cohort comes from the
    surviving records, never the live routed map."""
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 2, "tok-x", now - 10)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())

    spool.fold_pass(R(S1, S2), now)          # S2 routed NOW, but was not
    # a gen-2 record holder
    state = _read_state(spool)
    assert state["gen"] == 2
    assert state["cohort"] == [S1]
    assert _read_delivery(spool, S2) is None


def test_failed_reconstruction_write_skips_open_this_pass(spool, monkeypatch):
    """Minor-1 pin: if the reconstructed state fails to go durable this
    pass, ``cur_gen`` must never silently fall back to 0 — that would
    let OPEN mint a fresh generation BELOW the surviving record's true
    gen the instant the write fails to land."""
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 5, "tok-x", now - 100)
    rec = ea.terminalize(rec, "acked", now=now - 50)   # done -> idle=True,
    # so OPEN WOULD be tempted to fire this pass if cur_gen wrongly fell
    # back to 0
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())
    _emit(spool, when=now - 10)

    monkeypatch.setattr(es.EventSpool, "_write_state",
                        lambda self, efd, event, obj: False)
    changed = spool.fold_pass(R(S1), now)
    assert changed == []
    assert _read_state(spool) is None          # reconstruction never landed
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))  # emission
    # survives — never folded into a bogus gen 1

    monkeypatch.undo()
    changed = spool.fold_pass(R(S1), now + 10)
    state = _read_state(spool)
    assert state["gen"] >= 5           # reconstructed at (at least) the
    # surviving record's gen — NEVER gen 1
    assert state["gen"] != 1


def test_quarantine_failure_defers_reconstruction_instead_of_overwriting(spool, monkeypatch, caplog):
    """Minor-3 pin: when the corrupt state file cannot be moved aside,
    the reconstructed state must never silently overwrite it (the only
    surviving forensic evidence of the corruption) — the failure must be
    logged and reconstruction deferred, not swallowed."""
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 3, "tok-x", now - 10)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())
    _write_raw(_state_dir(spool) / f"{EV}.json", "{not json")

    monkeypatch.setattr(es.EventSpool, "_quarantine_state",
                        lambda self, efd, event, now: False)
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        spool.fold_pass(R(S1), now)
    assert _read_state(spool) is None          # not overwritten this pass
    assert (_state_dir(spool) / f"{EV}.json").read_text() == "{not json"
    assert any("quarantine" in r.message for r in caplog.records)

    monkeypatch.undo()
    spool.fold_pass(R(S1), now + 10)
    state = _read_state(spool)
    assert state["gen"] == 3
    assert list(_state_dir(spool).glob(".corrupt-*"))


def test_fold_pass_defers_rather_than_reconstructs_from_an_unreadable_state(
        spool, monkeypatch):
    """Important-2 pin: an EMFILE-style open failure reading the STATE file
    must defer the WHOLE pair this pass — no reconstruct, no repair, no
    open. Treating an OSError the same as "state absent/corrupt" would
    silently roll a healthy generation back to whatever the surviving
    delivery records show, or worse, quarantine a state file whose content
    was never even seen."""
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 5, "tok-x", now - 100)
    rec = ea.terminalize(rec, "acked", now=now - 50)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())
    real_state = {"v": es.STATE_SCHEMA_VERSION, "event": EV, "gen": 5,
                  "cohort": [S1], "folded": [], "opened_ts": now - 200}
    _write_raw(_state_dir(spool) / f"{EV}.json",
              es.canonical_marker_bytes(real_state).decode())
    _emit(spool, when=now - 10)

    target = f"{EV}.json"
    real_open = os.open

    def flaky_open(name, flags, *a, **kw):
        if name == target:
            raise OSError(errno.EMFILE, "too many open files")
        return real_open(name, flags, *a, **kw)

    monkeypatch.setattr(es.os, "open", flaky_open)
    try:
        changed = spool.fold_pass(R(S1), now)
    finally:
        monkeypatch.undo()

    assert changed == []
    # the REAL (healthy, gen-5) state file was never reconstructed over,
    # quarantined, or otherwise touched.
    assert (_state_dir(spool) / f"{EV}.json").exists()
    assert list(_state_dir(spool).glob(".corrupt-*")) == []

    # a later pass, fd pressure gone, reads the SAME gen-5 state back.
    state = _read_state(spool)
    assert state["gen"] == 5


def test_fold_treats_an_unreadable_pending_delivery_as_blocking_not_absent(
        spool, monkeypatch, caplog):
    """Sol+Terra converged: a transient read failure (EMFILE/EIO) on a
    PENDING delivery record must never fold into "absent" the way a
    genuinely missing record does — that would let fold compute
    idle=True (the unreadable record is invisible to the pending scan)
    and OPEN a fresh generation right over an in-flight delivery this
    pass simply failed to see, rotating its token out from under it. An
    unreadable delivery must BLOCK the whole pair this pass: no
    reconstruct, no repair, no open, no unlink, no state advance —
    mirroring the existing unreadable-STATE defer exactly."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)                 # gen1, S1 pending
    path = _delivery_path(spool, S1)
    assert path.exists()
    _emit(spool, when=1200.0)                      # a second, queued emission

    target = path.name
    real_open = os.open

    def flaky_open(name, flags, *a, **kw):
        if name == target:
            raise OSError(errno.EMFILE, "too many open files")
        return real_open(name, flags, *a, **kw)

    monkeypatch.setattr(es.os, "open", flaky_open)
    try:
        with caplog.at_level(logging.WARNING, logger="event_spool"):
            changed = spool.fold_pass(R(S1), 1300.0)
    finally:
        monkeypatch.undo()

    assert changed == []
    state = _read_state(spool)
    assert state["gen"] == 1                        # no new generation opened
    # the queued 1200.0 emission was never folded, and gen1's already-
    # folded emission was never re-unlinked off an unproven read
    assert len(list(_emissions_dir(spool).glob(f"{EV}--*.json"))) == 1
    assert sum("unreadable" in r.message for r in caplog.records) == 1  # once

    # fd pressure gone: a later pass resumes normally — S1's gen1 record
    # is still pending, so there is still nothing to fold (not idle).
    changed = spool.fold_pass(R(S1), 1400.0)
    assert changed == []
    rec = _read_delivery(spool, S1)
    assert rec is not None and rec["gen"] == 1 and rec["status"] == "pending"


def test_repair_reproves_delivery_durability_before_unlinking_folded_emissions(
        spool, monkeypatch):
    """Sol I3 residual: ``_strict_replace_at``'s rename can land while its
    OWN post-rename dir fsync still fails — ``_write_delivery`` correctly
    reports False, but the record is fully VISIBLE on disk with valid
    content from that moment on. A LATER pass's REPAIR completeness gate
    must not trust that visibility alone as fan-out proof: it must
    re-prove durability (re-fsync the file + the delivery dir)
    immediately before unlinking the folded emissions the record is
    meant to prove, and retain them on any re-proof failure — even a
    PERMANENT one, indefinitely."""
    _emit(spool, when=1000.0)
    real_fsync = os.fsync
    calls = {"i": 0}

    def flaky_fsync(fd):
        calls["i"] += 1
        # _write_state's own _strict_replace_at makes 3 fsync calls
        # (staged file, STATE_DIR, emitter dir) — let those land. Then
        # the delivery write's staged-file fsync (#4) lands too (so the
        # rename actually happens), but its post-rename DELIVERY_DIR
        # fsync (#5) fails — exactly Sol I3's residual.
        if calls["i"] == 5:
            raise OSError(5, "simulated post-rename dir fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(es.os, "fsync", flaky_fsync)
    try:
        changed = spool.fold_pass(R(S1), 2000.0)
    finally:
        monkeypatch.undo()

    assert changed == []                       # _write_delivery reported False
    # yet the record IS fully on disk despite that False return
    assert _delivery_path(spool, S1).exists()
    rec_on_disk = _read_delivery(spool, S1)
    assert rec_on_disk is not None and rec_on_disk["gen"] == 1
    # the folded emission must still be retained — never unlinked off an
    # unproven write within this same pass (all_written was False)
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))

    # next pass: the record now reads back as valid gen-1 "proof" of
    # fan-out completeness — but re-proof is what must gate the unlink,
    # and fsync is STILL broken (permanent failure), so the emission
    # must stay retained indefinitely, not merely once.
    def always_broken(fd):
        raise OSError(5, "still broken")

    monkeypatch.setattr(es.os, "fsync", always_broken)
    try:
        changed = spool.fold_pass(R(S1), 2100.0)
    finally:
        monkeypatch.undo()
    assert changed == []
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))   # still retained

    # fsync healthy again: re-proof finally succeeds, the emission unlinks.
    changed = spool.fold_pass(R(S1), 2200.0)
    assert changed == []
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_open_reproves_delivery_durability_before_unlinking_folded_emissions(
        spool, monkeypatch):
    """Same Sol I3 residual, pinned at the OPEN site: even a fully
    successful ``all_written=True`` pass must re-prove durability right
    before the unlink, symmetric with REPAIR's own site above — this pin
    only proves the reprove step runs (and can be made to fail) at OPEN
    too, not a distinct production scenario (OPEN's own ``all_written``
    gate already implies each write's dir-fsync succeeded truly within
    the SAME pass)."""
    _emit(spool, when=1000.0)
    real_open = os.open
    target = f"{EV}--{S1}.json"

    def flaky_open(name, flags, *a, **kw):
        if name == target and (flags & os.O_CREAT) == 0:
            # the re-proof step's read-only reopen of the freshly-written
            # delivery file — never the write itself (O_CREAT).
            raise OSError(errno.EMFILE, "too many open files")
        return real_open(name, flags, *a, **kw)

    monkeypatch.setattr(es.os, "open", flaky_open)
    try:
        changed = spool.fold_pass(R(S1), 2000.0)
    finally:
        monkeypatch.undo()

    # the write itself succeeded (changed reports it) but the re-proof
    # reopen failed, so the folded emission must be retained regardless
    assert changed == [(E, EV, S1)]
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))

    # fd pressure gone: the next pass's REPAIR re-proves and unlinks.
    changed = spool.fold_pass(R(S1), 2100.0)
    assert changed == []
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


# ---------------------------------------------------------------------------
# fold_pass — crash injection between OPEN's steps
# ---------------------------------------------------------------------------


def _boom_after(monkeypatch, target_module, name, n):
    """Raise on the *n*-th call to ``target_module.<name>``, delegating to
    the original on every other call."""
    orig = getattr(target_module, name)
    calls = {"i": 0}

    def wrapper(*a, **kw):
        calls["i"] += 1
        if calls["i"] == n:
            raise RuntimeError("simulated crash")
        return orig(*a, **kw)

    monkeypatch.setattr(target_module, name, wrapper)
    return calls


def test_crash_between_state_write_and_record_upserts_self_heals(spool, monkeypatch):
    """REPAIR's backfill is a PARTIAL-WRITE recovery, not a from-nothing
    mint (Important-1 fix): it only ever touches a cohort member who
    ALREADY has a record sitting at a stale gen. So this scenario first
    runs one full successful gen1 cycle (both subscribers acquire a real,
    terminal gen1 record) — THEN the second open's record-write loop is
    crashed on its very first call, which aborts the whole loop and
    leaves BOTH subscribers still holding their old gen1 record. REPAIR
    must backfill both of them on the next pass, from that prior history."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1, S2), 1100.0)       # gen1, cohort=[S1,S2]
    for sub in (S1, S2):
        rec = _read_delivery(spool, sub)
        spool.ack(E, EV, rec["ack_token"], now=1200.0)   # both -> done

    _emit_n(spool, 3, start=2000.0)
    # The state write for gen2 succeeds; the crash lands on the FIRST
    # _write_delivery call of the record-upsert loop, aborting it before
    # EITHER subscriber's gen2 record is written.
    _boom_after(monkeypatch, es.EventSpool, "_write_delivery", 1)
    changed = spool.fold_pass(R(S1, S2), 3000.0)
    assert changed == []                     # the exception aborted this
    # pair's fold before anything was recorded as "changed"
    state = _read_state(spool)
    assert state is not None and state["gen"] == 2   # state IS durable
    assert _read_delivery(spool, S1)["gen"] == 1     # neither record was
    assert _read_delivery(spool, S2)["gen"] == 1     # actually rewritten

    monkeypatch.undo()
    changed = spool.fold_pass(R(S1, S2), 3100.0)
    assert set(changed) == {(E, EV, S1), (E, EV, S2)}
    assert _read_delivery(spool, S1)["gen"] == 2
    assert _read_delivery(spool, S2)["gen"] == 2
    # folded emissions are gone once the pass completes cleanly
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_crash_between_record_upserts_and_unlink_self_heals(spool, monkeypatch):
    _emit_n(spool, 3, start=1000.0)
    # State + records are written durably; the crash lands on the FIRST
    # emission unlink of OPEN's final step.
    real_unlink = es._unlink_quiet
    calls = {"i": 0}

    def boom_unlink(name, dir_fd):
        calls["i"] += 1
        if calls["i"] == 1:
            raise RuntimeError("simulated crash mid-unlink")
        return real_unlink(name, dir_fd)

    monkeypatch.setattr(es, "_unlink_quiet", boom_unlink)
    changed = spool.fold_pass(R(S1), 2000.0)
    # the crash propagates out of _fold_one before it returns, so this
    # pass reports nothing changed — but the record write that already
    # happened is durable on disk regardless (proven below)
    assert changed == []
    state = _read_state(spool)
    assert state["gen"] == 1
    rec = _read_delivery(spool, S1)
    assert rec is not None and rec["gen"] == 1 and rec["status"] == "pending"
    # at least one emission is still on disk — the crash interrupted the
    # unlink loop
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))

    monkeypatch.undo()
    # next pass: not idle (S1 pending) so OPEN won't refire, but REPAIR's
    # gated folded-unlink step (gated on fan-out completeness: all eligible
    # members hold valid records at state_gen) cleans up the survivors
    spool.fold_pass(R(S1), 2100.0)
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_crash_leaving_state_written_records_partial_then_repair_completes(spool, monkeypatch):
    """Same reasoning as the test above: REPAIR only backfills a member
    who already has SOME record at a stale gen, so S2 must hold a real
    gen1 record before the gen2 write is made to fail for it."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1, S2), 1100.0)       # gen1, cohort=[S1,S2]
    for sub in (S1, S2):
        rec = _read_delivery(spool, sub)
        spool.ack(E, EV, rec["ack_token"], now=1200.0)   # both -> done
    gen1_s2 = _read_delivery(spool, S2)

    _emit_n(spool, 3, start=2000.0)
    real_write = es.EventSpool._write_delivery

    def boom_write(self, efd, dfd, event, subscriber, rec):
        if subscriber == S2:
            return False                     # simulated undurable write
        return real_write(self, efd, dfd, event, subscriber, rec)

    monkeypatch.setattr(es.EventSpool, "_write_delivery", boom_write)
    changed = spool.fold_pass(R(S1, S2), 3000.0)
    assert (E, EV, S1) in changed
    assert (E, EV, S2) not in changed
    assert _read_delivery(spool, S2) == gen1_s2   # S2's write never
    # landed — its old gen1 record survives byte-for-byte

    monkeypatch.undo()
    changed = spool.fold_pass(R(S1, S2), 3100.0)   # REPAIR backfills S2
    assert changed == [(E, EV, S2)]
    assert _read_delivery(spool, S2)["gen"] == 2


def test_first_generation_crash_on_first_write_retains_emission_and_reheals(spool, monkeypatch):
    """Round-2 NEW-1 probe A: a crash during the VERY FIRST fan-out (no
    cohort member has EVER held a record for this pair) must never lose
    the folded emission. REPAIR can never backfill a `rec is None`
    member (Important-1, round 1), so completeness for this generation
    can never be proven by REPAIR alone — the emission must be RETAINED,
    not unlinked, so OPEN can re-fold it once the pair goes idle."""
    _emit(spool, when=1000.0)
    # The crash lands on the FIRST _write_delivery call of OPEN's own
    # record-write loop — before EITHER cohort member gets anything.
    _boom_after(monkeypatch, es.EventSpool, "_write_delivery", 1)
    changed = spool.fold_pass(R(S1, S2), 2000.0)
    assert changed == []
    state = _read_state(spool)
    assert state is not None and state["gen"] == 1   # state IS durable
    assert _read_delivery(spool, S1) is None
    assert _read_delivery(spool, S2) is None
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))  # RETAINED —
    # the fan-out was never proven complete, so nothing unlinked it

    monkeypatch.undo()
    # Nobody has ANY record yet, so idle is ALREADY true: the very next
    # pass re-folds the SAME emission into a fresh generation for BOTH
    # cohort members.
    changed = spool.fold_pass(R(S1, S2), 2100.0)
    assert set(changed) == {(E, EV, S1), (E, EV, S2)}
    state = _read_state(spool)
    assert state["gen"] == 2
    assert _read_delivery(spool, S1)["gen"] == 2
    assert _read_delivery(spool, S2)["gen"] == 2
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_first_generation_partial_write_retains_emission_until_idle_then_refolds(spool, monkeypatch):
    """Round-2 NEW-1 probe C: S1's write lands, S2's doesn't, on the
    FIRST-EVER generation for this pair. The emission is RETAINED (fan-
    out incomplete — S2 has no record REPAIR could ever backfill) until
    S1's lone pending record resolves and the pair goes genuinely idle,
    at which point a fresh generation re-folds the SAME emission for
    BOTH members. S1's original gen-1 record must never be resurrected —
    the gen-2 record is a distinct, fresh mint, not a mutation of the
    old terminal one."""
    _emit(spool, when=1000.0)
    real_write = es.EventSpool._write_delivery

    def boom_write(self, efd, dfd, event, subscriber, rec):
        if subscriber == S2:
            return False              # simulated undurable write — does
            # NOT raise, so the write loop continues past it
        return real_write(self, efd, dfd, event, subscriber, rec)

    monkeypatch.setattr(es.EventSpool, "_write_delivery", boom_write)
    changed = spool.fold_pass(R(S1, S2), 2000.0)
    assert changed == [(E, EV, S1)]
    s1_gen1 = _read_delivery(spool, S1)
    assert s1_gen1["gen"] == 1 and s1_gen1["status"] == "pending"
    assert _read_delivery(spool, S2) is None
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))   # RETAINED
    # — fan-out incomplete (S2 has no record at all)

    monkeypatch.undo()
    # Not idle yet (S1 still pending) -> no re-fold, nothing changes.
    changed = spool.fold_pass(R(S1, S2), 2100.0)
    assert changed == []
    assert _read_state(spool)["gen"] == 1
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))

    # S1 acks its gen-1 record -> the pair is genuinely idle (S1 done,
    # S2 never had a record at all).
    spool.ack(E, EV, s1_gen1["ack_token"], now=2200.0)
    changed = spool.fold_pass(R(S1, S2), 2300.0)
    assert set(changed) == {(E, EV, S1), (E, EV, S2)}   # S2 finally woken
    state = _read_state(spool)
    assert state["gen"] == 2
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []

    # S1's ORIGINAL gen-1 record is untouched (it was already fully
    # overwritten, never resurrected in place) — the new gen-2 record is
    # a fresh, distinct mint with its own token, not the old terminal
    # one flipped back to pending.
    s1_gen2 = _read_delivery(spool, S1)
    assert s1_gen2["gen"] == 2
    assert s1_gen2["status"] == "pending"
    assert s1_gen2["ack_token"] != s1_gen1["ack_token"]


def test_open_retains_emissions_when_fsync_of_a_delivery_write_fails(
        spool, monkeypatch):
    """Important-3 pin: OPEN's ``all_written`` gate must key on the
    delivery record being PROVEN durable, not merely written+renamed. A
    failing fsync (simulated directly at the ``os.fsync`` layer — the
    best-effort path this used to go through would have logged and kept
    going, silently reporting success) must make ``_write_delivery``
    return False, which must in turn hold ``all_written`` False and
    retain the folded emissions — exactly like an outright write
    failure."""
    _emit(spool, when=1000.0)
    real_fsync = os.fsync
    calls = {"i": 0}

    def flaky_fsync(fd):
        calls["i"] += 1
        # The generation-state write (_write_state) makes exactly 3
        # os.fsync calls on a clean success (staged file, STATE_DIR,
        # emitter dir) — let those land, then fail every fsync from the
        # delivery write onward.
        if calls["i"] > 3:
            raise OSError(5, "simulated fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(es.os, "fsync", flaky_fsync)
    try:
        changed = spool.fold_pass(R(S1), 2000.0)
    finally:
        monkeypatch.undo()

    assert changed == []                       # S1's write never proven durable
    assert _read_delivery(spool, S1) is None
    state = _read_state(spool)
    assert state is not None and state["gen"] == 1   # state IS durable
    # fan-out incomplete -> the folded emission is RETAINED, never unlinked
    # against a delivery record that was never actually proven durable.
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))

    # fsync healthy again: idle (S1 never got a record at all) -> the
    # SAME emission re-folds into a fresh generation (gen 2 — the
    # already-durable gen-1 state anchor is never rolled back).
    changed = spool.fold_pass(R(S1), 2100.0)
    assert changed == [(E, EV, S1)]
    assert _read_delivery(spool, S1)["gen"] == 2
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_crash_mid_unlink_then_ack_does_not_refold(spool, monkeypatch):
    """Round-2 FAN-OUT-COMPLETENESS probe: REPAIR's fan-out completeness
    gate (eligible = cohort & routed_subs, all members have records at
    state_gen) must unlink folded emissions even when the records have been
    acked to "done" status. A mutation adding `status=="pending"` to the
    predicate would fail here — after acking, the records would have
    status="done", but the fan-out is still complete (both eligible members
    hold valid records), so the unlink must still happen."""
    _emit(spool, when=1000.0)
    # Crash BEFORE the folded-unlink step of OPEN: state is durable, both
    # records are durable, but the folded emission remains on disk.
    real_unlink = es._unlink_quiet
    calls = {"i": 0}

    def boom_unlink(name, dir_fd):
        calls["i"] += 1
        if calls["i"] == 1:
            raise RuntimeError("simulated crash mid-unlink")
        return real_unlink(name, dir_fd)

    monkeypatch.setattr(es, "_unlink_quiet", boom_unlink)
    changed = spool.fold_pass(R(S1, S2), 1100.0)
    assert changed == []
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))  # still on disk
    state = _read_state(spool)
    assert state is not None and state["gen"] == 1

    monkeypatch.undo()
    # Ack both records to transition them from "pending" to "done".
    for sub in (S1, S2):
        rec = _read_delivery(spool, sub)
        spool.ack(E, EV, rec["ack_token"], now=1200.0)

    # REPAIR's completeness gate must still unlink the folded emissions: all
    # eligible members (S1, S2) hold valid records at state_gen=1, even
    # though they are now "done" instead of "pending". The gate unlinks, no
    # new generation is minted (changed == []).
    changed = spool.fold_pass(R(S1, S2), 1300.0)
    assert changed == []
    assert _read_state(spool)["gen"] == 1
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_crash_mid_unlink_then_dropped_member_does_not_refold(spool, monkeypatch):
    """Round-2 FAN-OUT-COMPLETENESS probe: REPAIR's fan-out completeness
    gate is keyed to `eligible = cohort & routed_subs`, not just `cohort`.
    A mutation changing `for sub in eligible` to `for sub in cohort` would
    fail here — after S2 is de-routed and removed, eligible={S1 only}, but
    cohort={S1,S2}. The emission must still be unlinked because all members
    of eligible (just S1) hold valid records at state_gen."""
    _emit(spool, when=1000.0)
    # Crash BEFORE the folded-unlink step: state is durable, both records
    # are durable, but the folded emission remains on disk.
    real_unlink = es._unlink_quiet
    calls = {"i": 0}

    def boom_unlink(name, dir_fd):
        calls["i"] += 1
        if calls["i"] == 1:
            raise RuntimeError("simulated crash mid-unlink")
        return real_unlink(name, dir_fd)

    monkeypatch.setattr(es, "_unlink_quiet", boom_unlink)
    changed = spool.fold_pass(R(S1, S2), 1100.0)
    assert changed == []
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))  # still on disk
    state = _read_state(spool)
    assert state is not None and state["gen"] == 1

    monkeypatch.undo()
    # Ack both records.
    for sub in (S1, S2):
        rec = _read_delivery(spool, sub)
        spool.ack(E, EV, rec["ack_token"], now=1200.0)

    # Sweep with S2 no longer in the installed set (de-route S2, remove its
    # tombstone).
    spool.sweep({}, installed={S1}, registry_valid=True, now=1250.0)

    # REPAIR's completeness gate must still unlink: eligible={S1 only} (the
    # intersection of cohort={S1,S2} and routed_subs={S1}), and S1 holds a
    # valid record at state_gen=1. The fact that S2 is in cohort but not
    # routed should not prevent the unlink. No new generation is created
    # (changed == []).
    changed = spool.fold_pass(R(S1), 1300.0)
    assert changed == []
    assert _read_state(spool)["gen"] == 1
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_fold_one_does_not_leak_fds_when_a_later_subdir_open_fails(spool, monkeypatch):
    """Minor-2 pin: the STATE fd must be closed even when the DELIVERY
    open that follows it fails — nested try/finally, not three bare
    opens ahead of one shared try block."""
    _emit(spool, when=1000.0)   # gives _candidate_events something to
    # find via the emissions listing, so _fold_one still gets invoked
    # despite DELIVERY_DIR failing to open.
    orig_open_dir = es._open_dir

    def boom(name, dir_fd):
        if name == es.DELIVERY_DIR:
            raise OSError(5, "simulated")
        return orig_open_dir(name, dir_fd)

    fd_dir = Path("/proc/self/fd")
    before = len(os.listdir(fd_dir))
    monkeypatch.setattr(es, "_open_dir", boom)
    spool.fold_pass(R(S1), 2000.0)
    monkeypatch.undo()
    after = len(os.listdir(fd_dir))
    assert after == before


def test_open_only_unlinks_names_recorded_in_folded(spool):
    """Minor-6(d) pin: an emission whose filename fails to parse a
    u32hex token is never added to ``state.folded`` — and must therefore
    never be unlinked either. Unlinking a name absent from ``folded``
    would leave nothing for REPAIR's folded-unlink step to reach if a
    crash landed between the state write and this unlink."""
    _emit(spool, when=1000.0)
    bogus = _emissions_dir(spool) / f"{EV}--not-8-hex.json"
    bogus.write_bytes(b'{"v":1}')
    _utime(bogus, 999.0)

    changed = spool.fold_pass(R(S1), 2000.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert bogus.name not in [f"{EV}--{u}.json" for u in state["folded"]]
    assert bogus.exists()          # never unlinked — not trackable via
    # `folded`, so an unlink here would be unrecoverable-from-disk


def test_fold_pass_survives_a_malformed_routed_value(spool, caplog):
    """Minor-5(b) pin: ``pair_routed``'s own computation must be inside
    the per-pair guard — a `routed` value that is truthy but not a
    mapping (so ``.get`` raises) must abort only that pair, never crash
    the whole pass."""
    _emit(spool, when=1000.0)
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        changed = spool.fold_pass(["not", "a", "dict"], 2000.0)
    assert changed == []
    assert any("fold" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# ROUTING_UNAVAILABLE — strict no-op for fold
# ---------------------------------------------------------------------------


def test_fold_pass_under_routing_unavailable_is_a_strict_noop(spool):
    _emit(spool, when=1000.0)
    _write_raw(_state_dir(spool) / f"{EV}.json", "{garbage")
    before_emissions = sorted(os.listdir(_emissions_dir(spool)))
    before_state = sorted(os.listdir(_state_dir(spool)))

    changed = spool.fold_pass(ROUTING_UNAVAILABLE, 2000.0)
    assert changed == []
    assert sorted(os.listdir(_emissions_dir(spool))) == before_emissions
    assert sorted(os.listdir(_state_dir(spool))) == before_state
    assert spool.spool_issues() == []        # no reconstruction attempted


# ---------------------------------------------------------------------------
# update_delivery_nudge
# ---------------------------------------------------------------------------


def _mutate_nudge(**kv):
    def _m(rec):
        rec.update(kv)
        return rec
    return _m


def test_update_delivery_nudge_happy_path(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(nudges=1, last_nudge_ts=1150.0,
                                             next_nudge_ts=1450.0))
    assert ok is True
    after = _read_delivery(spool, S1)
    assert after["nudges"] == 1
    assert after["last_nudge_ts"] == 1150.0
    assert after["next_nudge_ts"] == 1450.0
    assert after["ack_token"] == rec["ack_token"]    # untouched


def test_update_delivery_nudge_refuses_gen_mismatch(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"] + 1, _mutate_nudge(nudges=1))
    assert ok is False
    assert _read_delivery(spool, S1)["nudges"] == 0


def test_update_delivery_nudge_refuses_touching_disallowed_field(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(ack_token="hijacked"))
    assert ok is False
    assert _read_delivery(spool, S1)["ack_token"] == rec["ack_token"]

    ok2 = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(gen=rec["gen"] + 1))
    assert ok2 is False


def test_update_delivery_nudge_refused_after_concurrent_ack(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    outcome, sub = spool.ack(E, EV, rec["ack_token"], now=1200.0)
    assert outcome == "acked"

    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(nudges=1))
    assert ok is False
    after = _read_delivery(spool, S1)
    assert after["status"] == "done" and after["outcome"] == "acked"


def test_exhaustion_is_one_atomic_update(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"],
        _mutate_nudge(status="done", outcome="exhausted", noted=True,
                      ended_ts=1500.0, next_nudge_ts=None))
    assert ok is True
    after = _read_delivery(spool, S1)
    assert after["status"] == "done"
    assert after["outcome"] == "exhausted"
    assert after["noted"] is True
    assert after["ended_ts"] == 1500.0

    # done is immutable forever — a further update always refuses
    ok2 = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(noted=False))
    assert ok2 is False


def test_update_delivery_nudge_unknown_record_returns_false(spool):
    assert spool.update_delivery_nudge(E, EV, "nobody", 1, _mutate_nudge(nudges=1)) is False


def test_mark_delivery_noted_flips_only_noted_on_a_done_record(spool):
    """#532: the ONE sanctioned mutation of a done record — ``noted``
    False→True, gen-matched — so the exhaustion notice can be
    notify-after-mark (at-least-once) instead of lost with the record."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"],
        _mutate_nudge(status="done", outcome="exhausted", noted=False,
                      ended_ts=1500.0, next_nudge_ts=None))
    assert ok is True

    assert spool.mark_delivery_noted(E, EV, S1, rec["gen"]) is True
    after = _read_delivery(spool, S1)
    assert after["noted"] is True
    assert after["status"] == "done" and after["outcome"] == "exhausted"
    assert after["ack_token"] == rec["ack_token"]      # untouched
    assert after["gen"] == rec["gen"]

    # Idempotent: an already-noted record reports success, no rewrite needed.
    assert spool.mark_delivery_noted(E, EV, S1, rec["gen"]) is True


def test_mark_delivery_noted_refuses_pending_gen_mismatch_and_unknown(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)

    # Still pending — refuse (update_delivery_nudge owns pending records).
    assert spool.mark_delivery_noted(E, EV, S1, rec["gen"]) is False

    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"],
        _mutate_nudge(status="done", outcome="exhausted", noted=False,
                      ended_ts=1500.0, next_nudge_ts=None))
    assert ok is True
    # Rotated/stale generation — refuse.
    assert spool.mark_delivery_noted(E, EV, S1, rec["gen"] + 1) is False
    assert _read_delivery(spool, S1)["noted"] is False
    # Unknown record — refuse.
    assert spool.mark_delivery_noted(E, EV, "nobody", 1) is False


# ---------------------------------------------------------------------------
# ack — typed results + stale-token CAS
# ---------------------------------------------------------------------------


def test_ack_acked_then_already_done_no_mutation(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    token = rec["ack_token"]

    outcome, sub = spool.ack(E, EV, token, now=1200.0)
    assert (outcome, sub) == ("acked", S1)
    done_rec = _read_delivery(spool, S1)
    assert done_rec["status"] == "done" and done_rec["outcome"] == "acked"

    outcome2, sub2 = spool.ack(E, EV, token, now=1300.0)
    assert (outcome2, sub2) == ("already_done", S1)
    unchanged = _read_delivery(spool, S1)
    assert unchanged == done_rec             # byte-for-byte no mutation


def test_ack_no_match_for_unknown_token(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    outcome, sub = spool.ack(E, EV, "not-a-real-token", now=1200.0)
    assert (outcome, sub) == ("no_match", None)


def test_ack_no_match_for_unknown_emitter_or_event(spool):
    assert spool.ack("ghost", EV, "x", now=1000.0) == ("no_match", None)
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    assert spool.ack(E, "ghost-event", "x", now=1200.0) == ("no_match", None)


def test_stale_token_cas_after_fold_rotates_it(spool):
    now = 5000.0
    old_rec = ea.new_record(E, EV, S1, 1, "tok-old", now - 100)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(old_rec).decode())
    state = {"v": 1, "event": EV, "gen": 2, "cohort": [S1], "folded": [],
            "opened_ts": now - 50}
    _write_raw(_state_dir(spool) / f"{EV}.json",
              es.canonical_marker_bytes(state).decode())

    spool.fold_pass(R(S1), now)              # REPAIR upserts a fresh token
    new_rec = _read_delivery(spool, S1)
    assert new_rec["gen"] == 2
    assert new_rec["ack_token"] != "tok-old"

    assert spool.ack(E, EV, "tok-old", now=now + 10) == ("no_match", None)
    outcome, sub = spool.ack(E, EV, new_rec["ack_token"], now=now + 10)
    assert (outcome, sub) == ("acked", S1)


def test_ack_write_failure_does_not_report_a_false_acked(spool, monkeypatch, caplog):
    """Minor-4 pin: a token match whose durable rewrite fails must never
    be reported as ``acked`` — the record is unchanged on disk (still
    pending, same token), so a caller retrying the exact same ack once
    the write succeeds must still be able to."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    monkeypatch.setattr(es.EventSpool, "_write_delivery",
                        lambda *a, **kw: False)
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        outcome, sub = spool.ack(E, EV, rec["ack_token"], now=1200.0)
    assert (outcome, sub) == ("no_match", None)
    assert any("ack write failed" in r.message for r in caplog.records)

    monkeypatch.undo()
    after = _read_delivery(spool, S1)
    assert after["status"] == "pending"      # unchanged — safe to retry
    assert after["ack_token"] == rec["ack_token"]

    outcome2, sub2 = spool.ack(E, EV, rec["ack_token"], now=1300.0)
    assert (outcome2, sub2) == ("acked", S1)   # the retry succeeds


# ---------------------------------------------------------------------------
# sweep — the watermark trio (Sol/Terra-r3 #1 pins)
# ---------------------------------------------------------------------------


def test_sweep_deletes_emissions_of_an_unrouted_pair_next_pass(spool):
    p = _emit(spool, when=1000.0)
    spool.sweep({}, installed=set(), registry_valid=True, now=1100.0)
    assert not p.exists()


def test_sweep_never_resurrects_an_already_deleted_emission_on_late_consent(spool):
    p = _emit(spool, when=1000.0)
    spool.sweep({}, installed=set(), registry_valid=True, now=1100.0)
    assert not p.exists()
    spool.sweep(R(S1), installed={S1}, registry_valid=True, now=1200.0)
    assert not p.exists()                    # gone forever


def test_sweep_leaves_a_routed_pairs_queue_untouched_when_a_second_subscriber_joins(spool):
    p1 = _emit(spool, when=1000.0)
    spool.sweep(R(S1), installed={S1}, registry_valid=True, now=1100.0)
    assert p1.exists()
    p2 = _emit(spool, when=1150.0)
    spool.sweep(R(S1, S2), installed={S1, S2}, registry_valid=True, now=1200.0)
    assert p1.exists() and p2.exists()


def test_sweep_post_consent_emission_survives(spool):
    p = _emit(spool, when=1000.0)
    # already routed from the start
    spool.sweep(R(S1), installed={S1}, registry_valid=True, now=1100.0)
    assert p.exists()


# ---------------------------------------------------------------------------
# sweep — disk-pressure valve
# ---------------------------------------------------------------------------


def test_sweep_disk_pressure_valve_deletes_oldest_overflow_with_log(spool, caplog):
    paths = _emit_n(spool, MAX_EMISSION_FILES + 1, start=1000.0, step=1.0)
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        report = spool.sweep(R(S1), installed={S1}, registry_valid=True,
                             now=3000.0)
    assert report.deleted_valve == 1
    remaining = sorted(os.listdir(_emissions_dir(spool)))
    assert len(remaining) == MAX_EMISSION_FILES
    assert not paths[0].exists()             # the single oldest is gone
    assert all(p.exists() for p in paths[1:])
    assert any("disk-pressure valve" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# sweep — corrupt delivery quarantine
# ---------------------------------------------------------------------------


def test_sweep_quarantines_corrupt_delivery_file_and_surfaces_issue(spool):
    _write_raw(_delivery_path(spool, S1), "{not json")
    report = spool.sweep(R(S1), installed={S1}, registry_valid=True, now=1000.0)
    assert report.quarantined_delivery == 1
    assert not _delivery_path(spool, S1).exists()
    quarantined = list(_delivery_dir(spool).glob(".corrupt-*"))
    assert len(quarantined) == 1
    issues = spool.spool_issues()
    assert any(i["kind"] == "corrupt_delivery" for i in issues)


def test_sweep_defers_rather_than_quarantines_an_unreadable_delivery_file(
        spool, monkeypatch):
    """Important-2 pin: an EMFILE-style open failure reading a delivery
    record must leave it UNTOUCHED — never quarantined. Collapsing an
    OSError (fd pressure, transient I/O) into the same MarkerState.INVALID
    a malformed-content read produces would let this destructively rename
    a record whose actual content was never even seen — it might be
    perfectly healthy."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    path = _delivery_path(spool, S1)
    assert path.exists()

    target = path.name
    real_open = os.open

    def flaky_open(name, flags, *a, **kw):
        if name == target:
            raise OSError(errno.EMFILE, "too many open files")
        return real_open(name, flags, *a, **kw)

    monkeypatch.setattr(es.os, "open", flaky_open)
    try:
        report = spool.sweep(R(S1), installed={S1}, registry_valid=True,
                             now=1200.0)
    finally:
        monkeypatch.undo()

    assert report.quarantined_delivery == 0
    assert path.exists()                      # untouched — no rename
    assert list(_delivery_dir(spool).glob(".corrupt-*")) == []
    # a later pass, fd pressure gone, reads the SAME healthy record back.
    rec = _read_delivery(spool, S1)
    assert rec is not None and rec["status"] == "pending"


def test_corrupt_delivery_does_not_block_idle_and_is_unackable(spool):
    _write_raw(_delivery_path(spool, S1), "{not json")
    _emit(spool, when=1000.0)
    # idle: the invalid record is excluded from the pending scan
    changed = spool.fold_pass(R(S1), 2000.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 1


def test_no_match_ack_for_a_token_that_was_never_valid(spool):
    _write_raw(_delivery_path(spool, S1), "{not json")
    assert spool.ack(E, EV, "anything", now=1000.0) == ("no_match", None)


# ---------------------------------------------------------------------------
# sweep — removed-vs-revoked classification
# ---------------------------------------------------------------------------


def test_sweep_classifies_revoked_when_subscriber_still_installed(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    spool.sweep({}, installed={S1}, registry_valid=True, now=1200.0)
    rec = _read_delivery(spool, S1)
    assert rec["status"] == "done" and rec["outcome"] == "revoked"


def test_sweep_classifies_removed_when_subscriber_no_longer_installed(spool, monkeypatch):
    # A subscriber unrouted AND already uninstalled goes terminal and is
    # dropped in the SAME pass (nothing gates the drop on a second pass —
    # only on the removal record being durable first). Spy on the write
    # to observe the "removed" outcome before the file is unlinked.
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    written = []
    orig = es.EventSpool._write_delivery

    def spy(self, efd, dfd, event, subscriber, rec):
        written.append(dict(rec))
        return orig(self, efd, dfd, event, subscriber, rec)

    monkeypatch.setattr(es.EventSpool, "_write_delivery", spy)
    report = spool.sweep({}, installed=set(), registry_valid=True, now=1200.0)
    assert report.terminalized == 1
    assert report.dropped_records == 1
    assert _read_delivery(spool, S1) is None
    assert any(r["outcome"] == "removed" for r in written)


def test_sweep_retains_unrouted_records_as_tombstones_until_plugin_removed(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    spool.sweep({}, installed={S1}, registry_valid=True, now=1200.0)
    assert _delivery_path(spool, S1).exists()      # tombstoned, not dropped
    assert spool.list_removal_records() == []


# ---------------------------------------------------------------------------
# sweep — removal records: tagged union, drop-only-after-durable
# ---------------------------------------------------------------------------


def test_sweep_drops_tombstoned_record_only_after_durable_removal_record(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    # first pass: S1 unrouted but still installed -> revoked tombstone
    spool.sweep({}, installed={S1}, registry_valid=True, now=1200.0)
    assert _delivery_path(spool, S1).exists()

    # second pass: S1's plugin is now uninstalled -> drop, with a removal
    # record written first
    report = spool.sweep({}, installed=set(), registry_valid=True, now=1300.0)
    assert report.removal_records_written == 1
    assert report.dropped_records == 1
    assert not _delivery_path(spool, S1).exists()

    records = spool.list_removal_records()
    assert len(records) == 1
    name, rec = records[0]
    assert rec["plugin"] == S1
    assert rec["noted"] is False
    assert rec["entries"] == [{"kind": "record", "emitter": E, "event": EV,
                               "gen": 1}]


def test_removal_record_tagged_union_includes_corrupt_entry(spool):
    _write_raw(_delivery_path(spool, "ghost"), "{not json")
    # attribute the corrupt file to "ghost", never installed
    report = spool.sweep({}, installed=set(), registry_valid=True, now=1000.0)
    assert report.quarantined_delivery == 1
    assert report.removal_records_written == 1
    assert report.dropped_corrupt == 1

    records = spool.list_removal_records()
    assert len(records) == 1
    name, rec = records[0]
    assert rec["plugin"] == "ghost"
    assert len(rec["entries"]) == 1
    entry = rec["entries"][0]
    assert entry["kind"] == "corrupt"
    assert entry["file"].startswith(".corrupt-")

    # and the quarantined file itself is gone (deleted after durability)
    assert list(_delivery_dir(spool).glob(".corrupt-*")) == []


def test_removal_record_batches_record_and_corrupt_entries_for_one_subscriber(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    _write_raw(_delivery_dir(spool) / f"other-event--{S1}.json", "{not json")

    report = spool.sweep({}, installed=set(), registry_valid=True, now=1200.0)
    assert report.removal_records_written == 1
    records = spool.list_removal_records()
    assert len(records) == 1
    _, rec = records[0]
    kinds = sorted(e["kind"] for e in rec["entries"])
    assert kinds == ["corrupt", "record"]


# ---------------------------------------------------------------------------
# sweep — ROUTING_UNAVAILABLE strict no-op (Sol-r5 #1 pin)
# ---------------------------------------------------------------------------


def test_sweep_under_routing_unavailable_only_does_part_ttl(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    part = _emissions_dir(spool) / ".part-deadbeef"
    part.write_bytes(b"x")
    _utime(part, 1000.0 - TEMP_TTL_S - 100)

    report = spool.sweep(ROUTING_UNAVAILABLE, installed=set(),
                         registry_valid=True, now=2000.0)
    assert not part.exists()                 # part-TTL housekeeping ran
    assert report.deleted_watermark == 0
    assert report.terminalized == 0
    assert report.removal_records_written == 0
    # the pending delivery is untouched — no destructive action ran
    rec = _read_delivery(spool, S1)
    assert rec["status"] == "pending"
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []  # folded
    # already, from the earlier fold_pass call — unrelated to this sweep


# ---------------------------------------------------------------------------
# sweep — registry_valid gating (Critical-2)
# ---------------------------------------------------------------------------


def test_sweep_under_invalid_registry_only_does_part_ttl_even_with_authoritative_routed(spool):
    """Critical-2(a) pin: an AUTHORITATIVE (non-sentinel) routed map is not
    enough on its own — registry_valid=False must degrade sweep exactly
    like ROUTING_UNAVAILABLE: no watermark deletion, no terminalization, no
    drop, no removal record. Only part-TTL housekeeping runs."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    part = _emissions_dir(spool) / ".part-deadbeef"
    part.write_bytes(b"x")
    _utime(part, 1000.0 - TEMP_TTL_S - 100)

    # An authoritative EMPTY routed map (installed=set()) would normally
    # terminalize-then-drop S1's record outright — but registry_valid=False
    # must suppress all of that.
    report = spool.sweep({}, installed=set(), registry_valid=False, now=2000.0)
    assert not part.exists()                 # part-TTL housekeeping ran
    assert report.deleted_watermark == 0
    assert report.terminalized == 0
    assert report.dropped_records == 0
    assert report.removal_records_written == 0
    rec = _read_delivery(spool, S1)
    assert rec is not None and rec["status"] == "pending"


def test_sweep_registry_heals_resumes_full_authoritative_behavior(spool):
    """The other half of Critical-2(a): once registry_valid returns to
    True, the SAME unrouted/uninstalled pair terminalizes and drops
    normally — the degrade is transient, not sticky."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    # registry momentarily invalid — nothing destructive happens
    spool.sweep({}, installed=set(), registry_valid=False, now=1200.0)
    rec = _read_delivery(spool, S1)
    assert rec["status"] == "pending"
    # registry heals — the pass now proceeds exactly as it would have
    report = spool.sweep({}, installed=set(), registry_valid=True, now=1300.0)
    assert report.terminalized == 1
    assert report.dropped_records == 1
    assert _read_delivery(spool, S1) is None


def test_sweep_never_drops_a_pending_record_the_drop_gate_requires_done(
        spool, monkeypatch):
    """Critical-2(b) pin: the DROP path requires ``status == "done"`` — a
    record that is still routed this pass (so the terminalize branch never
    runs) but whose subscriber has already fallen out of `installed` must
    be left alone, not dropped while pending. Forces exactly that
    inconsistent-but-real state (routed snapshot stale relative to a
    fresher `installed`) directly, since the reconciler is not exercised
    here."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    assert rec["status"] == "pending"

    # S1 is STILL routed (routed=R(S1)) but no longer "installed" — an
    # inconsistency the sweep must never resolve by silently dropping a
    # pending record.
    report = spool.sweep(R(S1), installed=set(), registry_valid=True, now=1200.0)
    assert report.terminalized == 0
    assert report.dropped_records == 0
    assert report.removal_records_written == 0
    still = _read_delivery(spool, S1)
    assert still is not None and still["status"] == "pending"
    assert still == rec                       # entirely untouched


# ---------------------------------------------------------------------------
# fold_pass / sweep — falsy non-dict `routed` normalization (Minor-7)
# ---------------------------------------------------------------------------


def test_fold_pass_falsy_non_dict_routed_is_a_strict_no_op(spool, caplog):
    """A falsy non-dict (None here) must never be silently normalized to an
    authoritative-empty {} by the per-pair `routed or {}` fallback — it is
    treated as unavailable (a strict no-op), exactly like the sentinel."""
    _emit(spool, when=1000.0)
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        changed = spool.fold_pass(None, 2000.0)
    assert changed == []
    assert _read_state(spool) is None            # no generation opened
    assert any("fold" in r.message for r in caplog.records)


def test_sweep_falsy_non_dict_routed_only_does_part_ttl(spool):
    """Mirrors the fold_pass pin above: a falsy non-dict routed value must
    degrade sweep to part-TTL housekeeping only, never an authoritative
    empty map (which would delete the queued emission as unrouted)."""
    p = _emit(spool, when=1000.0)
    report = spool.sweep(None, installed=set(), registry_valid=True, now=1100.0)
    assert p.exists()
    assert report.deleted_watermark == 0


# ---------------------------------------------------------------------------
# part-file TTL sweep
# ---------------------------------------------------------------------------


def test_sweep_deletes_stale_part_files(spool):
    part = _emissions_dir(spool) / ".part-cafebabe"
    part.write_bytes(b"x")
    _utime(part, 0.0)
    spool.sweep({}, installed=set(), registry_valid=True, now=TEMP_TTL_S + 100)
    assert not part.exists()


def test_sweep_keeps_a_fresh_part_file(spool):
    part = _emissions_dir(spool) / ".part-cafebabe"
    part.write_bytes(b"x")
    _utime(part, 1000.0)
    spool.sweep({}, installed=set(), registry_valid=True, now=1000.0 + 10)
    assert part.exists()


# ---------------------------------------------------------------------------
# replace-temp staging residue sweep — state/, .index/, emitter dir root
# (Minor-4, mirrors callback_spool.py:2906-2925)
# ---------------------------------------------------------------------------


def _stale_replace_temp(dirpath: Path, base_name: str, when: float) -> Path:
    p = dirpath / f".{base_name}{es.REPLACE_TEMP_INFIX}9999-deadbeef"
    p.write_bytes(b"x")
    _utime(p, when)
    return p


def test_sweep_removes_stale_replace_temp_at_emitter_root(spool):
    """write_ready's _replace_json stages directly at the emitter dir
    root (ready.json lives there, not in a subdir) — never covered by
    _sweep_part_temps (emissions/ only)."""
    stale = _stale_replace_temp(_edir(spool), "ready.json", 0.0)
    report = spool.sweep({}, installed=set(), registry_valid=True,
                         now=TEMP_TTL_S + 100)
    assert not stale.exists()
    assert report.deleted_temps == 1


def test_sweep_removes_stale_replace_temp_in_state_dir(spool):
    """_write_state/_quarantine_state stage in state/ — never covered by
    any existing sweep phase before this fix."""
    stale = _stale_replace_temp(_state_dir(spool), f"{EV}.json", 0.0)
    report = spool.sweep({}, installed=set(), registry_valid=True,
                         now=TEMP_TTL_S + 100)
    assert not stale.exists()
    assert report.deleted_temps == 1


def test_sweep_removes_stale_replace_temp_in_index_dir(spool):
    """write_index_entry stages in .index/ — never covered by any
    existing sweep phase before this fix."""
    spool.write_index_entry("k1", {"x": 1})       # creates .index/
    index_dir = _edir(spool).parent / es.INDEX_DIR
    stale = _stale_replace_temp(index_dir, "k1.json", 0.0)
    report = spool.sweep({}, installed=set(), registry_valid=True,
                         now=TEMP_TTL_S + 100)
    assert not stale.exists()
    assert report.deleted_temps == 1
    assert spool.read_index_marker("k1").state == MarkerState.PRESENT  # untouched


def test_sweep_keeps_fresh_replace_temps_everywhere(spool):
    root_temp = _stale_replace_temp(_edir(spool), "ready.json", 1000.0)
    state_temp = _stale_replace_temp(_state_dir(spool), f"{EV}.json", 1000.0)
    spool.write_index_entry("k1", {"x": 1})
    index_dir = _edir(spool).parent / es.INDEX_DIR
    index_temp = _stale_replace_temp(index_dir, "k1.json", 1000.0)
    spool.sweep({}, installed=set(), registry_valid=True, now=1000.0 + 10)
    assert root_temp.exists() and state_temp.exists() and index_temp.exists()


def test_sweep_housekeeping_only_still_removes_stale_replace_temps(spool):
    """Under the sentinel/invalid-registry degraded path (Critical-2), the
    SAME staging-residue cleanup still runs — it is non-destructive
    housekeeping, exactly like the pre-existing .part- sweep."""
    root_temp = _stale_replace_temp(_edir(spool), "ready.json", 0.0)
    state_temp = _stale_replace_temp(_state_dir(spool), f"{EV}.json", 0.0)
    spool.write_index_entry("k1", {"x": 1})
    index_dir = _edir(spool).parent / es.INDEX_DIR
    index_temp = _stale_replace_temp(index_dir, "k1.json", 0.0)

    spool.sweep(ROUTING_UNAVAILABLE, installed=set(), registry_valid=True,
               now=TEMP_TTL_S + 100)
    assert not root_temp.exists()
    assert not state_temp.exists()
    assert not index_temp.exists()


def test_sweep_reaps_delivery_temp_residue_with_future_mtime_fail_closed(spool):
    """Minor-6(c) pin: delivery/ staging-temp reaping must use the
    skew-aware ``_expired`` (like part-file reaping already does), not a
    bare ``now - mtime > TTL`` — a forward clock jump must not park a
    residue file that regains "freshness" once the clock returns."""
    stray = _delivery_dir(spool) / f".{EV}--{S1}.json.tmp-1234-abcd"
    stray.write_bytes(b"x")
    _utime(stray, 1000.0 + es.SKEW_S + 100)
    spool.sweep({}, installed=set(), registry_valid=True, now=1000.0)
    assert not stray.exists()


def test_prune_reaps_removal_temp_residue_with_future_mtime_fail_closed(spool):
    """Same pin as above, for ``.removals/`` staging residue reaped by
    ``prune_removal_records``."""
    removals = Path(spool.root) / ".removals"
    removals.mkdir(exist_ok=True)
    stray = removals / ".ghost.json.tmp-1234-abcd"
    stray.write_bytes(b"x")
    _utime(stray, 1000.0 + es.SKEW_S + 100)
    spool.prune_removal_records(now=1000.0)
    assert not stray.exists()


def test_sweep_survives_one_emitter_raising(spool, monkeypatch, caplog):
    """Minor-5(a) pin: an exception raised while sweeping one emitter
    must not abort the whole pass — every OTHER emitter still gets
    swept, and the removal-record drop step at the end still runs."""
    spool.ensure_emitter_dirs("other")
    p_good = _emit(spool, emitter="other", when=1000.0)
    _emit(spool, emitter=E, when=1000.0)

    orig = es.EventSpool._sweep_emissions

    def boom(self, emitter, efd, routed, now, report):
        if emitter == E:
            raise RuntimeError("simulated")
        return orig(self, emitter, efd, routed, now, report)

    monkeypatch.setattr(es.EventSpool, "_sweep_emissions", boom)
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        spool.sweep({}, installed=set(), registry_valid=True, now=1100.0)
    # "other"'s pair had no routed subscriber -> watermark delete still
    # happened despite E's sweep raising
    assert not p_good.exists()
    assert any("sweep of emitter" in r.message for r in caplog.records)


def test_state_files_are_never_deleted_by_sweep(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    state_path = _state_dir(spool) / f"{EV}.json"
    assert state_path.exists()
    spool.sweep({}, installed=set(), registry_valid=True, now=1200.0)
    assert state_path.exists()


# ---------------------------------------------------------------------------
# removal records — list / mark noted / prune
# ---------------------------------------------------------------------------


def test_list_removal_records_retires_unreadable_entries(spool):
    (Path(spool.root) / ".removals").mkdir(exist_ok=True)
    bad = Path(spool.root) / ".removals" / "ghost-deadbeefdeadbeefdeadbeefdeadbeef.json"
    bad.write_text("{not json")
    assert spool.list_removal_records() == []
    assert not bad.exists()


def test_mark_removal_noted_then_prune_after_window(spool):
    _write_raw(_delivery_path(spool, "ghost"), "{not json")
    spool.sweep({}, installed=set(), registry_valid=True, now=1000.0)
    records = spool.list_removal_records()
    assert len(records) == 1
    name, rec = records[0]

    assert spool.mark_removal_noted(name, now=1100.0) is True
    records = spool.list_removal_records()
    _, rec = records[0]
    assert rec["noted"] is True and rec["noted_ts"] == 1100.0

    pruned = spool.prune_removal_records(now=1100.0 + es.REMOVAL_RECORD_PRUNE_S - 10)
    assert pruned == 0
    pruned = spool.prune_removal_records(now=1100.0 + es.REMOVAL_RECORD_PRUNE_S + 10)
    assert pruned == 1
    assert spool.list_removal_records() == []


def test_prune_never_removes_un_noted_records(spool):
    """#532 (Sol/Terra design r2): an un-noted removal record is the ONLY
    evidence an operator notice is still owed — age alone must never
    delete it (a 30-day outage window used to prune it at boot, seconds
    before the channel came up). Once noted, the noted-clock prune
    applies as before."""
    _write_raw(_delivery_path(spool, "ghost"), "{not json")
    spool.sweep({}, installed=set(), registry_valid=True, now=1000.0)
    pruned = spool.prune_removal_records(
        now=1000.0 + es.REMOVAL_RECORD_MAX_AGE_S + 10)
    assert pruned == 0
    records = spool.list_removal_records()
    assert len(records) == 1 and records[0][1]["noted"] is False


# ---------------------------------------------------------------------------
# orphan GC — gated
# ---------------------------------------------------------------------------


def _backdate_tree(root: Path, when: float) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            _utime(Path(dirpath) / name, when)
        _utime(Path(dirpath), when)


def test_gc_orphan_dirs_is_a_noop_when_registry_invalid(spool):
    _backdate_tree(_edir(spool), 0.0)
    removed = spool.gc_orphan_dirs(registry_valid=False, member_plugins=set(),
                                   now=QUIESCENCE_S + 10000)
    assert removed == []
    assert _edir(spool).exists()


def test_gc_orphan_dirs_removes_a_quiescent_uninstalled_emitter(spool):
    _backdate_tree(_edir(spool), 0.0)
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=QUIESCENCE_S + 10000)
    assert removed == [E]
    assert not _edir(spool).exists()


def test_gc_orphan_dirs_skips_a_member_plugin(spool):
    _backdate_tree(_edir(spool), 0.0)
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins={E},
                                   now=QUIESCENCE_S + 10000)
    assert removed == []
    assert _edir(spool).exists()


def test_gc_orphan_dirs_skips_a_non_quiescent_dir(spool):
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=time.time())
    assert removed == []
    assert _edir(spool).exists()


def test_gc_orphan_dirs_writes_removal_record_when_inventory_nonempty(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    _backdate_tree(_edir(spool), 0.0)
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=QUIESCENCE_S + 10000)
    assert removed == [E]
    records = spool.list_removal_records()
    assert len(records) == 1
    _, rec = records[0]
    assert rec["plugin"] == E
    assert rec["entries"] == [{"kind": "record", "emitter": E, "event": EV,
                               "gen": 1}]


def test_gc_orphan_dirs_defers_when_delivery_listing_is_unprovable(spool, monkeypatch, caplog):
    """Important-2 pin: an unreadable delivery listing must never be
    mistaken for an empty (nothing-to-account-for) inventory — that
    would license an rmtree with no removal record naming what was
    inside. Simulate the unprovable listing directly (the real
    filesystem gives no easy way to make a live directory's listdir
    fail); once the "fault" clears, GC proceeds normally."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    _backdate_tree(_edir(spool), 0.0)

    monkeypatch.setattr(es, "_listdir_strict", lambda fd: None)
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                       now=QUIESCENCE_S + 10000)
    assert removed == []
    assert _edir(spool).exists()
    assert spool.list_removal_records() == []
    assert any("delivery inventory is unprovable" in r.message
              for r in caplog.records)

    monkeypatch.undo()
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=QUIESCENCE_S + 20000)
    assert removed == [E]
    assert len(spool.list_removal_records()) == 1


def test_gc_orphan_dirs_inventories_a_scribbled_delivery_file_not_silently(spool):
    """Important-2 pin: a delivery file that exists BY NAME but never
    validates (and was never quarantined by a sweep pass) must still be
    accounted for in the removal record — never silently dropped from
    the inventory just because its content doesn't parse."""
    _write_raw(_delivery_path(spool, S1), "{not json")
    _backdate_tree(_edir(spool), 0.0)
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=QUIESCENCE_S + 10000)
    assert removed == [E]
    records = spool.list_removal_records()
    assert len(records) == 1
    _, rec = records[0]
    assert rec["plugin"] == E
    assert rec["entries"] == [{"kind": "corrupt", "file": f"{EV}--{S1}.json"}]


def test_inventory_for_removal_fails_closed_on_unprovable_emitter_fd(spool, monkeypatch, caplog):
    """Round-2 Minor pin: ``_inventory_for_removal``'s OWN
    ``_emitter_fd`` call must fail CLOSED (``_SCAN_ERROR``), symmetric
    with the round-1 Important-2 fix — not fail-open (``[]``, shadowed
    only by ``_newest_mtime``'s ordering happening to run first).
    ``_newest_mtime``'s own call to ``_emitter_fd`` is left to succeed so
    this exercises the SECOND call, ``_inventory_for_removal``'s own."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    _backdate_tree(_edir(spool), 0.0)

    orig = es.EventSpool._emitter_fd
    calls = {"i": 0}

    def boom(self, emitter):
        calls["i"] += 1
        if calls["i"] > 1:          # call 1 belongs to _newest_mtime
            raise OSError(5, "simulated")
        return orig(self, emitter)

    monkeypatch.setattr(es.EventSpool, "_emitter_fd", boom)
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                       now=QUIESCENCE_S + 10000)
    assert removed == []
    assert _edir(spool).exists()
    assert spool.list_removal_records() == []
    assert any("delivery inventory is unprovable" in r.message
              for r in caplog.records)


# ---------------------------------------------------------------------------
# removal-record-before-drop ordering (Important-3)
# ---------------------------------------------------------------------------


def test_removal_record_write_failure_defers_the_whole_drop(spool, monkeypatch):
    """A mutation that swaps ``_drop_removed_subscribers`` to drop-then-
    record instead of record-then-drop must be caught: force the
    strict-durable write underneath every removal record to fail, and
    confirm nothing physical is dropped until it succeeds."""
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)

    monkeypatch.setattr(es, "_strict_replace_at", lambda *a, **kw: False)
    report = spool.sweep({}, installed=set(), registry_valid=True, now=1200.0)
    assert report.removal_records_written == 0
    assert report.dropped_records == 0
    assert _delivery_path(spool, S1).exists()      # tombstone still there
    assert spool.list_removal_records() == []

    monkeypatch.undo()
    report2 = spool.sweep({}, installed=set(), registry_valid=True, now=1300.0)
    assert report2.removal_records_written == 1
    assert report2.dropped_records == 1
    assert not _delivery_path(spool, S1).exists()
    assert len(spool.list_removal_records()) == 1


def test_write_removal_record_refuses_an_unsafe_plugin_component(spool, caplog):
    """Minor-3 pin: `plugin` is interpolated directly into the ledger
    filename (`<plugin>-<uuid>.json`) — an unsafe component must be
    refused, never reach the filesystem call, and never write an
    invisible/misplaced record."""
    for bad in ("../escape", "a/b", ".", "..", "a\0b"):
        with caplog.at_level(logging.WARNING, logger="event_spool"):
            ok = spool._write_removal_record(bad, [
                {"kind": "record", "emitter": E, "event": EV, "gen": 1}],
                now=1000.0)
        assert ok is False
        assert any("unsafe plugin component" in r.message
                   for r in caplog.records)
    # nothing was written under any name for any of the attempts above
    assert spool.list_removal_records() == []


# ---------------------------------------------------------------------------
# recovery_pass composition
# ---------------------------------------------------------------------------


def test_recovery_pass_folds_and_sweeps(spool):
    _emit(spool, when=1000.0)
    report = spool.recovery_pass(R(S1), installed={S1}, registry_valid=True,
                                 now=2000.0, boot=False)
    assert report.opened == [(E, EV, S1)]
    assert report.sweep is not None
    state = _read_state(spool)
    assert state["gen"] == 1


def test_recovery_pass_gates_fold_on_registry_valid_same_as_worker(spool):
    """Terra High: recovery must apply the SAME gate ``event_episodes.
    _worker_pass`` applies before ever calling ``fold_pass`` — an
    authoritative-LOOKING ``routed`` dict does not make ``fold_pass``
    safe to call when ``registry_valid`` is False: no reconstruct, no
    repair, no open, no unlink, no state advance."""
    _emit(spool, when=1000.0)
    report = spool.recovery_pass(R(S1), installed={S1}, registry_valid=False,
                                 now=2000.0, boot=False)
    assert report.opened == []
    assert _read_state(spool) is None          # no state advance
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))  # untouched,
    # never unlinked

    # registry heals: the SAME emission finally folds normally
    report2 = spool.recovery_pass(R(S1), installed={S1}, registry_valid=True,
                                  now=2100.0)
    assert report2.opened == [(E, EV, S1)]
    assert _read_state(spool)["gen"] == 1


def test_recovery_pass_runs_gc_only_on_boot_with_valid_registry(spool):
    _backdate_tree(_edir(spool), 0.0)
    report = spool.recovery_pass({}, installed=set(), registry_valid=True,
                                 now=QUIESCENCE_S + 10000, boot=False)
    assert report.gc_removed == []
    assert _edir(spool).exists()

    report2 = spool.recovery_pass({}, installed=set(), registry_valid=True,
                                  now=QUIESCENCE_S + 20000, boot=True)
    assert report2.gc_removed == [E]


# ---------------------------------------------------------------------------
# ready / index surface + list_emissions + SpoolClosed (Minor-6(a))
# ---------------------------------------------------------------------------


def test_write_ready_then_read_marker_present(spool):
    spool.write_ready(E, {"a": 1})
    m = spool.read_marker(E)
    assert m.state == MarkerState.PRESENT
    assert m.payload == {"a": 1}


def test_read_marker_absent_before_write(spool):
    m = spool.read_marker(E)
    assert m.state == MarkerState.ABSENT


def test_read_marker_absent_for_unknown_emitter(spool):
    m = spool.read_marker("nobody")
    assert m.state == MarkerState.ABSENT


def test_delete_ready_removes_marker(spool):
    spool.write_ready(E, {"a": 1})
    assert spool.delete_ready(E) is True
    assert spool.read_marker(E).state == MarkerState.ABSENT


def test_delete_ready_of_missing_emitter_dir_is_true(spool):
    assert spool.delete_ready("nobody") is True


def test_published_emitters_lists_only_ready_emitters(spool):
    spool.ensure_emitter_dirs("other")
    spool.write_ready(E, {})
    assert spool.published_emitters() == [E]


def test_index_entry_write_read_delete(spool):
    key = "abc123"
    spool.write_index_entry(key, {"x": 1})
    m = spool.read_index_marker(key)
    assert m.state == MarkerState.PRESENT and m.payload == {"x": 1}
    assert key in spool.index_keys()
    assert spool.delete_index_entry(key) is True
    assert spool.read_index_marker(key).state == MarkerState.ABSENT
    assert key not in spool.index_keys()


def test_delete_index_entry_of_missing_key_is_true(spool):
    assert spool.delete_index_entry("neverwritten") is True


def test_write_index_entry_refuses_an_unsafe_key(spool):
    """Minor-5 pin: `key` is caller-supplied and interpolated directly
    into a path component — an unsafe one (a stray "/", "..", a null
    byte) must be refused before it ever reaches the filesystem."""
    for bad in ("../escape", "a/b", ".", "..", "a\0b"):
        with pytest.raises(ValueError):
            spool.write_index_entry(bad, {"x": 1})
    assert spool.index_keys() == []


def test_delete_index_entry_refuses_an_unsafe_key(spool):
    for bad in ("../escape", "a/b", "."):
        with pytest.raises(ValueError):
            spool.delete_index_entry(bad)


def test_read_index_marker_of_an_unsafe_key_is_absent_not_raise(spool):
    """Read-only convenience: degrades to ABSENT rather than raising,
    mirroring read_marker's own unsafe-name handling."""
    for bad in ("../escape", "a/b", "."):
        assert spool.read_index_marker(bad).state == MarkerState.ABSENT


def test_list_emissions_reflects_current_files(spool):
    _emit(spool, when=1000.0)
    _emit(spool, when=1001.0)
    names = spool.list_emissions(E, EV)
    assert len(names) == 2
    assert all(n.startswith(f"{EV}--") for n in names)


def test_list_emissions_empty_for_unknown_emitter(spool):
    assert spool.list_emissions("nobody", EV) == []


def test_spool_closed_raises_on_ensure_emitter_dirs(spool):
    spool.close()
    with pytest.raises(es.SpoolClosed):
        spool.ensure_emitter_dirs("late")


def test_spool_closed_raises_on_write_ready(spool):
    spool.close()
    with pytest.raises(es.SpoolClosed):
        spool.write_ready(E, {})


def test_spool_closed_read_marker_degrades_to_absent(spool):
    spool.close()
    assert spool.read_marker(E).state == MarkerState.ABSENT


def test_spool_closed_fold_pass_returns_empty(spool):
    spool.close()
    assert spool.fold_pass(R(S1), 1000.0) == []


def test_spool_closed_sweep_returns_empty_report(spool):
    spool.close()
    report = spool.sweep({}, installed=set(), registry_valid=True, now=1000.0)
    assert report.deleted_temps == 0


# ---------------------------------------------------------------------------
# spool_issues
# ---------------------------------------------------------------------------


def test_spool_issues_empty_on_a_clean_spool(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    assert spool.spool_issues() == []


# ---------------------------------------------------------------------------
# init_spool / get_spool / env override
# ---------------------------------------------------------------------------


def test_init_and_get_spool_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv(es.SPOOL_ROOT_ENV, str(tmp_path / "envroot"))
    s = es.init_spool()
    try:
        assert es.get_spool() is s
        assert s.root == tmp_path / "envroot"
    finally:
        s.close()


def test_get_spool_before_init_is_none_or_prior():
    # get_spool reflects whatever the LAST init_spool call in this process
    # set — no assumption about ordering across tests beyond "callable
    # without raising".
    es.get_spool()


def test_module_level_spool_issues_degrades_quietly_without_a_spool(monkeypatch):
    monkeypatch.setattr(es, "_SPOOL", None)
    assert es.spool_issues() == []
