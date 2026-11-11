"""Engagement primitive — Tier 2 Specialists' and residents' background jobs.

Symmetric with :mod:`specialist_registry`. Owns:
- EngagementRecord (one in-flight engagement)
- EngagementRegistry (in-memory dict + ``/data/engagements.json``: in-flight
  records for crash recovery PLUS terminal tombstones, which age out after
  ``_TERMINAL_RETENTION_DAYS`` — D-4, v0.69.0)
- Idle sweep (session-suspends live clients idle past a day)
- Orphan recovery (startup: load the file; "active" rows are reconciled to
  idle — no driver survives a restart — and remain dormant until the next
  message for them; ``tools.reap_stale_engagements`` retires them after the
  reap TTL)

See docs/superpowers/specs/2026-04-22-3.5-plan2-engagement-primitive-design.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from atomic_io import atomic_write_json
from channels.tg_address import AddressError, TgAddress, is_route
from job_registry import CASA_SHUTDOWN_REASON
from sensitivity import TIERS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sweep constants
# ---------------------------------------------------------------------------

_SESSION_SUSPEND_IDLE_S = 86400
_IDLE_SWEEP_CRON = "0 8 * * *"            # daily 08:00 user TZ
# D-4 (v0.69.0): terminal tombstones stay on disk this long, then age out of
# the snapshot on the next write — bounds the file while keeping post-mortems
# working across restarts.
_TERMINAL_RETENTION_DAYS = 30
# The key, on the record's ``origin``, of the working directory the
# engagement's CLI was first opened in (its sessions' transcript folder). Set
# once, at the first open, persisted with the record, and read — never the
# current configuration — by everything that registers or reaps the
# engagement's transcripts (``engagement_transcript_reaper.transcript_cwd``).
LAUNCH_CWD_KEY = "launch_cwd"


def _usable_time(value: object) -> bool:
    """#1094: whether a persisted ``completed_at`` is a time retention can age.

    ``load()`` carries ``completed_at`` exactly as the tombstone holds it
    (INV-ENG-018), so it can be anything JSON decodes to. This is the test
    ``agent._replay_time_sentence`` applies to the same value — an int or
    float, not a bool, and finite — mirrored rather than imported, because
    ``agent`` imports this module; a test pins that the two agree. Unlike the
    renderer, it does not also require ``datetime.fromtimestamp`` to accept
    the number: a finite value outside that range still orders correctly
    against the cutoff, so retention uses it as the number it is.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:       # an int too large for a float, e.g. 10**400
        return False

# The keyboard posts a job leaves in the operator's direct chat.
JOB_POST_START = "start"
JOB_POST_QUESTION = "question"
JOB_POST_PARKED = "parked"


_JOB_POST_KINDS = (JOB_POST_START, JOB_POST_QUESTION, JOB_POST_PARKED)


def _post_address(value: object) -> TgAddress | None:
    """A stored ``[bot, chat, message]`` post as an address naming a
    message; ``None`` for anything else."""
    try:
        addr = TgAddress.from_json(value)
    except AddressError:
        return None
    return addr if addr.message_id is not None else None


def _dm_posts(value: object, engagement_id: object) -> list[TgAddress]:
    """A persisted ``dm_posts`` list, ``[[bot, chat, message], ...]``. An
    entry that does not name its bot, chat and message is refused, loudly:
    there is no default bot to file it under."""
    if not isinstance(value, list):
        return []
    posts: list[TgAddress] = []
    for item in value:
        addr = _post_address(item)
        if addr is None:
            logger.error("engagement %s: direct chat post %r names no bot, "
                         "chat and message; skipped", engagement_id, item)
            continue
        posts.append(addr)
    return posts


def _keyboard_posts(value: object,
                    engagement_id: object) -> list[tuple[TgAddress, str]]:
    """A persisted ``ui_posts`` list, ``[[bot, chat, message, kind], ...]``,
    as ``(address, kind)`` pairs. An entry without its bot is refused,
    loudly, like a ``dm_posts`` entry."""
    if not isinstance(value, list):
        return []
    posts: list[tuple[TgAddress, str]] = []
    for item in value:
        addr = (_post_address(item[:3])
                if isinstance(item, list) and len(item) == 4 else None)
        if addr is None or item[3] not in _JOB_POST_KINDS:
            logger.error("engagement %s: keyboard post %r names no bot, chat, "
                         "message and kind; skipped", engagement_id, item)
            continue
        posts.append((addr, item[3]))
    return posts


def _post_key(addr: TgAddress) -> tuple[int, int, int]:
    """The index key of one post. Telegram numbers messages per bot and per
    chat, so a message id alone, or a chat and message id, can name two
    bots' posts."""
    return (addr.bot_id, addr.chat_id, addr.message_id)


def _question_map(value: object) -> dict:
    """A persisted ``questions`` map: question id -> text and options."""
    if not isinstance(value, dict):
        return {}
    return {qid: {"text": str(q.get("text", "")),
                  "options": [str(o) for o in (q.get("options") or [])
                              if isinstance(o, str)]}
            for qid, q in value.items()
            if isinstance(qid, str) and isinstance(q, dict)}


def _new_question_id() -> str:
    """Eight random base32 characters (40 bits) — what a question's buttons
    carry, so a button can only ever answer the question it was posted with."""
    return base64.b32encode(secrets.token_bytes(5)).decode("ascii")


# v0.79.0 (§3): sentinel for the strict terminal transition's full-field
# snapshot — distinguishes "origin had no such key" from "key was None" so the
# rollback can DELETE a key the transition added rather than leaving it None.
_FIELD_MISSING = object()


@dataclass(eq=False)
class LaunchCancellation:
    """#698: a handle on one in-flight launch, held by the registry.

    Identity-keyed (``eq=False``): ``unregister_launch`` removes THIS handle,
    never "a handle for this engagement id", so a late unregister from an
    unwound launcher cannot drop a successor's registration."""

    engagement_id: str
    task: Any


# #326: the statuses no direct mutator may overwrite. try_transition_terminal
# is the single authoritative gate INTO this set; mark_idle/mark_error/
# mark_completed/mark_cancelled re-check membership under the lock so a
# finalizer that won during one of their callers' awaits is never overwritten.
_TERMINAL_STATUSES = ("completed", "cancelled", "error")


# Personality Task 10: origin dicts are snapshotted off ``agent.origin_var``,
# which — like every ContextVar-carried origin — may hold LIVE, non-JSON objects
# for in-process reads only (a SpeakerProvenance dataclass, the voice foreground
# handoff reservation, the progress sink, a capabilities frozenset). Those must
# never reach the tombstone's ``json.dumps``; the tombstone is a durable record
# of the engagement, and none of these live objects are meaningful once restored.
# Strip them at the serialization boundary (they stay on the in-memory
# ``rec.origin`` for live use). The route (``_tg_bot``) is NOT one of them:
# it persists with the origin, ``CASA`` included, so a job reloaded after a
# restart still delivers through the bot it was started on.
_NON_PERSISTABLE_ORIGIN_KEYS = frozenset({
    "speaker_provenance",
    "_voice_handoff_reservation",
    "_progress_sink",
    "voice_route_capabilities",
    "voice_deadline",
    # #1038: the live TurnScope; an engagement's own scope is re-minted from
    # the persisted record (``TurnScope.for_engagement``), never stored.
    "turn_scope",
})


def _persistable_origin(origin: dict[str, Any]) -> dict[str, Any]:
    """A JSON-safe copy of *origin* for tombstone persistence: drops the known
    live/non-serializable ContextVar-carried keys (see
    ``_NON_PERSISTABLE_ORIGIN_KEYS``)."""
    return {k: v for k, v in origin.items() if k not in _NON_PERSISTABLE_ORIGIN_KEYS}


