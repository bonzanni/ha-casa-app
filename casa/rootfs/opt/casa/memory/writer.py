"""The writer: one serialized worker that files events into the ledgers.

**Jobs.** Every change to a ledger is a job in ``jobs``, of one of four kinds:
``ordinary`` and ``correction`` file the events pending in a ledger,
``condense`` shortens over-long entries and ``regroup`` arranges entries into
groups and topics. Only the worker runs any of them, one at a time, a
``correction`` job first. :func:`enqueue` adds a job unless one of the same
kind and ledger is already queued.

**Ledgers (M6).** A job belongs to one maintained ledger *L*
(:mod:`memory.ledgers`). Its model call is given only what is readable at
*L* — the events pending in *L*, a window of earlier events readable at *L*,
and *L*'s own objects — all built by :func:`assemble_input`, the only
builder of a job's input; its patches change objects of *L* only
(:mod:`memory.patches`). A lower ledger's pending events are only those the
``private`` job has classified at or below it, and the ``private`` job runs
first (:meth:`Writer.next_job`), so a batch reaches each lower ledger after
it is classified.

**Cadence.** Each tick, a ledger with pending events this process's runs have
not yet been given (a ``seq`` above the highest any filing run's input held)
gets an ``ordinary`` job once those events have been quiet for
:data:`QUIET_S`, or once the oldest has waited :data:`MAX_WAIT_S`. ``seq`` is
assigned inside the store's single write transaction, so an event a run's
snapshot missed always counts as not yet given. The end of a session (an explicit reset) queues one
for every ledger with anything pending. Correction marking queues a
``correction`` job (:mod:`memory.corrections`); the nightly pass queues
``condense``, ``regroup`` and a retry of every failed job
(:mod:`memory.nightly`). Nothing here runs on a turn's path: the worker is a
background task, and every blocking SQLite call runs in a thread.

**A run** (:meth:`Writer.run_job`):

1. :func:`assemble_input` builds the job's input from the pending view as it
   is now, in one read transaction on one connection: the events, each
   event's correction mark, a short preceding window, the ledger's objects,
   the base revisions and ``mark_seen``. Every run assembles afresh (a first
   run, a requeue, a backoff retry, a nightly retry); nothing assembled for an
   earlier run is reused, and the job row's ``input_seqs``, ``base_revisions``
   and ``mark_seen`` record the current run only.
2. In the ``private`` job, each provisional event is classified
   (:mod:`memory.classify`).
3. The model is called (:mod:`memory.model_port`), outside any transaction.
   A ledger over :data:`WHOLE_LEDGER_MAX_TOKENS` is given as its index (every
   object without its body); a filing run's first answer may then name up to
   :data:`MAX_OPENED` objects to open, and a second call gives them in full,
   from the same snapshot, so nothing is read twice.
4. :func:`_commit` writes everything in one ``BEGIN IMMEDIATE`` transaction:
   the patches and their revisions, a disposition for every input event,
   the classification verdicts and the job's ``committed`` state, all under
   one ``commit_no``. It first checks that the ledger's revisions are the
   ones the run read and that no input event's correction mark changed since
   (a stale base or a new mark rolls back and requeues the job), and after
   the patches that every current object is reachable (otherwise the job
   fails). It also reads, through the pending view, which input events are
   still pending: if another commit filed some of them meanwhile (a second
   writer process), the answer is stale and the job is requeued with nothing
   written; if it filed all of them, the run writes nothing and ends
   committed. So an event is filed once however many writers run, and no
   patch cites an event another commit filed. Only this commit marks a job ``committed`` (INV-MEM-028), so an
   event stays pending until the commit that files it, and every write to
   ``objects``, ``revisions`` or ``dispositions`` is made here.

A run whose fresh input is empty commits with no disposition.

**Crash recovery.** A running job carries this process's ``boot_id``. At
start, every ``running`` job of an earlier process goes back to ``queued``;
its events are still pending, since only a commit files them. A job's state
changes after ``running`` only while it is still running under this
process's ``boot_id``.

**Failures.** A failed run is retried after a backoff; after
:data:`MAX_ATTEMPTS` the job is ``failed`` and its events stay pending, the
operator gets one private notice, and the nightly pass retries it once a
night.

**The store failing under the worker.** The worker owns recovery from it,
on every pass (:meth:`Writer.pass_once`):

* a store that could not be opened at boot is opened again, after
  :data:`REOPEN_FIRST_S` and then at doubling intervals up to
  :data:`REOPEN_MAX_S`;
* a job whose move out of ``running`` could not be written (the commit and
  the failure count both hit the failing store) is remembered, and once the
  store takes writes it is sent back to ``queued`` by boot recovery's own
  rule, only while it is still ``running`` under this process's id. Its next
  run assembles afresh, as any requeued run does; the outage counts as no
  attempt of the job's;
* a spool capture left is drained, so what was said during the outage is
  stored even when no later capture comes to start a drain.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from memory import capture, classify, meter, patches, pending, view
from memory.ledgers import maintained as _maintained
from memory.model_port import TIMEOUT_S, ModelPort, ModelReply
from memory.store import MemoryStore

logger = logging.getLogger(__name__)

ORDINARY = "ordinary"
CORRECTION = "correction"
CONDENSE = "condense"
REGROUP = "regroup"
KINDS = (ORDINARY, CORRECTION, CONDENSE, REGROUP)
_FILING = (ORDINARY, CORRECTION)

#: Seconds of quiet after the newest unseen event before a job runs.
QUIET_S = 600.0
#: Seconds the oldest unseen event may wait before a job runs regardless.
MAX_WAIT_S = 3600.0
#: Seconds between the worker's cadence checks.
TICK_S = 30.0
#: Failed runs before a job is ``failed``.
MAX_ATTEMPTS = 3
#: Seconds before the first retry of a failed run; doubled for the next.
BACKOFF_S = 60.0
#: Seconds the worker waits for the model's answer before the run fails.
#: The worker bounds every model await itself, whatever port it is given
#: (the classification's bound is :data:`memory.classify.TIMEOUT_S`); the
#: production port carries the same bound inside it.
MODEL_TIMEOUT_S = TIMEOUT_S
#: Seconds before the worker first retries opening an unavailable store;
#: doubled after each failure, up to :data:`REOPEN_MAX_S`.
REOPEN_FIRST_S = 30.0
REOPEN_MAX_S = 900.0
#: Earlier events of the same session shown as context.
WINDOW = 6
#: An entry over this many tokens is condensed at night.
CONDENSE_OVER_TOKENS = 1024
#: Ledger tokens up to which a run is given the whole ledger; past them it is
#: given the ledger's index and opens the entries it needs.
WHOLE_LEDGER_MAX_TOKENS = 40_000
#: The most objects one run may open from the index.
MAX_OPENED = 80

FAILURE_NOTICE = ("Memory couldn't file what was said at {time}; it's kept "
                  "and I'll try again tonight.")

#: This process's id, carried by every job it runs.
BOOT_ID = uuid.uuid4().hex

_PROMPT = Path(__file__).with_name("prompts") / "writer.md"

#: Named points of a run a test can pause or kill at; each is called with no
#: arguments. ``selected``: inside assembly's read transaction, after the
#: event selection; ``after_model``: the model replied, nothing committed;
#: ``in_commit``: inside the commit transaction, every row written, before
#: COMMIT.
_hooks: dict[str, Callable[[], None]] = {
    "selected": lambda: None,
    "after_model": lambda: None,
    "in_commit": lambda: None,
}

#: A job's ledger's rank on the clearance ladder, as SQL.
_LEDGER_RANK = ("(CASE ledger WHEN 'public' THEN 0 WHEN 'friends' THEN 1 "
                "WHEN 'family' THEN 2 ELSE 3 END)")

_SET_COMMITTED = ("UPDATE jobs SET state = 'committed', last_error = NULL "
                  "WHERE job_id = ? AND state = 'running' AND boot_id = ?")


class Stale(Exception):
    """The ledger or a correction mark changed since the run read them."""


class RunFailed(Exception):
    """The run cannot commit (an unusable reply, an unreachable ledger)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def tokens(text: str | None) -> int:
    """A cheap token estimate: four characters a token."""
    return len(text or "") // 4


