"""Task 2: durable controller state."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from casa_browser.protocol import (
    ACTIVE_STATES,
    Admission,
    ProtocolError,
    RunRecord,
    RunSpec,
    SafeEvent,
    SafeNotice,
    Site,
    SiteGrant,
)
from casa_browser.store import MIGRATIONS, PairingState, Store

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def spec(request_id: str = "req-1", requester: str = "finance", text: str = "get the invoice") -> RunSpec:
    return RunSpec(request_id=request_id, requester_id=requester, mode="account",
                   task_text=text, task_kind="invoice",
                   sites=(SiteGrant("site-a", "account"),), result_expectation="a PDF",
                   admission=Admission("operator_telegram", "tg-1", "sha256:" + "0" * 64))


def site(site_id: str = "site-a") -> Site:
    return Site(site_id=site_id, display_name="Site A", origins=("https://a.example",),
                login_origins=("https://login.a.example",), vault_item_id="item123",
                provider_bindings=(), start_page_private="https://a.example/start", revision=1)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def test_single_active_run_and_fifo(store):
    ids = []
    for n in range(3):
        run, created = store.create_run(spec(f"req-{n}"), f"run{n}", now=T0 + timedelta(seconds=n))
        assert created
        ids.append(run.run_id)
    first = store.lease_next_run()
    assert first.run_id == "run0" and first.state == "starting"
    assert store.lease_next_run() is None  # lease held
    for state_from, state_to in (("starting", "running"), ("running", "waiting_live"),
                                 ("waiting_live", "live"), ("live", "finishing")):
        assert store.transition("run0", state_from, state_to)
        assert store.lease_next_run() is None
    assert store.transition("run0", "finishing", "completed")
    assert store.lease_next_run().run_id == "run1"
    assert store.transition("run1", "starting", "cancelled")
    assert store.lease_next_run().run_id == "run2"


def test_lease_is_exclusive_across_threads(store):
    for n in range(6):
        store.create_run(spec(f"r{n}"), f"run{n}")
    got = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        got.append(store.lease_next_run())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([g for g in got if g is not None]) == 1


def test_transition_is_compare_and_set(store):
    store.create_run(spec(), "run0")
    assert not store.transition("run0", "running", "completed")
    assert store.get_run("run0").state == "queued"
    assert store.transition("run0", "queued", "cancelled")
    # Terminal never moves.
    assert not store.transition("run0", "cancelled", "running")
    assert store.get_run("run0").state == "cancelled"


def test_create_run_idempotent_by_request_id(store):
    run, created = store.create_run(spec(), "run0")
    again, created2 = store.create_run(spec(), "run-other")
    assert created and not created2 and again.run_id == "run0"
    with pytest.raises(ProtocolError) as err:
        store.create_run(spec(text="something else"), "run-third")
    assert err.value.code == "conflict"
    assert store.get_run("run-third") is None


def test_restart_interrupts_active_and_queued(store):
    for n, state in enumerate(ACTIVE_STATES):
        store.create_run(spec(f"req-{n}"), f"run{n}")
        if state != "queued":
            store._db.execute("UPDATE runs SET state=? WHERE run_id=?", (state, f"run{n}"))
    store.create_run(spec("done"), "rundone")
    store._db.execute("UPDATE runs SET state='completed' WHERE run_id='rundone'")
    interrupted = store.interrupt_nonterminal(now=T0)
    assert sorted(interrupted) == sorted(f"run{n}" for n in range(len(ACTIVE_STATES)))
    for n in range(len(ACTIVE_STATES)):
        assert store.get_run(f"run{n}").state == "interrupted"
    assert store.get_run("rundone").state == "completed"
    notices = store.notices_after(0)
    assert {n.run_id for n in notices} == set(interrupted)
    # A second restart neither re-interrupts nor duplicates notices.
    assert store.interrupt_nonterminal(now=T0) == []
    assert len(store.notices_after(0)) == len(interrupted)


def test_events_sequence_monotonic_per_run(store):
    store.create_run(spec("a"), "runA")
    store.create_run(spec("b"), "runB")
    for n in range(3):
        ev = SafeEvent(0, "runA", "step", None, None, f"t{n}", "ok", None, T0)
        assert store.append_event("runA", ev) == n + 1
    assert store.append_event("runB", SafeEvent(0, "runB", "step", None, None, "x", "ok",
                                                None, T0)) == 1
    assert [e.seq for e in store.events_after("runA", 1)] == [2, 3]
    assert store.events_after("runB", 5) == []


def test_sites_round_trip_and_private_repr(store):
    store.put_site(site())
    got = store.get_site("site-a")
    assert got == site()
    assert "a.example" not in repr(got) and "item123" not in repr(got)


def test_records_scoped_to_requester(store):
    store.create_run(spec("a", requester="finance"), "runA")
    store.create_run(spec("b", requester="mtg"), "runB")
    for run_id, requester in (("runA", "finance"), ("runB", "mtg")):
        store.put_record(RunRecord(run_id, requester, ("site-a",), "done", (), (), (), T0))
    assert [r.run_id for r in store.records_for("finance", T0 - timedelta(days=1))] == ["runA"]
    assert store.records_for("finance", T0 + timedelta(days=1)) == []


def test_notice_dedupe(store):
    n1 = store.add_notice(SafeNotice(0, "credential_saved", None, "site-a", "Casa saved a new password",
                                     T0, dedupe_key="save:1"))
    n2 = store.add_notice(SafeNotice(0, "credential_saved", None, "site-a", "Casa saved a new password",
                                     T0, dedupe_key="save:1"))
    assert n1 == n2 and len(store.notices_after(0)) == 1


def test_pairing_state_round_trip(store):
    assert store.get_pairing().generation == 0
    state = PairingState(3, "ab" * 32, "Casa (Fido plugin)", "172.30.32.5", T0, None)
    store.put_pairing(state)
    assert store.get_pairing() == state
    assert "ab" * 32 not in repr(store.get_pairing())


def test_migration_and_restart_twice(tmp_path):
    path = tmp_path / "state.db"
    s = Store(path)
    s.put_site(site())
    s.create_run(spec(), "run0")
    s.close()
    for _ in range(2):
        s = Store(path)
        assert s.schema_version() == len(MIGRATIONS)
        s.interrupt_nonterminal(now=T0)
        assert s.get_site("site-a") == site()
        assert len(s.notices_after(0)) == 1
        s.close()


def test_owed_releases_upgrade_from_task12_schema(tmp_path):
    """A database at the Task 12 schema (version 5) gains the owed-release table."""
    import sqlite3

    from casa_browser import store as store_mod
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.execute("INSERT INTO schema_version VALUES (5)")
    for script in store_mod.MIGRATIONS[:5]:
        db.executescript(script)
    db.commit()
    db.close()
    s = store_mod.Store(tmp_path / "state.db")
    try:
        assert s.schema_version() == len(store_mod.MIGRATIONS) >= 6
        s.owe_release("r1")
        assert s.owed_releases() == ["r1"]
    finally:
        s.close()


def test_binding_keys_upgrade_from_task13_schema(tmp_path):
    """A database at the Task 13 schema (version 6) gains the binding-key table; a site's row
    goes with the site, and ``None`` clears it."""
    import sqlite3

    from casa_browser import store as store_mod
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.execute("INSERT INTO schema_version VALUES (6)")
    for script in store_mod.MIGRATIONS[:6]:
        db.executescript(script)
    db.commit()
    db.close()
    s = store_mod.Store(tmp_path / "state.db")
    try:
        assert s.schema_version() == 10 == len(store_mod.MIGRATIONS)
        s.put_site(site())
        s.put_binding_key("site-a", "k1")
        s.put_binding_key("site-a", "k2")
        assert s.binding_keys() == {"site-a": "k2"}
        s.put_binding_key("site-a", None)
        assert s.binding_keys() == {}
    finally:
        s.close()


def _rows(path, table):
    import sqlite3
    db = sqlite3.connect(path)
    try:
        return db.execute(f'SELECT * FROM "{table}" ORDER BY 1, 2').fetchall()
    finally:
        db.close()


def test_migration_9_drops_the_approval_model_and_keeps_everything_else(tmp_path):
    """Slice 7: a database at schema 8 holding approvals, proposal bundles, hints and events
    that name a bundle loses exactly those three tables and the events' bundle column; every
    other row stays as it was (sites, runs, every event's other columns, questions, stored
    responses, sources, notices), and the store works on it."""
    import json as _json
    import sqlite3

    from casa_browser import store as store_mod
    # Rows as this code writes them, in a scratch store; copied into a schema-8 database.
    scratch = store_mod.Store(tmp_path / "scratch.db")
    scratch.put_site(site())
    scratch.create_run(spec(), "run0")
    scratch.append_event("run0", SafeEvent(0, "run0", "step", "https://a.example", "h1", "seen",
                                           "ok", "shot1", T0))
    scratch.append_event("run0", SafeEvent(0, "run0", "action", None, None, "asked", "asked",
                                           None, T0 + timedelta(seconds=1)))
    scratch.create_question("q1", "run0", 1, "d" * 64, {"text": "Confirm?"}, T0)
    scratch.put_response("run0", "j1", {"outcome": "done"})
    scratch.add_source("run0", "https://pub.example/a", "A page", "src_1", 50)
    scratch.add_notice(SafeNotice(0, "credential_saved", "run0", "site-a", "saved", T0,
                                  dedupe_key="save:1"))
    scratch.close()
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.execute("INSERT INTO schema_version VALUES (8)")
    for script in store_mod.MIGRATIONS[:8]:
        db.executescript(script)
    db.execute("ATTACH DATABASE ? AS scratch", (str(tmp_path / "scratch.db"),))
    for table in ("sites", "runs", "confirmations", "responses", "sources", "notices"):
        db.execute(f'INSERT INTO main."{table}" SELECT * FROM scratch."{table}"')
    db.execute("DELETE FROM main.pairing")
    db.execute("INSERT INTO main.pairing SELECT * FROM scratch.pairing")
    db.execute("INSERT INTO main.events SELECT run_id, seq, kind, origin, handle, text_masked, "
               "outcome, screenshot_id, 'bd_1', at FROM scratch.events")
    db.commit()
    db.execute("DETACH DATABASE scratch")
    db.execute("INSERT INTO approvals VALUES ('ap1', 'site-a', 'page', x'00ff', '[]', 'read', "
               "'allowed', 1)")
    db.execute("INSERT INTO bundles VALUES ('bd_1', 'run0', 'proposal', 1, 'open', x'01', '{}', "
               "'{}', NULL, NULL, NULL, ?, NULL)", (T0.isoformat(),))
    db.execute("INSERT INTO hints (site_id, task_kind, recorded_at, lines) VALUES "
               "('site-a', 'invoice', ?, '[]')", (T0.isoformat(),))
    db.commit()
    db.close()
    kept = {t: _rows(path, t) for t in ("sites", "runs", "confirmations", "responses", "sources",
                                         "notices", "pairing")}
    events_before = [r[:8] + r[9:] for r in _rows(path, "events")]
    assert len(events_before) == 2

    s = store_mod.Store(path)
    try:
        assert s.schema_version() == 10 == len(store_mod.MIGRATIONS)
        tables = {r[0] for r in s._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables & {"approvals", "bundles", "hints"}
        columns = [r[1] for r in s._db.execute("PRAGMA table_info(events)")]
        assert columns == ["run_id", "seq", "kind", "origin", "handle", "text_masked", "outcome",
                           "screenshot_id", "at"]
        assert s._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert s._db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert s.get_site("site-a") == site()
        assert s.get_run("run0").spec == spec()
        assert [(e.seq, e.kind, e.origin, e.handle, e.text_masked, e.outcome, e.screenshot_id,
                 e.at) for e in s.events_after("run0", 0)] == [
            (1, "step", "https://a.example", "h1", "seen", "ok", "shot1", T0),
            (2, "action", None, None, "asked", "asked", None, T0 + timedelta(seconds=1))]
        assert "bundle_id" not in _json.dumps(s.events_after("run0", 0)[0].to_wire())
        assert s.get_question("q1").view == {"text": "Confirm?"}
        assert s.get_response("run0", "j1") == {"outcome": "done"}
        assert s.append_event("run0", SafeEvent(0, "run0", "step", None, None, "x", "ok", None,
                                                T0)) == 3
    finally:
        s.close()
    assert {t: _rows(path, t) for t in kept} == kept
    assert [r for r in _rows(path, "events")][:2] == events_before
    s = store_mod.Store(path)          # a second start applies nothing again
    try:
        assert s.schema_version() == 10 and len(s.events_after("run0", 0)) == 3
    finally:
        s.close()


def test_a_cancelled_request_id_survives_a_reopen_and_creates_its_run_cancelled(tmp_path):
    """The Stop's record (``cancelled_requests``) is durable: after a reopen the request id is
    still recorded, and a run created for it then starts ``cancelled`` and is never leased;
    another request's run is queued as before."""
    path = tmp_path / "state.db"
    s = Store(path)
    s.cancel_request_id("req-stop", now=T0)
    s.cancel_request_id("req-stop", now=T0 + timedelta(seconds=5))     # a repeat keeps it
    s.close()
    s = Store(path)
    try:
        assert s.request_cancelled("req-stop") and not s.request_cancelled("req-other")
        run, created = s.create_run(spec("req-stop"), "run_s", now=T0)
        assert created and run.state == "cancelled"
        assert s._db.execute("SELECT ended_at FROM runs WHERE run_id='run_s'").fetchone()[0]
        other, _ = s.create_run(spec("req-other"), "run_o", now=T0)
        assert other.state == "queued"
        assert s.lease_next_run().run_id == "run_o"
        assert s.run_by_request("req-stop").run_id == "run_s"
        assert s._db.execute("SELECT COUNT(*) FROM cancelled_requests").fetchone()[0] == 1
    finally:
        s.close()


def test_a_queued_run_whose_request_was_stopped_is_never_leased(store):
    """Ruling B: ``lease_next_run`` skips a queued run whose request id is recorded stopped,
    whatever moved (or did not move) its state."""
    store.create_run(spec("req-a"), "run_a", now=T0)
    store.create_run(spec("req-b"), "run_b", now=T0 + timedelta(seconds=1))
    store.cancel_request_id("req-a", now=T0)
    assert store.lease_next_run().run_id == "run_b"
