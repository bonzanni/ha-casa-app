"""in_casa driver — embedded claude_agent_sdk engagement runtime.

Every engagement is a background job, and a job is ONE long session. Casa
writes its brief into the session once and everything later (an operator's
message, a tapped answer, an approval's continuation, Casa's own nudge, the
restart line) as it arrives, with ``client.query()``. Input written while a
turn runs is absorbed by the CLI at the next tool boundary, or runs as the
next turn when no boundary is left (measured). Casa therefore never
serialises turns. Per live job session:

- **One reader** consumes ``client.receive_messages()`` for the session's
  whole life: it puts each turn's words and steps in the agent's log,
  detects API faults, discharges the tickets the CLI echoes back as taken up, and calls
  the injected turn-end handler at every ``ResultMessage`` and the
  session-lost handler when the stream ends without Casa's teardown.
- **One delivery worker** per job (it outlives a session replacement) is
  the ONLY writer: it takes items from the job's queue in order, resumes the
  session when none is registered (under the per-engagement lifecycle lock,
  the one resume owner), admits each write synchronously immediately before
  ``client.query`` and stamps it with its ticket's uuid.
- **Tickets** track operator input: *admitted* at the seam, *written* once
  the client took it, *taken up* when the CLI's replay echo carrying its uuid
  arrives (``--replay-user-messages``: emitted once, only when the CLI takes
  the message into a turn, never at write time; measured). No timeout or
  frame-order rule ever discharges a ticket.
- **The journal** (``job_inbox``) keeps every operator message until the
  session transcript holds it, so a Casa restart or a CLI death redelivers
  what the session never read and nothing it did.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
import uuid as uuid_mod
from dataclasses import dataclass, field, is_dataclass, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import background_jobs
from drivers.driver_protocol import DriverProtocol
from drivers.turn_progress import step_log_line
from error_kinds import ErrorKind, api_error_kind, result_api_error_kind
from engagement_registry import LAUNCH_CWD_KEY, EngagementRecord
import job_inbox
import sdk_logging
from log_cid import cid_var, new_cid
from output_boundary import ApprovalCut

if TYPE_CHECKING:
    from agent_logs import AgentLogs

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"completed", "cancelled", "error"})


ResultObserver = Callable[[EngagementRecord, ResultMessage], None]
"""(engagement, ResultMessage) → None — Task 6 (spec §4.6) per-turn cost/usage
observer. Called synchronously for every ``ResultMessage`` seen on an
engagement turn; casa_core wires it to feed interactive specialist cost into
``SpecialistTelemetry`` (filtering by ``engagement.kind``). Must not raise."""

TurnEndHandler = Callable[[EngagementRecord, ResultMessage, "ErrorKind | None"], None]
"""(engagement, ResultMessage, api_error) → None — what Casa does when a job's
turn ends (the turn-end guard: continue, nudge or park). ``api_error`` is the
API fault the turn ended on, or ``None``. Called SYNCHRONOUSLY by the
session's reader at every ResultMessage, after the turn's words are
logged, ``turn_open`` cleared, activity stamped and the job's cost
updated. Must not raise; may enqueue Casa's own writes
(:meth:`InCasaDriver.submit_casa`)."""

SessionLostHandler = Callable[[EngagementRecord], Awaitable[None]]
"""(engagement) → None — what Casa does when a job's session ended without
Casa's own teardown (the CLI exited or crashed), after the driver marked the
job ``recovering``, removed the dead session and started retiring it. Run in
a Casa-owned task. The handler resumes once (:meth:`InCasaDriver.recover`) or
ends the job; either way the ``recovering`` flag is clear when it returns."""

SessionIdPersister = Callable[[str, str], Awaitable[None]]
"""(engagement_id, session_id) → None — registry persist hook.

Matches engagement_registry.persist_session_id's bound-method signature."""


class _EngagementTurn:
    """The engagement's running turn, as the SDK read task sees it. One per
    client: created when a session opens, bound into ``log_cid.cid_var``
    before the client's ``__aenter__`` (the read task snapshots context
    there, so a per-turn contextvar never reaches it), and MUTATED IN PLACE
    by the session's reader at each turn — replacing it would leave the read
    task rendering the first turn's cid.

    ``value`` is the running (or last) turn's cid and renders as the record's
    cid; it is not cleared between turns. ``approvals`` is the authorization
    hook's per-call record for the running turn (#1252/#1207: tool_use_id →
    ("pending" | "consumed", GrantKey)), a fresh dict from the turn's start
    to its end and None between turns."""

    __slots__ = ("value", "approvals")

    def __init__(self) -> None:
        self.value = "-"
        self.approvals: dict[str, tuple[str, Any]] | None = None

    def note_approval(self, tool_use_id: Any, kind: str, key: Any) -> None:
        """#1207: SYNCHRONOUS — record what the authorization hook decided for
        one call ("pending": its deny left a keyboard up; "consumed": it spent
        the operator's grant). The hook reaches THIS holder through its own
        context — the SDK reader of the client the holder was bound to before
        ``__aenter__`` — so the record lands on that client's running turn and
        no other; between turns it is dropped."""
        if tool_use_id and self.approvals is not None:
            self.approvals[str(tool_use_id)] = (kind, key)

    def __str__(self) -> str:
        return self.value


class _EngagementCapture:
    """What one engagement turn of a memory-writing specialist captures into
    memory: the operator's message to the job, keyed by its Telegram id,
    and each text-bearing message the specialist writes. Executors and plugin
    jobs are not memory-writing specialists and capture nothing. Every
    event's floor is the engagement's read clearance. A capture failure is
    logged and never fails the turn.

    The turn is a run (``memory/transcript_runs.py``): :meth:`register` puts
    its row in place before the operator's capture, naming the session the
    turn resumes; ``_observe_session_id`` names every later one first; each
    capture is one of the run's obligations, and the turn's outermost
    ``finally`` confirms it (:attr:`run`)."""

    CHANNEL = "telegram"

    def __init__(self, engagement: EngagementRecord) -> None:
        from memory import transcript_runs
        from sensitivity import clearance_for_origin
        origin = getattr(engagement, "origin", None) or {}
        self._engagement = engagement
        self._floor = clearance_for_origin(
            origin.get("_origin_route"), origin.get("_origin_clearance"),
            str(origin.get("channel", "")))
        self.run = transcript_runs.unregistered(
            transcript_runs.ENGAGEMENT, role=engagement.role_or_type)
        self._owed = 0
        self._closing = False
        self._cut = False

    @classmethod
    def for_turn(cls, engagement: EngagementRecord) -> "_EngagementCapture | None":
        from channel_policy import writes_to_bank
        from delegated_memory import role_writes_memory
        if (getattr(engagement, "kind", "") != "specialist"
                or not writes_to_bank(cls.CHANNEL)
                or not role_writes_memory(engagement.role_or_type)):
            return None
        return cls(engagement)

    async def register(self, session_id: str | None) -> None:
        """The turn's row, before its first capture, naming the session the
        turn continues (``None`` for a fresh one, named when it reports)."""
        from engagement_transcript_reaper import transcript_cwd
        from memory import transcript_runs
        try:
            directory = transcript_cwd(self._engagement)
        except Exception:  # noqa: BLE001 — the row then names no directory
            directory = None
        self.run = await transcript_runs.Run.start(
            transcript_runs.ENGAGEMENT, role=self._engagement.role_or_type,
            directory=directory, session_id=session_id)
        # A close or a cut that came while the row was being registered was
        # applied to the placeholder; apply it to the registered run.
        self._settle()

    async def _record(self, source, text: str, **fields) -> None:
        await self.run.kept(self._kept(source, text, **fields))

    async def _kept(self, source, text: str, **fields) -> bool:
        from memory import capture, eligibility
        try:
            rule = eligibility.admit(source, text=text, channel_writes=True)
            if rule is None:
                return True
            item = capture.event(
                role=self._engagement.role_or_type, channel=self.CHANNEL,
                origin_kind="operator", text=text, eligibility=rule,
                origin_tier=self._floor, **fields)
        except Exception:  # noqa: BLE001 — capture never fails a turn
            logger.exception("memory capture in engagement %s failed",
                             self._engagement.id[:8])
            return False
        return await capture.record(item) is not None

    async def operator(self, text: str, tg_message_id: int) -> None:
        from memory import eligibility, occurrence
        eid = self._engagement.id
        await self._record(
            eligibility.Source.ENGAGEMENT_TURN, text,
            source_key=occurrence.engagement_key(
                self.CHANNEL, eid, str(tg_message_id)),
            provider_message_id=str(tg_message_id),
            # Only the operator reads at ``private``; a lower floor means a
            # lower-clearance sender has steered this engagement.
            speaker_kind=("operator" if self._floor == "private"
                          else "third_party"),
            session_id=self._engagement.sdk_session_id)

    def owe(self) -> None:
        """Reserve one capture before anything it waits on: a job's operator
        message is written first and captured after, and the turn can end in
        between. The turn's close waits until every reserved capture is
        :meth:`paid`."""
        self._owed += 1

    def paid(self, *, cut: bool = False) -> None:
        """A reserved capture is over; ``cut`` when it was cancelled before it
        was kept, which leaves the run unconfirmed."""
        self._owed -= 1
        self._cut = self._cut or cut
        self._settle()

    def close(self) -> None:
        self._closing = True
        self._settle()

    def _settle(self) -> None:
        if not self._closing or self._owed:
            return
        if self._cut:
            self.run.interrupt()
        self.run.close()

    async def answer(self, sdk_msg: Any, session_id: str | None) -> None:
        from memory import eligibility
        text = "".join(b.text for b in getattr(sdk_msg, "content", [])
                       if isinstance(b, TextBlock))
        sid = getattr(sdk_msg, "session_id", None) or session_id
        await self._record(
            eligibility.Source.ENGAGEMENT_ANSWER, text,
            speaker_kind="specialist",
            speaker_id=self._engagement.role_or_type,
            session_id=sid if isinstance(sid, str) and sid else None)