# --- jobs --------------------------------------------------------------------

def _enqueue(conn, ledger: str, kind: str) -> str | None:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    if conn.execute(
            "SELECT 1 FROM jobs WHERE ledger = ? AND kind = ? "
            "AND state = 'queued'", (ledger, kind)).fetchone():
        return None
    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO jobs (job_id, ledger, kind, state) "
        "VALUES (?, ?, ?, 'queued')", (job_id, ledger, kind))
    return job_id


def enqueue(store: MemoryStore, ledger: str, kind: str) -> str | None:
    """Queue a *kind* job for *ledger*, unless one is already queued.
    Returns the new job's id, or ``None``. Blocking."""
    with store.transaction(write=True) as conn:
        return _enqueue(conn, ledger, kind)


def recover(store: MemoryStore, boot_id: str = BOOT_ID) -> int:
    """Send every ``running`` job of an earlier process back to ``queued``.
    Returns how many. Blocking."""
    with store.transaction(write=True) as conn:
        return conn.execute(
            "UPDATE jobs SET state = 'queued', boot_id = NULL "
            "WHERE state = 'running' AND (boot_id IS NULL OR boot_id != ?)",
            (boot_id,)).rowcount


def _job_kind(store: MemoryStore, job_id: str) -> tuple[str, str] | None:
    """A job's ledger and kind, or ``None`` when there is no such job."""
    with store.transaction() as conn:
        return conn.execute("SELECT ledger, kind FROM jobs WHERE job_id = ?",
                            (job_id,)).fetchone()