def _restore_origin_field(rec: "EngagementRecord", key: str, snapped: Any) -> None:
    """Restore ``rec.origin[key]`` to a strict-transition snapshot value.

    ``_FIELD_MISSING`` means the key was ABSENT before the transition — the
    rollback removes it rather than resurrecting it as ``None``."""
    if snapped is _FIELD_MISSING:
        rec.origin.pop(key, None)
    else:
        rec.origin[key] = snapped


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass
class EngagementRecord:
    """One in-flight engagement.

    ``kind`` = "specialist" for a specialist's background job (or an interactive
    conversation an older install left open); "executor" for Tier 3; "plugin"
    for a resident-hosted plugin job. ``role_or_type`` is the specialist role (e.g. "finance") or the
    executor type (e.g. "configurator").

    ``status`` transitions:
      active ──first idle sweep past 24h──▶ idle
      active ──registry load after restart──▶ idle   (D-4 boot reconcile)
      idle    ──next user turn──▶ active
      active  ──emit_completion──▶ completed
      active  ──Cancel button / cancel_engagement──▶ cancelled
      active/idle ──reap sweep past ENGAGEMENT_REAP_DAYS──▶ cancelled  (D-4)
      active  ──resume twice failed / sweep orphan──▶ error
    """

    id: str
    kind: str
    role_or_type: str
    driver: str
    status: str
    started_at: float
    last_user_turn_ts: float
    completed_at: float | None
    sdk_session_id: str | None
    origin: dict[str, Any]
    task: str
    # C-1 v0.37.2: snapshot of executor's tools.allowed at engagement
    # creation.
    tools_allowed: tuple[str, ...] = ()
    # G-1 v0.37.7: snapshot of executor's permission_mode at engagement
    # creation.
    permission_mode: str = "acceptEdits"
    # §3.8: immutable snapshot of the resolved plugin artifacts this
    # engagement launched with — each {"name","artifact_id","path"}. A resume
    # rebuilds from THESE recorded paths, never a re-resolution of current
    # assignments. Preserved by every rewrite.
    plugin_artifacts: tuple[dict, ...] = ()
    # Set by a memory wipe (written into the record file while Casa is down)
    # on every live in_casa engagement: the wipe removed its session
    # transcripts, so there is nothing to resume. While True, every resume
    # path refuses to resume the recorded session and the tool choke points
    # refuse to bind the record (``tools._engagement_rebuild_fence``); the
    # job ends as one that could not be resumed. Never cleared.
    context_rebuild_pending: bool = False
    # Task 6 (spec §4.6): the concurrency Permit this specialist
    # engagement holds, if any (set by tools.py's
    # _launch_interactive_engagement right after `create()`, None for executor
    # engagements — they never acquire one). NOT persisted to the
    # tombstone (`_write_tombstone_locked` below lists fields explicitly)
    # — a live Permit cannot survive a restart; concurrency state is
    # memory-only and resets with the process. Released exactly once by
    # `_finalize_engagement` (the shared completion/cancel/reap funnel)
    # or, for a pre-finalize failure (driver start), inline at the
    # point of failure — see _launch_interactive_engagement.
    permit: Any = None
    # #283: the agent-spawn occupancy token (specialist_limits.SpawnToken)
    # this record holds when its origin carries ``_agent_spawned`` — a
    # SEPARATE field from ``permit`` (design r3: an agent-context specialist
    # engagement holds BOTH, and sharing the field would overwrite one or
    # leak the other). Memory-only like ``permit`` (never tombstoned; boot
    # reconciliation re-mints it via the limiter's restore()). Attached by
    # ``create()`` itself (design r4: ownership transfers at successful
    # create, never after driver start — a cancellation in that window must
    # not release a token whose marked record remains live). Released
    # exactly once by ``_release_permit`` on every terminal transition.
    agent_spawn_permit: Any = None
    # #766: this record's terminal outcome was committed by the FINALIZATION
    # FUNNEL, which announces it to the party that asked for the work — and
    # that telling has not yet been acknowledged. Set in the same durable write
    # as the terminal status (so a record can never be terminal and owe nothing
    # while the announcement has not happened), and cleared ONLY by the
    # delivery acknowledgement of the notice that carries it.
    #
    # The obligation follows the WRITER, not the record, which is why there is
    # no ``_owes_*`` predicate for it: ``_finalize_engagement`` and
    # ``tools._report_launch_death`` call ``try_transition_terminal`` with the
    # same outcome on the same kind of record, and only the first announces.
    # So the ONE caller that announces opts in explicitly and every other
    # terminal writer — including the three legacy mutators below — arms
    # nothing, by default rather than by remembering a guard.
    #
    # Exempt from the terminal-retention expiry: ageing the row out would drop
    # the obligation with it, leaving the party that asked for the work
    # permanently untold.
    terminal_notification_pending: bool = False
    # This in_casa record's CLI transcripts are all gone: the transcript
    # reaper found none left to delete (or none it could ever find). Until it
    # is set the record's tombstone does not age out — the record is the
    # reaper's only index of its sessions. Never cleared.
    transcripts_reaped: bool = False
    # A background job's posts in the operator's direct chat — its start line,
    # its questions and its plugin's delivered posts — oldest first. A reply
    # quoting one of them goes into the job while the record counts as live
    # (``EngagementRegistry.counts_as_live``). Unbounded while the job lives:
    # a job may run for days and every post it made stays
    # steerable. Kept past the end, with the tombstone, so a reply quoting
    # one of them after the job ended is known as a reply to this job
    # (``EngagementRegistry.ended_job_post_owner``); a post that lands after
    # the end is added too. Each is the post's ``TgAddress``: the bot that
    # posted it, the chat and the message (stored ``[bot, chat, message]``).
    dm_posts: list = field(default_factory=list)
    # The subset of those posts that carry a keyboard, each
    # ``(TgAddress, kind)`` (``JOB_POST_START``, ``JOB_POST_QUESTION`` or
    # ``JOB_POST_PARKED``; stored ``[bot, chat, message, kind]``). Kept past
    # the end until the keyboards are retired (``take_ui_posts``).
    ui_posts: list = field(default_factory=list)
    # The questions the job asked the operator, keyed by the random id their
    # buttons carry: ``{"text": str, "options": [str, ...]}``. Emptied by the
    # write that settles the terminal transition.
    questions: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TerminalPreconditionFailed(Exception):
    """G4 D2 (v0.96.0): raised by ``try_transition_terminal`` when the
    caller-supplied ``terminal_hook`` vetoes the terminal flip (e.g. unread
    operator inbound at completion time). The record is left LIVE and
    unmodified."""


def _session_working(driver: Any, engagement_id: str) -> bool:
    """A turn is running in the job's session, or Casa is recovering it."""
    for probe in ("turn_open", "recovering"):
        fn = getattr(driver, probe, None)
        try:
            if callable(fn) and fn(engagement_id) is True:
                return True
        except Exception:  # noqa: BLE001 — no reading, not working
            logger.debug("%s probe failed for %s", probe, engagement_id[:8],
                         exc_info=True)
    return False


