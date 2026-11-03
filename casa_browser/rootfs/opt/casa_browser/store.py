"""Controller-private durable state: one SQLite database under ``/data/state/``.

It holds the site registry, runs and their FIFO lease, the masked step log, run records, file metadata, durable notices and
the pairing state. Profiles live elsewhere (``/data/profiles/<site-id>/``, browser-owned)
and private screenshots/files under ``/data/artifacts/``; this module only records their
generated paths.

Guarantees:

* WAL, foreign keys on, schema migrations applied in order inside one transaction.
* One active run at a time: ``lease_next_run`` moves the oldest ``queued`` run to
  ``starting`` only when no run is between ``starting`` and ``finishing``, in one
  ``BEGIN IMMEDIATE`` transaction.
* ``transition`` is a compare-and-set on the run state; a terminal run never moves.
* ``interrupt_nonterminal`` (app start) interrupts every non-terminal run, queued ones
  included, and adds one durable notice per run (idempotent by its dedupe key).
* Event sequence numbers are per run, strictly increasing, assigned inside the
  transaction that appends the event.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .protocol import (
    ACTIVE_STATES,
    Admission,
    Change,
    FileRecord,
    ProtocolError,
    ProviderBinding,
    Run,
    RunRecord,
    RunSpec,
    SafeEvent,
    SafeNotice,
    Site,
    SiteGrant,
    Source,
    TERMINAL_STATES,
    spec_digest,
)

LEASED_STATES = tuple(s for s in ACTIVE_STATES if s != "queued")

MIGRATIONS: tuple[str, ...] = (
    # 1: initial schema
    """
    CREATE TABLE sites (
        site_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        origins TEXT NOT NULL,
        login_origins TEXT NOT NULL,
        vault_item_id TEXT,
        provider_bindings TEXT NOT NULL,
        start_page_private TEXT,
        revision INTEGER NOT NULL
    );
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        exact_identity BLOB NOT NULL,
        generalisations TEXT NOT NULL,
        effect TEXT NOT NULL,
        state TEXT NOT NULL,
        revision INTEGER NOT NULL
    );
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        fifo INTEGER NOT NULL UNIQUE,
        request_id TEXT NOT NULL UNIQUE,
        requester_id TEXT NOT NULL,
        spec TEXT NOT NULL,
        spec_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        ended_at TEXT,
        policy_revision INTEGER NOT NULL
    );
    CREATE INDEX runs_state ON runs(state, fifo);
    CREATE TABLE events (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL,
        origin TEXT,
        handle TEXT,
        text_masked TEXT NOT NULL,
        outcome TEXT NOT NULL,
        screenshot_id TEXT,
        bundle_id TEXT,
        at TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    );
    CREATE TABLE records (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
        requester_id TEXT NOT NULL,
        sites TEXT NOT NULL,
        summary_masked TEXT NOT NULL,
        changes TEXT NOT NULL,
        file_ids TEXT NOT NULL,
        delivery_outcomes TEXT NOT NULL,
        ended_at TEXT NOT NULL
    );
    CREATE INDEX records_requester ON records(requester_id, ended_at);
    CREATE TABLE files (
        file_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        requester_id TEXT NOT NULL,
        safe_name TEXT NOT NULL,
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        private_path TEXT NOT NULL,
        source_handle TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE screenshots (
        screenshot_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        private_path TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE notices (
        notice_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        run_id TEXT,
        site_id TEXT,
        text_masked TEXT NOT NULL,
        at TEXT NOT NULL,
        dedupe_key TEXT UNIQUE
    );
    CREATE TABLE pending_deletions (
        path TEXT PRIMARY KEY
    );
    CREATE TABLE pairing (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        generation INTEGER NOT NULL,
        secret_sha256 TEXT,
        caller_label TEXT,
        observed_peer TEXT,
        claimed_at TEXT,
        armed_until TEXT
    );
    INSERT INTO pairing (id, generation) VALUES (1, 0);
    """,
    # 2: delivery attempts (Task 10). One row per (file, delivery_id); the latest attempt by
    # ``seq`` is the file's current one.
    """
    CREATE TABLE deliveries (
        file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
        delivery_id TEXT NOT NULL,
        requester_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        seq INTEGER NOT NULL UNIQUE,
        first_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (file_id, delivery_id)
    );
    """,
    # 3: proposal bundles (run-scoped, swept with the run) and journey hints (site data) —
    # Task 11. ``private`` holds the items' sites, exact identities and freedoms; it never
    # leaves controller-private state.
    """
    CREATE TABLE bundles (
        bundle_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        revision INTEGER NOT NULL,
        state TEXT NOT NULL,
        salt BLOB NOT NULL,
        view TEXT NOT NULL,
        private TEXT NOT NULL,
        decision TEXT,
        outcome TEXT,
        operator_ref TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT
    );
    CREATE UNIQUE INDEX bundles_one_open ON bundles(run_id) WHERE state = 'open';
    CREATE TABLE hints (
        hint_id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
        task_kind TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        lines TEXT NOT NULL
    );
    """,
    # 4: provider bindings become keyed tags (Task 9); bindings stored before hold raw
    # components and are dropped, so each provider sign-in is learned again.
    "UPDATE sites SET provider_bindings = '[]';",
    # 5: a run's recorded public-session sources (Task 12, v9.2). ``url`` is controller-private
    # run data: it leaves the app only through the trusted sources route, and is swept with
    # the run.
    """
    CREATE TABLE sources (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        url TEXT NOT NULL,
        title_masked TEXT NOT NULL,
        seq INTEGER NOT NULL,
        PRIMARY KEY (run_id, source_id),
        UNIQUE (run_id, url)
    );
    """,
    # 6: runs whose broker ``end_run`` was never acknowledged (Task 13, the B1 rule across a
    # controller restart): no queued run is leased while a row exists. No foreign key: the
    # obligation outlives the run's retention.
    """
    CREATE TABLE owed_releases (
        run_id TEXT PRIMARY KEY,
        since TEXT NOT NULL
    );
    """,
    # 7: which proxy binding key a site's provider binding was learned under (Task 14). HA
    # backups exclude the key (``proxy/*``), so after a restore the proxy makes a new one and
    # every restored binding's tags are stale: the site needs a live login.
    """
    CREATE TABLE binding_keys (
        site_id TEXT PRIMARY KEY REFERENCES sites(site_id) ON DELETE CASCADE,
        key_id TEXT NOT NULL
    );
    """,
    # 8: the action model (slice 2). ``confirmations`` holds each question the check asked the
    # operator: its view (no exact destination), its state and the stored response of its one
    # answer; one open or confirming question per run. ``responses`` is the stored response of
    # every verdict a run's judgements received, keyed by judgement id, written before it is
    # returned (and, provisionally ``outcome_unknown``, immediately before a dispatching input).
    # Both are swept with the run.
    """
    CREATE TABLE confirmations (
        question_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,
        digest TEXT NOT NULL,
        state TEXT NOT NULL,
        view TEXT NOT NULL,
        outcome TEXT,
        response TEXT,
        operator_ref TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT
    );
    CREATE UNIQUE INDEX confirmations_one_open ON confirmations(run_id)
        WHERE state IN ('open', 'confirming');
    CREATE TABLE responses (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        seq INTEGER NOT NULL,
        response TEXT NOT NULL,
        PRIMARY KEY (run_id, key)
    );
    """,
    # 9: the approval model is gone (action model, slice 7): its approvals, proposal bundles
    # and journey hints, and the events' bundle reference. Nothing reads them any more.
    """
    DROP TABLE bundles;
    DROP TABLE hints;
    DROP TABLE approvals;
    ALTER TABLE events DROP COLUMN bundle_id;
    """,
    # 10: the request ids whose cancel won (the operator's Stop): a cancel that ended its
    # run, or found no run yet, records the id, and a run later created for it is created
    # already cancelled. No foreign key: the row is what the request was, run or not; it is
    # swept with the runs' retention.
    """
    CREATE TABLE cancelled_requests (
        request_id TEXT PRIMARY KEY,
        at TEXT NOT NULL
    );
    """,
)

QUESTION_STATES = ("open", "confirming", "executed", "outcome_unknown", "lapsed", "declined",
                   "answered", "expired")
RESPONSES_KEPT = 16       # a run's stored verdict responses (its last 16 judgement ids)


@dataclass(frozen=True)
class Question:
    question_id: str
    run_id: str
    revision: int
    digest: str
    state: str
    view: dict
    outcome: str | None
    response: dict | None
    operator_ref: str | None
    created_at: datetime
    decided_at: datetime | None

# The one move a delivery attempt may make after it is recorded (Plan 2 Task 7's outcomes).
DELIVERY_MOVES = {("deposited", "delivered"), ("deposited", "withheld")}


def _ts(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("naive datetime")
    return at.astimezone(timezone.utc).isoformat()


def _dt(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PairingState:
    generation: int
    secret_sha256: str | None
    caller_label: str | None
    observed_peer: str | None
    claimed_at: datetime | None
    armed_until: datetime | None

    def __repr__(self) -> str:  # the secret's hash is not a display value either
        return (f"PairingState(generation={self.generation}, paired={self.secret_sha256 is not None},"
                f" caller_label={self.caller_label!r})")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._depth = 0
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA synchronous=FULL")
        self._migrate()

    # -- plumbing -----------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction; nested calls join the outer one."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self._db
                finally:
                    self._depth -= 1
                return
            self._db.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._db
            except BaseException:
                self._depth = 0
                self._db.execute("ROLLBACK")
                raise
            self._depth = 0
            self._db.execute("COMMIT")

    def _migrate(self) -> None:
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = db.execute("SELECT version FROM schema_version").fetchone()
            current = row["version"] if row else 0
            if row is None:
                db.execute("INSERT INTO schema_version (version) VALUES (0)")
            for number, script in enumerate(MIGRATIONS, start=1):
                if number <= current:
                    continue
                for statement in script.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("UPDATE schema_version SET version = ?", (number,))

    def schema_version(self) -> int:
        with self._lock:
            return self._db.execute("SELECT version FROM schema_version").fetchone()["version"]

    # -- sites ---------------------------------------------------------------------------

    def put_site(self, site: Site) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO sites VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(site_id) DO UPDATE SET "
                "display_name=excluded.display_name, origins=excluded.origins, "
                "login_origins=excluded.login_origins, vault_item_id=excluded.vault_item_id, "
                "provider_bindings=excluded.provider_bindings, "
                "start_page_private=excluded.start_page_private, revision=excluded.revision",
                (site.site_id, site.display_name, json.dumps(list(site.origins)),
                 json.dumps(list(site.login_origins)), site.vault_item_id,
                 json.dumps([b.__dict__ for b in site.provider_bindings]),
                 site.start_page_private, site.revision))

    @staticmethod
    def _site(row: sqlite3.Row) -> Site:
        return Site(
            site_id=row["site_id"], display_name=row["display_name"],
            origins=tuple(json.loads(row["origins"])),
            login_origins=tuple(json.loads(row["login_origins"])),
            vault_item_id=row["vault_item_id"],
            provider_bindings=tuple(ProviderBinding(**b) for b in json.loads(row["provider_bindings"])),
            start_page_private=row["start_page_private"], revision=row["revision"])

    def get_site(self, site_id: str) -> Site | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        return None if row is None else self._site(row)

    def list_sites(self) -> list[Site]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sites ORDER BY display_name, site_id").fetchall()
        return [self._site(r) for r in rows]

    # -- runs ------------------------------------------------------------------------

    @staticmethod
    def _spec_json(spec: RunSpec) -> str:
        return json.dumps(spec.to_wire(), sort_keys=True)

    @staticmethod
    def _spec(text: str) -> RunSpec:
        raw = json.loads(text)
        adm = raw["admission"]
        return RunSpec(
            request_id=raw["request_id"], requester_id=raw["requester_id"], mode=raw["mode"],
            task_text=raw["task_text"], task_kind=raw["task_kind"],
            sites=tuple(SiteGrant(s["site_id"], s["access"]) for s in raw["sites"]),
            result_expectation=raw["result_expectation"],
            admission=Admission(adm["origin_kind"], adm["origin_ref"], adm["admitted_spec_digest"]),
            start_url=raw.get("start_url"))

    def _run(self, row: sqlite3.Row) -> Run:
        return Run(run_id=row["run_id"], spec=self._spec(row["spec"]), state=row["state"],
                   created_at=_dt(row["created_at"]), policy_revision=row["policy_revision"])

    def create_run(self, spec: RunSpec, run_id: str, now: datetime | None = None) -> tuple[Run, bool]:
        """Queue a run. Idempotent on ``request_id``: the same request with the same
        members returns the existing run (``created=False``); different members raise
        ``ProtocolError('conflict')``. A request id in ``cancelled_requests`` is created in
        state ``cancelled``."""
        digest = spec_digest(spec.members())
        now = now or _now()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE request_id=?", (spec.request_id,)).fetchone()
            if row is not None:
                if row["spec_digest"] != digest:
                    raise ProtocolError("conflict")
                return self._run(row), False
            fifo = db.execute("SELECT COALESCE(MAX(fifo), 0) + 1 AS n FROM runs").fetchone()["n"]
            # a request whose cancel was recorded before its run existed is created cancelled:
            # it is never leased, so no browser starts for it
            stopped = db.execute("SELECT 1 FROM cancelled_requests WHERE request_id=?",
                                 (spec.request_id,)).fetchone() is not None
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (run_id, fifo, spec.request_id, spec.requester_id, self._spec_json(spec),
                        digest, "cancelled" if stopped else "queued", _ts(now),
                        _ts(now) if stopped else None, 0))
            return self._run(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()), True

    def run_by_request(self, request_id: str) -> Run | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE request_id=?",
                                   (request_id,)).fetchone()
        return None if row is None else self._run(row)

    def cancel_request_id(self, request_id: str, now: datetime | None = None) -> None:
        """Record that a cancel of this request id won (kept as first recorded)."""
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO cancelled_requests VALUES (?,?)",
                       (request_id, _ts(now or _now())))

    def request_cancelled(self, request_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM cancelled_requests WHERE request_id=?",
                                    (request_id,)).fetchone() is not None

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else self._run(row)

    def list_runs(self, states: tuple[str, ...]) -> list[Run]:
        marks = ",".join("?" for _ in states)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY fifo", states).fetchall()
        return [self._run(r) for r in rows]

    def transition(self, run_id: str, expected_state: str, new_state: str,
                   now: datetime | None = None) -> bool:
        """Compare-and-set. ``False`` when the run is not in ``expected_state``."""
        if expected_state in TERMINAL_STATES:
            return False
        with self.transaction() as db:
            ended = _ts(now or _now()) if new_state in TERMINAL_STATES else None
            cur = db.execute("UPDATE runs SET state=?, ended_at=COALESCE(?, ended_at) "
                             "WHERE run_id=? AND state=?",
                             (new_state, ended, run_id, expected_state))
            return cur.rowcount == 1

    def lease_next_run(self) -> Run | None:
        """The FIFO lease: oldest queued → ``starting``, only when no run holds the lease."""
        marks = ",".join("?" for _ in LEASED_STATES)
        with self.transaction() as db:
            busy = db.execute(f"SELECT 1 FROM runs WHERE state IN ({marks}) LIMIT 1",
                              LEASED_STATES).fetchone()
            if busy:
                return None
            # a run whose request was stopped (``cancelled_requests``) is never leased
            row = db.execute("SELECT * FROM runs WHERE state='queued' AND request_id NOT IN "
                             "(SELECT request_id FROM cancelled_requests) ORDER BY fifo "
                             "LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE runs SET state='starting' WHERE run_id=?", (row["run_id"],))
            return self._run(db.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone())

    def set_run_spec(self, run_id: str, spec: RunSpec) -> None:
        """Slice 5n: the run's spec once the operator's tap on a sign-in card gave it a site
        (``mode`` account, the site's account grant). Only the ``spec`` column changes: the
        ``spec_digest`` column stays the admitted request's, so a resubmission of the same
        request still finds this run, and the crash path (``interrupt_nonterminal``) reads the
        account mode and owes the broker release."""
        with self.transaction() as db:
            db.execute("UPDATE runs SET spec=? WHERE run_id=?", (self._spec_json(spec), run_id))

    def set_policy_revision(self, run_id: str, revision: int) -> None:
        with self.transaction() as db:
            db.execute("UPDATE runs SET policy_revision=? WHERE run_id=?", (revision, run_id))

    def interrupt_nonterminal(self, now: datetime | None = None) -> list[str]:
        now = now or _now()
        marks = ",".join("?" for _ in ACTIVE_STATES)
        with self.transaction() as db:
            rows = db.execute(f"SELECT run_id, spec FROM runs WHERE state IN ({marks}) "
                              "ORDER BY fifo", ACTIVE_STATES).fetchall()
            ids = [r["run_id"] for r in rows]
            for row in rows:
                run_id = row["run_id"]
                db.execute("UPDATE runs SET state='interrupted', ended_at=? WHERE run_id=?",
                           (_ts(now), run_id))
                if self._spec(row["spec"]).mode == "account":
                    # Owed in the same transaction (Task 13): a restart before the broker
                    # acknowledges this run's end_run still owes it.
                    db.execute("INSERT OR IGNORE INTO owed_releases VALUES (?, ?)",
                               (run_id, _ts(now)))
                self.add_notice(SafeNotice(0, "run_interrupted", run_id, None,
                                           "A browser run was interrupted by an app restart "
                                           "and was not resumed.", now,
                                           dedupe_key=f"interrupted:{run_id}"))
            return ids

    # -- events --------------------------------------------------------------------------

    def append_event(self, run_id: str, event: SafeEvent) -> int:
        with self.transaction() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events WHERE run_id=?",
                             (run_id,)).fetchone()["n"]
            db.execute("INSERT INTO events (run_id, seq, kind, origin, handle, text_masked, "
                       "outcome, screenshot_id, at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (run_id, seq, event.kind, event.origin, event.handle, event.text_masked,
                        event.outcome, event.screenshot_id, _ts(event.at)))
            return seq

    def events_after(self, run_id: str, after: int, limit: int = 200) -> list[SafeEvent]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after, limit)).fetchall()
        return [SafeEvent(seq=r["seq"], run_id=r["run_id"], kind=r["kind"], origin=r["origin"],
                          handle=r["handle"], text_masked=r["text_masked"], outcome=r["outcome"],
                          screenshot_id=r["screenshot_id"], at=_dt(r["at"])) for r in rows]

    # -- records and files ----------------------------------------------------------------

    def put_record(self, record: RunRecord) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                "summary_masked=excluded.summary_masked, changes=excluded.changes, "
                "file_ids=excluded.file_ids, delivery_outcomes=excluded.delivery_outcomes, "
                "sites=excluded.sites, ended_at=excluded.ended_at",
                (record.run_id, record.requester_id, json.dumps(list(record.sites)),
                 record.summary_masked, json.dumps([c.__dict__ for c in record.changes]),
                 json.dumps(list(record.file_ids)),
                 json.dumps([list(d) for d in record.delivery_outcomes]),
                 _ts(record.ended_at)))

    def records_for(self, requester_id: str, since: datetime) -> list[RunRecord]:
        """A record's ``delivery_outcomes`` are read from the delivery attempts of the run's
        files (deliveries happen after the record is written), oldest attempt first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM records WHERE requester_id=? AND ended_at>=? ORDER BY ended_at",
                (requester_id, _ts(since))).fetchall()
            out = []
            for r in rows:
                attempts = tuple(self.deliveries_for_run(r["run_id"]))
                out.append(RunRecord(
                    run_id=r["run_id"], requester_id=r["requester_id"],
                    sites=tuple(json.loads(r["sites"])), summary_masked=r["summary_masked"],
                    changes=tuple(Change(**c) for c in json.loads(r["changes"])),
                    file_ids=tuple(json.loads(r["file_ids"])), delivery_outcomes=attempts,
                    ended_at=_dt(r["ended_at"])))
        return out

    def record_delivery(self, file_id: str, requester_id: str, delivery_id: str, outcome: str,
                        now: datetime | None = None) -> tuple[str, bool]:
        """Record one delivery attempt's outcome; returns ``(outcome, recorded)``.

        A new ``delivery_id`` starts an attempt, which becomes the file's current one. A repeat
        of the attempt's outcome changes nothing (``recorded`` false). ``deposited`` may move to
        ``delivered`` or ``withheld``; every other change of a recorded attempt is ``conflict``.
        A file that is unknown or another requester's is ``not_found``."""
        at = _ts(now or _now())
        with self.transaction() as db:
            f = db.execute("SELECT requester_id FROM files WHERE file_id=?", (file_id,)).fetchone()
            if f is None or f["requester_id"] != requester_id:
                raise ProtocolError("not_found")
            row = db.execute("SELECT outcome FROM deliveries WHERE file_id=? AND delivery_id=?",
                             (file_id, delivery_id)).fetchone()
            if row is not None:
                if row["outcome"] == outcome:
                    return outcome, False
                if (row["outcome"], outcome) not in DELIVERY_MOVES:
                    raise ProtocolError("conflict")
                db.execute("UPDATE deliveries SET outcome=?, updated_at=? "
                           "WHERE file_id=? AND delivery_id=?", (outcome, at, file_id, delivery_id))
                return outcome, True
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM deliveries").fetchone()["n"]
            db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)",
                       (file_id, delivery_id, requester_id, outcome, seq, at, at))
            return outcome, True

    def deliveries_for_run(self, run_id: str) -> list[tuple[str, str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT d.file_id, d.delivery_id, d.outcome FROM deliveries d "
                "JOIN files f ON f.file_id = d.file_id WHERE f.run_id=? ORDER BY d.seq",
                (run_id,)).fetchall()
        return [(r["file_id"], r["delivery_id"], r["outcome"]) for r in rows]

    def current_delivery(self, file_id: str) -> tuple[str, str] | None:
        """The file's current attempt ``(delivery_id, outcome)``: the latest one started."""
        with self._lock:
            r = self._db.execute("SELECT delivery_id, outcome FROM deliveries WHERE file_id=? "
                                 "ORDER BY seq DESC LIMIT 1", (file_id,)).fetchone()
        return None if r is None else (r["delivery_id"], r["outcome"])

    def put_file(self, record: FileRecord, now: datetime | None = None) -> None:
        created = record.created_at or now or _now()
        with self.transaction() as db:
            db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (record.file_id, record.run_id, record.requester_id, record.safe_name,
                        record.mime, record.size, record.sha256, record.private_path,
                        record.source_handle, _ts(created)))

    def get_file(self, file_id: str) -> FileRecord | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
        if r is None:
            return None
        return FileRecord(file_id=r["file_id"], run_id=r["run_id"], requester_id=r["requester_id"],
                          safe_name=r["safe_name"], mime=r["mime"], size=r["size"],
                          sha256=r["sha256"], private_path=r["private_path"],
                          source_handle=r["source_handle"], created_at=_dt(r["created_at"]))

    def files_for_run(self, run_id: str) -> list[FileRecord]:
        with self._lock:
            ids = [r["file_id"] for r in self._db.execute(
                "SELECT file_id FROM files WHERE run_id=? ORDER BY created_at, file_id",
                (run_id,)).fetchall()]
        return [self.get_file(i) for i in ids]

    def put_screenshot(self, screenshot_id: str, run_id: str, private_path: str,
                       now: datetime | None = None) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO screenshots VALUES (?,?,?,?)",
                       (screenshot_id, run_id, private_path, _ts(now or _now())))

    def screenshot_path(self, run_id: str, screenshot_id: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT private_path FROM screenshots WHERE screenshot_id=? AND run_id=?",
                                 (screenshot_id, run_id)).fetchone()
        return None if r is None else r["private_path"]

    # -- notices ---------------------------------------------------------------------------

    def add_notice(self, notice: SafeNotice) -> int:
        """Append a durable notice; a repeat of the same ``dedupe_key`` returns the first id."""
        with self.transaction() as db:
            if notice.dedupe_key is not None:
                row = db.execute("SELECT notice_id FROM notices WHERE dedupe_key=?",
                                 (notice.dedupe_key,)).fetchone()
                if row is not None:
                    return row["notice_id"]
            cur = db.execute(
                "INSERT INTO notices (kind, run_id, site_id, text_masked, at, dedupe_key) "
                "VALUES (?,?,?,?,?,?)",
                (notice.kind, notice.run_id, notice.site_id, notice.text_masked, _ts(notice.at),
                 notice.dedupe_key))
            return cur.lastrowid

    def notices_after(self, after: int, limit: int = 200) -> list[SafeNotice]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM notices WHERE notice_id>? ORDER BY notice_id LIMIT ?",
                                    (after, limit)).fetchall()
        return [SafeNotice(notice_id=r["notice_id"], kind=r["kind"], run_id=r["run_id"],
                           site_id=r["site_id"], text_masked=r["text_masked"], at=_dt(r["at"]),
                           dedupe_key=r["dedupe_key"]) for r in rows]

    # -- sources (Task 12) -------------------------------------------------------------------

    def add_source(self, run_id: str, url: str, title_masked: str, new_id: str,
                   limit: int) -> str | None:
        """The run's source id for ``url``: the existing one, else ``new_id`` when the run
        holds fewer than ``limit`` sources, else ``None``."""
        with self.transaction() as db:
            row = db.execute("SELECT source_id FROM sources WHERE run_id=? AND url=?",
                             (run_id, url)).fetchone()
            if row is not None:
                return row["source_id"]
            count = db.execute("SELECT COUNT(*) AS n FROM sources WHERE run_id=?",
                               (run_id,)).fetchone()["n"]
            if count >= limit:
                return None
            db.execute("INSERT INTO sources VALUES (?,?,?,?,?)",
                       (run_id, new_id, url, title_masked, count + 1))
            return new_id

    def sources_for(self, run_id: str) -> list[Source]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sources WHERE run_id=? ORDER BY seq",
                                    (run_id,)).fetchall()
        return [Source(r["source_id"], r["url"], r["title_masked"]) for r in rows]

    # -- owed releases (Task 13) ---------------------------------------------------------------

    def owe_release(self, run_id: str, now: datetime | None = None) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO owed_releases VALUES (?, ?)",
                       (run_id, _ts(now or _now())))

    def clear_owed_release(self, run_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM owed_releases WHERE run_id=?", (run_id,))

    def put_binding_key(self, site_id: str, key_id: str | None) -> None:
        """Record the proxy key a site's provider binding was learned under (``None`` clears)."""
        with self.transaction() as db:
            if key_id is None:
                db.execute("DELETE FROM binding_keys WHERE site_id=?", (site_id,))
            else:
                db.execute("INSERT INTO binding_keys VALUES (?, ?) ON CONFLICT(site_id) "
                           "DO UPDATE SET key_id=excluded.key_id", (site_id, key_id))

    def binding_keys(self) -> dict[str, str]:
        with self.transaction() as db:
            return {r["site_id"]: r["key_id"] for r in db.execute(
                "SELECT site_id, key_id FROM binding_keys").fetchall()}

    def owed_releases(self) -> list[str]:
        with self._lock:
            return [r["run_id"] for r in self._db.execute(
                "SELECT run_id FROM owed_releases ORDER BY since, run_id").fetchall()]

    # -- pairing ---------------------------------------------------------------------------

    def get_pairing(self) -> PairingState:
        with self._lock:
            r = self._db.execute("SELECT * FROM pairing WHERE id=1").fetchone()
        return PairingState(generation=r["generation"], secret_sha256=r["secret_sha256"],
                            caller_label=r["caller_label"], observed_peer=r["observed_peer"],
                            claimed_at=_dt(r["claimed_at"]), armed_until=_dt(r["armed_until"]))

    def put_pairing(self, state: PairingState) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE pairing SET generation=?, secret_sha256=?, caller_label=?, observed_peer=?, "
                "claimed_at=?, armed_until=? WHERE id=1",
                (state.generation, state.secret_sha256, state.caller_label, state.observed_peer,
                 None if state.claimed_at is None else _ts(state.claimed_at),
                 None if state.armed_until is None else _ts(state.armed_until)))

    # -- the action model: stored responses and questions (migration 8) ------------------------

    def put_response(self, run_id: str, key: str, response: dict) -> None:
        """Store (or replace) the complete response of one control request, keyed by its
        identity; a run keeps its newest ``RESPONSES_KEPT``."""
        text = json.dumps(response, sort_keys=True, ensure_ascii=False)
        with self.transaction() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM responses WHERE run_id=?",
                             (run_id,)).fetchone()["n"]
            db.execute("INSERT INTO responses VALUES (?,?,?,?) ON CONFLICT(run_id, key) DO "
                       "UPDATE SET response=excluded.response", (run_id, key, seq, text))
            db.execute("DELETE FROM responses WHERE run_id=? AND seq NOT IN (SELECT seq FROM "
                       "responses WHERE run_id=? ORDER BY seq DESC LIMIT ?)",
                       (run_id, run_id, RESPONSES_KEPT))

    def get_response(self, run_id: str, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT response FROM responses WHERE run_id=? AND key=?",
                                   (run_id, key)).fetchone()
        return None if row is None else json.loads(row["response"])

    @staticmethod
    def _question(row: sqlite3.Row) -> Question:
        return Question(row["question_id"], row["run_id"], row["revision"], row["digest"],
                        row["state"], json.loads(row["view"]), row["outcome"],
                        None if row["response"] is None else json.loads(row["response"]),
                        row["operator_ref"], _dt(row["created_at"]), _dt(row["decided_at"]))

    def create_question(self, question_id: str, run_id: str, revision: int, digest: str,
                        view: dict, now: datetime) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO confirmations (question_id, run_id, revision, digest, state, "
                       "view, created_at) VALUES (?,?,?,?,'open',?,?)",
                       (question_id, run_id, revision, digest,
                        json.dumps(view, sort_keys=True, ensure_ascii=False), _ts(now)))

    def get_question(self, question_id: str) -> Question | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM confirmations WHERE question_id=?",
                                   (question_id,)).fetchone()
        return None if row is None else self._question(row)

    def open_question(self, run_id: str) -> Question | None:
        """The run's open or confirming question, if any."""
        with self._lock:
            row = self._db.execute("SELECT * FROM confirmations WHERE run_id=? AND state IN "
                                   "('open','confirming')", (run_id,)).fetchone()
        return None if row is None else self._question(row)

    def move_question(self, question_id: str, expected: tuple[str, ...], state: str, *,
                      response: dict | None = None, outcome: str | None = None,
                      operator_ref: str | None = None, now: datetime | None = None) -> bool:
        """Compare-and-set on the question's state; the answer's stored response (when given)
        is written in the same transaction as the move."""
        if state not in QUESTION_STATES:
            raise ValueError("question state")
        marks = ",".join("?" for _ in expected)
        with self.transaction() as db:
            cur = db.execute(
                f"UPDATE confirmations SET state=?, outcome=COALESCE(?, outcome), "
                f"response=COALESCE(?, response), operator_ref=COALESCE(?, operator_ref), "
                f"decided_at=COALESCE(?, decided_at) WHERE question_id=? AND state IN ({marks})",
                (state, outcome, None if response is None else json.dumps(
                    response, sort_keys=True, ensure_ascii=False), operator_ref,
                 None if now is None else _ts(now), question_id, *expected))
            return cur.rowcount == 1

    def set_question_response(self, question_id: str, response: dict) -> None:
        with self.transaction() as db:
            db.execute("UPDATE confirmations SET response=? WHERE question_id=?",
                       (json.dumps(response, sort_keys=True, ensure_ascii=False), question_id))

    def end_questions(self, run_id: str | None = None, now: datetime | None = None, *,
                      confirming: str = "outcome_unknown") -> list[tuple[str, str, str]]:
        """A run's (every run's, at start) open questions expire; its confirming ones become
        ``confirming`` — ``outcome_unknown`` at a restart (the press may have happened), or
        ``expired`` when the run ends with the press known not to have started. Returns
        (run, question, new state)."""
        out = []
        with self.transaction() as db:
            where, args = ("", ()) if run_id is None else (" AND run_id=?", (run_id,))
            rows = db.execute("SELECT question_id, run_id, state FROM confirmations WHERE state IN "
                              f"('open','confirming'){where}", args).fetchall()
            for row in rows:
                new = "expired" if row["state"] == "open" else confirming
                db.execute("UPDATE confirmations SET state=?, decided_at=? WHERE question_id=?",
                           (new, _ts(now or _now()), row["question_id"]))
                out.append((row["run_id"], row["question_id"], new))
        return out

    # -- retention support --------------------------------------------------------------------

    def expired_run_ids(self, cutoff: datetime) -> list[str]:
        marks = ",".join("?" for _ in TERMINAL_STATES)
        with self._lock:
            rows = self._db.execute(
                f"SELECT run_id FROM runs WHERE state IN ({marks}) AND ended_at IS NOT NULL "
                f"AND ended_at<=? ORDER BY fifo", (*sorted(TERMINAL_STATES), _ts(cutoff))).fetchall()
        return [r["run_id"] for r in rows]

    def delete_runs_queue_files(self, run_ids: list[str], cutoff: datetime) -> int:
        """In one transaction: queue every private path of the runs for deletion, delete
        the runs (events, records, files and screenshots cascade) and notices older than
        the cutoff, and the cancelled request ids recorded before it. Returns the number of
        runs deleted."""
        with self.transaction() as db:
            for run_id in run_ids:
                for table in ("files", "screenshots"):
                    for r in db.execute(f"SELECT private_path FROM {table} WHERE run_id=?",
                                        (run_id,)).fetchall():
                        db.execute("INSERT OR IGNORE INTO pending_deletions VALUES (?)",
                                   (r["private_path"],))
                db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM notices WHERE at<=?", (_ts(cutoff),))
            db.execute("DELETE FROM cancelled_requests WHERE at<=?", (_ts(cutoff),))
            return len(run_ids)

    def pending_deletions(self) -> list[str]:
        with self._lock:
            return [r["path"] for r in self._db.execute(
                "SELECT path FROM pending_deletions ORDER BY path").fetchall()]

    def clear_pending_deletion(self, path: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM pending_deletions WHERE path=?", (path,))