async def _engagement_memory_note(engagement: EngagementRecord) -> str:
    """The correction note for one engagement turn of a memory-role
    specialist: every correction pending in the ledger the engagement's
    read clearance reads, and every one committed after the engagement's
    start (its base, derived from its record's start time), the operator's
    own words, read off the event loop in this turn's snapshot. ``""`` for plugin
    jobs, a specialist served no memory, or no store. A failure is logged and
    never fails the turn."""
    from delegated_memory import role_writes_memory
    from memory import capture
    from memory import note as memory_note
    from sensitivity import clearance_for_origin
    if (getattr(engagement, "kind", "") != "specialist"
            or not role_writes_memory(engagement.role_or_type)):
        return ""
    store = capture.active_store()
    if store is None or not store.available:
        return ""
    origin = getattr(engagement, "origin", None) or {}
    try:
        note = await asyncio.to_thread(
            memory_note.for_engagement, store,
            clearance=clearance_for_origin(
                origin.get("_origin_route"), origin.get("_origin_clearance"),
                str(origin.get("channel", ""))),
            started_at=float(engagement.started_at))
    except Exception:  # noqa: BLE001 — the turn runs without the note
        logger.warning("memory correction note not read in engagement %s",
                       engagement.id[:8], exc_info=True)
        return ""
    return note.text


def _session_id_from_message(sdk_msg: Any) -> str | None:
    """Extract the CLI session id from an SDK message (v0.69.11).

    The pinned Agent SDK's ``ClaudeSDKClient`` exposes no ``session_id``
    attribute; the id arrives in the stream — on the ``SystemMessage`` whose
    ``subtype == "init"`` (``data["session_id"]``) and on every
    ``ResultMessage`` (``.session_id``). Same source ``sdk_client_pool`` uses.
    """
    if isinstance(sdk_msg, SystemMessage):
        if getattr(sdk_msg, "subtype", None) == "init":
            data = getattr(sdk_msg, "data", None) or {}
            sid = data.get("session_id") if isinstance(data, dict) else None
            if isinstance(sid, str) and sid:
                return sid
    elif isinstance(sdk_msg, ResultMessage):
        sid = getattr(sdk_msg, "session_id", None)
        if isinstance(sid, str) and sid:
            return sid
    return None


def _at_launch_cwd(engagement: EngagementRecord, options: Any) -> Any:
    """*options* run in the engagement's launch cwd: recorded at its first
    open (and persisted with the record's next write), then reused by every
    later build — every resume — so a reload
    that changes the configured cwd never moves its sessions out of the
    folder its runs and its reaper use (engagement_registry.LAUNCH_CWD_KEY)."""
    cwd = getattr(options, "cwd", None)
    if not isinstance(cwd, (str, os.PathLike)) or not is_dataclass(options):
        return options          # a test double carries no real launch cwd
    launched = (getattr(engagement, "origin", None) or {}).get(LAUNCH_CWD_KEY)
    if isinstance(launched, str) and launched:
        return options if str(cwd) == launched else replace(options, cwd=launched)
    engagement.origin[LAUNCH_CWD_KEY] = str(cwd)
    return options


class DriverNotAliveError(RuntimeError):
    """Raised when a job has no session that can be opened or resumed."""


class EngagementTerminalError(DriverNotAliveError):
    """#690: the registry refused this write's admission — the record is
    terminal and must not be written to. A subclass of
    :class:`DriverNotAliveError` on purpose: every handler that treats "no
    live client" correctly treats this identically."""


RESULT_SUBTYPE_MAX_TURNS = "error_max_turns"
"""#1141: the CLI's own result subtype for a turn that stopped at its step
ceiling. For a job it is a continuation boundary, read by the turn-end
handler; ``is_error`` is also true of other aborts, so the subtype is the
only carrier."""


# -- tickets ---------------------------------------------------------------

TICKET_ADMITTED = "admitted"
TICKET_WRITTEN = "written"
TICKET_TAKEN_UP = "taken_up"

OPERATOR = "operator"
CASA = "casa"


class _Ticket:
    """One item for a job's session: an operator's message (``kind=operator``,
    held in the admission ledger until taken up, vetoing a successful
    completion meanwhile) or one of Casa's own writes (``kind=casa``: the
    brief, a nudge, "continue", the restart line), which no ledger holds.

    Every write is stamped with ``uuid``; the CLI's replay echo of that uuid
    is take-up. ``text`` is the message as admitted; ``prefix`` is prepended
    when it is redelivered after a restart."""

    __slots__ = ("uuid", "text", "kind", "state", "tg_message_id", "notice",
                 "journaled", "prefix", "memory_note")

    def __init__(self, text: str, *, kind: str, uuid: str | None = None,
                 tg_message_id: int | None = None, notice: Any = None,
                 journaled: bool = False, prefix: str = "",
                 memory_note: bool = False) -> None:
        self.uuid = uuid or str(uuid_mod.uuid4())
        self.text = str(text)
        self.kind = kind
        self.state = TICKET_ADMITTED
        self.tg_message_id = tg_message_id
        self.notice = notice
        self.journaled = journaled
        self.prefix = prefix
        self.memory_note = memory_note or kind == OPERATOR

    def written_text(self) -> str:
        return f"{self.prefix}\n{self.text}" if self.prefix else self.text


async def _one_message(payload: dict) -> Any:
    yield payload


def _user_payload(ticket: _Ticket, text: str) -> dict:
    """The stream-json line for one write. The SDK passes a dict through to
    the CLI unchanged; the CLI echoes ``uuid`` back at take-up."""
    return {"type": "user", "message": {"role": "user", "content": text},
            "parent_tool_use_id": None, "session_id": "default",
            "uuid": ticket.uuid}


# -- sessions --------------------------------------------------------------

@dataclass(eq=False)
class _Segment:
    """One turn as the reader sees it: from the first frame after a result
    (or the write that opened it while idle) to its ResultMessage."""

    cid: str
    capture: Any = None                 # _EngagementCapture | None
    capture_ready: Any = None           # its registration task
    # The turn's text-bearing messages not yet in the log: those written
    # after a call still waiting on the operator's approval are held here
    # until the cut clears (#1207). The ApprovalCut's indices count into it.
    messages: list = field(default_factory=list)
    approval_cut: ApprovalCut = field(default_factory=ApprovalCut)
    api_error: ErrorKind | None = None
    idx: int = 0
    started_ms: float = field(default_factory=lambda: time.monotonic() * 1000)
    tool_names: dict = field(default_factory=dict)
    taken_up: list = field(default_factory=list)