class EngagementRegistry:
    """In-memory dict + ``/data/engagements.json`` tombstone.

    All mutation methods acquire ``self._lock`` and write the tombstone
    inside the lock, same pattern as SpecialistRegistry.register_delegation.
    """

    def __init__(
        self,
        *,
        tombstone_path: str,
        agent_spawn_limiter: Any | None = None,
    ) -> None:
        self._tombstone_path = tombstone_path
        # #698: the launch-cancellation ledger. All three are process-local,
        # synchronous and I/O-free — the shape ``JobRegistry.begin_shutdown``
        # already uses on the job ledger, which is the ledger this one is a
        # sibling of and deliberately not coupled to.
        self._launch_handles: dict[int, LaunchCancellation] = {}
        self._launch_causes: dict[str, str] = {}
        self._launch_shutdown: str = ""
        # #283: injected specialist_limits.AgentSpawnLimiter. Optional (None
        # keeps every existing construction site and test working); when
        # present, ``load()`` restores one occupancy token per live marked
        # record so a restart cannot refill the spawn pool while marked
        # engagements are still live. MUST be constructed and injected
        # BEFORE ``load()`` runs (design r3, Sol: casa_core's boot order).
        self._agent_spawn_limiter = agent_spawn_limiter
        self._records: dict[str, EngagementRecord] = {}
        # DM message id -> the job record that posted it, for every post of a
        # record that counts as live. Rebuilt from the records at ``load()``,
        # extended by ``record_job_post`` and emptied for a record when its
        # terminal transition settles.
        # Keyed ``(bot, chat, message)`` (``_post_key``).
        self._job_post_index: dict[tuple[int, int, int], str] = {}
        # #1522: (bot, chat, message) -> the ended job record that posted
        # it. Its posts move here from the index above when the terminal
        # transition settles; a post landing after that is filed here
        # directly. Rebuilt from terminal records at ``load()``, so a record
        # that aged out of the tombstone leaves it at the next boot.
        self._ended_post_index: dict[tuple[int, int, int], str] = {}
        self._lock = asyncio.Lock()
        # #588: strong refs for the background tombstone persists scheduled by
        # ``begin_turn_delivery`` (which may not await). Without a reference the
        # loop may drop a running task mid-write.
        self._deferred_persists: set[Any] = set()
        # #529 / #588 (Sol, diff review r3): whether the LAST tombstone write
        # settled successfully. Strict callers consult it to decide whether a
        # failure owes a rollback, so it must never be unset (AttributeError in
        # an except block) or carry a previous write's verdict.
        self._last_tombstone_ok = False
        # #1173: ids whose STRICT terminal transition is committed in memory
        # but whose write has not settled. Such a record has left
        # ``active_and_idle()`` and may still roll back to live, so it keeps
        # occupying its plugin for the job guards (``job_occupants``).
        # In-process only: never persisted, empty at boot.
        self._terminal_pending: set[str] = set()
        # #1053: told of each record that wins a terminal transition; see
        # ``_on_terminal_locked``. Optional.
        self._terminal_observer: Any | None = None
        # #1093: the records THIS process loaded owing a telling, each with
        # the status it was loaded with — what the boot replay may claim. Set
        # by ``load()`` only and never persisted; see ``records_owed_at_load``.
        self._owed_at_load: tuple[tuple[EngagementRecord, str], ...] = ()

    async def load(self) -> None:
        """Read the tombstone into memory. Called once at startup."""
        if not os.path.exists(self._tombstone_path):
            return
        try:
            with open(self._tombstone_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Engagement tombstone corrupt or unreadable (%s): %s — truncating",
                self._tombstone_path, exc,
            )
            try:
                with open(self._tombstone_path, "w", encoding="utf-8") as fh:
                    json.dump([], fh)
            except OSError:
                pass
            return
        if not isinstance(raw, list):
            logger.error(
                "Engagement tombstone %s is not a JSON array; truncating",
                self._tombstone_path,
            )
            try:
                with open(self._tombstone_path, "w", encoding="utf-8") as fh:
                    json.dump([], fh)
            except OSError:
                pass
            return
        reconciled_any = False
        # Keyed exactly like ``self._records``, so a duplicated id resolves to
        # the same surviving record here as there.
        loaded: dict[Any, EngagementRecord] = {}
        for row in raw:
            origin = row.get("origin") if isinstance(row, dict) else None
            if (isinstance(origin, dict) and origin.get("channel") == "telegram"
                    and not is_route(origin.get("_tg_bot"))):
                # A Telegram job delivers through the bot it was started
                # on; a record that does not name it has no bot to go to,
                # and none is guessed for it.
                logger.error(
                    "Skipping engagement %s: its Telegram origin has no valid "
                    "route (_tg_bot=%r)", row.get("id"), origin.get("_tg_bot"))
                continue
            try:
                rec = EngagementRecord(
                    id=row["id"],
                    kind=row["kind"],
                    role_or_type=row["role_or_type"],
                    driver=row["driver"],
                    status=row["status"],
                    started_at=float(row["started_at"]),
                    last_user_turn_ts=float(row["last_user_turn_ts"]),
                    completed_at=row.get("completed_at"),
                    sdk_session_id=row.get("sdk_session_id"),
                    origin=dict(row.get("origin") or {}),
                    task=row.get("task", ""),
                    tools_allowed=tuple(row.get("tools_allowed") or ()),
                    permission_mode=row.get("permission_mode") or "acceptEdits",
                    plugin_artifacts=tuple(row.get("plugin_artifacts") or ()),
                    context_rebuild_pending=bool(
                        row.get("context_rebuild_pending", False)),
                    # #766: a row written before this field existed owes
                    # NOTHING. The opposite default would re-announce every
                    # terminal engagement inside the 30-day tombstone window at
                    # the first boot after upgrade.
                    terminal_notification_pending=bool(
                        row.get("terminal_notification_pending", False)),
                    transcripts_reaped=bool(
                        row.get("transcripts_reaped", False)),
                    dm_posts=_dm_posts(row.get("dm_posts"), row.get("id")),
                    ui_posts=_keyboard_posts(row.get("ui_posts"),
                                             row.get("id")),
                    questions=_question_map(row.get("questions")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping malformed engagement row: %s", exc)
                continue
            # D-4 boot reconcile (v0.69.0): a record loaded as "active" claims
            # a live driver, but no driver survives a restart — the process
            # that ran it died with the old container. Idle is the truthful
            # state: dormant, resumable on the next message for it
            # (update_user_turn flips it back), and visible to the reap sweep.
            if rec.status == "active":
                rec.status = "idle"
                reconciled_any = True
                logger.info(
                    "boot reconcile: engagement %s active→idle "
                    "(no driver survives a restart)", rec.id[:8],
                )
            # #283 boot restore: every LIVE record whose origin carries the
            # agent-spawn marker re-occupies one limiter slot, held on the
            # record exactly as a fresh create would. restore() counts
            # unconditionally, so more live marked rows than the cap become
            # DEBT — new acquisition stays refused until terminal releases
            # drain below the cap (design r3: a saturating acquire would let
            # the first reap admit a spawn while cap+ marked rows stay live).
            if (self._agent_spawn_limiter is not None
                    and rec.status in ("active", "idle")
                    and rec.origin.get("_agent_spawned")):
                rec.agent_spawn_permit = self._agent_spawn_limiter.restore()
            self._records[rec.id] = rec
            loaded[rec.id] = rec
            if rec.status in ("active", "idle"):
                for addr in rec.dm_posts:
                    self._job_post_index[_post_key(addr)] = rec.id
            else:
                # An ended job's posts are kept so a reply to one is known as
                # a reply to this ended job; its questions are not
                # answerable any more (the settling write stores them empty).
                for addr in rec.dm_posts:
                    self._ended_post_index[_post_key(addr)] = rec.id
                rec.questions = {}

        # #1093: what the previous process left owed, captured here — before
        # any path that can arm an obligation in this process is reachable.
        self._owed_at_load = tuple(
            (r, r.status) for r in loaded.values()
            if r.terminal_notification_pending)

        # v0.69.6: persist the reconcile so the on-disk tombstone matches the
        # in-memory state immediately after boot. Without this the file kept
        # showing "active" until the next mutation, and the disk-reading
        # auditor (invariant E) saw the stale status. Only write when
        # something actually changed (no needless boot churn). load() runs
        # single-threaded during init, but take the lock for consistency with
        # every other tombstone write.
        if reconciled_any:
            async with self._lock:
                await self._write_tombstone_locked()

    def active_and_idle(self) -> list[EngagementRecord]:
        return [r for r in self._records.values() if r.status in ("active", "idle")]

    def job_occupants(self) -> list[EngagementRecord]:
        """#1173: the records that occupy a plugin for the job guards — the
        live ones, plus any whose strict terminal write is still pending,
        which a failed write would put back live. Synchronous, like
        ``active_and_idle``, so ``claim_job_start`` reads it and claims with
        no suspension between."""
        return [r for r in self._records.values()
                if r.status in ("active", "idle") or r.id in self._terminal_pending]

    def terminal_records(self) -> list[EngagementRecord]:
        """Terminal records, for the engagement transcript reaper."""
        return [
            r for r in self._records.values()
            if r.status in ("completed", "cancelled", "error")
        ]

    def get(self, engagement_id: str) -> EngagementRecord | None:
        return self._records.get(engagement_id)

    async def settled_status(self, engagement_id: str) -> str:
        """#692: the record's status, read UNDER the registry lock; ``""`` for
        an unknown id.

        :meth:`get` is lock-free by design and every other caller wants that.
        This one does not: ``try_transition_terminal``'s strict path commits
        the terminal fields IN MEMORY, then awaits the tombstone write, then
        FULL-FIELD restores them if that write fails — all inside
        ``self._lock``. A lock-free reader landing in that window sees
        ``completed`` for a transition that rolls back live.

        Nothing is awaited inside the critical section, so this adds no new
        suspension point to the lock's own holders. Callers must still bound
        their wait: the lock is held across a tombstone write, and a caller on
        a Telegram delivery task must not be able to wedge behind a wedged
        registry.
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            return "" if rec is None else rec.status

    def counts_as_live(self, engagement_id: str) -> bool:
        """Whether the record is live for routing and filing a job's direct
        chat posts: ``active`` or ``idle``, or terminal in memory while its
        strict terminal write is still pending — a write that fails puts it
        back live, so a reply routed into it in that window either reaches it
        after the rollback or is refused and told. Synchronous."""
        rec = self._records.get(engagement_id)
        return rec is not None and (
            rec.status in ("active", "idle")
            or engagement_id in self._terminal_pending)

    def terminal_pending(self, engagement_id: str) -> bool:
        """Whether the record's strict terminal write is committed in memory
        and not yet settled. Synchronous."""
        return engagement_id in self._terminal_pending

    async def settle_pending(self, engagement_id: str) -> None:
        """Wait until a pending strict terminal write of this record has
        settled — committed or rolled back. The strict transition holds the
        registry lock from its in-memory commit until its write settles, so
        taking the lock once is the wait. Returns at once when nothing is
        pending."""
        if engagement_id in self._terminal_pending:
            async with self._lock:
                pass

    def job_post_owner(self, addr: Any) -> "EngagementRecord | None":
        """The job record that filed the post at *addr* (a ``TgAddress``)
        as one of its direct chat posts, while that record counts as live;
        otherwise ``None``. Only the bot that posted the message names it: an
        equal message id on another bot is a different message."""
        if not isinstance(addr, TgAddress) or addr.message_id is None:
            return None
        engagement_id = self._job_post_index.get(_post_key(addr))
        if engagement_id is None or not self.counts_as_live(engagement_id):
            return None
        return self._records.get(engagement_id)

    def ended_job_post_owner(self, addr: Any) -> "EngagementRecord | None":
        """#1522: the ended job record that filed the post at *addr* (a
        ``TgAddress``); ``None`` for any other message, and for a record that
        still counts as live (``job_post_owner`` answers for it). A record
        that is no longer held answers ``None``."""
        if not isinstance(addr, TgAddress) or addr.message_id is None:
            return None
        key = _post_key(addr)
        # The live index too: a direct terminal mutator has flipped the
        # status before its write settles and the posts move.
        for engagement_id in (self._ended_post_index.get(key),
                              self._job_post_index.get(key)):
            rec = self._records.get(engagement_id) if engagement_id else None
            if (rec is not None and rec.status in _TERMINAL_STATUSES
                    and not self.counts_as_live(rec.id)
                    and isinstance(rec.origin.get("job"), dict)):
                return rec
        return None

    def record_job_post(self, engagement_id: str, addr: Any, *,
                        keyboard: str | None = None) -> bool:
        """File one landed direct chat post of a job, at *addr* (the
        ``TgAddress`` the posting endpoint gave it): append it to the
        record's ``dm_posts`` (and, with ``keyboard``, to ``ui_posts``),
        index it, and schedule the tombstone write. SYNCHRONOUS — the
        channel calls it from the step in which the post landed.

        ``False`` when the post was not filed as a live job's: an unknown
        record, one that is not a job, an address that names no message, or a
        record whose terminal transition has settled. A post of that last kind
        is still kept as the ended job's (``ended_job_post_owner``), but its
        keyboard is the caller's to retire, since the terminal-time
        retirement has already run (``take_ui_posts``). A record whose
        terminal write is still pending files the post as live, so a
        rollback leaves it steerable and a commit's retirement still takes
        its keyboard."""
        rec = self._records.get(engagement_id)
        if (rec is None or not isinstance(addr, TgAddress)
                or addr.message_id is None
                or not isinstance(rec.origin.get("job"), dict)):
            return False
        if not self.counts_as_live(engagement_id):
            # #1522: a job can end while one of its posts is still being
            # sent; a reply to that post is a reply to the ended job.
            if addr not in rec.dm_posts:
                rec.dm_posts.append(addr)
            self._ended_post_index[_post_key(addr)] = engagement_id
            self._schedule_tombstone_persist()
            return False
        if addr not in rec.dm_posts:
            rec.dm_posts.append(addr)
        self._job_post_index[_post_key(addr)] = engagement_id
        if keyboard is not None and not any(
                post[0] == addr for post in rec.ui_posts):
            rec.ui_posts.append((addr, keyboard))
        self._schedule_tombstone_persist()
        return True

    def take_ui_posts(self, engagement_id: str) -> list:
        """Take and clear the record's keyboard posts, as ``(TgAddress,
        kind)`` pairs, in one synchronous step (and schedule the write), so
        each keyboard is retired by one owner. Called for a terminal record
        only."""
        rec = self._records.get(engagement_id)
        if rec is None or not rec.ui_posts:
            return []
        posts, rec.ui_posts = rec.ui_posts, []
        self._schedule_tombstone_persist()
        return posts

    def records_with_ui_posts(self) -> list["EngagementRecord"]:
        """Terminal records that still carry keyboard posts — the boot
        retirement pass's work list. Each record's ``ui_posts`` are
        ``(TgAddress, kind)`` pairs."""
        return [r for r in self._records.values()
                if r.ui_posts and r.status in _TERMINAL_STATUSES]

    def new_job_question(self, engagement_id: str, text: str,
                         options: list[str]) -> str | None:
        """Record a question a live job is about to post and return its id:
        eight random base32 characters, drawn again until no question of this
        record holds them. SYNCHRONOUS; the write is scheduled, never awaited,
        so nothing stands between this and the post. ``None`` for a record
        that does not count as live."""
        rec = self._records.get(engagement_id)
        if rec is None or not self.counts_as_live(engagement_id):
            return None
        qid = _new_question_id()
        while qid in rec.questions:
            qid = _new_question_id()
        rec.questions[qid] = {"text": str(text),
                              "options": [str(o) for o in options]}
        self._schedule_tombstone_persist()
        return qid

    def _settle_job_posts(self, rec: "EngagementRecord") -> None:
        """A record whose terminal transition just settled moves its posts
        from the job-post index to the ended-post index (#1522) and forgets
        its questions. The write that settled it already stored the questions
        empty (``_write_tombstone_locked`` writes a terminal record's
        questions empty) and kept the posts, so memory now matches disk."""
        for addr in rec.dm_posts:
            key = _post_key(addr)
            if self._job_post_index.get(key) == rec.id:
                del self._job_post_index[key]
            self._ended_post_index[key] = rec.id
        rec.questions = {}

    async def promote_from_quiet(self, engagement_id: str) -> bool:
        """End a quiet run's quietness because the operator interacted with it
        (it asked, or they replied or tapped). Under the registry lock, which
        also waits out a pending strict terminal write; STRICT — the stamp is
        rolled back when its write fails.

        ``True`` when the record is live and not quiet afterwards (including
        one that never was). ``False`` when the interaction must be refused:
        the record is unknown or terminal — a terminal job receives nothing,
        so a quiet completed end stays told to no one — or the write failed.
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or rec.status not in ("active", "idle"):
                return False
            job = rec.origin.get("job")
            if not isinstance(job, dict) or job.get("quiet") is not True:
                return True
            job["quiet"] = False
            try:
                await self._write_tombstone_locked(strict=True)
            except asyncio.CancelledError:
                if not self._last_tombstone_ok:
                    job["quiet"] = True
                raise
            except Exception as exc:  # noqa: BLE001 — refused, rolled back
                job["quiet"] = True
                logger.warning("engagement %s: ending a quiet run's quietness "
                               "could not be persisted: %s",
                               engagement_id[:8], exc)
                return False
            return True

    # -- Persist helper ---------------------------------------------------

    async def _write_tombstone_locked(self, *, strict: bool = False) -> None:
        """Caller MUST hold self._lock.

        Terminal records are persisted as real tombstones (D-4, v0.69.0) —
        they used to be silently dropped, so the file never matched its
        name. Tombstones age out after ``_TERMINAL_RETENTION_DAYS`` to bound
        the file.

        ``strict`` (B3, Sol r1): when True, a persistence failure PROPAGATES
        instead of being swallowed — used by the strict mutators, whose
        in-memory change must match disk. All other callers keep the
        best-effort warn-and-continue semantics (strict=False).

        #529 settle-on-cancel: the ``to_thread`` write cannot be interrupted,
        only abandoned — and an abandoned write's ``os.replace`` can land a
        STALE snapshot over a newer writer's after this caller's lock
        releases. A cancellation arriving while the write is in flight is
        therefore absorbed until the write SETTLES, then re-raised.
        ``self._last_tombstone_ok`` records whether the settled write
        committed (True) or failed (False) so a strict caller unwinding on
        CancelledError knows whether its in-memory mutation matches disk;
        callers hold ``self._lock``, so the flag cannot be clobbered between
        this return/raise and their read.
        """
        # Reset BEFORE the snapshot is built (Sol, diff review r3): a raise
        # during construction would otherwise leave the previous write's
        # verdict in place, and a strict caller consulting it would skip a
        # rollback it owes — no write happened at all.
        self._last_tombstone_ok = False
        cutoff = time.time() - _TERMINAL_RETENTION_DAYS * 86400
        snapshot = []
        for rec in self._records.values():
            if (rec.status in ("completed", "cancelled", "error")
                    # #1094: only a usable time is compared. The value is
                    # carried as persisted, and a string, list or dict here
                    # raised TypeError while the snapshot was being built —
                    # before the write's own failure handling — so every
                    # write failed, best-effort ones included. A record whose
                    # time is not usable has no known age and is RETAINED,
                    # as None, NaN and 10**400 always were.
                    and _usable_time(rec.completed_at)
                    and rec.completed_at < cutoff
                    # #766: nor is a record that still owes its engager the
                    # telling of this very outcome. Expiring it would discard
                    # the obligation AND the origin the replay is addressed
                    # from, so the party that asked for the work would be
                    # permanently untold — the harm the marker exists to close.
                    and not rec.terminal_notification_pending
                    # An in_casa record is the transcript reaper's only index
                    # of its sessions: it ages out only once the reaper has
                    # found nothing left to delete (whatever downtime came
                    # between), never at a fixed age alone.
                    and (rec.driver != "in_casa" or rec.transcripts_reaped)):
                continue
            snapshot.append({
                "id": rec.id,
                "kind": rec.kind,
                "role_or_type": rec.role_or_type,
                "driver": rec.driver,
                "status": rec.status,
                "started_at": rec.started_at,
                "last_user_turn_ts": rec.last_user_turn_ts,
                "completed_at": rec.completed_at,
                "sdk_session_id": rec.sdk_session_id,
                "origin": _persistable_origin(rec.origin),
                "task": rec.task,
                "tools_allowed": list(rec.tools_allowed),
                "permission_mode": rec.permission_mode,
                "plugin_artifacts": [dict(pa) for pa in rec.plugin_artifacts],
                "context_rebuild_pending": rec.context_rebuild_pending,
                "terminal_notification_pending":
                    rec.terminal_notification_pending,
                "transcripts_reaped": rec.transcripts_reaped,
                # A terminal record's questions are stored empty: the write
                # that commits the terminal status is the one that drops them
                # (``_settle_job_posts`` then empties memory). Its posts are
                # kept (#1522), so a reply to one names the ended job.
                "dm_posts": [addr.to_json() for addr in rec.dm_posts],
                "ui_posts": [[*addr.to_json(), kind]
                             for addr, kind in rec.ui_posts],
                "questions": ({} if rec.status in _TERMINAL_STATUSES
                              else {qid: dict(q)
                                    for qid, q in rec.questions.items()}),
            })
        write = asyncio.ensure_future(
            asyncio.to_thread(self._write_tombstone, snapshot),
        )
        cancelled: asyncio.CancelledError | None = None
        while not write.done():
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError as exc:
                cancelled = exc          # absorbed; keep waiting for settle
            except Exception:  # noqa: BLE001 — retrieved below
                break
        exc = None if write.cancelled() else write.exception()
        self._last_tombstone_ok = exc is None
        if cancelled is not None:
            raise cancelled
        if exc is not None:
            if strict:
                raise exc
            logger.warning("Failed to persist engagement tombstone: %s", exc)

    def _write_tombstone(self, snapshot: list[dict[str, Any]]) -> None:
        # Atomic (temp-file + fsync + os.replace): a crash mid-write must not
        # lose all in-flight engagement state to a truncated tombstone (M15).
        # #335: 0600 — every row carries that engagement's origin, so this file
        # must not be world-readable. Passing the mode explicitly also MIGRATES
        # a pre-#335 0644 file on the first write after upgrade.
        atomic_write_json(
            self._tombstone_path, snapshot, indent=2, mode=0o600)

    # -- Mutators ---------------------------------------------------------

    async def create(
        self,
        kind: str,
        role_or_type: str,
        driver: str,
        task: str,
        origin: dict[str, Any],
        tools_allowed: tuple[str, ...] | list[str] = (),
        permission_mode: str = "acceptEdits",
        plugin_artifacts: tuple[dict, ...] | list[dict] = (),
        agent_spawn_permit: Any = None,
    ) -> EngagementRecord:
        # #283 belt-and-suspenders: a record whose origin carries the
        # agent-spawn marker must arrive WITH its reservation — the call
        # sites acquire before any external side effect; a marked create
        # without a token means a coding error upstream, and admitting it
        # would run an uncounted engagement. Fail closed. (No limiter wired
        # ⇒ the cap feature is off for this construction site — tests,
        # legacy callers — and the guard stays quiet.)
        if (self._agent_spawn_limiter is not None
                and (origin or {}).get("_agent_spawned")
                and agent_spawn_permit is None):
            raise ValueError(
                "agent-spawned engagement created without a spawn "
                "reservation (#283)")
        engagement_id = uuid.uuid4().hex
        now = time.time()
        rec = EngagementRecord(
            id=engagement_id,
            kind=kind,
            role_or_type=role_or_type,
            driver=driver,
            status="active",
            started_at=now,
            last_user_turn_ts=now,
            completed_at=None,
            sdk_session_id=None,
            origin=dict(origin),
            task=task,
            tools_allowed=tuple(tools_allowed),
            permission_mode=permission_mode or "acceptEdits",
            plugin_artifacts=tuple(dict(pa) for pa in plugin_artifacts),
            # #283 (design r4): ownership transfers HERE, at create — once
            # this call returns successfully, terminal transitions own the
            # release. If create raises (rollback ran, no record exists),
            # the caller's lexical finally still owns it; SpawnToken.release
            # is idempotent so the overlap is safe.
            agent_spawn_permit=agent_spawn_permit,
        )
        async with self._lock:
            self._records[engagement_id] = rec

            # #326: STRICT persistence — the record's whole point is crash
            # recovery, so reporting success after a swallowed write failure
            # hands the caller a running workspace that a restart will
            # neither recover nor reap. On failure the in-memory insert is
            # rolled back and the error propagates (the engage/delegate call
            # sites already surface launch-path failures as tool errors).
            # Shield-and-await like the sibling strict mutators so a caller
            # cancelled mid-``to_thread`` cannot tear memory from disk.
            def _rollback() -> None:
                self._records.pop(engagement_id, None)

            async def _persist() -> None:
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    _rollback()
                    raise

            async def _settle_despite_cancel(fut: "asyncio.Future") -> None:
                """Wait for *fut* to finish, absorbing ANY number of caller
                cancellations (a to_thread write cannot be interrupted, only
                abandoned — and an abandoned commit is exactly the ghost this
                path exists to prevent). Retrieves the result so a failure is
                never left 'never retrieved'; the caller re-raises its own
                cancellation afterwards."""
                while not fut.done():
                    try:
                        await asyncio.shield(fut)
                    except asyncio.CancelledError:
                        continue          # cancelled again — keep waiting
                    except Exception:  # noqa: BLE001 — retrieved below
                        break
                if not fut.cancelled():
                    fut.exception()

            task = asyncio.ensure_future(_persist())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Terra r2-1/r3-1: a cancelled caller never receives this
                # record, so a COMMITTED persist would strand a durable
                # active engagement with no driver. The persist must first
                # SETTLE — through repeated cancellation — before we can
                # know whether to compensate; then the rollback + removal
                # persist runs to completion the same way, all under the
                # lock. A failed undo write leaves only an on-disk ghost
                # row, which boot reconcile + the reap TTL already retire.
                await _settle_despite_cancel(task)
                if not task.cancelled() and task.exception() is None:
                    _rollback()
                    undo = asyncio.ensure_future(self._write_tombstone_locked())
                    await _settle_despite_cancel(undo)
                    if not undo.cancelled() and undo.exception() is not None:
                        logger.warning(
                            "engagement create: compensating tombstone write "
                            "failed after cancellation — on-disk ghost row "
                            "until boot reconcile/reap",
                        )
                raise
        logger.info(
            "Engagement %s created (kind=%s role_or_type=%s)",
            engagement_id[:8], kind, role_or_type,
        )
        return rec

    @staticmethod
    def _release_permit(rec: "EngagementRecord") -> None:
        """Task 6 (spec §4.6): release the specialist concurrency permit this
        record holds, if any. Called synchronously by EVERY terminal
        transition below (right after the status change, BEFORE tombstone
        I/O) so a leaked permit can never outlive its engagement — including
        direct ``mark_error`` routes (resume/orphan failures in
        channels/telegram.py) that bypass ``_finalize_engagement``.
        ``Permit.release()`` is idempotent, so ``_finalize_engagement``'s
        own release (and re-entrant terminal calls) are safe no-ops.
        Executor engagements carry ``permit=None`` → guarded no-op."""
        # #283: BOTH permit fields release here (design r3: an agent-context
        # specialist engagement holds a SpecialistLimiter permit AND an
        # agent-spawn token; releasing only one wedges or overspends the
        # other pool). Each release is idempotent.
        for field in ("permit", "agent_spawn_permit"):
            permit = getattr(rec, field, None)
            if permit is not None:
                try:
                    permit.release()
                except Exception:  # noqa: BLE001 — a bookkeeping release must never break a terminal transition
                    logger.warning("engagement %s %s release raised",
                                   rec.id[:8], field, exc_info=True)

    def set_terminal_observer(self, observer) -> None:
        """#1053: wire a ``(record) -> None`` callable told of every record
        that wins a terminal transition in this process (tools, once)."""
        self._terminal_observer = observer

    def _on_terminal_locked(self, rec: "EngagementRecord") -> None:
        """Everything owed by a record that JUST won a terminal transition in
        this process: its job posts leave the index, then the #1053 observer.

        The strict path calls this after a DURABLE write, so a strict
        transition that rolled back — which leaves the record live — tells
        nobody; the direct mutators call it in a ``finally``, which also covers
        a non-strict write that failed (that contract leaves the in-memory flip
        standing). Under ``self._lock`` and MUST NOT await: the observer may
        only schedule, and it can never fail the transition that called it.
        On the strict path it runs before the record leaves
        ``_terminal_pending``, so a post filed while the write was pending is
        among the keyboards the observer takes."""
        self._settle_job_posts(rec)
        observer = getattr(self, "_terminal_observer", None)
        if observer is None:
            return
        try:
            observer(rec)
        except Exception:  # noqa: BLE001 — never fail a terminal transition
            logger.warning("engagement terminal observer failed for %s",
                           rec.id, exc_info=True)

    # ------------------------------------------------------------------
    # #698 — the launch-cancellation ledger
    # ------------------------------------------------------------------
    #
    # INV-JOB-009's doctrine, applied to this ledger rather than folded into
    # it: *the discriminator is the cause of the settling, and cause is
    # carried, not inferred*. The stop writes the cause onto the exact row it
    # is about BEFORE issuing that row's cancellation, and the terminal writers
    # below stamp it inside the registry — never at a call site, and never by
    # classifying the shape of a cancellation. A global "we are stopping" flag
    # read at the cancellation arms is the shape that doctrine rejects by name:
    # it misattributes a creator/barge-in cancel that lands in the same window.
    #
    # Synchronous, no I/O, idempotent — ``JobRegistry.begin_shutdown``'s shape,
    # not a coupling to it. The two ledgers stay separate (#689 declined to
    # mint that cross-ledger read); only the reason STRING is shared, imported
    # rather than re-minted.

    def register_launch(self, engagement_id: str, task: Any) -> LaunchCancellation:
        """Enrol an in-flight launch. Returns the handle to unregister with.

        Latch-aware: when the stop has already begun, this performs the
        canceller's own step for the launch it is enrolling — write the cause
        FIRST, then cancel, synchronously, in that order — because a launch
        that becomes durable after the latch would otherwise get no cause and
        therefore the ordinary destructive rollback.
        """
        handle = LaunchCancellation(engagement_id=engagement_id, task=task)
        self._launch_handles[id(handle)] = handle
        if self._launch_shutdown:
            self._launch_causes[engagement_id] = self._launch_shutdown
            try:
                task.cancel()
            except Exception:  # noqa: BLE001 — enrolment must never raise
                logger.warning("register_launch: cancel of %s failed",
                               engagement_id[:8], exc_info=True)
        return handle

    def unregister_launch(self, handle: LaunchCancellation | None) -> None:
        """Drop a handle by IDENTITY. Does NOT clear the cause.

        The reporter's strict flip runs in a separate task that can outlive the
        launcher's ``finally``, so a cause forgotten between the two is an
        annotation lost from the durable row. The map is written only by the
        stop, only for rows the stop cancelled, and the process ends
        immediately afterwards, so it cannot go stale.
        """
        if handle is not None:
            self._launch_handles.pop(id(handle), None)

    def launch_in_flight(self, engagement_id: str) -> bool:
        """SYNCHRONOUS: is this engagement's launch still enrolled — from the
        committed create until the launcher's ``finally``? A job's delivery
        worker writes nothing for it meanwhile: the launch owns the job."""
        return any(h.engagement_id == engagement_id
                   for h in self._launch_handles.values())

    def launch_cause(self, engagement_id: str) -> str:
        """The carried cause for this launch, or ``""`` when it carries none."""
        return self._launch_causes.get(engagement_id, "")

    def launch_shutdown_active(self) -> bool:
        """Design 2026-09-15 §2.A: has the graceful stop latched? Read
        SYNCHRONOUSLY by the launch handoff before it mints an owner task — a
        task minted after the latch is one the stop's drains never see."""
        return bool(self._launch_shutdown)

    def mark_launch_drains_complete(self) -> None:
        """§2.A: set by the stop as its last act after every launch drain; a
        never-started owner whose done callback fires after this mints
        nothing and logs instead (the interpreter's final sweep is next)."""
        self._launch_drains_complete = True

    def launch_drains_complete(self) -> bool:
        return bool(getattr(self, "_launch_drains_complete", False))

    def begin_launch_shutdown(self, reason: str = CASA_SHUTDOWN_REASON) -> int:
        """Latch the stop, then cause-then-cancel every enrolled launch.

        Returns the number of cancellations ISSUED — a count, not a status, so
        a caller can log what it actually did.

        A task already done, or already cancelling, is neither relabelled nor
        re-cancelled: that is what keeps a concurrent creator/barge-in cancel
        from ever being recorded as a stop, and it is why this needs no
        argument about a race window. ``Task.cancelling()`` is read ONLY here,
        to decide whether this is the first cancellation; nothing downstream
        ever classifies by it.
        """
        self._launch_shutdown = reason or CASA_SHUTDOWN_REASON
        issued = 0
        for handle in list(self._launch_handles.values()):
            task = handle.task
            try:
                if task.done() or task.cancelling():
                    continue
            except Exception:  # noqa: BLE001 — a foreign task-like object
                continue
            # Cause FIRST, then the cancellation it explains.
            self._launch_causes[handle.engagement_id] = self._launch_shutdown
            try:
                task.cancel()
            except Exception:  # noqa: BLE001 — the stop must complete
                logger.warning("begin_launch_shutdown: cancel of %s failed",
                               handle.engagement_id[:8], exc_info=True)
                continue
            issued += 1
        return issued

    async def drain_launches(self) -> None:
        """Wait for every enrolled launch to finish unwinding.

        Bounded by a NO-OUTPUT condition, never by an elapsed budget (D21):
        it loops snapshot-then-gather until a snapshot is empty. Termination is
        by construction — a launch's unwind is bounded by the driver's own
        rollback and each handle is dropped by its launcher's ``finally``, so a
        completed launch never re-enters a later snapshot.
        """
        while True:
            tasks = [h.task for h in self._launch_handles.values()
                     if not h.task.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def mark_transcripts_reaped(self, engagement_ids) -> None:
        """The transcript reaper found nothing left to delete for these
        terminal records: their tombstones may now age out. Best-effort
        persistence — a failed write leaves the mark for the next pass to
        set again."""
        async with self._lock:
            changed = False
            for engagement_id in engagement_ids:
                rec = self._records.get(engagement_id)
                if (rec is not None and not rec.transcripts_reaped
                        and rec.status in ("completed", "cancelled", "error")):
                    rec.transcripts_reaped = True
                    changed = True
            if changed:
                await self._write_tombstone_locked()

    def records_owing_terminal_notification(self) -> list["EngagementRecord"]:
        """#766: every terminal outcome whose engager has not been told yet —
        the boot replay owner's work list."""
        return [r for r in self._records.values()
                if r.terminal_notification_pending]

    def records_owed_at_load(self):
        """#1093: the boot replay's work list — each record this process
        LOADED owing a telling that, as it is yielded, still owes it and still
        has the status it was loaded with.

        A record armed by this process is not in it: that process told it
        live, and if that notice is never delivered the obligation is still on
        disk for the NEXT boot's load. The status test covers the one loaded
        record that can change status — an owed row that is not terminal, a
        corrupt shape — so a live finalization of it is not re-announced as a
        pre-restart outcome; such a row whose status changed during this boot
        is left to the next boot. Lazy: the tests run per record as the
        replay reaches it, so an acknowledgement landing mid-walk is seen.
        """
        for rec, status in self._owed_at_load:
            if rec.terminal_notification_pending and rec.status == status:
                yield rec

    async def ack_terminal_notification(self, engagement_id: str) -> None:
        """#766: discharge the obligation — ONLY once a telling was delivered.

        Called from the announcement's ``on_delivery`` callback, which the
        consuming resident invokes at its transport-confirmed delivery seam
        (``agent.Agent._ack_delivery``); never on the bus accepting a message.

        Best-effort persistence: a failed write leaves memory not-owed and disk owed, which costs one
        duplicate announcement at the next boot. The refused direction is a
        LOST notice, and that is not this one.

        A no-op when this record does not owe, so a retried or duplicated
        acknowledgement cannot clear an obligation armed by a later terminal.
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or not rec.terminal_notification_pending:
                return
            rec.terminal_notification_pending = False
            await self._write_tombstone_locked()

    async def mark_completed(self, engagement_id: str, completed_at: float) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or rec.status in _TERMINAL_STATUSES:
                return  # #326: never overwrite a terminal winner
            rec.status = "completed"
            rec.completed_at = completed_at
            self._release_permit(rec)
            try:
                await self._write_tombstone_locked()
            finally:
                # #599 (Sol, diff review): the write can SETTLE and then re-raise
                # a cancellation, which would skip this and leave a durably
                # terminal record owing a kill with no in-process owner.
                self._on_terminal_locked(rec)

    async def mark_cancelled(self, engagement_id: str) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or rec.status in _TERMINAL_STATUSES:
                return  # #326: never overwrite a terminal winner
            rec.status = "cancelled"
            rec.completed_at = time.time()
            self._release_permit(rec)
            try:
                await self._write_tombstone_locked()
            finally:
                self._on_terminal_locked(rec)

    def _stamp_shutdown_reason(self, rec: EngagementRecord) -> None:
        """#698: the ONE stamper. Every terminal write inside this registry
        carries the launch's recorded cause, at flip time.

        No call site passes it, which is INV-JOB-009's placement — *enforced
        inside the registry, never at the call sites* — and it is what makes a
        ``mark_error`` that wins the row inside a rollback carry the stop signal
        beside its own kind. D32: both facts travel, and neither overwrites the
        other, so ``error_kind`` is never substituted.

        ORDERED, not COMPLETE, and the invariant says so: a terminal write that
        commits before the stop records its cause carries only the failure it
        was made for. Closing that would mean reconciling a cause that arrives
        mid-persist, which is a third mechanism for one hazard.
        """
        cause = self._launch_causes.get(rec.id, "")
        if cause:
            rec.origin["shutdown_reason"] = cause

    async def mark_error(self, engagement_id: str, kind: str, message: str) -> bool:
        """Returns True when THIS call flipped the record to ``error`` — like
        ``try_transition_terminal``, only the winner may run terminal side
        effects. False for an unknown record or one a
        concurrent finalizer already committed terminal (#326): a failed
        resume racing a /cancel must not overwrite ``cancelled`` with
        ``error`` and run duplicate cleanup."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or rec.status in _TERMINAL_STATUSES:
                return False
            rec.status = "error"
            rec.completed_at = time.time()
            rec.origin["error_kind"] = kind
            rec.origin["error_message"] = message
            self._stamp_shutdown_reason(rec)
            self._release_permit(rec)
            try:
                await self._write_tombstone_locked()
            finally:
                self._on_terminal_locked(rec)
            return True

    async def try_transition_terminal(
        self,
        engagement_id: str,
        outcome: str,  # "completed" | "cancelled" | "error"
        *,
        completed_at: float | None = None,
        error_kind: str = "",
        error_message: str = "",
        stale_before: float | None = None,
        strict: bool = False,
        terminal_hook=None,
        owes_terminal_notification: "bool | Callable[[EngagementRecord], bool]" = False,
    ) -> bool:
        """Atomically move a record to a terminal status. Returns True only
        for the first caller; False if missing or already terminal.

        L75/L24: emit_completion's fast-path terminal check and
        _finalize_engagement's registry write are separated by real
        suspension points (e.g. a forced-reload await), so a concurrent
        /cancel can race between them. This method is the single
        authoritative gate — only the first caller to flip the record
        terminal may run finalize side effects (DelegationComplete
        NOTIFICATION, summary retain).

        ``stale_before`` (reap, v0.69.6): win ONLY if ``last_user_turn_ts`` is
        still older than the cutoff. The reap checks staleness before this
        call at a suspension point away; without this guard a user turn that
        revives the record in that window would still be reaped.

        ``owes_terminal_notification`` (#766): arm this record's durable
        obligation to tell the party that asked for the work, in the SAME write
        that commits the terminal status. An explicit keyword rather than a
        predicate over the record, because the obligation follows the WRITER:
        the finalization funnel and ``tools._report_launch_death`` reach this
        method with the same outcome on the same kind of record, and only the
        first announces over the bus. Defaulting to False is what makes every
        other terminal writer correct without a guard to remember. Never
        CLEARED here — only ``ack_terminal_notification`` clears it — so a
        second transition cannot silently discharge an outstanding telling.
        It may also be a callable taking the record: it is then evaluated
        inside this critical section, after ``terminal_hook`` and before the
        flip, so a decision that depends on the record (a quiet run's stamp,
        which an operator's interaction ends under this same lock) is read
        from the record as the transition commits it.

        ``strict`` (v0.79.0 §3, Sol r6-2/r7-2): the finalize path uses STRICT
        transactional persistence. Non-strict callers keep the historical
        best-effort behavior (a tombstone write failure is swallowed and the
        in-memory flip stands, which could leave an ended job with no
        terminal record for boot reconciliation to find). Strict snapshots
        EVERY field the transition mutates (status, completed_at, and the
        error metadata on ``origin``) and, on tombstone-write failure, restores
        the FULL snapshot and re-raises — so a persistence failure leaves the
        record exactly as it was (live), never a memory/disk split. The
        mutate+persist runs under a shield-and-await so cancellation during ``to_thread``
        cannot tear the pair.
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or rec.status in ("completed", "cancelled", "error"):
                return False
            if stale_before is not None and rec.last_user_turn_ts >= stale_before:
                # Revived since the reap snapshot — never cancel a live engagement.
                return False
            # G4 D2 (v0.96.0): caller-supplied SYNCHRONOUS terminal hook,
            # evaluated inside this critical section with NO suspension
            # between check and flip — the atomic completion precondition
            # (unread-inbound gate) and/or the unread-text snapshot for the
            # no-silent-loss annotation. A non-None return VETOES the flip.
            if terminal_hook is not None:
                abort_reason = terminal_hook()
                if abort_reason is not None:
                    raise TerminalPreconditionFailed(abort_reason)
            if callable(owes_terminal_notification):
                owes_terminal_notification = bool(owes_terminal_notification(rec))
            new_status = (
                outcome if outcome in ("completed", "cancelled") else "error"
            )
            new_completed = (
                completed_at if completed_at is not None else time.time()
            )
            if not strict:
                rec.status = new_status
                rec.completed_at = new_completed
                if new_status == "error":
                    rec.origin["error_kind"] = error_kind or "emit_completion_error"
                    rec.origin["error_message"] = error_message
                self._stamp_shutdown_reason(rec)
                # Task 6 (spec §4.6): release the specialist engagement's
                # concurrency permit on this terminal transition (no-op for
                # executor engagements, permit=None). Safe before the write:
                # the non-strict path has no rollback, so the record is
                # committed-terminal in memory regardless of persist outcome.
                # #766: same write as the status. Set, never cleared.
                if owes_terminal_notification:
                    rec.terminal_notification_pending = True
                self._release_permit(rec)
                try:
                    await self._write_tombstone_locked()
                finally:
                    self._on_terminal_locked(rec)
                return True

            # STRICT: full-field snapshot + shield-and-await + rollback-on-fail.
            snap_status = rec.status
            snap_completed = rec.completed_at
            snap_error_kind = rec.origin.get("error_kind", _FIELD_MISSING)
            snap_error_message = rec.origin.get("error_message", _FIELD_MISSING)
            snap_shutdown = rec.origin.get("shutdown_reason", _FIELD_MISSING)
            snap_owes_notification = rec.terminal_notification_pending

            def _restore() -> None:
                rec.status = snap_status
                rec.completed_at = snap_completed
                _restore_origin_field(rec, "error_kind", snap_error_kind)
                _restore_origin_field(rec, "error_message", snap_error_message)
                # #698: a rolled-back persist leaves NO partial annotation —
                # the stop signal goes back with the rest of the snapshot.
                _restore_origin_field(rec, "shutdown_reason", snap_shutdown)
                # #766: a rolled-back transition leaves the record LIVE, and a
                # live record owes nothing. A transition that did not
                # reach disk announced nothing, so it owes nothing; leaving the
                # bit set would have boot replay announce an outcome that never
                # happened, to a record that is still live.
                rec.terminal_notification_pending = snap_owes_notification

            # #588 (Sol, diff review): commit the terminal fields in memory
            # HERE, synchronously, before any suspension point. They used to be
            # set inside the task below — but ``asyncio.ensure_future`` only
            # SCHEDULES a coroutine, so its first line ran at the ``await
            # asyncio.shield(task)``, leaving a window between the snapshot and
            # the mutation. ``begin_turn_delivery`` reads (and writes) status
            # WITHOUT this lock by design, so in that window it observed the
            # pre-transition status, admitted a turn and flipped the record
            # ``active`` — and ``_restore()`` then put the snapshotted status
            # back, leaving a delivered turn running against a record whose
            # status says otherwise. That is precisely the authority loss this
            # release fixes, re-entering through the rollback path. Committing
            # first means a lock-free reader in that window sees the TERMINAL
            # status and declines to deliver, so the restore below has nothing
            # to overwrite.
            rec.status = new_status
            rec.completed_at = new_completed
            # #766: committed HERE, with the terminal fields and before any
            # suspension point, so the one durable write below carries both
            # facts. Arming after the write would leave a crash window in which
            # the outcome is committed and nobody owes the telling — the exact
            # window the marker exists to close.
            if owes_terminal_notification:
                rec.terminal_notification_pending = True
            if new_status == "error":
                rec.origin["error_kind"] = error_kind or "emit_completion_error"
                rec.origin["error_message"] = error_message
            self._stamp_shutdown_reason(rec)
            # #1173: from this synchronous commit until the write below
            # settles, the record still occupies its plugin for the job guards.
            self._terminal_pending.add(rec.id)

            async def _mutate_and_persist() -> bool:
                try:
                    return await _persist()
                finally:
                    # After the restore (a failed write leaves the record live)
                    # or the durable commit; before the caller resumes.
                    self._terminal_pending.discard(rec.id)

            async def _persist() -> bool:
                try:
                    await self._write_tombstone_locked(strict=True)
                # BaseException, not Exception (Sol, diff review r2): the
                # rollback must be cancellation-complete. Committing the fields
                # above — before the task exists — means EVERY later failure
                # path owes a restore, and a `CancelledError` (a repeated
                # cancellation reaches the write through the gather below) is
                # not an `Exception`, so it used to escape leaving memory
                # terminal while disk still said live — the INV-ENG-002 split
                # this path exists to prevent. Guarded by
                # ``_last_tombstone_ok`` (#529): a cancellation whose settled
                # write COMMITTED keeps the mutation, because memory and disk
                # already agree.
                except BaseException:
                    if not self._last_tombstone_ok:
                        _restore()
                    else:
                        # #599: the write SETTLED and a cancellation then landed
                        # (``_write_tombstone_locked`` re-raises it after
                        # recording the verdict). The record is durably terminal
                        # and durably owing, so its discharge must be scheduled
                        # before unwinding — the same hole diff review found in
                        # the direct mutators, in the path the finalize funnel
                        # itself takes.
                        self._release_permit(rec)
                        self._on_terminal_locked(rec)
                    raise
                # Task 6 (spec §4.6): release the permit ONLY after the
                # terminal status is durably committed — the strict path can
                # roll the status back to live on a persist failure, and
                # releasing a still-live engagement's permit would free its
                # scope slot while the specialist engagement is still running.
                self._release_permit(rec)
                # #599: the obligation is now durable, so the discharge may
                # start. Inside the persist task (not after the shield below)
                # because a cancellation landing on the shield would otherwise
                # skip registration and leave a durable obligation with no
                # in-process owner until the next boot (Terra, design round 4).
                self._on_terminal_locked(rec)
                return True

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    # Let the inner persist (and, on failure, the rollback)
                    # finish under the lock before honoring the cancel — never
                    # a torn memory/disk pair.
                    #
                    # A REPEATED cancellation was raised as a hazard here
                    # (Terra, diff review r3): cancel the gather, the lock
                    # releases while the record still carries its transient
                    # terminal status, and another writer persists it. MEASURED
                    # against this code: it does not happen. Cancelling the
                    # gather cancels its child, but the child cannot complete
                    # until `_write_tombstone_locked`'s own absorb-until-settle
                    # loop sees its `to_thread` write settle — so the gather
                    # keeps waiting and the lock stays held however many
                    # cancellations arrive. Pinned end-to-end by
                    # test_repeated_cancellation_holds_the_lock_until_the_write_settles.
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def mark_idle(self, engagement_id: str) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or rec.status in _TERMINAL_STATUSES:
                # #326: the idle sweep calls this after awaiting
                # driver.cancel(); a finalizer that committed
                # completed/cancelled/error during that await must not be
                # flipped back to a resumable idle (resurrection).
                return
            rec.status = "idle"
            await self._write_tombstone_locked()

    def begin_turn_delivery(self, engagement_id: str) -> bool:
        """#588: may this record receive a turn right now — and if so, say so.

        ``True`` means "deliver"; a record found ``idle`` is flipped back to
        ``active`` first, because a turn entering the engagement IS the
        engagement becoming live again.

            terminal -> False   (the engagement is over; do not deliver)
            idle     -> True, flipped to ``active``
            active   -> True    (unchanged)
            unknown  -> True    (no opinion — see below)

        SYNCHRONOUS BY CONTRACT — it must never become a coroutine and must
        never await. The driver runs it immediately before the call that hands
        the turn to the engagement, and the ABSENCE of a suspension point is
        the whole guarantee: no other coroutine on casa-main's loop — no tool
        call, no terminal transition — can run in between. Any await here (a
        lock, a tombstone write) reopens exactly the window this closes.
        Durability is therefore deferred to a background persist: readers use
        the in-memory record, and a write that never lands costs only that a
        restart re-idles the record.

        Timestamps are deliberately untouched. A redelivery is not a new user
        turn; re-stamping ``last_user_turn_ts`` on every restart would postpone
        the idle suspension indefinitely. That is why ``update_user_turn`` is not
        reused here.

        An UNKNOWN record delivers rather than refusing: records leave
        ``_records`` only on a ``create()`` rollback, so this is unreachable for
        a live engagement, and making delivery hostage to registry knowledge
        would risk silently dropping operator turns for no authority gain.

        What this does NOT promise (design review r3-r5, stated so it is not
        mistaken for more): it fences the decision to deliver only. A terminal
        transition landing after delivery has begun cannot revoke the turn.
        Stopping an in-flight turn is ``driver.cancel``'s job, which the
        finalize path already does.
        """
        rec = self._records.get(engagement_id)
        if rec is None:
            return True
        if rec.status in _TERMINAL_STATUSES:
            return False
        if rec.status == "idle":
            rec.status = "active"
            self._schedule_tombstone_persist()
        return True

    def _schedule_tombstone_persist(self) -> None:
        """Best-effort durable catch-up for a mutation committed in memory.

        Fire-and-forget by design: the callers (``begin_turn_delivery``,
        ``note_activity``) may not await. Failures are logged and dropped — the in-memory
        record is what every authority check reads, and the next mutation's own
        write carries this one along (the snapshot is rebuilt from memory).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                      # no loop (sync test context) — nothing to do

        async def _persist() -> None:
            try:
                async with self._lock:
                    await self._write_tombstone_locked()
            except Exception as exc:  # noqa: BLE001 — best-effort by contract
                logger.warning(
                    "deferred tombstone persist failed: %s", exc)

        task = loop.create_task(_persist())
        self._deferred_persists.add(task)
        task.add_done_callback(self._deferred_persists.discard)

    def note_activity(self, engagement_id: str, ts: float) -> None:
        """SYNCHRONOUS: stamp ``last_user_turn_ts`` in memory and schedule its
        persist. A job's driver calls it when operator input is accepted, at
        every turn end and while it recovers a session, so the stale reap
        and the idle sweep see a working job as active. Never flips status:
        admission (``begin_turn_delivery``) does that."""
        rec = self._records.get(engagement_id)
        if rec is None or rec.status in _TERMINAL_STATUSES:
            return
        rec.last_user_turn_ts = max(rec.last_user_turn_ts, ts)
        self._schedule_tombstone_persist()

    async def update_user_turn(self, engagement_id: str, ts: float) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.last_user_turn_ts = ts
            if rec.status == "idle":
                rec.status = "active"
            await self._write_tombstone_locked()

    async def update_plugin_profiles(self, engagement_id: str,
                                     profiles: dict) -> None:
        """S8: record a build's plan on the record's ``plugin_artifacts``
        rows, per plugin name — the EFFECTIVE profile it applied to each
        plugin it loaded, and (#1186) the profile name its plan gives a
        plugin it withheld — what a later resume's fallback row reads when
        the live registry no longer assigns the plugin to this target.
        Written after the options are built and before the client starts,
        from the plan the build returned, never a re-read. Unknown record ⇒
        no-op."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            # Only rows NAMED in the mapping change. A launch names a plugin
            # its build withheld only with a profile NAME (#1186); the driver
            # re-writes every row with the value the resume build left, which
            # for a withheld plugin is its own or a profile name — never an
            # erasure. The write is strict — a caller that cannot record what
            # its build decided must not open the client.
            rec.plugin_artifacts = tuple(
                {**row, "profile": profiles[row.get("name")]}
                if isinstance(row, dict) and row.get("name") in profiles else row
                for row in rec.plugin_artifacts)
            await self._write_tombstone_locked(strict=True)

    async def persist_origin(self, engagement_id: str) -> None:
        """Persist updates made to a live record's origin."""
        async with self._lock:
            if engagement_id in self._records:
                await self._write_tombstone_locked()

    async def persist_session_id(self, engagement_id: str, session_id: str) -> None:
        """#302: STRICT persistence with rollback. The driver's retry guard
        compares ``engagement.sdk_session_id`` against the observed sid — and
        ``engagement`` usually IS this registry's record object, so a swallowed
        write failure that left the field mutated would silently disable every
        later retry while the durable file never received the ID (the record
        could not resume after a restart). A failed write restores the prior
        value and PROPAGATES so the caller knows to retry.

        CANCELLATION-SAFE (Sol r4): a caller cancelled mid-``to_thread``
        must not tear the retry guard from disk — the write SETTLES first
        (shielded, absorbing repeated cancels, like ``create()``'s
        settle-despite-cancel), then memory reflects the settled outcome:
        committed ⇒ the sid stays (no compensation needed — a durable sid on
        a cancelled caller is simply durable), failed ⇒ rolled back so the
        next message retries. The original cancellation then propagates."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            prior = rec.sdk_session_id
            rec.sdk_session_id = session_id
            task = asyncio.ensure_future(
                self._write_tombstone_locked(strict=True))
            cancelled: asyncio.CancelledError | None = None
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    cancelled = exc       # settle first; re-raise after
                except Exception:  # noqa: BLE001 — retrieved below
                    break
            if task.cancelled() or task.exception() is not None:
                rec.sdk_session_id = prior
            if cancelled is not None:
                raise cancelled
            if task.exception() is not None:
                raise task.exception()

    # -- v0.79.0 (§4) question numbering + open-question ledger --------------

    # -- v0.79.0 (§5) live-summary state ------------------------------------

    async def sweep_idle_and_suspend(
        self, *, driver: Any, now_override: float | None = None,
    ) -> None:
        """Daily scan: tear down in_casa clients idle past the suspend threshold.

        ``driver`` is the ``DriverProtocol`` instance for in_casa — used to
        check is_alive, read session_id, and close the client. For tests,
        ``now_override`` short-circuits ``time.time()``.
        """
        import time

        now = now_override if now_override is not None else time.time()

        for rec in list(self.active_and_idle()):
            idle_s = now - rec.last_user_turn_ts

            # Session suspension (in_casa only). A job whose session has a
            # turn running (one turn can run for hours) or that is being
            # recovered is working, not idle, and is skipped.
            if (rec.driver == "in_casa" and rec.status == "active"
                    and idle_s > _SESSION_SUSPEND_IDLE_S
                    and driver.is_alive(rec)
                    and not _session_working(driver, rec.id)):
                session_id = driver.get_session_id(rec)
                try:
                    await driver.cancel(rec)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sweep: driver.cancel(%s) failed: %s", rec.id[:8], exc,
                    )
                if session_id is not None:
                    # #302: persist_session_id is STRICT now. The client is
                    # already cancelled, so on a failed write we still mark
                    # idle (nothing can be undone) — the session id is then
                    # lost to a restart, WARN says so.
                    try:
                        await self.persist_session_id(rec.id, session_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "sweep: session-id persist for %s failed — the "
                            "suspended session cannot resume across a "
                            "restart: %s", rec.id[:8], exc,
                        )
                await self.mark_idle(rec.id)

