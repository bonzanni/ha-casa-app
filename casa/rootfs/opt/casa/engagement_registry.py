"""Engagement primitive — Tier 2 Specialist interactive mode + (Plan 3+) Tier 3 Executors.

Symmetric with :mod:`specialist_registry`. Owns:
- EngagementRecord (one in-flight engagement)
- EngagementRegistry (in-memory dict + ``/data/engagements.json``: in-flight
  records for crash recovery PLUS terminal tombstones, which age out after
  ``_TERMINAL_RETENTION_DAYS`` — D-4, v0.69.0)
- Idle sweep (fires ``idle_detected`` bus events + session-suspends live clients)
- Orphan recovery (startup: load the file; "active" rows are reconciled to
  idle — no driver survives a restart — and remain dormant until the next
  user turn in their topic; ``tools.reap_stale_engagements`` retires them
  after the reap TTL)

See docs/superpowers/specs/2026-04-22-3.5-plan2-engagement-primitive-design.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from atomic_io import atomic_write_json
from engagement_uids import UNALLOCATED_UID
from sensitivity import TIERS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sweep constants
# ---------------------------------------------------------------------------

_IDLE_REMINDER_DAYS_SPECIALIST = 3
_IDLE_REMINDER_DAYS_EXECUTOR = 7          # default; per-type override lands Plan 3
_IDLE_REMINDER_REFIRE_DAYS = 7
_SESSION_SUSPEND_IDLE_S = 86400
_IDLE_SWEEP_CRON = "0 8 * * *"            # daily 08:00 user TZ
# D-4 (v0.69.0): terminal tombstones stay on disk this long, then age out of
# the snapshot on the next write — bounds the file while keeping the P32
# duplicate-task guard and post-mortems working across restarts.
_TERMINAL_RETENTION_DAYS = 30

# v0.79.0 (§3): sentinel for the strict terminal transition's full-field
# snapshot — distinguishes "origin had no such key" from "key was None" so the
# rollback can DELETE a key the transition added rather than leaving it None.
_FIELD_MISSING = object()


_STALE_KIND_DEFAULT = "plain"

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
# ``rec.origin`` for live use).
_NON_PERSISTABLE_ORIGIN_KEYS = frozenset({
    "speaker_provenance",
    "_voice_handoff_reservation",
    "_progress_sink",
    "voice_route_capabilities",
    "voice_deadline",
})


def _valid_token_or_blank(value: Any) -> str:
    """#335: a persisted ``auth_token`` is usable only as a non-empty ASCII
    string — anything else (a JSON number, null, a non-ASCII string) becomes
    ``""`` so ``load()``'s backfill mints a fresh one. Keeping an unusable
    value would leave the record permanently unbindable."""
    if isinstance(value, str) and value and value.isascii():
        return value
    return ""


def _persistable_origin(origin: dict[str, Any]) -> dict[str, Any]:
    """A JSON-safe copy of *origin* for tombstone persistence: drops the known
    live/non-serializable ContextVar-carried keys (see
    ``_NON_PERSISTABLE_ORIGIN_KEYS``)."""
    return {k: v for k, v in origin.items() if k not in _NON_PERSISTABLE_ORIGIN_KEYS}


def normalize_stale_mid_entry(entry: Any) -> dict[str, Any]:
    """v0.84.0 (round-4 §D6): a ``stale_mids`` entry is ``{"mid": int, "kind":
    str}`` with ``kind ∈ {"reanchored", "plain"}`` — ``reanchored`` renders the
    marker-only moved copy, ``plain`` keeps today's full-body+suffix rendering.
    An entry missing ``kind`` (or a corrupt non-dict entry) defaults to
    ``"plain"`` rather than raising — corruption tolerance, fail-safe to the
    existing rendering. Both the registry's own mutators (``stage_stale_mid``/
    ``unstage_stale_mid``) and ``claude_code_driver``'s stale-settle paths
    (``_settle_ledger_entry`` + boot reconcile) normalize through this one
    function so every reader agrees on the shape regardless of on-disk
    vintage. Never mutates ``entry``; always returns a fresh dict."""
    if isinstance(entry, dict):
        return {"mid": entry.get("mid"), "kind": entry.get("kind") or _STALE_KIND_DEFAULT}
    return {"mid": entry, "kind": _STALE_KIND_DEFAULT}


def _restore_origin_field(rec: "EngagementRecord", key: str, snapped: Any) -> None:
    """Restore ``rec.origin[key]`` to a strict-transition snapshot value.

    ``_FIELD_MISSING`` means the key was ABSENT before the transition — the
    rollback removes it rather than resurrecting it as ``None``."""
    if snapped is _FIELD_MISSING:
        rec.origin.pop(key, None)
    else:
        rec.origin[key] = snapped


# ---------------------------------------------------------------------------
# W2/Sol B9 (Task 7) — interaction_state pure transition core.
# ---------------------------------------------------------------------------
#
# Transition table (normative — design §W2, Sol r3-B4):
#   first_contact      : first_contact_required -> awaiting_operator
#   operator_answered  : {first_contact_required, awaiting_operator} -> authorized
#   operator_turn      : {first_contact_required, awaiting_operator} -> authorized
#   anything else (including from "authorized", from "" (not
#   interaction-required), or an event not valid from the current state)
#   -> no-op (None). Never backwards.


def _pure_interaction_transition(current: str, event: str) -> str | None:
    """Compute the next ``interaction_state``, or ``None`` for a no-op.

    Pure (no I/O, no locking) so it's trivially unit-testable and reusable
    by the atomic locked mutator below.
    """
    if event == "first_contact":
        return "awaiting_operator" if current == "first_contact_required" else None
    if event in ("operator_answered", "operator_turn"):
        if current in ("first_contact_required", "awaiting_operator"):
            return "authorized"
        return None
    return None


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass
class EngagementRecord:
    """One in-flight engagement.

    ``kind`` = "specialist" for Tier 2 interactive mode; "executor" for Tier 3
    (Plan 3+). ``role_or_type`` is the specialist role (e.g. "finance") or the
    executor type (e.g. "configurator").

    ``status`` transitions:
      active ──first idle sweep past 24h──▶ idle
      active ──registry load after restart──▶ idle   (D-4 boot reconcile)
      idle    ──next user turn──▶ active
      active  ──emit_completion / /complete──▶ completed
      active  ──/cancel / cancel_engagement──▶ cancelled
      active/idle ──reap sweep past ENGAGEMENT_REAP_DAYS──▶ cancelled  (D-4)
      active  ──resume twice failed / sweep orphan──▶ error
    """

    id: str
    kind: str
    role_or_type: str
    driver: str
    status: str
    topic_id: int | None
    started_at: float
    last_user_turn_ts: float
    last_idle_reminder_ts: float
    completed_at: float | None
    sdk_session_id: str | None
    origin: dict[str, Any]
    task: str
    # #335: per-engagement secret authenticating this engagement's id on the
    # internal surfaces (MCP tools/call + /internal/channel/*). Generated at
    # ``create()``, backfilled at ``load()`` for pre-upgrade rows, provisioned
    # ONLY into this engagement's own workspace ``.mcp.json`` — the id alone
    # is endpoint-visible and must never confer authority. Persisted so a
    # restart-resumed engagement's existing workspace credential stays valid.
    auth_token: str = ""
    # E-12 (v0.37.0): channel-side state for in-place edits across restarts.
    pinned_message_id: int | None = None
    progress_message_id: int | None = None
    current_state_emoji: str | None = None
    # C-1 v0.37.2: snapshot of executor's tools.allowed at engagement
    # creation. Drives the engagement_permission_relay hook (spec §3.5).
    tools_allowed: tuple[str, ...] = ()
    # G-1 v0.37.7: snapshot of executor's permission_mode at engagement
    # creation. When "auto" or "bypassPermissions" the relay hook
    # short-circuits without surfacing a permission keyboard.
    permission_mode: str = "acceptEdits"
    # §3.8: immutable snapshot of the resolved plugin artifacts this
    # engagement launched with — each {"name","artifact_id","path"}. Boot
    # replay renders --plugin-dir flags from THESE recorded paths, never a
    # re-resolution of current assignments. Preserved by every rewrite.
    plugin_artifacts: tuple[dict, ...] = ()
    # #369: set (durably, in the same strict write as the clearance clamp)
    # when a downgrade invalidates this engagement's session context. While
    # True, every resume path refuses to resume the recorded session and the
    # tool choke points refuse to bind the record — the flag clears only once
    # a fresh session has been established at the clamped floor. One-way per
    # downgrade: a crash between clamp and rebuild leaves a record that
    # refuses resume, never one that resurrects the pre-clamp transcript.
    context_rebuild_pending: bool = False
    # #369 (Terra diff-gate r2): monotonic generation, bumped by every clamp
    # that moves the record. The boolean above cannot protect a launch that
    # was suspended across a full clamp→rebuild→flag-clear cycle — the stale
    # launch resumes, sees False, and delivers its pre-clamp prompt. A
    # launcher captures this at prompt-source time and the drivers compare it
    # immediately before the initial enqueue; any change aborts the launch.
    context_generation: int = 0
    # W2/Sol B9 (Task 7): observational turn-taking state. "" (default) =
    # not interaction-required (most engagements). Interaction-required
    # engagements start at "first_contact_required" (set by engage_executor
    # at create — Task 8) and advance via ``advance_interaction_state``:
    # first_contact_required -> awaiting_operator -> authorized. Never
    # backwards; see ``_pure_interaction_transition``.
    interaction_state: str = ""
    # #215: the procedural-epoch digest computed at LAUNCH from the exact
    # bytes the launch consumed (executor_epoch.compute_procedural_epoch).
    # Persisted so finalize stamps the summary with the epoch its engagement
    # actually ran under — never the finalize-time definition, which a
    # reload may have moved (design r1, Sol S-B2/Terra T-B1). "" = legacy or
    # non-executor record: the summary is then retained UNTAGGED and a later
    # archive filter drops it.
    procedural_epoch: str = ""
    # Task 6 (spec §4.6): the concurrency Permit this interactive
    # specialist delegation holds, if any (set by tools.py's
    # delegate_to_agent right after `create()`, None for executor
    # engagements — they never acquire one). NOT persisted to the
    # tombstone (`_write_tombstone_locked` below lists fields explicitly)
    # — a live Permit cannot survive a restart; concurrency state is
    # memory-only and resets with the process. Released exactly once by
    # `_finalize_engagement` (the shared completion/cancel/reap funnel)
    # or, for a pre-finalize failure (topic/driver-start), inline at the
    # point of failure — see delegate_to_agent's interactive branch.
    permit: Any = None
    # #283: the agent-spawn occupancy token (specialist_limits.SpawnToken)
    # this record holds when its origin carries ``_agent_spawned`` — a
    # SEPARATE field from ``permit`` (design r3: an agent-context interactive
    # specialist holds BOTH, and sharing the field would overwrite one or
    # leak the other). Memory-only like ``permit`` (never tombstoned; boot
    # reconciliation re-mints it via the limiter's restore()). Attached by
    # ``create()`` itself (design r4: ownership transfers at successful
    # create, never after driver start — a cancellation in that window must
    # not release a token whose marked record remains live). Released
    # exactly once by ``_release_permit`` on every terminal transition.
    agent_spawn_permit: Any = None
    # v0.79.0 (§4): persisted question numbering. ``next_question_number`` is a
    # monotonic per-engagement allocator (never rewound, even when a question
    # closes) so every displayed ``Q<n>`` is durable and unique across restarts.
    # ``open_questions`` is the set of still-open (unsettled) questions, each a
    # ``{"n": int, "tg_message_id": int|None}`` dict — boot reconciliation
    # settles any entry whose broker record did not survive the restart.
    # v0.83.0 (§A3, Sol r2-7/r3-5/r5-3): entries gain ``answered: bool`` (the
    # answer-lifecycle decision, split from visual settlement — an answered entry
    # is INVISIBLE to ``open_question_numbers``/``oldest_open_anchor`` and the A3
    # gates/summary, yet stays present for raw reconcile/settle iteration) and
    # ``stale_mids: list[{"mid": int, "kind": str}]`` (re-anchor OLD copies
    # awaiting a confirmed settle). BOTH ``answered``/``stale_mids`` are
    # absent-tolerated on load (pre-v0.83 rows have neither key → each accessor
    # ``.get``-defaults). v0.84.0 (round-4 §D6): each ``stale_mids`` entry also
    # carries a ``kind`` (``"reanchored"`` renders the marker-only moved copy;
    # ``"plain"`` keeps the full-body+suffix rendering) — legacy bare-integer
    # entries (pre-round-4) tolerate on read via ``normalize_stale_mid_entry``,
    # defaulting to ``"plain"``. Entry-removal invariant: an entry is REMOVED
    # only when its CURRENT copy's settle edit is confirmed AND ``stale_mids``
    # is empty.
    next_question_number: int = 1
    open_questions: tuple[dict, ...] = ()
    # v0.79.0 (§5): the pinned live-summary controller state. ``summary_message_id``
    # is the Telegram id of the first (pinned) topic message, posted at boot
    # BEFORE the subprocess starts so a resumed engagement adopts it on attach.
    # ``summary_revision`` is the engagement-wide monotonic revision allocator —
    # every lifecycle status transition acquires the next revision here (totally
    # ordered, collision-free), so a newer revision may lower the status rank
    # while an older/equal one never overrides.
    summary_message_id: int | None = None
    summary_revision: int = 0
    # W-R6 (v0.81.0): the persisted SHORT topic title (2-3 words). Set once at
    # engage_executor ingest (engager-supplied ``topic_title`` normalized, or a
    # Casa-derived fallback from the brief/task), then read by BOTH the
    # topic-name state edit (telegram.update_topic_state) and the live-summary
    # title (claude_code_driver._summary_goal_line) — a single durable source.
    # Additive + absent-tolerant on load (legacy rows have no key → "" → each
    # reader falls back to the derived concise_task label, no crash).
    topic_title: str = ""
    # Containment Stage 2 (Task 3): the OS uid this engagement's subprocess
    # runs as, once containment lands a per-engagement identity boundary.
    # Allocated (via ``EngagementRegistry``'s injected ``UidAllocator``) in
    # ``create()`` for ``driver == "claude_code"`` records only — specialists
    # and any other driver stay at the ``UNALLOCATED_UID`` sentinel forever.
    # Persisted so a resumed/restarted engagement keeps the SAME uid (the
    # allocator's never-reuse invariant is meaningless if a reload could hand
    # a live engagement a different one); legacy rows predating this field
    # load with the sentinel via ``load()``'s ``.get``-default.
    allocated_uid: int = UNALLOCATED_UID


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TerminalPreconditionFailed(Exception):
    """G4 D2 (v0.96.0): raised by ``try_transition_terminal`` when the
    caller-supplied ``terminal_hook`` vetoes the terminal flip (e.g. unread
    operator inbound at completion time). The record is left LIVE and
    unmodified."""


class EngagementRegistry:
    """In-memory dict + ``/data/engagements.json`` tombstone.

    All mutation methods acquire ``self._lock`` and write the tombstone
    inside the lock, same pattern as SpecialistRegistry.register_delegation.
    The ``bus`` parameter is used to publish ``idle_detected`` events from
    the sweep task; tests that don't exercise the sweep may pass ``None``.
    """

    def __init__(
        self,
        *,
        tombstone_path: str,
        bus: Any | None,
        uid_allocator: Any | None = None,
        agent_spawn_limiter: Any | None = None,
    ) -> None:
        self._tombstone_path = tombstone_path
        self._bus = bus
        # Containment Stage 2 (Task 3): injected ``engagement_uids.UidAllocator``,
        # already ``.reconstruct()``-ed by the caller before this registry is
        # used. Optional/defaults to None so every existing construction site
        # (and every pre-Task-10 test) keeps working unchanged — a create()
        # for a claude_code engagement with no allocator configured simply
        # leaves ``allocated_uid`` at the sentinel (see create()) rather than
        # raising, and downstream containment code is expected to fail closed
        # on the sentinel rather than render a root/unallocated uid.
        self._uid_allocator = uid_allocator
        # #283: injected specialist_limits.AgentSpawnLimiter. Optional (None
        # keeps every existing construction site and test working); when
        # present, ``load()`` restores one occupancy token per live marked
        # record so a restart cannot refill the spawn pool while marked
        # engagements are still live. MUST be constructed and injected
        # BEFORE ``load()`` runs (design r3, Sol: casa_core's boot order).
        self._agent_spawn_limiter = agent_spawn_limiter
        self._records: dict[str, EngagementRecord] = {}
        self._topic_index: dict[int, str] = {}
        self._lock = asyncio.Lock()

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
        for row in raw:
            try:
                rec = EngagementRecord(
                    id=row["id"],
                    kind=row["kind"],
                    role_or_type=row["role_or_type"],
                    driver=row["driver"],
                    status=row["status"],
                    topic_id=row.get("topic_id"),
                    started_at=float(row["started_at"]),
                    last_user_turn_ts=float(row["last_user_turn_ts"]),
                    last_idle_reminder_ts=float(row.get("last_idle_reminder_ts", 0.0)),
                    completed_at=row.get("completed_at"),
                    sdk_session_id=row.get("sdk_session_id"),
                    origin=dict(row.get("origin") or {}),
                    task=row.get("task", ""),
                    # #335: a corrupt/hand-edited row can carry a non-string
                    # (or non-ASCII) token; normalize to "" so the backfill
                    # below re-mints a usable one rather than leaving a value
                    # every auth check must refuse (Terra, review r1).
                    auth_token=_valid_token_or_blank(row.get("auth_token")),
                    pinned_message_id=row.get("pinned_message_id"),
                    progress_message_id=row.get("progress_message_id"),
                    current_state_emoji=row.get("current_state_emoji"),
                    tools_allowed=tuple(row.get("tools_allowed") or ()),
                    permission_mode=row.get("permission_mode") or "acceptEdits",
                    plugin_artifacts=tuple(row.get("plugin_artifacts") or ()),
                    interaction_state=row.get("interaction_state") or "",
                    procedural_epoch=row.get("procedural_epoch") or "",
                    next_question_number=int(row.get("next_question_number", 1) or 1),
                    open_questions=tuple(
                        dict(q) for q in (row.get("open_questions") or ())
                    ),
                    summary_message_id=row.get("summary_message_id"),
                    summary_revision=int(row.get("summary_revision", 0) or 0),
                    topic_title=row.get("topic_title", "") or "",
                    allocated_uid=int(row.get("allocated_uid", UNALLOCATED_UID)),
                    context_rebuild_pending=bool(
                        row.get("context_rebuild_pending", False)),
                    context_generation=int(
                        row.get("context_generation", 0) or 0),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping malformed engagement row: %s", exc)
                continue
            # D-4 boot reconcile (v0.69.0): a record loaded as "active" claims
            # a live driver, but no driver survives a restart — the process
            # that ran it died with the old container. Idle is the truthful
            # state: dormant, resumable on the next user turn in its topic
            # (update_user_turn flips it back), and visible to the reap sweep.
            if rec.status == "active":
                rec.status = "idle"
                reconciled_any = True
                logger.info(
                    "boot reconcile: engagement %s active→idle "
                    "(no driver survives a restart)", rec.id[:8],
                )
            # #335 boot backfill: a pre-upgrade row has no auth token. Every
            # in-memory record must carry one (the internal surfaces fail
            # CLOSED on a token-less record), so mint it here; boot replay
            # then rewrites the workspace .mcp.json from the record before
            # the engagement's CLI is respawned, keeping resumed engagements
            # working. Uniform for terminal tombstones too — cheap, and no
            # record class is left permanently unbindable.
            if not rec.auth_token:
                rec.auth_token = secrets.token_urlsafe(32)
                reconciled_any = True
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
            if rec.topic_id is not None:
                self._topic_index[rec.topic_id] = rec.id

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

    def terminal_records(self) -> list[EngagementRecord]:
        """v0.79.0 (§3): terminal records, for the boot spool-reconciliation
        owner (drains inbound spools still holding pending receipts/notices)."""
        return [
            r for r in self._records.values()
            if r.status in ("completed", "cancelled", "error")
        ]

    def get(self, engagement_id: str) -> EngagementRecord | None:
        return self._records.get(engagement_id)

    def by_topic_id(self, topic_id: int) -> EngagementRecord | None:
        rec_id = self._topic_index.get(topic_id)
        return self._records.get(rec_id) if rec_id else None

    def recent_for_origin(
        self,
        *,
        channel: str,
        chat_id: str,
        max_age_s: float,
        now: float | None = None,
    ) -> EngagementRecord | None:
        """P32 (v0.37.10): return the most-recent engagement (by
        ``started_at``) for this ``(channel, chat_id)`` started within
        the last ``max_age_s`` seconds, regardless of status.

        Includes completed / cancelled / errored engagements: they stay
        in ``_records`` for the process lifetime and (since D-4,
        v0.69.0) persist on disk as tombstones for
        ``_TERMINAL_RETENTION_DAYS``, so the guard also holds across
        restarts. The duplicate-task guard at the ``engage_executor``
        call site uses this to refuse spawns that overlap with whichever
        task was spawned last.

        ``chat_id`` is coerced via ``str()`` for the compare; channel
        adapters may store the value as int (telegram) or str.
        """
        if now is None:
            now = time.time()
        cutoff = now - max_age_s
        candidates: list[EngagementRecord] = []
        for rec in self._records.values():
            if rec.origin.get("channel", "") != channel:
                continue
            if str(rec.origin.get("chat_id", "")) != chat_id:
                continue
            if rec.started_at < cutoff:
                continue
            candidates.append(rec)
        if not candidates:
            return None
        candidates.sort(key=lambda r: r.started_at, reverse=True)
        return candidates[0]

    # -- Persist helper ---------------------------------------------------

    async def _write_tombstone_locked(self, *, strict: bool = False) -> None:
        """Caller MUST hold self._lock.

        Terminal records are persisted as real tombstones (D-4, v0.69.0) —
        they used to be silently dropped, so the P32 duplicate-task guard
        forgot recent spawns across restarts and the file never matched its
        name. Tombstones age out after ``_TERMINAL_RETENTION_DAYS`` to bound
        the file.

        ``strict`` (B3, Sol r1): when True, a persistence failure PROPAGATES
        instead of being swallowed — used by ``advance_interaction_state``,
        where returning the new state while the authorization never reached
        disk lets the telegram callback commit an ask that a restart would
        then un-authorize, and by ``set_channel_state(strict=True)`` (#529),
        whose sentinel scheme needs a durable write-ahead barrier. All other
        callers keep the best-effort warn-and-continue semantics
        (strict=False).

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
        cutoff = time.time() - _TERMINAL_RETENTION_DAYS * 86400
        snapshot = []
        for rec in self._records.values():
            if (rec.status in ("completed", "cancelled", "error")
                    and rec.completed_at is not None
                    and rec.completed_at < cutoff):
                continue
            snapshot.append({
                "id": rec.id,
                "kind": rec.kind,
                "role_or_type": rec.role_or_type,
                "driver": rec.driver,
                "status": rec.status,
                "topic_id": rec.topic_id,
                "started_at": rec.started_at,
                "last_user_turn_ts": rec.last_user_turn_ts,
                "last_idle_reminder_ts": rec.last_idle_reminder_ts,
                "completed_at": rec.completed_at,
                "sdk_session_id": rec.sdk_session_id,
                "origin": _persistable_origin(rec.origin),
                "task": rec.task,
                "auth_token": rec.auth_token,
                "pinned_message_id": rec.pinned_message_id,
                "progress_message_id": rec.progress_message_id,
                "current_state_emoji": rec.current_state_emoji,
                "tools_allowed": list(rec.tools_allowed),
                "permission_mode": rec.permission_mode,
                "plugin_artifacts": [dict(pa) for pa in rec.plugin_artifacts],
                "interaction_state": rec.interaction_state,
                "procedural_epoch": rec.procedural_epoch,
                "next_question_number": rec.next_question_number,
                "open_questions": [dict(q) for q in rec.open_questions],
                "summary_message_id": rec.summary_message_id,
                "summary_revision": rec.summary_revision,
                "topic_title": rec.topic_title,
                "allocated_uid": rec.allocated_uid,
                "context_rebuild_pending": rec.context_rebuild_pending,
                "context_generation": rec.context_generation,
            })
        self._last_tombstone_ok = False
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
        # #335: 0600 — every row carries that engagement's ``auth_token``, so
        # this file is secret-bearing and must not stay world-readable. This
        # is defense in depth, NOT isolation: engagement subprocesses run as
        # root in this container and can still read it. Passing the mode
        # explicitly also MIGRATES a pre-#335 0644 file on the first write
        # after upgrade.
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
        topic_id: int | None,
        tools_allowed: tuple[str, ...] | list[str] = (),
        permission_mode: str = "acceptEdits",
        plugin_artifacts: tuple[dict, ...] | list[dict] = (),
        interaction_state: str = "",
        topic_title: str = "",
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
            topic_id=topic_id,
            started_at=now,
            last_user_turn_ts=now,
            last_idle_reminder_ts=0.0,
            completed_at=None,
            sdk_session_id=None,
            origin=dict(origin),
            task=task,
            # #335: minted here, before first persist, so the workspace
            # provisioner can bake it into .mcp.json and every internal
            # surface can verify id claims against it.
            auth_token=secrets.token_urlsafe(32),
            tools_allowed=tuple(tools_allowed),
            permission_mode=permission_mode or "acceptEdits",
            plugin_artifacts=tuple(dict(pa) for pa in plugin_artifacts),
            interaction_state=interaction_state,
            topic_title=topic_title,
            # #283 (design r4): ownership transfers HERE, at create — once
            # this call returns successfully, terminal transitions own the
            # release. If create raises (rollback ran, no record exists),
            # the caller's lexical finally still owns it; SpawnToken.release
            # is idempotent so the overlap is safe.
            agent_spawn_permit=agent_spawn_permit,
        )
        async with self._lock:
            self._records[engagement_id] = rec
            if topic_id is not None:
                self._topic_index[topic_id] = engagement_id

            # Containment Stage 2 (Task 3): allocate a durable OS uid for a
            # claude_code engagement BEFORE the strict tombstone write below —
            # a uid that never reached disk must never be handed to a live
            # subprocess. Specialists / any other driver stay UNALLOCATED_UID.
            # A missing allocator is the fail-CLOSED default: rather than
            # mint a bogus uid (or raise and break every caller that hasn't
            # been wired to inject one yet — that's Task 10), the record is
            # left at the sentinel and containment's own preflight (Task 6/7)
            # refuses to render/launch a claude_code process at that uid.
            if driver == "claude_code":
                if self._uid_allocator is not None:
                    try:
                        rec.allocated_uid = self._uid_allocator.allocate()
                    except Exception:
                        self._records.pop(engagement_id, None)
                        if (topic_id is not None
                                and self._topic_index.get(topic_id) == engagement_id):
                            del self._topic_index[topic_id]
                        raise
                else:
                    logger.warning(
                        "engagement %s (claude_code) created with no "
                        "uid_allocator configured — allocated_uid left at "
                        "the sentinel; containment preflight must refuse it",
                        engagement_id[:8],
                    )

            # #326: STRICT persistence — the record's whole point is crash
            # recovery, so reporting success after a swallowed write failure
            # hands the caller a running topic/workspace that a restart will
            # neither recover nor reap. On failure the in-memory insert is
            # rolled back and the error propagates (the engage/delegate call
            # sites already surface launch-path failures as tool errors).
            # Shield-and-await like the sibling strict mutators so a caller
            # cancelled mid-``to_thread`` cannot tear memory from disk.
            def _rollback() -> None:
                self._records.pop(engagement_id, None)
                if (topic_id is not None
                        and self._topic_index.get(topic_id) == engagement_id):
                    del self._topic_index[topic_id]

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
            "Engagement %s created (kind=%s role_or_type=%s topic_id=%s)",
            engagement_id[:8], kind, role_or_type, topic_id,
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
        # interactive specialist holds a SpecialistLimiter permit AND an
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

    async def mark_completed(self, engagement_id: str, completed_at: float) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or rec.status in _TERMINAL_STATUSES:
                return  # #326: never overwrite a terminal winner
            rec.status = "completed"
            rec.completed_at = completed_at
            self._release_permit(rec)
            await self._write_tombstone_locked()

    async def mark_cancelled(self, engagement_id: str) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or rec.status in _TERMINAL_STATUSES:
                return  # #326: never overwrite a terminal winner
            rec.status = "cancelled"
            rec.completed_at = time.time()
            self._release_permit(rec)
            await self._write_tombstone_locked()

    async def mark_error(self, engagement_id: str, kind: str, message: str) -> bool:
        """Returns True when THIS call flipped the record to ``error`` — like
        ``try_transition_terminal``, only the winner may run terminal side
        effects (topic cleanup). False for an unknown record or one a
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
            self._release_permit(rec)
            await self._write_tombstone_locked()
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
    ) -> bool:
        """Atomically move a record to a terminal status. Returns True only
        for the first caller; False if missing or already terminal.

        L75/L24: emit_completion's fast-path terminal check and
        _finalize_engagement's registry write are separated by real
        suspension points (e.g. a forced-reload await), so a concurrent
        /cancel can race between them. This method is the single
        authoritative gate — only the first caller to flip the record
        terminal may run finalize side effects (topic close, DelegationComplete
        NOTIFICATION, summary retain).

        ``stale_before`` (reap, v0.69.6): win ONLY if ``last_user_turn_ts`` is
        still older than the cutoff. The reap checks staleness before this
        call at a suspension point away; without this guard a user turn that
        revives the record in that window would still be reaped.

        ``strict`` (v0.79.0 §3, Sol r6-2/r7-2): the finalize path uses STRICT
        transactional persistence. Non-strict callers keep the historical
        best-effort behavior (a tombstone write failure is swallowed and the
        in-memory flip stands, which could leave a closed topic with no
        terminal record for boot reconciliation to find). Strict snapshots
        EVERY field the transition mutates (status, completed_at, and the
        error metadata on ``origin``) and, on tombstone-write failure, restores
        the FULL snapshot and re-raises — so a persistence failure leaves the
        record exactly as it was (live), never a memory/disk split. The
        mutate+persist runs under a shield-and-await (mirroring
        ``advance_interaction_state``) so cancellation during ``to_thread``
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
                # Task 6 (spec §4.6): release the interactive delegation's
                # concurrency permit on this terminal transition (no-op for
                # executor engagements, permit=None). Safe before the write:
                # the non-strict path has no rollback, so the record is
                # committed-terminal in memory regardless of persist outcome.
                self._release_permit(rec)
                await self._write_tombstone_locked()
                return True

            # STRICT: full-field snapshot + shield-and-await + rollback-on-fail.
            snap_status = rec.status
            snap_completed = rec.completed_at
            snap_error_kind = rec.origin.get("error_kind", _FIELD_MISSING)
            snap_error_message = rec.origin.get("error_message", _FIELD_MISSING)

            def _restore() -> None:
                rec.status = snap_status
                rec.completed_at = snap_completed
                _restore_origin_field(rec, "error_kind", snap_error_kind)
                _restore_origin_field(rec, "error_message", snap_error_message)

            async def _mutate_and_persist() -> bool:
                rec.status = new_status
                rec.completed_at = new_completed
                if new_status == "error":
                    rec.origin["error_kind"] = error_kind or "emit_completion_error"
                    rec.origin["error_message"] = error_message
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    _restore()
                    raise
                # Task 6 (spec §4.6): release the permit ONLY after the
                # terminal status is durably committed — the strict path can
                # roll the status back to live on a persist failure, and
                # releasing a still-live engagement's permit would free its
                # scope slot while the interactive specialist is still running.
                self._release_permit(rec)
                return True

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    # Let the inner mutate+persist (and, on failure, the
                    # rollback) finish under the lock before honoring the
                    # cancel — never a torn memory/disk pair.
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

    async def update_user_turn(self, engagement_id: str, ts: float) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.last_user_turn_ts = ts
            # C-fix (2026-05-29): reset the idle-reminder debounce so the next
            # reminder tracks *activity* (the N-day-since-last-turn threshold)
            # rather than the 7-day-since-last-reminder refire clock. Without
            # this, a re-engaged specialist (3 d threshold < 7 d refire) gets
            # its second reminder a few days late. See current-state-spec D7.
            rec.last_idle_reminder_ts = 0.0
            if rec.status == "idle":
                rec.status = "active"
            await self._write_tombstone_locked()

    async def lower_origin_clearance(
        self, engagement_id: str, clearance: str,
    ) -> bool:
        """#336: clamp an engagement's read-clearance DOWN to *clearance*.

        An engagement reads at the clearance of the turn that created it. But
        anyone in the engagement supergroup can send into its topic, and that
        message steers the engagement — so an engagement answering a steering
        turn must not read above the person steering it. This lowers the
        record's stamped clearance to the floor of everyone who has steered
        it; it NEVER raises one (a monotonic clamp is race-free: two
        concurrent steerers can only drive it further down, and re-applying
        is a no-op).

        Returns True when the record actually moved. A record carrying no
        stamped clearance is left alone — it resolves channel-keyed, which is
        the pre-existing behaviour for engagements created before the markers
        existed, and inventing a marker here would silently change it.
        """
        if clearance not in TIERS:
            return False
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return False
            current = rec.origin.get("_origin_clearance")
            if current not in TIERS:
                return False
            if TIERS.index(clearance) >= TIERS.index(current):
                return False          # already at or below this floor
            rec.origin["_origin_clearance"] = clearance
            # #369: the clamp gates future READS, but the session's transcript
            # and launch-injected archive were built at the old tier — mark the
            # context for rebuild in the SAME step, so the two facts ("reads
            # lowered" and "old context must not be resumed") can never be
            # persisted apart.
            rec.context_rebuild_pending = True
            rec.context_generation += 1
            # And withhold the LAUNCH MATERIALS on the record itself: task,
            # brief, context and world-state were authored at the creating
            # turn's clearance, and every later render — boot-replay CLAUDE.md
            # refresh, the session rebuild, resume options — re-derives from
            # the record. Evicting the session while the record still carries
            # them would just re-import them into the fresh floor session
            # (Sol, #369 design review r2).
            rec.task = (
                "[The original task description is withheld: this "
                "engagement's clearance was lowered after it started. "
                "Continue from the conversation in the topic.]"
            )
            for key in ("brief", "context", "world_state_summary"):
                rec.origin.pop(key, None)
            logger.info(
                "engagement %s read-clearance lowered %s→%s (steered by a "
                "lower-clearance sender); session context marked for rebuild",
                engagement_id[:8], current, clearance,
            )
            # The IN-MEMORY clamp is what gates every read from here on, and it
            # is applied above regardless of what disk does — a persistence
            # failure must never leave this process reading at the higher tier.
            # The write is what makes it survive a restart, so a failure is
            # security-relevant and says so; it is deliberately NOT rolled back
            # (rolling back would RAISE the clearance, the one direction this
            # clamp must never move).
            try:
                await self._write_tombstone_locked(strict=True)
            except Exception as exc:  # noqa: BLE001 — in-memory clamp stands
                logger.warning(
                    "engagement %s read-clearance lowered to %s in memory but "
                    "the write failed (%s) — a restart would restore %s",
                    engagement_id[:8], clearance, exc, current,
                )
            return True

    async def update_last_idle_reminder(self, engagement_id: str, ts: float) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.last_idle_reminder_ts = ts
            await self._write_tombstone_locked()

    async def set_resume_fail_count(self, engagement_id: str, count: int) -> None:
        """#326 (low): persist the two-strike resume-failure counter into the
        record's origin. Before this, the counter lived only on the in-memory
        origin dict, so a restart reset it and an unrecoverable engagement
        could dodge the error transition indefinitely. Best-effort (non-strict)
        persistence: a write failure degrades to in-memory-only counting for
        this process lifetime — exactly the pre-fix behavior."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.origin["_resume_fail_count"] = count
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

    async def clear_session_id(self, engagement_id: str) -> None:
        """#369: durably drop the resume pointer after a clearance downgrade
        invalidated the session — a restart must start fresh, never resume the
        pre-clamp transcript. In-memory first: like the clamp itself, a
        persistence failure must never leave THIS process able to resume, so
        the field stays cleared even when the write fails (logged; the
        still-set ``context_rebuild_pending`` refuses resume durably)."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.sdk_session_id = None
            try:
                await self._write_tombstone_locked(strict=True)
            except Exception as exc:  # noqa: BLE001 — in-memory clear stands
                logger.warning(
                    "engagement %s session pointer cleared in memory but the "
                    "write failed (%s) — context_rebuild_pending still blocks "
                    "a restart resume", engagement_id[:8], exc,
                )

    async def clear_context_rebuild_pending(self, engagement_id: str) -> None:
        """#369: mark the context rebuild COMPLETE — called only once a fresh
        session at the clamped floor is established. STRICT: if this cannot be
        persisted the caller must know, because an un-persisted clear would
        make a restart demand a rebuild that already happened (safe but
        surprising), while the in-memory flag governs this process either way."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.context_rebuild_pending = False
            await self._write_tombstone_locked(strict=True)

    async def backfill_allocated_uid(self, engagement_id: str) -> int:
        """Containment Stage 2 (Task 10): allocate + persist an OS uid for a
        legacy ``claude_code`` record whose ``allocated_uid`` is still the
        ``UNALLOCATED_UID`` sentinel (created before Stage 2, or before an
        allocator was wired), and return the resulting uid.

        Called by boot replay for every UNDERGOING claude_code record before it
        re-renders the run script under the record's uid. Idempotent: a record
        that already carries a REAL uid (``!= UNALLOCATED_UID``) is returned
        unchanged, no allocation, no write.

        Fail-CLOSED: with no allocator configured, or if allocation raises
        (e.g. a missing/malformed counter left the allocator un-reconstructed),
        this raises :class:`engagement_uids.UidStateError` — the caller must
        refuse the resume rather than launch a root/unallocated CLI. STRICT
        persistence with rollback (mirrors :meth:`persist_session_id`): the uid
        the subprocess will run as MUST reach disk before it is used, or the
        next boot would re-backfill a *different* uid and orphan a workspace
        chowned to the first — so a failed write restores the sentinel and
        propagates.
        """
        from engagement_uids import UidStateError

        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                raise UidStateError(
                    f"backfill_allocated_uid: unknown engagement {engagement_id}"
                )
            if rec.allocated_uid != UNALLOCATED_UID:
                return rec.allocated_uid
            if self._uid_allocator is None:
                raise UidStateError(
                    "backfill_allocated_uid: no uid_allocator configured — "
                    "cannot allocate a uid for a legacy record"
                )
            new_uid = self._uid_allocator.allocate()   # may raise UidStateError
            prior = rec.allocated_uid
            rec.allocated_uid = new_uid
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
                rec.allocated_uid = prior
            if cancelled is not None:
                raise cancelled
            if task.exception() is not None:
                raise task.exception()
            return new_uid

    async def set_channel_state(
        self,
        engagement_id: str,
        *,
        pinned_message_id: int | None = None,
        progress_message_id: int | None = None,
        current_state_emoji: str | None = None,
        strict: bool = False,
    ) -> None:
        """E-12 (v0.37.0): update the channel-state subset on a record.

        Each kwarg is applied only if not None; omitting an arg leaves the
        current value untouched. Unknown ``engagement_id`` is a no-op (matches
        the other mutators' tolerance for stale callers).

        ``strict`` (#529): a persistence failure PROPAGATES and the in-memory
        fields ROLL BACK to their prior values — memory must never claim what
        disk refused, because ``update_topic_state``'s no-op guard reads the
        live record and a restart reloads the disk truth. The rollback also
        runs when a cancellation propagated with the settled write FAILED
        (``_last_tombstone_ok`` False); a cancellation whose settled write
        committed keeps the mutation (memory and disk agree).
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            prior = (
                rec.pinned_message_id, rec.progress_message_id,
                rec.current_state_emoji,
            )
            if pinned_message_id is not None:
                rec.pinned_message_id = pinned_message_id
            if progress_message_id is not None:
                rec.progress_message_id = progress_message_id
            if current_state_emoji is not None:
                rec.current_state_emoji = current_state_emoji
            try:
                await self._write_tombstone_locked(strict=strict)
            except BaseException:
                if strict and not self._last_tombstone_ok:
                    (
                        rec.pinned_message_id, rec.progress_message_id,
                        rec.current_state_emoji,
                    ) = prior
                raise

    async def set_initial_state_emoji(
        self, engagement_id: str, emoji: str,
    ) -> None:
        """#529 (Terra design-r2): launch-time initial-emoji publication —
        sets ``current_state_emoji`` ONLY when the record is non-terminal and
        no emoji state exists yet (``None``). An unconditional launch-path
        write could overwrite a settled terminal paint's emoji (disk says
        active on a closed topic, with no later repaint to repair it) or an
        in-flight paint's uncertain ``""`` sentinel (re-arming the wrongful
        no-op the sentinel exists to break). Best-effort persistence, like
        the launch paths it serves."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            if rec.status in ("completed", "cancelled", "error"):
                return
            if rec.current_state_emoji is not None:
                return
            rec.current_state_emoji = emoji
            await self._write_tombstone_locked()

    async def set_procedural_epoch(
        self, engagement_id: str, epoch: str,
    ) -> None:
        """#215: launch-time publication of the procedural epoch this
        engagement's prompt/doctrine bytes were digested to. Write-once
        (a set value is never overwritten) and only on a non-terminal
        record; best-effort persistence like the launch paths it serves."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            if rec.status in ("completed", "cancelled", "error"):
                return
            if rec.procedural_epoch:
                return
            rec.procedural_epoch = epoch
            await self._write_tombstone_locked()

    async def advance_interaction_state(
        self, engagement_id: str, event: str,
    ) -> str | None:
        """W2/Sol B9 (Task 7): atomic compare-and-set on ``interaction_state``.

        Read record -> compute the pure transition -> write field +
        persist, all under ``self._lock`` so two coroutines racing the same
        event on the same record resolve to exactly one transition (the
        second sees the already-advanced state and gets a no-op). Returns
        the new state, or ``None`` for an unknown engagement or a no-op
        transition (never backwards — see ``_pure_interaction_transition``).
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return None
            new_state = _pure_interaction_transition(rec.interaction_state, event)
            if new_state is None:
                return None
            # B3 (Sol r1): persist STRICTLY and roll back on failure — the
            # telegram callback commits the ask on a successful return, so the
            # authorization MUST have reached disk before we report the new
            # state. On a write failure the callback's `except` path
            # abort_claims + "please tap again" (verified end-to-end by
            # test_telegram_inline_callback).
            prev_state = rec.interaction_state

            async def _mutate_and_persist() -> str:
                rec.interaction_state = new_state
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.interaction_state = prev_state
                    raise
                return new_state

            # B4 (Sol diff r2): SHIELD the mutate+persist so cancelling the
            # CALLER (e.g. the telegram callback task) cannot tear the pair.
            # The inner task runs to completion UNDER THE LOCK — on cancel we
            # await it to completion BEFORE re-raising, so the durable write
            # (and, on failure, the rollback) always finishes while we still
            # hold the lock. Without this a CancelledError mid-``to_thread``
            # left the request armed-then-aborted despite disk authorization
            # (expiring ``no_answer`` on an answered ask). The callback treats
            # a cancellation-after-authorization as committable.
            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    # Retrieve any inner exception (already rolled back) so it
                    # is not flagged "never retrieved"; then honor the cancel.
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def set_interaction_violated(self, engagement_id: str) -> None:
        """W2/Sol B9 (Task 7): flag a mutating tool-use taken while
        ``awaiting_operator`` — ``_finalize_engagement`` reads
        ``rec.origin.get("interaction_violated")`` to append a violation
        line to the completion summary. Unknown engagement is a no-op
        (matches the other mutators' tolerance for stale callers).

        B3 (Sol diff r2): persist STRICTLY and roll back on failure, mirroring
        ``advance_interaction_state``. The driver seam
        (``claude_code_driver._on_stream_event``) only marks
        ``_violation_flagged`` after a SUCCESSFUL return, so a swallowed write
        failure would permanently drop the completion warning after a restart;
        raising lets the seam retry on the next mutating-tool frame.
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            had_flag = "interaction_violated" in rec.origin
            prev = rec.origin.get("interaction_violated")
            rec.origin["interaction_violated"] = True
            try:
                await self._write_tombstone_locked(strict=True)
            except Exception:
                if had_flag:
                    rec.origin["interaction_violated"] = prev
                else:
                    rec.origin.pop("interaction_violated", None)
                raise

    # -- v0.79.0 (§4) question numbering + open-question ledger --------------

    async def allocate_question_number(self, engagement_id: str) -> int | None:
        """Atomically allocate the next durable ``Q<n>`` for an engagement.

        Bumps ``next_question_number`` under the lock and persists (same
        transactional shield-and-await pattern as ``advance_interaction_state``
        so a cancelled caller never tears the counter from disk). Returns the
        allocated number, or ``None`` for an unknown engagement."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return None
            allocated = rec.next_question_number
            prev = rec.next_question_number
            rec.next_question_number = allocated + 1

            async def _mutate_and_persist() -> int:
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.next_question_number = prev
                    raise
                return allocated

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def add_open_question(
        self, engagement_id: str, number: int, tg_message_id: int | None,
        text: str | None = None, kind: str = "button",
        source_hash: str | None = None,
    ) -> None:
        """Record a still-open question ``{n, tg_message_id, text, kind}``
        (persisted). ``text`` is the canonical displayed question so boot
        reconciliation can re-render the settle copy over it (memory-only broker
        state does not survive a restart). ``kind`` is ``"button"`` (broker tap)
        or ``"anchor"`` (free-text — settled by the next operator message).
        Idempotent on ``number``.

        wb2-1 (whole-branch gate wave 2): ``source_hash`` (optional) is the
        projection hash of the ask that produced this anchor — the SAME hash the
        topic relay computes for the ask's tool_use block. Persisted so the relay's
        driver-injected ``open_anchor_state`` seam can report it, letting a
        narration-suppression candidate bind POSITIVELY to the anchor its OWN ask
        produced (never a prior / co-existing open anchor). Absent for legacy rows
        and button asks (harmless — button asks are never relay candidates).

        v0.79.0 (§4, Sol F6): STRICT persistence — a tombstone-write failure
        rolls ``open_questions`` back (full-field) and RE-RAISES rather than
        silently leaving a keyboard that the ledger/summary/boot-reconciler
        cannot see. The ask handler settles the keyboard fail-closed on the
        raise. Uses the shield-and-await transactional pattern so cancellation
        during ``to_thread`` cannot split memory from disk."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            prev = rec.open_questions
            entries = [q for q in rec.open_questions if q.get("n") != number]
            entry = {
                "n": number, "tg_message_id": tg_message_id, "kind": kind,
                # v0.83.0 (§A3): the answer-lifecycle flag + re-anchor stale-copy
                # list. New rows carry them explicitly; old rows are .get-tolerant.
                "answered": False, "stale_mids": [],
            }
            if text is not None:
                entry["text"] = text
            if source_hash is not None:
                entry["source_hash"] = source_hash
            entries.append(entry)
            rec.open_questions = tuple(entries)

            async def _mutate_and_persist() -> None:
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.open_questions = prev
                    raise

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def _commit_open_questions_strict(self, rec: EngagementRecord,
                                            prev: tuple[dict, ...]) -> None:
        """Shared strict-persist transaction for the ``open_questions`` mutators
        (§A3): shield-and-await the tombstone write; on failure ROLL BACK the
        whole tuple (``prev``) and RE-RAISE, so a caller can fail closed and a
        cancelled ``to_thread`` never splits memory from disk. Caller MUST hold
        ``self._lock`` and have already assigned the new ``rec.open_questions``."""
        async def _mutate_and_persist() -> None:
            try:
                await self._write_tombstone_locked(strict=True)
            except Exception:
                rec.open_questions = prev
                raise

        task = asyncio.ensure_future(_mutate_and_persist())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)
            raise

    async def mark_question_answered(self, engagement_id: str, number: int) -> bool:
        """§A3 (Sol r2-7): mark an open-question entry ANSWERED — the
        answer-lifecycle decision, split from visual settlement. STRICT-persisted
        (rollback + re-raise like ``add_open_question``). The entry stays in the
        ledger (removed only after a confirmed settle edit) but becomes INVISIBLE
        to ``open_question_numbers``/``oldest_open_anchor`` so the A3 gates and the
        pinned summary stop treating an already-answered question as live.

        Returns ``True`` when an entry with ``number`` was flagged, ``False`` for
        an unknown engagement or number (idempotent on an already-answered entry).
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return False
            prev = rec.open_questions
            found = False
            entries: list[dict] = []
            for q in rec.open_questions:
                nq = dict(q)
                if nq.get("n") == number:
                    found = True
                    nq["answered"] = True
                entries.append(nq)
            if not found:
                return False
            rec.open_questions = tuple(entries)
            await self._commit_open_questions_strict(rec, prev)
            return True

    async def stage_stale_mid(self, engagement_id: str, number: int,
                              mid: int, kind: str = _STALE_KIND_DEFAULT) -> bool:
        """§A3 staged re-anchor: append ``{"mid": mid, "kind": kind}`` to an
        entry's ``stale_mids`` (the OLD copy awaiting a confirmed settle).
        STRICT-persisted, idempotent on ``mid`` (dedup ignores ``kind`` — the
        first stage wins). v0.84.0 (round-4 §D6): ``kind`` defaults to
        ``"plain"`` — the re-anchor pass ALWAYS stages plain (the atomic flip to
        ``"reanchored"``, paired with the new-mid persist, is a separate
        transaction owned by ``update_question_mid`` — Task D2). Any pre-existing
        legacy bare-int entries on this question's list are normalized to dicts
        as a side effect of this write. Returns ``False`` for an unknown
        engagement/number."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return False
            prev = rec.open_questions
            found = False
            entries: list[dict] = []
            for q in rec.open_questions:
                nq = dict(q)
                if nq.get("n") == number:
                    found = True
                    stale = [
                        normalize_stale_mid_entry(m)
                        for m in (nq.get("stale_mids") or [])
                    ]
                    if not any(s["mid"] == mid for s in stale):
                        stale.append({"mid": mid, "kind": kind})
                    nq["stale_mids"] = stale
                entries.append(nq)
            if not found:
                return False
            rec.open_questions = tuple(entries)
            await self._commit_open_questions_strict(rec, prev)
            return True

    async def unstage_stale_mid(self, engagement_id: str, number: int,
                                mid: int) -> bool:
        """§A3: remove ``mid`` from an entry's ``stale_mids`` once its OLD copy is
        confirmed-settled. STRICT-persisted, no-op-tolerant when the mid is absent.
        Normalizes every remaining entry to ``{"mid", "kind"}`` (tolerates a mix
        of legacy bare-int and dict entries on the same list). Returns ``False``
        for an unknown engagement/number."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return False
            prev = rec.open_questions
            found = False
            entries: list[dict] = []
            for q in rec.open_questions:
                nq = dict(q)
                if nq.get("n") == number:
                    found = True
                    nq["stale_mids"] = [
                        normalize_stale_mid_entry(m)
                        for m in (nq.get("stale_mids") or [])
                        if normalize_stale_mid_entry(m)["mid"] != mid
                    ]
                entries.append(nq)
            if not found:
                return False
            rec.open_questions = tuple(entries)
            await self._commit_open_questions_strict(rec, prev)
            return True

    async def update_question_mid(self, engagement_id: str, number: int,
                                  new_mid: int) -> bool:
        """§A3 staged re-anchor step 3: strict-persist an entry's live
        ``tg_message_id`` to ``new_mid`` (the freshly-posted re-anchor copy).
        STRICT-persisted (rollback + re-raise like the sibling mutators), so the
        caller can settle the new copy fail-closed on a raise. Returns ``False``
        for an unknown engagement/number.

        v0.84.0 (round-4 §D6, Sol r3-3/r17): the SAME transaction also flips the
        just-staged stale entry — the one whose ``mid`` equals THIS question's
        OLD ``tg_message_id`` (captured before the overwrite), not every
        ``"plain"`` entry on the question — from ``"plain"`` to ``"reanchored"``.
        Both mutations are folded into the single ``rec.open_questions`` tuple
        assigned before ``_commit_open_questions_strict``, so a failed commit
        rolls back the whole tuple (mid AND kind) via its existing ``prev``
        restore, and a successful commit persists both in one write. No
        intermediate durable (or in-memory) state exists between "old mid +
        plain" and "new mid + reanchored"."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return False
            prev = rec.open_questions
            found = False
            entries: list[dict] = []
            for q in rec.open_questions:
                nq = dict(q)
                if nq.get("n") == number:
                    found = True
                    old_mid = nq.get("tg_message_id")
                    nq["tg_message_id"] = new_mid
                    # D2: atomic flip — same transaction as the mid persist
                    # above. Targets ONLY the staged old-mid entry (mid ==
                    # old_mid), never every "plain" entry on this question.
                    stale = [
                        normalize_stale_mid_entry(m)
                        for m in (nq.get("stale_mids") or [])
                    ]
                    for s in stale:
                        if s["mid"] == old_mid:
                            s["kind"] = "reanchored"
                    nq["stale_mids"] = stale
                entries.append(nq)
            if not found:
                return False
            rec.open_questions = tuple(entries)
            await self._commit_open_questions_strict(rec, prev)
            return True

    def open_question_entries(self, engagement_id: str) -> list[dict]:
        """RAW list copy of every open-question entry — answered or not (§A3).
        Used by the visual settle / boot reconciliation paths, which iterate the
        WHOLE ledger; the gates/summary use the answered-filtered accessors."""
        rec = self._records.get(engagement_id)
        if rec is None:
            return []
        return [dict(q) for q in rec.open_questions]

    def oldest_open_anchor(self, engagement_id: str) -> dict | None:
        """The oldest still-open, UNANSWERED free-text anchor (``kind ==
        "anchor"``), or ``None``. The next operator message settles it (§4).
        v0.83.0 (§A3): answered anchors are excluded — an answered-but-unsettled
        anchor must not gate replies or be re-posted as unresolved."""
        rec = self._records.get(engagement_id)
        if rec is None:
            return None
        anchors = [
            q for q in rec.open_questions
            if q.get("kind") == "anchor" and not q.get("answered", False)
        ]
        if not anchors:
            return None
        return min(anchors, key=lambda q: q.get("n", 0))

    async def close_open_question(self, engagement_id: str, number: int) -> None:
        """Remove a settled question from the open-question ledger (persisted).
        ``next_question_number`` is NEVER rewound. Unknown engagement/number is
        a no-op.

        v0.83.0 (§A3, M4): the closing removal is now STRICT — a tombstone-write
        failure ROLLS BACK ``open_questions`` (full-tuple) and RE-RAISES, like the
        sibling mutators, so the entry can never vanish from memory while surviving
        on disk. Callers (``_settle_ledger_entry``) treat a raise as RETAINED — the
        entry stays present for a later settle / boot-reconcile pass."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            prev = rec.open_questions
            remaining = tuple(
                q for q in rec.open_questions if q.get("n") != number
            )
            if len(remaining) == len(rec.open_questions):
                return
            rec.open_questions = remaining
            await self._commit_open_questions_strict(rec, prev)

    def open_question_numbers(self, engagement_id: str) -> list[int]:
        """Accessor for summary consumers (T4): the sorted list of still-open,
        UNANSWERED question numbers (``Open questions: Q4, Q6``). v0.83.0 (§A3,
        Sol r3-5): answered entries are excluded — this feeds the pinned summary's
        ``Open questions:`` line and ``recompute_engagement_status``, so an
        answered-but-unconfirmed-settle entry must stop showing/gating."""
        rec = self._records.get(engagement_id)
        if rec is None:
            return []
        return sorted(
            q["n"] for q in rec.open_questions
            if "n" in q and not q.get("answered", False)
        )

    # -- v0.79.0 (§5) live-summary state ------------------------------------

    async def set_summary_message_id(
        self, engagement_id: str, message_id: int | None,
    ) -> None:
        """Persist the pinned summary Telegram message id (posted at boot).
        No-op for an unknown engagement.

        v0.79.0 (§5, Sol F6): STRICT persistence — a tombstone-write failure
        rolls ``summary_message_id`` back and RE-RAISES rather than leaving a
        posted-but-unpersisted summary that a restart cannot resume; the boot
        summary post ABORTS the launch on the raise (§5 post-failure-aborts).
        Shield-and-await so cancellation during ``to_thread`` cannot split
        memory from disk."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            prev = rec.summary_message_id
            rec.summary_message_id = message_id

            async def _mutate_and_persist() -> None:
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.summary_message_id = prev
                    raise

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def allocate_summary_revision(self, engagement_id: str) -> int | None:
        """Atomically allocate the next monotonic summary REVISION (§5).

        Every lifecycle status transition acquires its revision here, so the
        three status sources (driver turn lifecycle, ``interaction_state``, ask
        registry) are totally ordered and collision-free. Uses the same
        transactional shield-and-await pattern as ``allocate_question_number``
        (a cancelled caller never tears the counter from disk). Returns the
        allocated revision, or ``None`` for an unknown engagement."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return None
            allocated = rec.summary_revision
            prev = rec.summary_revision
            rec.summary_revision = allocated + 1

            async def _mutate_and_persist() -> int:
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.summary_revision = prev
                    raise
                return allocated

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def sweep_idle_and_suspend(
        self, *, driver: Any, now_override: float | None = None,
    ) -> None:
        """Daily scan: fire idle_detected + tear down clients past suspend threshold.

        ``driver`` is the ``DriverProtocol`` instance for in_casa — used to
        check is_alive, read session_id, and close the client. For tests,
        ``now_override`` short-circuits ``time.time()``.
        """
        import time
        from bus import BusMessage, MessageType

        now = now_override if now_override is not None else time.time()

        for rec in list(self.active_and_idle()):
            idle_s = now - rec.last_user_turn_ts

            # 1) Session suspension (in_casa only)
            if (rec.driver == "in_casa" and rec.status == "active"
                    and idle_s > _SESSION_SUSPEND_IDLE_S
                    and driver.is_alive(rec)):
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

            # 2) Idle reminder
            threshold_s = (
                _IDLE_REMINDER_DAYS_SPECIALIST * 86400
                if rec.kind == "specialist"
                else _IDLE_REMINDER_DAYS_EXECUTOR * 86400
            )
            if idle_s > threshold_s and (
                rec.last_idle_reminder_ts == 0
                or now - rec.last_idle_reminder_ts > _IDLE_REMINDER_REFIRE_DAYS * 86400
            ):
                if self._bus is not None:
                    try:
                        await self._bus.notify(BusMessage(
                            type=MessageType.NOTIFICATION,
                            source=rec.role_or_type,
                            target="observer",
                            content={
                                "event": "idle_detected",
                                "engagement_id": rec.id,
                                "last_user_turn_ts": rec.last_user_turn_ts,
                                "idle_days": int(idle_s // 86400),
                            },
                            context={"engagement_id": rec.id},
                        ))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("sweep: idle notify failed: %s", exc)
                await self.update_last_idle_reminder(rec.id, now)