@dataclass(eq=False)
class _JobSession:
    """One live client of a job and the state its reader and the job's
    delivery worker share."""

    client: Any
    ctx: Any
    holder: _EngagementTurn
    reader: asyncio.Task | None = None
    # Held only across one ``client.query()``: two writes never interleave
    # their JSON lines, and nothing else is serialised.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # A turn is running: set (synchronously, before the awaited write) by a
    # write while idle or by the first frame after a result; cleared at the
    # ResultMessage.
    turn_open: bool = False
    # Casa's own teardown (cancel, suspend) is closing this session; its
    # stream end is quiet.
    closing: bool = False
    # The stream ended without Casa's teardown.
    lost: bool = False
    segment: _Segment | None = None
    # Tickets written into this session and not yet taken up, by uuid.
    written: dict = field(default_factory=dict)
    retire_task: asyncio.Task | None = None
    retired: asyncio.Event = field(default_factory=asyncio.Event)
    # Set, then replaced, at each turn end (once its cost is recorded) and
    # when the stream ends: what :meth:`InCasaDriver.wait_turn_end` waits on.
    turn_end: asyncio.Event = field(default_factory=asyncio.Event)
    # The job's cost before this client opened (the record's, at the open).
    # The CLI reports ``total_cost_usd`` cumulatively for its own process, so
    # the job's cost at a turn end is this plus that.
    cost_base: float = 0.0