# --- input -------------------------------------------------------------------

@dataclass
class Input:
    job_id: str
    ledger: str
    kind: str
    events: list[dict] = field(default_factory=list)
    context: list[dict] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    #: Condense: the entries to condense.
    entries: list[dict] = field(default_factory=list)
    base_revisions: dict[str, int] = field(default_factory=dict)
    #: ``mark_no`` of every input event that carries a mark, by ``seq``.
    mark_seen: dict[int, int] = field(default_factory=dict)
    #: The ledger is past :data:`WHOLE_LEDGER_MAX_TOKENS`: the run is given
    #: its index, and opens what it needs from this same snapshot.
    index: bool = False

    @property
    def input_seqs(self) -> list[int]:
        return [e["seq"] for e in self.events]

    @property
    def empty(self) -> bool:
        if self.kind in _FILING:
            return not self.events
        if self.kind == CONDENSE:
            return not self.entries
        return not self.objects


def _rows(conn, sql: str, args=()) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur]


def _readable(event: dict, ledger: str) -> bool:
    return (pending.rank(event["tier"]) <= pending.rank(ledger)
            and (event["tier_status"] == "classified" or ledger == "private"))


def assemble_input(store: MemoryStore, job_id: str, ledger: str,
                   kind: str) -> Input:
    """The only builder of a job's input (blocking), read in ONE read
    transaction on one connection and closed before the model is called:
    the events pending in *ledger* now, their correction marks, a short
    preceding window readable at *ledger*, the ledger's objects and the base
    revisions. Nothing outside *ledger*'s clearance enters it (M6,
    INV-MEM-029)."""
    inp = Input(job_id=job_id, ledger=ledger, kind=kind)
    with store.transaction() as conn:
        if kind in _FILING:
            inp.events = _rows(
                conn, "SELECT e.* FROM pending AS p JOIN events AS e "
                "ON e.seq = p.seq WHERE p.ledger = ? ORDER BY e.seq",
                (ledger,))
        _hooks["selected"]()
        if inp.events:
            marks = {r["seq"]: r for r in _rows(
                conn, "SELECT m.seq, m.mark_no, m.marked_at "
                "FROM correction_marks AS m JOIN pending AS p "
                "ON p.seq = m.seq AND p.ledger = ?", (ledger,))}
            for event in inp.events:
                mark = marks.get(event["seq"])
                if mark is not None:
                    event["correction"] = {"marked_at": mark["marked_at"]}
                    inp.mark_seen[event["seq"]] = mark["mark_no"]
            inp.context = _window(conn, inp.events, ledger)
        objects = _rows(conn, "SELECT * FROM objects WHERE ledger = ? "
                              "ORDER BY rowid", (ledger,))
    inp.base_revisions = {o["object_id"]: o["revision"] for o in objects}
    inp.objects = [o for o in objects if o["status"] == "current"]
    if kind == CONDENSE:
        inp.entries = [o for o in inp.objects if o["kind"] == "entry"
                       and tokens(o["body"]) > CONDENSE_OVER_TOKENS]
    inp.index = sum(tokens(o["body"]) for o in inp.objects) \
        > WHOLE_LEDGER_MAX_TOKENS
    return inp


def _window(conn, events: list[dict], ledger: str) -> list[dict]:
    """Up to :data:`WINDOW` earlier events of each input event's session,
    readable at *ledger*, that are not input themselves."""
    first: dict[str, int] = {}
    for e in events:
        if e["session_id"] is not None:
            first.setdefault(e["session_id"], e["seq"])
    inputs = {e["seq"] for e in events}
    out: list[dict] = []
    for session_id, seq in first.items():
        earlier = _rows(
            conn, "SELECT * FROM events WHERE session_id = ? AND seq < ? "
            "ORDER BY seq DESC LIMIT ?", (session_id, seq, WINDOW * 4))
        kept = [e for e in earlier
                if e["seq"] not in inputs and _readable(e, ledger)][:WINDOW]
        out.extend(reversed(kept))
    return sorted(out, key=lambda e: e["seq"])


# --- the prompt and the reply -------------------------------------------------

def _local(event: dict) -> str:
    try:
        at = datetime.fromisoformat(event["occurred_at"])
        return at.astimezone(ZoneInfo(event["tz"])).isoformat(
            timespec="minutes")
    except (ValueError, KeyError, TypeError):
        return event.get("occurred_at") or ""