@dataclass(eq=False)
class _JobLane:
    """A job's delivery queue and its worker. Outlives any one session: a
    resume, a recovery or a suspension keeps the queue in order."""

    rec: Any
    queue: collections.deque = field(default_factory=collections.deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None
    # "Casa is working on this job's session": set synchronously when its
    # stream ended unexpectedly, cleared once the replacement is registered,
    # the journal reconciled and the restart items staged — or when the
    # recovery failed. Maintenance sweeps that skip a running turn skip a
    # recovering job too.
    recovering: bool = False
    recovery_done: asyncio.Event = field(default_factory=asyncio.Event)
    recovery_failed: bool = False
    retiring: _JobSession | None = None
    stopped: bool = False

    def __post_init__(self) -> None:
        self.recovery_done.set()


class InCasaDriver(DriverProtocol):
    """Holds one continuous ClaudeSDKClient session per live job.

    ``agent_logs`` is the agent log sink: the reader puts each turn's words
    and steps in the job's log with its synchronous ``log()``, never waiting
    on Telegram. ``None`` logs nothing. Injected, like the registry hooks and
    the job callbacks, to keep the driver pure/testable.
    """

    def __init__(
        self,
        *,
        agent_logs: "AgentLogs | None" = None,
        persist_session_id: SessionIdPersister | None = None,
        persist_plugin_profiles: "Callable[[str, dict], Awaitable[None]] | None" = None,
        result_observer: "ResultObserver | None" = None,
        record_lookup: Callable[[str], Any] | None = None,
        begin_turn_delivery: "Callable[[str], bool] | None" = None,
        stamp_activity: "Callable[[str, float], None] | None" = None,
        lifecycle_lock: "Callable[[Any], asyncio.Lock] | None" = None,
        on_turn_end: "TurnEndHandler | None" = None,
        on_session_lost: "SessionLostHandler | None" = None,
        on_operator_written: "Callable[[EngagementRecord], None] | None" = None,
        launch_in_flight: "Callable[[str], bool] | None" = None,
    ) -> None:
        self._agent_logs = agent_logs
        self._persist_session_id = persist_session_id
        # S8: persists the rows a rebuilt session's plan decided onto the
        # record's plugin_artifacts — called after the options are built and
        # BEFORE the client opens.
        self._persist_plugin_profiles = persist_plugin_profiles
        self._result_observer = result_observer
        # Live registry lookup (engagement_id -> record | None).
        self._record_lookup = record_lookup
        # #690: the registry's SYNCHRONOUS admission decision, run at the
        # seam and again immediately before every ``client.query``. ``None``
        # means no fence (tests).
        self._begin_turn_delivery = begin_turn_delivery
        # SYNCHRONOUS activity stamp (engagement_id, ts): a working job is
        # active to the stale reap and the idle sweep.
        self._stamp_activity = stamp_activity
        # The per-engagement lifecycle lock every resume owner takes (the
        # channel's, so its own resume path and this driver's are one
        # owner). Without one, the driver keeps its own per-engagement locks.
        self._lifecycle_lock = lifecycle_lock
        self._own_locks: dict[str, asyncio.Lock] = {}
        self._on_turn_end = on_turn_end
        self._on_session_lost = on_session_lost
        self._on_operator_written = on_operator_written
        self._launch_in_flight = launch_in_flight
        self._sessions: dict[str, _JobSession] = {}
        self._lanes: dict[str, _JobLane] = {}
        self._settle_locks: dict[str, asyncio.Lock] = {}
        # Every client this driver registered for a job and has not yet
        # seen retired, whatever took it out of the map (a death, the idle
        # suspension, a cancel): what a new open of that job waits for.
        self._clients: dict[str, list[_JobSession]] = {}
        # Every journaled message uuid this process accepted (or re-issued
        # from the journal), per job: what a live-session reconcile leaves to
        # the running session.
        self._issued: dict[str, set[str]] = {}
        # Casa-owned tasks (readers, workers, retirements, recoveries, a
        # completion's finalization): anchored here so none is collected.
        self._tasks: set[asyncio.Task] = set()
        # v0.69.11: the CLI's session id, captured from the message stream.
        self._session_ids: dict[str, str] = {}
        # The admission ledger (INV-ENG-003): per engagement, every operator
        # ticket admitted and not yet taken up, in admission order. Admitted
        # or written-but-not-taken-up both veto a successful completion.
        self._tickets: dict[str, dict[_Ticket, None]] = {}
        # #663 ingress reservations for broker-driven system continuations.
        # No attach or detach step: an absent key is a POSITIVE zero.
        self._inbound_reservations: dict[str, int] = {}
        self._inbound_command_reservations: dict[str, int] = {}

    # -- small helpers -----------------------------------------------------

    def _spawn(self, coro: Any, *, name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def spawn_owned(self, coro: Any) -> asyncio.Task:
        """A Casa-owned task anchored in the driver's task set. A completion's
        whole finalization runs in one, created before the terminal
        transition: the tool callback only awaits it shielded, so retiring
        the session (which cancels the client's outstanding callbacks)
        cannot cut a committed completion short."""
        return self._spawn(coro)

    def _latest(self, engagement_id: str, fallback: Any = None) -> Any:
        latest = (self._record_lookup(engagement_id)
                  if self._record_lookup is not None else None)
        return latest if latest is not None else fallback

    def _is_terminal(self, engagement_id: str, fallback: Any = None) -> bool:
        rec = self._latest(engagement_id, fallback)
        return getattr(rec, "status", None) in _TERMINAL

    def _stamp(self, engagement_id: str) -> None:
        if self._stamp_activity is None:
            return
        try:
            self._stamp_activity(engagement_id, time.time())
        except Exception:  # noqa: BLE001 — a stamp never fails delivery
            logger.warning("activity stamp failed for %s", engagement_id[:8],
                           exc_info=True)

    def _lock_for(self, engagement: Any) -> asyncio.Lock:
        if self._lifecycle_lock is not None:
            return self._lifecycle_lock(engagement)
        return self._own_locks.setdefault(engagement.id, asyncio.Lock())

    def _lane(self, engagement: Any) -> _JobLane:
        lane = self._lanes.get(engagement.id)
        if lane is None or lane.stopped:
            lane = self._lanes[engagement.id] = _JobLane(rec=engagement)
        else:
            lane.rec = engagement
        return lane

    def _ensure_worker(self, engagement: Any) -> _JobLane:
        lane = self._lane(engagement)
        if lane.worker is None or lane.worker.done():
            lane.worker = self._spawn(self._work(engagement.id),
                                      name=f"job-worker-{engagement.id[:8]}")
        lane.wake.set()
        return lane

    def turn_open(self, engagement_id: str) -> bool:
        """SYNCHRONOUS: is a turn running in the job's live session? The
        idle sweep and the stale reap skip such a job."""
        session = self._sessions.get(engagement_id)
        return session is not None and session.turn_open

    async def wait_turn_end(self, engagement_id: str, timeout: float) -> bool:
        """Wait, at most ``timeout`` seconds, until the turn running in the
        job's live session has ended — its ResultMessage handled and its cost
        recorded — or the session is gone. ``True`` when nothing is left to
        wait for; ``False`` when the bound passed first. A completion's
        teardown waits on this so the job's end shows the completing turn's
        cost."""
        session = self._sessions.get(engagement_id)
        if session is None:
            return True

        async def ended() -> None:
            while session.turn_open and not (session.closing or session.lost):
                await session.turn_end.wait()

        try:
            await asyncio.wait_for(ended(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    @staticmethod
    def _signal_turn_end(session: _JobSession) -> None:
        signal, session.turn_end = session.turn_end, asyncio.Event()
        signal.set()

    def recovering(self, engagement_id: str) -> bool:
        """SYNCHRONOUS: is Casa recovering the job's session after it ended
        unexpectedly? The sweeps that skip a running turn skip this too."""
        lane = self._lanes.get(engagement_id)
        return lane is not None and lane.recovering

    # -- lifecycle ---------------------------------------------------------

    async def open(
        self,
        engagement: EngagementRecord,
        options: ClaudeAgentOptions,
    ) -> None:
        """Open the job's session: enter the client, register it, start its
        reader and the job's delivery worker. The launch owner then enqueues
        the brief (:meth:`enqueue_brief`). A failure here, before the session
        is registered, takes the launch-abort arms; once registered, session
        loss owns every failure. While the launch is in flight the job's
        worker writes and resumes nothing: an operator message that arrives
        meanwhile waits in the queue behind the brief, which the launch
        queues at the head."""
        await self._open_session(engagement, options)

    def wake_worker(self, engagement_id: str) -> None:
        """SYNCHRONOUS: the launcher let go of the job — let its worker
        re-read the job's state (write the brief, or, when the launch failed
        and left the record live, resume it and so reach the ordinary
        failed-resume end)."""
        lane = self._lanes.get(engagement_id)
        if lane is not None:
            lane.wake.set()

    def enqueue_brief(self, engagement: EngagementRecord, prompt: str) -> None:
        """SYNCHRONOUS: queue the job's brief, stamped with the record's brief
        uuid so a later resume can tell from the session transcript whether
        the job ever read it. Admitted at write time like every write: a job
        cancelled between ``open`` and the write receives nothing."""
        job = (getattr(engagement, "origin", None) or {}).get("job") or {}
        self.submit_casa(engagement, prompt, uuid=job.get("brief_uuid"),
                         brief=True, head=True)

    async def _persist_applied_profiles(self, engagement: EngagementRecord) -> None:
        """S8: write the record's rows as ``build_engagement_resume_options``
        left them through the persister, BEFORE the client opens. Strict: a
        failure propagates and no client opens."""
        if self._persist_plugin_profiles is None:
            return
        rows = getattr(engagement, "plugin_artifacts", None) or ()
        if not rows:
            return
        profiles = {row.get("name"): row.get("profile")
                    for row in rows if isinstance(row, dict)}
        await self._persist_plugin_profiles(engagement.id, profiles)

    async def resume(
        self, engagement: EngagementRecord, session_id: str,
    ) -> None:
        """Reopen the job's session with ``resume=session_id``, with the FULL
        option set rebuilt from the record (fail closed if the config is
        gone). Raises on failure. The caller holds the engagement's lifecycle
        lock: this is the step every resume owner shares.

        A record marked ``context_rebuild_pending`` (a memory wipe removed
        every session transcript) has nothing to resume and is refused."""
        latest = self._latest(engagement.id, engagement)
        if getattr(latest, "context_rebuild_pending", False):
            raise DriverNotAliveError(
                f"engagement {engagement.id[:8]}: its session was reset")
        if self.is_alive(engagement):
            logger.warning("resume() called on engagement %s that is already alive",
                           engagement.id[:8])
            return
        from tools import build_engagement_resume_options
        options = await asyncio.to_thread(
            build_engagement_resume_options, engagement, session_id)
        await self._persist_applied_profiles(engagement)
        await self._open_session(engagement, options)
        logger.info("Engagement %s resumed (session=%s)",
                    engagement.id[:8], session_id)

    async def _await_prior_clients(self, engagement_id: str) -> None:
        """No client opens while an earlier client of the same job is still
        disconnecting, whatever took it out of the driver — a death, the idle
        suspension, a cancel. The record is the driver's own list of every
        client it registered for the job and has not seen retired, which no
        teardown path clears early; it is re-read after every wait. A client
        still registered as the live one is not waited for: the one-owner
        check refuses the new client instead."""
        while True:
            live = self._sessions.get(engagement_id)
            kept = [s for s in self._clients.get(engagement_id, ())
                    if not s.retired.is_set()]
            if kept:
                self._clients[engagement_id] = kept
            else:
                self._clients.pop(engagement_id, None)
            pending = [s for s in kept if s is not live]
            if not pending:
                return
            await pending[0].retired.wait()

    async def _open_session(
        self, engagement: EngagementRecord, options: Any,
    ) -> _JobSession:
        # Every path that opens a job's client comes through here, so this is
        # where retirement-before-replacement is enforced, whatever the
        # caller waited on before.
        await self._await_prior_clients(engagement.id)
        # E-E (v0.29.0): bind engagement_var BEFORE ClaudeSDKClient.__aenter__
        # so the SDK's inner read task — which dispatches tool callbacks —
        # captures the engagement in its ContextVar copy. Lazy import: tools
        # imports engagement_registry.
        from tools import engagement_var

        options = _at_launch_cwd(engagement, options)
        client = ClaudeSDKClient(sdk_logging.with_stderr_callback(
            options, engagement_id=engagement.id[:8]))
        holder = _EngagementTurn()
        token = engagement_var.set(engagement)
        cid_token = cid_var.set(holder)
        try:
            ctx = client.__aenter__()
            entered = await ctx if asyncio.iscoroutine(ctx) else ctx
            entered = entered or client
            latest = self._latest(engagement.id)
            refusal: BaseException | None = None
            # The one-owner check, after the client is entered and before it
            # is registered: a job that went terminal meanwhile, or another
            # owner that registered first, means this client lost — closed,
            # never registered.
            if getattr(latest, "status", None) in _TERMINAL:
                refusal = EngagementTerminalError(
                    f"engagement {engagement.id[:8]} ended while its session opened")
            if refusal is None and engagement.id in self._sessions:
                refusal = DriverNotAliveError(
                    f"engagement {engagement.id[:8]} already has a live session")
            if refusal is not None:
                try:
                    await _close_client(entered, client)
                except Exception:  # noqa: BLE001 — best effort
                    logger.warning("losing client close failed for %s",
                                   engagement.id[:8], exc_info=True)
                raise refusal
            job = (getattr(latest or engagement, "origin", None) or {}).get("job")
            base = job.get("cost_usd") if isinstance(job, dict) else None
            session = _JobSession(
                client=entered, ctx=client, holder=holder,
                cost_base=float(base) if isinstance(base, (int, float)) else 0.0)
            # The lane exists before the reader starts, so a death of this
            # client at any point after registration finds its recovery owner.
            self._lane(engagement)
            self._sessions[engagement.id] = session
            self._clients.setdefault(engagement.id, []).append(session)
            # The reader is created in this context, so it carries the
            # engagement binding; it sets its own cid per turn.
            session.reader = self._spawn(
                self._read(session, engagement),
                name=f"job-reader-{engagement.id[:8]}")
            logger.info("Engagement %s driver=in_casa client opened",
                        engagement.id[:8])
        finally:
            cid_var.reset(cid_token)
            engagement_var.reset(token)
        await self._log_context_usage(session, engagement)
        self._ensure_worker(engagement)
        return session

    async def _log_context_usage(self, session: _JobSession,
                                 engagement: EngagementRecord) -> None:
        """One ``get_context_usage()`` per client open, before anything is
        written (no model call): the window and auto-compaction threshold the
        CLI actually applies, and where it took them from — whether the
        long-session window env took effect on this model is read here."""
        probe = getattr(session.client, "get_context_usage", None)
        if probe is None:
            return
        try:
            usage = await asyncio.wait_for(probe(), 10)
            get = usage.get
        except Exception as exc:  # noqa: BLE001 — observability only
            logger.info("Engagement %s context usage unavailable: %s",
                        engagement.id[:8], exc)
            return
        logger.info(
            "Engagement %s context window=%s threshold=%s source=%s auto=%s",
            engagement.id[:8], get("maxTokens"), get("autoCompactThreshold"),
            get("autocompactSource"), get("isAutoCompactEnabled"))

    async def cancel(self, engagement: EngagementRecord) -> None:
        """Casa's own teardown (terminal, or the idle suspension): mark the
        session closing so its stream end is quiet, retire it, and retire a
        dead one still retiring. At terminal the job's worker stops; a
        suspended job keeps its queue and resumes on its next input."""
        eid = engagement.id
        session = self._sessions.pop(eid, None)
        lane = self._lanes.get(eid)
        retiring = lane.retiring if lane is not None else None
        self._session_ids.pop(eid, None)
        for s in (session, retiring):
            if s is not None and not s.retired.is_set():
                s.closing = True
        for s in (session, retiring):
            if s is not None:
                try:
                    await self._retire(s)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Engagement %s cancel: client close raised %s",
                                   eid[:8], exc)
        if self._record_lookup is None or self._is_terminal(eid, engagement) \
                or self._latest(eid) is None:
            self._end_lane(eid)

    def _end_lane(self, engagement_id: str) -> None:
        lane = self._lanes.pop(engagement_id, None)
        if lane is None:
            return
        lane.stopped = True
        lane.wake.set()
        lane.recovery_done.set()
        # What was still queued is the journal's: the terminal reconcile tells
        # each operator message the transcript lacks as lost, once.
        lane.queue.clear()

    async def _retire(self, session: _JobSession) -> None:
        """Disconnect the session's client explicitly — the SDK's close
        cancels the client's outstanding hook and tool callbacks; EOF alone
        does not — and set ``retired``. Idempotent: a second caller waits for
        the first."""
        if session.retire_task is None:
            session.retire_task = self._spawn(self._disconnect(session))
        await asyncio.shield(session.retire_task)

    async def _disconnect(self, session: _JobSession) -> None:
        try:
            await _close_client(session.client, session.ctx)
        finally:
            session.retired.set()

    def get_session_id(self, engagement: EngagementRecord) -> str | None:
        """The session id captured from this engagement's message stream,
        falling back to the record's persisted value."""
        return (self._session_ids.get(engagement.id)
                or getattr(engagement, "sdk_session_id", None))

    def is_alive(self, engagement: EngagementRecord) -> bool:
        return engagement.id in self._sessions

    # -- the admission ledger (INV-ENG-003) ---------------------------------

    def discharge_inbound(self, engagement_id: str, token: object) -> None:
        """SYNCHRONOUS + IDEMPOTENT: drop one ticket from the ledger (and the
        job's queue, if it was never written)."""
        bucket = self._tickets.get(engagement_id)
        if bucket is not None and token in bucket:
            del bucket[token]
            if not bucket:
                self._tickets.pop(engagement_id, None)
        lane = self._lanes.get(engagement_id)
        if (lane is not None and isinstance(token, _Ticket)
                and token.state == TICKET_ADMITTED and token in lane.queue):
            lane.queue.remove(token)

    def inbound_token_held(self, engagement_id: str, token: object) -> bool:
        return token in self._tickets.get(engagement_id, ())

    def inbound_unread_depth(self, engagement_id: str) -> int:
        """The completion gate's read: operator tickets admitted, or written
        and not yet taken up. > 0 refuses a successful completion."""
        return len(self._tickets.get(engagement_id, ()))

    def inbound_unread_texts(self, engagement_id: str) -> list[str]:
        """The unread tickets' exact texts, for the veto re-check inside the
        terminal transition and the dying-message disclosure."""
        return [t.text for t in self._tickets.get(engagement_id, ())]

    def _take_up(self, engagement_id: str, ticket: _Ticket) -> None:
        ticket.state = TICKET_TAKEN_UP
        bucket = self._tickets.get(engagement_id)
        if bucket is not None and ticket in bucket:
            del bucket[ticket]
            if not bucket:
                self._tickets.pop(engagement_id, None)
        if ticket.journaled:
            job_inbox.mark_taken_up(engagement_id, ticket.uuid)

    # -- #663 ingress reservations -------------------------------------------

    def reserve_inbound(self, engagement_id: str, *,
                        command: bool = False,
                        text: str | None = None,
                        message_id: int | None = None) -> None:
        """SYNCHRONOUS ingress reservation for a system continuation whose
        text does not exist anywhere yet; released at the entry that admits
        its text-bearing ticket, in the same event-loop step, so the
        completion gate never observes both populations empty. ``text`` and
        ``message_id`` are ignored: there is nothing to record yet."""
        self._inbound_reservations[engagement_id] = (
            self._inbound_reservations.get(engagement_id, 0) + 1)
        if command:
            self._inbound_command_reservations[engagement_id] = (
                self._inbound_command_reservations.get(engagement_id, 0) + 1)

    def release_inbound_reservation(self, engagement_id: str, *,
                                    command: bool = False,
                                    message_id: int | None = None) -> None:
        """Release one reservation, clamped at zero. Release-guaranteed by its
        owner on every exit: an unreleased one would refuse every successful
        completion."""
        n = self._inbound_reservations.get(engagement_id, 0) - 1
        if n <= 0:
            self._inbound_reservations.pop(engagement_id, None)
        else:
            self._inbound_reservations[engagement_id] = n
        if command:
            c = self._inbound_command_reservations.get(engagement_id, 0) - 1
            if c <= 0:
                self._inbound_command_reservations.pop(engagement_id, None)
            else:
                self._inbound_command_reservations[engagement_id] = c

    def inbound_reservation_texts(self, engagement_id: str) -> list[str]:
        """#691: always empty — a reservation is taken for text that does not
        exist yet, so its disclosure stays a count."""
        return []

    def inbound_reservations(self, engagement_id: str) -> int:
        """The gate read: a reservation refuses a successful completion
        exactly as an unread ticket does."""
        return self._inbound_reservations.get(engagement_id, 0)

    def inbound_message_reservations(self, engagement_id: str) -> int:
        """#664 disclosure projection — reservations minus the ones a
        recognized command holds for itself. Clamped."""
        return max(
            0,
            self._inbound_reservations.get(engagement_id, 0)
            - self._inbound_command_reservations.get(engagement_id, 0))

    # -- the seams' synchronous entry -----------------------------------------

    def submit_operator(
        self, engagement: EngagementRecord, text: str, *,
        tg_message_id: int | None = None, notice: dict | None = None,
    ) -> _Ticket:
        """SYNCHRONOUS — nothing is awaited: commit the operator's message to
        the job's journal, admit it (a terminal record refuses), stamp the
        job's activity and append the ticket to the job's delivery queue.
        The caller returns at once; the delivery worker writes it.

        Raises :class:`job_inbox.JournalError` when the journal could not be
        written and :class:`EngagementTerminalError` when the record is
        terminal; in both cases nothing was accepted and the seam answers
        with its "please resend" reply."""
        eid = engagement.id
        ticket = _Ticket(text, kind=OPERATOR, tg_message_id=tg_message_id,
                         notice=notice, journaled=True)
        job_inbox.append(eid, uuid=ticket.uuid, text=ticket.text, notice=notice)
        self._issued.setdefault(eid, set()).add(ticket.uuid)
        if (self._begin_turn_delivery is not None
                and not self._begin_turn_delivery(eid)):
            job_inbox.remove(eid, [ticket.uuid])
            raise EngagementTerminalError(
                f"engagement {eid[:8]} is terminal — not accepting a message")
        self._tickets.setdefault(eid, {})[ticket] = None
        self._stamp(eid)
        lane = self._ensure_worker(engagement)
        lane.queue.append(ticket)
        lane.wake.set()
        return ticket

    def submit_casa(self, engagement: EngagementRecord, text: str, *,
                    head: bool = False, uuid: str | None = None,
                    brief: bool = False) -> _Ticket:
        """SYNCHRONOUS: queue one of Casa's own writes (the brief, the nudge,
        "continue", the restart line). Not journaled and not in the ledger;
        admitted at write time like every write."""
        ticket = _Ticket(text, kind=CASA, uuid=uuid, memory_note=brief)
        lane = self._ensure_worker(engagement)
        if head:
            lane.queue.appendleft(ticket)
        else:
            lane.queue.append(ticket)
        lane.wake.set()
        return ticket

    # -- the delivery worker: the only writer --------------------------------

    async def _work(self, engagement_id: str) -> None:
        while True:
            lane = self._lanes.get(engagement_id)
            if lane is None or lane.stopped:
                return
            if not lane.queue:
                lane.wake.clear()
                if self._is_terminal(engagement_id, lane.rec):
                    self._end_lane(engagement_id)
                    return
                await lane.wake.wait()
                continue
            if self._is_terminal(engagement_id, lane.rec):
                self._end_lane(engagement_id)
                return
            if lane.recovering:
                # Wait for the recovery without holding the lifecycle lock,
                # then reselect the head: the restart line and redeliveries
                # it staged go before the item this pass was holding.
                await lane.recovery_done.wait()
                continue
            if lane.recovery_failed:
                # The job is being ended; the terminal reconcile tells what
                # is left in the journal.
                lane.wake.clear()
                await lane.wake.wait()
                continue
            if (self._launch_in_flight is not None
                    and self._launch_in_flight(engagement_id)):
                # The launch owns the job until its launcher's finally: it
                # opens the client and queues the brief at the head. Nothing
                # is written, resumed or recovered meanwhile; the launcher
                # wakes this worker when it lets go (wake_worker).
                lane.wake.clear()
                await lane.wake.wait()
                continue
            session = self._sessions.get(engagement_id)
            if session is None:
                await self._resume_for_write(lane)
                continue
            ticket = lane.queue[0]
            try:
                await self._write(lane, session, ticket)
            except Exception:  # noqa: BLE001 — the worker never dies on one item
                # Not a CLI death (that is handled inside the write): the item
                # is dropped from the queue. An operator message stays in the
                # journal, so a restart redelivers it and a terminal job tells
                # it as lost.
                logger.warning("job %s delivery step failed",
                               engagement_id[:8], exc_info=True)
                if lane.queue and lane.queue[0] is ticket:
                    lane.queue.popleft()
                self.discharge_inbound(engagement_id, ticket)

    async def _resume_for_write(self, lane: _JobLane) -> None:
        """No live session and the job is not recovering: resume it, under
        the lifecycle lock (the one resume owner), so a suspended waiting job
        is resumed by its next input."""
        rec = self._latest(lane.rec.id, lane.rec)
        eid = rec.id
        async with self._lock_for(rec):
            if (eid in self._sessions or lane.recovering or lane.stopped
                    or self._is_terminal(eid, rec)):
                return
            sid = self.get_session_id(rec)
            try:
                if not sid:
                    raise DriverNotAliveError(
                        f"engagement {eid[:8]} has no session to resume")
                await self.resume(rec, sid)
                return
            except Exception as exc:  # noqa: BLE001 — handed to the owner below
                logger.warning("job %s resume for a write failed: %s", eid[:8], exc)
        if self._on_session_lost is not None:
            self._begin_recovery(lane, rec)
            return
        # No recovery owner (a rig without the job policy): nothing can be
        # written, so the queue is dropped; the journal keeps the operator's
        # messages.
        lane.queue.clear()
        lane.wake.clear()

    async def _write(self, lane: _JobLane, session: _JobSession,
                     ticket: _Ticket) -> None:
        rec = self._latest(lane.rec.id, lane.rec)
        eid = rec.id
        note = await _engagement_memory_note(rec) if ticket.memory_note else ""
        text = ticket.written_text()
        if note:
            text = f"{note}\n\n{text}"
        async with session.write_lock:
            if not lane.queue or lane.queue[0] is not ticket:
                return              # dropped or reordered while the note was read
            if self._sessions.get(eid) is not session:
                return              # replaced or closed meanwhile: reselect
            # INV-ENG-009: every write is admitted synchronously immediately
            # before ``client.query`` — nothing awaits in between.
            if (self._begin_turn_delivery is not None
                    and not self._begin_turn_delivery(eid)):
                # Terminal: nothing more is written. The journal's terminal
                # branch tells an operator message the job never read.
                lane.queue.popleft()
                logger.info("job %s is terminal; a queued write was dropped",
                            eid[:8])
                return
            opening = not session.turn_open
            session.turn_open = True
            if opening and session.segment is None:
                self._open_segment(session, rec)
            segment = session.segment
            owed = (segment.capture if segment is not None
                    and segment.capture is not None and ticket.kind == OPERATOR
                    and ticket.tg_message_id is not None else None)
            if owed is not None:
                owed.owe()
            try:
                await session.client.query(_one_message(_user_payload(ticket, text)))
            except BaseException as exc:  # noqa: BLE001 — the CLI died during the write
                if owed is not None:
                    # A failed write is retried and owes nothing here; a write
                    # cancelled midway may have landed, so the run stays open.
                    owed.paid(cut=not isinstance(exc, Exception))
                if not isinstance(exc, Exception):
                    raise
                failure: BaseException | None = exc
            else:
                failure = None
        if failure is not None:
            # The ticket stays unwritten at the head of the queue: after the
            # recovery it is written once into the replacement.
            logger.warning("job %s write failed: %s", eid[:8], failure)
            if self._on_session_lost is None:
                _drop(lane, ticket)
            self._begin_loss(session, rec)
            return
        # The ticket itself, not the queue's head: the head can change while
        # the write was awaited (a recovery's restart line, a brief).
        _drop(lane, ticket)
        ticket.state = TICKET_WRITTEN
        session.written[ticket.uuid] = ticket
        if ticket.kind == OPERATOR:
            if self._on_operator_written is not None:
                try:
                    self._on_operator_written(rec)
                except Exception:  # noqa: BLE001
                    logger.warning("operator-written hook failed for %s",
                                   eid[:8], exc_info=True)
            if owed is not None:
                cut = True
                try:
                    await asyncio.shield(segment.capture_ready)
                    await owed.operator(ticket.text, ticket.tg_message_id)
                    cut = False
                except Exception:  # noqa: BLE001 — capture never fails delivery
                    cut = False     # a failed capture is the run's to record
                    logger.warning("operator capture failed for %s", eid[:8],
                                   exc_info=True)
                finally:
                    owed.paid(cut=cut)

    # -- the reader ------------------------------------------------------------

    def _open_segment(self, session: _JobSession, rec: Any) -> _Segment:
        cid = new_cid()
        session.holder.value = cid
        session.holder.approvals = {}
        session.turn_open = True
        segment = _Segment(cid=cid)
        capture = _EngagementCapture.for_turn(rec)
        if capture is not None:
            segment.capture = capture
            segment.capture_ready = self._spawn(
                capture.register(self.get_session_id(rec)))
        session.segment = segment
        # The tie line is logged under the new turn's own cid, whichever task
        # opened the turn (the reader at its first frame, or the delivery
        # worker writing into an idle session).
        token = cid_var.set(cid)
        try:
            logger.info("Engagement %s turn cid=%s engaged_by=%s", rec.id, cid,
                        (getattr(rec, "origin", None) or {}).get("cid") or "-")
        finally:
            cid_var.reset(token)
        return segment

    async def _read(self, session: _JobSession, engagement: Any) -> None:
        eid = engagement.id
        try:
            async for sdk_msg in session.client.receive_messages():
                try:
                    await self._on_frame(session, engagement, sdk_msg)
                except Exception:  # noqa: BLE001 — a frame bug never ends the reader
                    logger.warning("job %s frame handling failed", eid[:8],
                                   exc_info=True)
        except asyncio.CancelledError:
            session.closing = True
            self._signal_turn_end(session)
            raise
        except Exception as exc:  # noqa: BLE001 — the stream raised: it ended
            logger.warning("job %s message stream raised: %s", eid[:8], exc)
        try:
            await self._end_segment(session, engagement, None)
            if session.closing:
                return
            self._begin_loss(session, engagement)
        finally:
            self._signal_turn_end(session)

    async def _on_frame(self, session: _JobSession, engagement: Any,
                        sdk_msg: Any) -> None:
        rec = self._latest(engagement.id, engagement)
        segment = session.segment
        if segment is None and not isinstance(sdk_msg, ResultMessage):
            segment = self._open_segment(session, rec)
        if segment is not None:
            cid_var.set(segment.cid)
        run = (segment.capture.run if segment is not None
               and segment.capture is not None else None)
        await self._observe_session_id(rec, sdk_msg, run)

        if isinstance(sdk_msg, ResultMessage):
            await self._end_segment(session, rec, sdk_msg)
            return
        if isinstance(sdk_msg, SystemMessage):
            sdk_logging.log_system_init(sdk_msg)
            if getattr(sdk_msg, "subtype", None) == "compact_boundary":
                data = getattr(sdk_msg, "data", None) or {}
                meta = data.get("compact_metadata", data) if isinstance(data, dict) else {}
                logger.info("Engagement %s context compacted trigger=%s pre=%s",
                            rec.id[:8], meta.get("trigger"), meta.get("pre_tokens"))
            return
        if isinstance(sdk_msg, UserMessage):
            echoed = session.written.pop(getattr(sdk_msg, "uuid", None) or "", None)
            if echoed is not None:
                # Take-up: the CLI's own statement, in stream order, that the
                # message entered a turn.
                self._take_up(rec.id, echoed)
                segment.taken_up.append(echoed)
                return
            for block in getattr(sdk_msg, "content", []) or []:
                if isinstance(block, ToolResultBlock):
                    sdk_logging.log_tool_result(
                        block, idx=segment.idx, started_ms=segment.started_ms,
                        name=segment.tool_names.get(
                            getattr(block, "tool_use_id", ""), ""))
                    # #1207: the hook's record for a call is read at its result.
                    segment.approval_cut.observe(
                        getattr(block, "tool_use_id", ""),
                        session.holder.approvals or {}, len(segment.messages))
            # A consume that cleared the cut releases the words held behind it.
            self._log_words(segment, rec)
            return
        if not isinstance(sdk_msg, AssistantMessage):
            return
        segment.idx += 1
        sdk_logging.log_assistant_message(sdk_msg, idx=segment.idx)
        steps: list[str] = []
        for block in getattr(sdk_msg, "content", []) or []:
            if isinstance(block, ToolUseBlock):
                segment.tool_names[getattr(block, "id", "")] = getattr(block, "name", "?")
                sdk_logging.log_tool_use(block, idx=segment.idx,
                                         started_ms=segment.started_ms)
                steps.append(str(getattr(block, "name", "")))
        # #568/#595: an API-level fault arrives as an assistant message whose
        # text is the CLI's own error prose. Never streamed; recorded (main
        # loop only, first one wins) so the turn ends with its kind.
        kind = api_error_kind(sdk_msg)
        if kind is not None:
            if segment.api_error is None and getattr(
                    sdk_msg, "parent_tool_use_id", None) is None:
                segment.api_error = kind
            logger.warning("engagement %s turn ended by an API error kind=%s",
                           rec.id[:8], kind.value)
            return
        if segment.capture is not None:
            try:
                await asyncio.shield(segment.capture_ready)
                await segment.capture.answer(sdk_msg, self._session_ids.get(rec.id))
            except Exception:  # noqa: BLE001
                logger.warning("answer capture failed for %s", rec.id[:8],
                               exc_info=True)
        msg_text = "".join(b.text for b in getattr(sdk_msg, "content", [])
                           if isinstance(b, TextBlock))
        if msg_text:
            segment.messages.append(msg_text)
            self._log_words(segment, rec)
        if getattr(sdk_msg, "parent_tool_use_id", None) is None:
            # The main loop's steps, after the words written before them. A
            # step names the tool, never its outcome, so it is not held.
            for name in steps:
                background_jobs.log_job_line(
                    self._agent_logs, rec, step_log_line(name))

    def _log_words(self, segment: _Segment, rec: Any) -> None:
        """#1207: put the turn's words in the job's log, unless a call is
        waiting on the operator's approval. Nothing written after such a call
        is logged; a consume of the same grant clears the cut, and the words
        written meanwhile are logged then. Synchronous: the log never makes
        the reader wait."""
        if segment.approval_cut.cut is not None:
            return
        held, segment.messages = segment.messages, []
        for text in held:
            background_jobs.log_job_line(self._agent_logs, rec, text)

    async def _end_segment(self, session: _JobSession, rec: Any,
                           result: Any) -> None:
        """A turn's end. At a ResultMessage the synchronous part runs first —
        detach the segment, clear ``turn_open``, stamp activity, drop the
        journal entries the turn took up — so a write landing during the
        awaits below opens a new turn rather than being folded into this one.
        Without a result (the stream ended mid-turn) only the log and the
        capture are closed."""
        segment = session.segment
        if segment is None:
            if result is None:
                return
            segment = self._open_segment(session, rec)
        session.segment = None
        session.holder.approvals = None
        if result is not None:
            session.turn_open = False
            self._stamp(rec.id)
            self._record_cost(session, rec, result)
            done = [t.uuid for t in segment.taken_up if t.journaled]
            if done:
                job_inbox.remove(rec.id, done)
            sdk_logging.log_turn_done(result, started_ms=segment.started_ms)
            if self._result_observer is not None:
                try:
                    self._result_observer(rec, result)
                except Exception:  # noqa: BLE001
                    logger.warning("result_observer raised for engagement %s",
                                   rec.id[:8], exc_info=True)
            self._signal_turn_end(session)
        # The turn's words that are not held: a turn that ends while a call
        # still waits on the operator's approval never logs what it wrote
        # after that call.
        cut = segment.approval_cut.cut
        for text in segment.messages[:cut]:
            background_jobs.log_job_line(self._agent_logs, rec, text)
        segment.messages = []
        if segment.capture is not None:
            try:
                await asyncio.shield(segment.capture_ready)
            except Exception:  # noqa: BLE001
                pass
            segment.capture.close()
        if result is None:
            return
        api_error = segment.api_error or result_api_error_kind(result)
        if api_error is not None:
            logger.warning("engagement %s: turn ended by an API fault kind=%s",
                           rec.id[:8], api_error.value)
        if self._on_turn_end is not None:
            try:
                self._on_turn_end(self._latest(rec.id, rec), result, api_error)
            except Exception:  # noqa: BLE001 — the guard never kills the reader
                logger.warning("turn-end handler failed for %s", rec.id[:8],
                               exc_info=True)

    def _record_cost(self, session: _JobSession, rec: Any, result: Any) -> None:
        """The job's cost at this turn end, in memory on the record's
        ``origin["job"]["cost_usd"]``: the cost before this client opened plus
        the CLI's cumulative ``total_cost_usd`` for its process. The turn-end
        handler persists it. Shown to the operator; nothing reads it to
        decide anything."""
        job = (getattr(rec, "origin", None) or {}).get("job")
        total = getattr(result, "total_cost_usd", None)
        if (not isinstance(job, dict) or isinstance(total, bool)
                or not isinstance(total, (int, float))):
            return
        job["cost_usd"] = round(session.cost_base + float(total), 6)

    # -- session loss and recovery ---------------------------------------------

    def _begin_loss(self, session: _JobSession, engagement: Any) -> None:
        """The session's stream ended without Casa's teardown, or a write into
        it failed. SYNCHRONOUS and idempotent: mark the job recovering (when a
        recovery owner is wired and the record is live), remove THIS session
        from the registry (identity-checked, so a reader of an old session
        never removes its replacement), start retiring it, then hand the job
        to the session-lost handler."""
        if session.lost or session.closing:
            return
        session.lost = True
        eid = engagement.id
        rec = self._latest(eid, engagement)
        lane = self._lanes.get(eid)
        policy = (self._on_session_lost is not None and lane is not None
                  and not lane.stopped and not self._is_terminal(eid, rec))
        if self._sessions.get(eid) is session:
            del self._sessions[eid]
        if lane is not None:
            lane.retiring = session
        self._spawn(self._retire(session))
        logger.warning("Engagement %s session ended unexpectedly", eid[:8])
        if policy:
            self._begin_recovery(lane, rec)
            return
        # No recovery owner (a rig without the job policy, or a terminal
        # record): the queue is emptied, so no write resumes a session that
        # keeps dying; the journal keeps the operator's messages.
        if lane is not None:
            lane.queue.clear()
            lane.wake.set()

    def _begin_recovery(self, lane: _JobLane, rec: Any) -> None:
        lane.recovering = True
        lane.recovery_done.clear()
        self._stamp(rec.id)
        self._spawn(self._run_session_lost(lane, rec))

    async def _run_session_lost(self, lane: _JobLane, rec: Any) -> None:
        try:
            await self._on_session_lost(rec)
        except Exception:  # noqa: BLE001
            logger.warning("session-lost handler failed for %s", rec.id[:8],
                           exc_info=True)
        finally:
            if lane.recovering or rec.id not in self._sessions:
                # The handler ended the job, or failed: no replacement.
                lane.recovery_failed = rec.id not in self._sessions
                lane.recovering = False
                lane.recovery_done.set()
            lane.wake.set()

    async def recover(self, engagement: EngagementRecord, *,
                      restart_line: str, brief: str | None = None,
                      redelivery_prefix: str) -> None:
        """Resume the job's session and put it back to work: the step a
        session-lost handler and the boot resume share. In order: wait for the
        dead session's retirement; under the lifecycle lock, re-check the
        record, open the replacement (its reader starts), reconcile the
        journal against the transcript, and stage at the head of the job's
        queue the brief (when the transcript lacks the brief's uuid) or the
        restart line, then every operator message the transcript lacks,
        prefixed with *redelivery_prefix*. ``recovering`` is cleared as soon
        as that is staged; the worker writes it. Raises on failure, leaving
        the job not recovering and marked failed (its owner ends it)."""
        eid = engagement.id
        lane = self._lane(engagement)
        lane.recovering = True
        lane.recovery_done.clear()
        try:
            retiring = lane.retiring
            if retiring is not None:
                await retiring.retired.wait()
            async with self._lock_for(engagement):
                rec = self._latest(eid, engagement)
                if self._is_terminal(eid, rec):
                    raise EngagementTerminalError(
                        f"engagement {eid[:8]} ended before it could be resumed")
                sid = self.get_session_id(rec)
                already_live = eid in self._sessions
                if not already_live:
                    if not sid:
                        raise DriverNotAliveError(
                            f"engagement {eid[:8]} has no session to resume")
                    await self.resume(rec, sid)
                session = self._sessions.get(eid)
                seen = await asyncio.to_thread(_transcript_uuids, rec, sid)
                if session is None or self._sessions.get(eid) is not session:
                    # The session died while the transcript was read: its
                    # own loss handling marked it retiring and started the
                    # recovery that will run next under this lock; nothing
                    # is staged here, and the retirement it waits for stays.
                    if self._on_session_lost is None:
                        raise DriverNotAliveError(
                            f"engagement {eid[:8]} session ended during recovery")
                    logger.info("Engagement %s session ended during recovery; "
                                "its own recovery takes over", eid[:8])
                    return
                redeliver = self._reconcile_live(rec, seen, redelivery_prefix,
                                                 live=already_live)
                job = (getattr(rec, "origin", None) or {}).get("job") or {}
                brief_uuid = job.get("brief_uuid")
                if brief is not None and brief_uuid and brief_uuid not in seen:
                    first = _Ticket(brief, kind=CASA, uuid=brief_uuid,
                                    memory_note=True)
                else:
                    first = _Ticket(restart_line, kind=CASA)
                lane = self._lane(rec)
                lane.queue.extendleft(reversed([first, *redeliver]))
                lane.retiring = None
                self._stamp(eid)
                lane.recovering = False
                lane.recovery_failed = False
                lane.recovery_done.set()
            self._ensure_worker(rec)
            logger.info("Engagement %s session recovered (%d redelivered)",
                        eid[:8], len(redeliver))
        except BaseException:
            lane.recovering = False
            lane.recovery_failed = True
            lane.recovery_done.set()
            lane.wake.set()
            raise

    def _reconcile_live(self, rec: Any, seen: set[str],
                        prefix: str, *, live: bool = False) -> list[_Ticket]:
        """The transcript is authoritative for the journal AND the ledger: a
        uuid it holds was read — its entry is dropped, its in-memory ticket
        discharged and any queued copy removed. A uuid it lacks gets exactly
        one ticket: one written to the dead session is re-bound (unwritten
        again) and redelivered; one still queued stays where it is; a
        journaled entry with no ticket (a Casa restart) gets a new one.

        *live*: the session was already running when the reconcile began
        (Telegram is up before the boot resume, so the job's own worker can
        resume it on an operator's message first). Every message this process
        accepted is then that session's to settle, whatever state it has
        reached meanwhile; the reconcile acts only on journal entries from
        before this process started."""
        eid = rec.id
        if live:
            return self._reconcile_restart_entries(rec, seen, prefix)
        lane = self._lane(rec)
        ledger = self._tickets.get(eid, {})
        known = {t.uuid for t in ledger}
        redeliver: list[_Ticket] = []
        for ticket in list(ledger):
            if ticket.uuid in seen:
                self._take_up(eid, ticket)
                if ticket in lane.queue:
                    lane.queue.remove(ticket)
            elif ticket.state == TICKET_WRITTEN:
                ticket.state = TICKET_ADMITTED
                ticket.prefix = prefix
                redeliver.append(ticket)
        for ticket in list(lane.queue):
            if ticket.uuid in seen:
                lane.queue.remove(ticket)
        entries = job_inbox.load(eid)
        for entry in entries:
            if entry["uuid"] in seen or entry["uuid"] in known:
                continue
            redeliver.append(self._ticket_for_entry(eid, entry, prefix))
        job_inbox.remove(eid, [e["uuid"] for e in entries if e["uuid"] in seen])
        retiring = lane.retiring
        if retiring is not None:
            # Casa's own writes the dead session never took up are not
            # redelivered: the restart line covers them.
            retiring.written.clear()
        return redeliver

    def _reconcile_restart_entries(self, rec: Any, seen: set[str],
                                   prefix: str) -> list[_Ticket]:
        """The live-session reconcile: only journal entries this process
        never accepted (written before Casa restarted) are considered — one
        the transcript holds goes, one it lacks gets the only ticket."""
        eid = rec.id
        issued = self._issued.get(eid, set())
        entries = [e for e in job_inbox.load(eid) if e["uuid"] not in issued]
        redeliver = [self._ticket_for_entry(eid, e, prefix)
                     for e in entries if e["uuid"] not in seen]
        job_inbox.remove(eid, [e["uuid"] for e in entries if e["uuid"] in seen])
        return redeliver

    def _ticket_for_entry(self, eid: str, entry: dict, prefix: str) -> _Ticket:
        ticket = _Ticket(entry.get("text", ""), kind=OPERATOR,
                         uuid=entry["uuid"], notice=entry.get("notice"),
                         journaled=True, prefix=prefix)
        self._tickets.setdefault(eid, {})[ticket] = None
        self._issued.setdefault(eid, set()).add(ticket.uuid)
        return ticket

    async def settle_journal_terminal(
        self, engagement: EngagementRecord,
        tell_lost: Callable[[EngagementRecord, dict], Awaitable[None]],
    ) -> int:
        """The journal's terminal branch, run after the job ended and its
        client closed: each entry the transcript lacks gets one lost-message
        notice (*tell_lost*, bounded by the caller); an entry the transcript
        holds goes silently. Matching in-memory tickets are discharged and
        the file goes. Returns the number of notices sent."""
        eid = engagement.id
        # Boot and a live terminal path may settle the same job at once: one
        # at a time per job, each loading the entries under the lock, so an
        # entry is told by whichever runs first and seen by the other as gone.
        async with self._settle_locks.setdefault(eid, asyncio.Lock()):
            return await self._settle_journal_locked(engagement, tell_lost)

    async def _settle_journal_locked(
        self, engagement: EngagementRecord,
        tell_lost: Callable[[EngagementRecord, dict], Awaitable[None]],
    ) -> int:
        eid = engagement.id
        entries = job_inbox.load(eid)
        told = 0
        if entries:
            seen = await asyncio.to_thread(
                _transcript_uuids, engagement, self.get_session_id(engagement))
            for entry in entries:
                if entry["uuid"] in seen:
                    continue
                try:
                    await tell_lost(engagement, entry)
                    told += 1
                except Exception:  # noqa: BLE001 — one bounded attempt
                    logger.warning("lost-message notice failed for %s",
                                   eid[:8], exc_info=True)
                # Gone as soon as it is told (or its one attempt failed), so
                # a Casa death later in this loop does not tell it again.
                job_inbox.remove(eid, [entry["uuid"]])
        journaled = {e["uuid"] for e in entries}
        for ticket in list(self._tickets.get(eid, {})):
            if ticket.uuid in journaled:
                self.discharge_inbound(eid, ticket)
        job_inbox.discard(eid)
        self._issued.pop(eid, None)
        return told

    # -- internal ------------------------------------------------------------

    async def _observe_session_id(
        self, engagement: EngagementRecord, sdk_msg: Any, run: Any = None,
    ) -> None:
        """Record the session id a stream message carries: the in-memory map
        and the registry's resume pointer. A turn's run (*run*) is named
        first, before anything else names the session."""
        sid = _session_id_from_message(sdk_msg)
        if not sid:
            return
        if run is not None:
            await run.name(sid)
        self._session_ids[engagement.id] = sid
        if (self._persist_session_id is not None
                and engagement.sdk_session_id != sid):
            # #302: mark the id persisted ONLY after the durable write
            # succeeded; the next message carrying it retries otherwise.
            try:
                await self._persist_session_id(engagement.id, sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Engagement %s persist_session_id failed "
                    "(retried on the next message): %s", engagement.id[:8], exc)
            else:
                engagement.sdk_session_id = sid


def _drop(lane: "_JobLane", ticket: "_Ticket") -> None:
    try:
        lane.queue.remove(ticket)
    except ValueError:
        pass


async def _close_client(client: Any, ctx: Any) -> None:
    """Disconnect a client: ``disconnect()`` (the SDK's ``_close_impl``),
    else the context manager's ``__aexit__``."""
    if hasattr(client, "disconnect"):
        await client.disconnect()
    elif ctx is not None and hasattr(ctx, "__aexit__"):
        await ctx.__aexit__(None, None, None)
    elif hasattr(client, "close"):
        await client.close()


def _transcript_uuids(rec: Any, session_id: str | None) -> set[str]:
    """Every host uuid the job's session transcript holds. Off the loop."""
    from engagement_transcript_reaper import transcript_cwd
    try:
        cwd = transcript_cwd(rec)
    except Exception:  # noqa: BLE001 — an unresolvable cwd holds nothing
        cwd = None
    return job_inbox.uuids_in_transcript(job_inbox.transcript_path(cwd, session_id))