def _event_view(event: dict) -> dict:
    view = {"seq": event["seq"], "time": _local(event),
            "speaker": event["speaker_kind"],
            "speaker_name": event["speaker_name"], "role": event["role"],
            "channel": event["channel"], "origin": event["origin_kind"],
            "scheduled": bool(event["scheduled"]), "text": event["text"]}
    if event.get("reply_to"):
        view["reply_to"] = event["reply_to"]
    if "correction" in event:
        view["correction"] = event["correction"]
    return view


_OBJECT_FIELDS = ("object_id", "kind", "entry_kind", "errand_state", "topic",
                  "subject", "body", "line", "parent")


def _object_view(obj: dict) -> dict:
    view = {f: obj[f] for f in _OBJECT_FIELDS if obj.get(f) is not None}
    view["cues"] = json.loads(obj["cues"])
    return view


#: What an index gives of each object: everything but the body.
_INDEX_FIELDS = tuple(f for f in _OBJECT_FIELDS if f != "body")


def _index_view(obj: dict) -> dict:
    view = {f: obj[f] for f in _INDEX_FIELDS if obj.get(f) is not None}
    view["cues"] = json.loads(obj["cues"])
    return view


def render_prompt(inp: Input, opened: list[dict] | None = None) -> str:
    """The user message of a job's model call. In index mode the ledger is
    its index, plus the objects the run opened (and, for a condense, the
    entries to condense)."""
    if inp.index:
        body: dict = {"job": inp.kind, "ledger_mode": "index",
                      "ledger": [_index_view(o) for o in inp.objects]}
        shown = list(opened or [])
        if inp.kind == CONDENSE:
            shown += [o for o in inp.entries if o not in shown]
        if opened is not None or shown:
            body["opened"] = [_object_view(o) for o in shown]
    else:
        body = {"job": inp.kind,
                "ledger": [_object_view(o) for o in inp.objects]}
    if inp.kind in _FILING:
        body["events"] = [_event_view(e) for e in inp.events]
        body["context"] = [_event_view(e) for e in inp.context]
    elif inp.kind == CONDENSE:
        body["entries"] = [o["object_id"] for o in inp.entries]
    return json.dumps(body, ensure_ascii=False, indent=1)


def opened_objects(reply: dict, inp: Input) -> list[dict] | None:
    """The objects an index-mode reply asks to open, at most
    :data:`MAX_OPENED`, from the run's own snapshot; ``None`` when the reply
    is not an open request (it is the run's answer)."""
    ids = reply.get("open")
    if not isinstance(ids, list) or "ops" in reply:
        return None
    by_id = {o["object_id"]: o for o in inp.objects}
    out: list[dict] = []
    for object_id in ids:
        obj = by_id.get(object_id) if isinstance(object_id, str) else None
        if obj is not None and obj not in out:
            out.append(obj)
        if len(out) == MAX_OPENED:
            break
    return out


def system_prompt() -> str:
    return _PROMPT.read_text(encoding="utf-8")


@dataclass
class Plan:
    """What a reply asks the commit to write."""

    ops: list = field(default_factory=list)
    outcomes: dict[int, str] = field(default_factory=dict)
    verdicts: dict[int, str] = field(default_factory=dict)


def _json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RunFailed("the reply holds no JSON object")
    try:
        value = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise RunFailed(f"the reply is not valid JSON ({exc})") from None
    if not isinstance(value, dict):
        raise RunFailed("the reply is not a JSON object")
    return value


def _combined(first: ModelReply, second: ModelReply) -> ModelReply:
    """The second reply, carrying both calls' usage and cost."""
    usage = None
    if first.usage is not None or second.usage is not None:
        usage = {}
        for key in ("input_tokens", "output_tokens"):
            values = [u.get(key) for u in (first.usage, second.usage)
                      if u and isinstance(u.get(key), int)]
            usage[key] = sum(values) if values else None
    costs = [c for c in (first.cost_usd, second.cost_usd) if c is not None]
    return ModelReply(text=second.text, usage=usage,
                      cost_usd=sum(costs) if costs else None)


def parse_reply(text: str, inp: Input) -> Plan:
    """The reply as a :class:`Plan`. Every input event gets an outcome: the
    one the reply names, else ``patched`` when an operation cites it, else
    ``deferred`` (still pending). An outcome for an event the run was not
    given is ignored: a disposition is written only for the run's input."""
    reply = _json_object(text)
    ops = reply.get("ops", [])
    if not isinstance(ops, list):
        raise RunFailed("ops is not a list")
    named = reply.get("events", {})
    if not isinstance(named, dict):
        raise RunFailed("events is not an object")
    cited: set = set()
    for op in ops:
        if isinstance(op, dict):
            specs = [op, *(op.get("into") if isinstance(op.get("into"), list)
                           else [op.get("into")])]
            for spec in specs:
                if isinstance(spec, dict) and isinstance(
                        spec.get("source_seqs"), list):
                    cited.update(spec["source_seqs"])
    plan = Plan(ops=ops)
    for seq in inp.input_seqs:
        outcome = named.get(str(seq), named.get(seq))
        if outcome is None:
            outcome = "patched" if seq in cited else "deferred"
        if outcome not in ("patched", "no_change", "deferred"):
            raise RunFailed(f"unknown outcome {outcome!r} for event {seq}")
        plan.outcomes[seq] = outcome
    extra = [k for k in named if str(k) not in {str(s) for s in inp.input_seqs}]
    if extra:
        logger.warning("memory writer: job %s named %d event(s) it was not "
                       "given; ignored", inp.job_id, len(extra))
    return plan


# --- the commit --------------------------------------------------------------

def _claim(store: MemoryStore, inp: Input, boot_id: str) -> bool:
    """Mark the job running under *boot_id* with this run's input."""
    with store.transaction(write=True) as conn:
        return conn.execute(
            "UPDATE jobs SET state = 'running', boot_id = ?, input_seqs = ?, "
            "base_revisions = ?, mark_seen = ? "
            "WHERE job_id = ? AND state = 'queued'",
            (boot_id, json.dumps(inp.input_seqs),
             json.dumps(inp.base_revisions, sort_keys=True),
             json.dumps({str(k): v for k, v in inp.mark_seen.items()},
                        sort_keys=True),
             inp.job_id)).rowcount == 1


def _commit(store: MemoryStore, inp: Input, plan: Plan, boot_id: str) -> int:
    """The one transaction that files a run. Returns its ``commit_no``.

    Raises :class:`Stale` (nothing written) when the ledger's revisions or an
    input event's correction mark differ from what the run read, and
    :class:`RunFailed` or :class:`patches.PatchError` (nothing written) when
    the patches are malformed or leave an object unreachable."""
    with store.transaction(write=True) as conn:
        now = {r[0]: r[1] for r in conn.execute(
            "SELECT object_id, revision FROM objects WHERE ledger = ?",
            (inp.ledger,))}
        if now != inp.base_revisions:
            raise Stale("the ledger changed since the run read it")
        for seq in inp.input_seqs:
            row = conn.execute("SELECT mark_no FROM correction_marks "
                               "WHERE seq = ?", (seq,)).fetchone()
            if (row[0] if row else None) != inp.mark_seen.get(seq):
                raise Stale(f"event {seq} was marked since the run read it")
        # The run's input must still be exactly what is owed. An event
        # another commit filed since the read (a second writer) makes the
        # whole answer stale: it may cite that event, so nothing of it is
        # written and the job reassembles from what survives. Each requeue
        # follows another commit's terminal filing, so it cannot repeat
        # without that progress. A run whose every event is gone has nothing
        # left to file: it writes nothing and ends committed.
        still = [seq for seq in inp.input_seqs
                 if pending.is_pending(conn, seq, inp.ledger)]
        if still and still != inp.input_seqs:
            raise Stale(f"{len(inp.input_seqs) - len(still)} input event(s) "
                        "were filed since the run read them")
        ops = plan.ops if still or not inp.input_seqs else []
        commit_no = pending.next_commit_no(conn)
        committed_at = _now_iso()
        patches.apply(conn, inp.ledger, ops, job_id=inp.job_id,
                      commit_no=commit_no, committed_at=committed_at)
        lost = view.unreachable(conn, inp.ledger)
        if lost:
            raise RunFailed(f"{len(lost)} object(s) left unreachable")
        for seq in still:
            if seq in plan.verdicts:
                conn.execute(
                    "UPDATE events SET tier = ?, tier_status = 'classified' "
                    "WHERE seq = ? AND tier_status = 'provisional'",
                    (plan.verdicts[seq], seq))
        conn.executemany(
            "INSERT INTO dispositions (seq, ledger, job_id, outcome, "
            "commit_no, committed_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(seq, inp.ledger, inp.job_id, plan.outcomes[seq], commit_no,
              committed_at) for seq in still])
        _hooks["in_commit"]()
        if conn.execute(_SET_COMMITTED, (inp.job_id, boot_id)).rowcount != 1:
            raise Stale("the job is no longer running in this process")
        return commit_no


def _requeue(store: MemoryStore, job_id: str, boot_id: str,
             error: str) -> bool:
    """Send a running job of this process back to ``queued``, its attempts
    unchanged (a stale base or a new mark is not a failure)."""
    with store.transaction(write=True) as conn:
        return conn.execute(
            "UPDATE jobs SET state = 'queued', last_error = ? "
            "WHERE job_id = ? AND state = 'running' AND boot_id = ?",
            (error, job_id, boot_id)).rowcount == 1


def _fail(store: MemoryStore, job_id: str, boot_id: str,
          error: str) -> tuple[int, str] | None:
    """Count a failed run of a running job of this process: back to
    ``queued``, or ``failed`` once it reaches :data:`MAX_ATTEMPTS`. Returns
    its attempts and state, or ``None`` when it was not running here."""
    with store.transaction(write=True) as conn:
        row = conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, last_error = ?, "
            "state = CASE WHEN attempts + 1 >= ? THEN 'failed' "
            "ELSE 'queued' END "
            "WHERE job_id = ? AND state = 'running' AND boot_id = ? "
            "RETURNING attempts, state",
            (error, MAX_ATTEMPTS, job_id, boot_id)).fetchone()
        return None if row is None else (row[0], row[1])


# --- the worker --------------------------------------------------------------

COMMITTED = "committed"
REQUEUED = "requeued"
RETRY = "retry"
FAILED = "failed"
SKIPPED = "skipped"


class Writer:
    """The one worker. :meth:`run_forever` is its task in the Casa process;
    :meth:`run_job` runs one job and is what tests drive."""

    def __init__(self, store: MemoryStore, port: ModelPort, *,
                 classifier: classify.Classifier | None = None,
                 notify: Callable[[str], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.time,
                 boot_id: str = BOOT_ID,
                 ledgers: tuple[str, ...] | None = None) -> None:
        self.store = store
        self.port = port
        self.classifier = classifier or classify.default_classifier
        self.notify = notify
        self.clock = clock
        self.boot_id = boot_id
        #: The ledgers this worker files; ``None``: the maintained ledgers
        #: (:mod:`memory.ledgers`), read at each pass.
        self._ledgers = ledgers
        #: Per ledger, the highest ``seq`` a filing run of this process was
        #: given: pending events above it are ones the writer has not been
        #: shown.
        self._shown_seq: dict[str, int] = {}
        #: Jobs of this process whose transition out of ``running`` did not
        #: land (the store failed under it); each pass requeues them.
        self._abandoned: set[str] = set()
        self._recovered = False
        #: When the next attempt to open an unavailable store is due, and the
        #: wait after it.
        self._reopen_at = 0.0
        self._reopen_wait = REOPEN_FIRST_S
        #: Per job, the earliest time its next retry may run.
        self._not_before: dict[str, float] = {}
        self._session_end = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None

    @property
    def ledgers(self) -> tuple[str, ...]:
        return self._ledgers if self._ledgers is not None else _maintained()

    # -- signals (thread-safe) --

    def wake(self) -> None:
        loop, event = self._loop, self._wakeup
        if loop is None or event is None or loop.is_closed():
            return
        try:
            if asyncio.get_running_loop() is loop:
                event.set()
                return
        except RuntimeError:
            pass
        loop.call_soon_threadsafe(event.set)

    def session_ended(self) -> None:
        self._session_end = True
        self.wake()

    # -- scheduling (blocking) --

    def schedule_due(self, *, session_end: bool = False) -> list[str]:
        """Queue the jobs the cadence makes due now (with *session_end*, one
        for every ledger with anything pending). Returns their ids."""
        queued = []
        now = self.clock()
        for ledger in self.ledgers:
            with self.store.transaction() as conn:
                arrivals = pending.pending_arrivals(conn, ledger)
            shown = self._shown_seq.get(ledger, 0)
            unseen = [datetime.fromisoformat(t).timestamp()
                      for seq, t in arrivals if seq > shown]
            due = session_end and bool(arrivals)
            if unseen and not due:
                due = (now - max(unseen) >= QUIET_S
                       or now - min(unseen) >= MAX_WAIT_S)
            if due:
                job = enqueue(self.store, ledger, ORDINARY)
                queued += [job] if job else []
        return queued

    def next_job(self) -> dict | None:
        """The next queued job whose backoff has passed: corrections first,
        then by ledger, ``private`` first and each lower ledger after it,
        then in queue order. A lower ledger is given only events the
        ``private`` job has classified, so filing ``private`` first lets a
        batch reach every ledger it is readable in at the next jobs."""
        now = self.clock()
        with self.store.transaction() as conn:
            rows = _rows(conn, "SELECT job_id, ledger, kind FROM jobs "
                               "WHERE state = 'queued' ORDER BY "
                               "(kind = 'correction') DESC, "
                               f"{_LEDGER_RANK} DESC, rowid")
        for row in rows:
            if self._not_before.get(row["job_id"], 0.0) <= now:
                return row
        return None

    # -- one run --

    async def run_job(self, job_id: str) -> str:
        """Run one queued job to its outcome: ``committed``, ``requeued``
        (stale), ``retry`` (failed, backing off), ``failed`` or ``skipped``
        (it was not queued)."""
        row = await asyncio.to_thread(_job_kind, self.store, job_id)
        if row is None:
            return SKIPPED
        ledger, kind = row
        inp = await asyncio.to_thread(assemble_input, self.store, job_id,
                                      ledger, kind)
        if not await asyncio.to_thread(_claim, self.store, inp, self.boot_id):
            return SKIPPED
        if inp.events:
            # Only what this run was given counts as shown. ``seq`` is
            # assigned inside the store's single write transaction, so an
            # event the run's snapshot missed has a higher one.
            self._shown_seq[ledger] = max(self._shown_seq.get(ledger, 0),
                                          max(inp.input_seqs))
        landed = False
        try:
            outcome = await self._run_claimed(inp)
            landed = True
            return outcome
        finally:
            if not landed:
                self._abandoned.add(job_id)

    async def _run_claimed(self, inp: Input) -> str:
        """Everything after the claim. Returns once the job has left
        ``running`` (or was found not to be running here); raises when the
        store failed before that transition landed."""
        job_id, ledger, kind = inp.job_id, inp.ledger, inp.kind
        reply: ModelReply | None = None
        try:
            plan = Plan()
            if not inp.empty:
                if ledger == "private":
                    plan.verdicts = await classify.classify_events(
                        inp.events, self.classifier)
                system = await asyncio.to_thread(system_prompt)
                reply = await asyncio.wait_for(
                    self.port.complete(system=system,
                                       prompt=render_prompt(inp)),
                    MODEL_TIMEOUT_S)
                if inp.index and kind in _FILING:
                    # Index mode: the first answer may ask to open entries;
                    # the second call gives them, from the same snapshot.
                    opened = opened_objects(_json_object(reply.text), inp)
                    if opened is not None:
                        first = reply
                        reply = await asyncio.wait_for(
                            self.port.complete(
                                system=system,
                                prompt=render_prompt(inp, opened)),
                            MODEL_TIMEOUT_S)
                        reply = _combined(first, reply)
                _hooks["after_model"]()
                parsed = parse_reply(reply.text, inp)
                plan.ops, plan.outcomes = parsed.ops, parsed.outcomes
            commit_no = await asyncio.to_thread(
                _commit, self.store, inp, plan, self.boot_id)
        except Stale as exc:
            await asyncio.to_thread(_requeue, self.store, job_id, self.boot_id,
                                    str(exc))
            logger.info("memory writer: job %s (%s, %s) requeued: %s",
                        job_id, kind, ledger, exc)
            self.wake()
            return REQUEUED
        except Exception as exc:  # noqa: BLE001 — any other failure fails it
            return await self._failed(inp, exc, reply)
        self._not_before.pop(job_id, None)
        self._cost_line(inp, "committed", reply, commit_no=commit_no)
        await self._persist_meter()
        return COMMITTED

    async def _failed(self, inp: Input, exc: Exception,
                      reply: ModelReply | None) -> str:
        error = f"{type(exc).__name__}: {exc}"
        outcome = await asyncio.to_thread(_fail, self.store, inp.job_id,
                                          self.boot_id, error)
        if outcome is None:
            return SKIPPED
        attempts, state = outcome
        self._cost_line(inp, "failed", reply, error=error)
        if state == "queued":
            self._not_before[inp.job_id] = (
                self.clock() + BACKOFF_S * 2 ** (attempts - 1))
            logger.warning("memory writer: job %s (%s, %s) attempt %d "
                           "failed: %s", inp.job_id, inp.kind, inp.ledger,
                           attempts, error)
            return RETRY
        self._not_before.pop(inp.job_id, None)
        logger.error("memory writer: job %s (%s, %s) failed after %d "
                     "attempts: %s", inp.job_id, inp.kind, inp.ledger,
                     attempts, error)
        if attempts == MAX_ATTEMPTS and inp.events:
            await self._notice(inp)
        await self._persist_meter()
        return FAILED

    async def _notice(self, inp: Input) -> None:
        if self.notify is None:
            return
        text = FAILURE_NOTICE.format(time=_local(inp.events[0])[11:16])
        try:
            await asyncio.wait_for(self.notify(text), 60.0)
        except Exception:  # noqa: BLE001 — the meter still shows the failure
            logger.warning("memory writer: failure notice not sent",
                           exc_info=True)

    def _cost_line(self, inp: Input, outcome: str, reply: ModelReply | None,
                   **extra) -> None:
        usage = reply.usage if reply is not None else None
        logger.info("memory writer cost %s", json.dumps({
            "job_id": inp.job_id, "ledger": inp.ledger, "kind": inp.kind,
            "outcome": outcome, "events": len(inp.events),
            "input_tokens": (usage or {}).get("input_tokens"),
            "output_tokens": (usage or {}).get("output_tokens"),
            "cost_usd": reply.cost_usd if reply is not None else None,
            **extra}, sort_keys=True))

    async def _persist_meter(self) -> None:
        try:
            await asyncio.to_thread(meter.persist, self.store)
        except Exception:  # noqa: BLE001 — the meter never fails a job
            logger.warning("memory meter not persisted", exc_info=True)

    # -- recovery from an unavailable or failing store --

    async def ensure_store(self) -> bool:
        """Whether the store is available, opening it again when it is not
        and a retry is due (:data:`REOPEN_FIRST_S`, doubling to
        :data:`REOPEN_MAX_S`)."""
        if self.store.available:
            return True
        now = self.clock()
        if now < self._reopen_at:
            return False
        if await asyncio.to_thread(self.store.open):
            self._reopen_wait = REOPEN_FIRST_S
            logger.info("memory writer: the store is available again")
            return True
        self._reopen_at = now + self._reopen_wait
        self._reopen_wait = min(self._reopen_wait * 2, REOPEN_MAX_S)
        return False

    def requeue_abandoned(self) -> int:
        """Send back to ``queued`` every job this process abandoned while it
        was ``running`` (blocking). The rule is boot recovery's, applied to
        this process's own jobs that it knows it lost: only a job still
        ``running`` under this process's id moves, so one whose commit did
        land is left alone. Returns how many moved."""
        ids = sorted(self._abandoned)
        if not ids:
            return 0
        moved = 0
        with self.store.transaction(write=True) as conn:
            for job_id in ids:
                moved += conn.execute(
                    "UPDATE jobs SET state = 'queued', boot_id = NULL "
                    "WHERE job_id = ? AND state = 'running' AND boot_id = ?",
                    (job_id, self.boot_id)).rowcount
        self._abandoned.difference_update(ids)
        if moved:
            logger.info("memory writer: %d job(s) cut off by a store failure "
                        "requeued", moved)
        return moved

    async def pass_once(self) -> bool:
        """One pass of the worker: reopen an unavailable store when due;
        then, with the store available, requeue running jobs of earlier
        processes (once) and the jobs this process abandoned, drain a spool
        capture left, queue what the cadence makes due and run the next job.
        Returns whether a job ran."""
        if not await self.ensure_store():
            return False
        if not self._recovered:
            n = await asyncio.to_thread(recover, self.store, self.boot_id)
            self._recovered = True
            if n:
                logger.info("memory writer: %d running job(s) of an earlier "
                            "process requeued", n)
        await asyncio.to_thread(self.requeue_abandoned)
        try:
            await asyncio.to_thread(capture.drain_waiting, self.store)
        except Exception:  # noqa: BLE001 — the spool stays for a later pass
            logger.warning("memory writer: spool drain failed; kept for a "
                           "later pass", exc_info=True)
        return await self.step()

    # -- the loop --

    async def step(self) -> bool:
        """Queue what is due, then run the next job. Returns whether a job
        ran."""
        session_end, self._session_end = self._session_end, False
        try:
            await asyncio.to_thread(self.schedule_due,
                                    session_end=session_end)
        except BaseException:
            self._session_end = self._session_end or session_end
            raise
        job = await asyncio.to_thread(self.next_job)
        if job is None:
            return False
        await self.run_job(job["job_id"])
        return True

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        while True:
            ran = False
            try:
                ran = await self.pass_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the worker never dies
                logger.warning("memory writer pass failed", exc_info=True)
            if ran:
                continue
            try:
                await asyncio.wait_for(self._wakeup.wait(), TICK_S)
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()


# --- the process's one writer -------------------------------------------------

_wiring: dict[str, object] = {"writer": None, "task": None}


def configure(store: MemoryStore, port: ModelPort, *,
              classifier: classify.Classifier | None = None,
              notify: Callable[[str], Awaitable[None]] | None = None) -> Writer:
    """Build the process's writer (not started)."""
    w = Writer(store, port, classifier=classifier, notify=notify)
    _wiring["writer"] = w
    return w


def active() -> Writer | None:
    w = _wiring["writer"]
    return w if isinstance(w, Writer) else None


def start() -> asyncio.Task | None:
    """Start the configured writer's task on the running loop."""
    w = active()
    if w is None:
        return None
    task = asyncio.get_running_loop().create_task(w.run_forever(),
                                                  name="memory-writer")
    _wiring["task"] = task
    return task


def wake() -> None:
    """Wake the writer (from any thread); a no-op when none runs."""
    w = active()
    if w is not None:
        w.wake()


def session_ended() -> None:
    """A session ended: file what is pending at the next pass."""
    w = active()
    if w is not None:
        w.session_ended()
