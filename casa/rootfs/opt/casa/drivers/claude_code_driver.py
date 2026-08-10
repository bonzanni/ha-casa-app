"""claude_code driver — s6-rc-supervised claude CLI per engagement.

See docs/superpowers/specs/2026-04-23-3.5-plan4a-claude-code-driver-design.md.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import pwd
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from atomic_io import fsync_directory
from drivers import s6_rc
from drivers.brief import brief_task_for
from drivers.driver_protocol import DriverProtocol
from drivers.workspace import (
    control_dir, engagement_log_dir, fifo_path, inbound_spool_path,
    provision_workspace, render_log_run_script, render_run_script,
    session_id_path, stderr_path, stream_cursor_path, write_casa_meta,
)
from engagement_registry import EngagementRecord, normalize_stale_mid_entry
from engagement_uids import UID_BASE, owner_uid_or_none, prune_identity
import plugin_outbox
import private_state
from safe_fs import SymlinkRefused, list_dir_beneath, open_beneath
from settle_gate import confirmed_settle_edit

logger = logging.getLogger(__name__)


# v0.79.0 (§3 Primitive B) — durable inbound envelope spool.
# Priority lanes: ordinary FIFO (cap 10) + redirect FIFO (cap 3), total 13.
_ORDINARY_LANE_CAP = 10
_PRIORITY_LANE_CAP = 3
_SPOOL_FILENAME = ".inbound_spool.jsonl"

# Exact operator-facing copies (§3 "Exact copies"; wording is binding).
_REDIRECT_PREFIX = (
    "[OPERATOR REDIRECT — drop your current agenda, re-plan from this message]"
)
_RECEIPT_COPY = "📥 Received — I'll get to this after the current step."
# §4 boot reconciliation: appended below an open question's stored text when a
# restart orphans it. This is the RESTART-orphaned copy — distinct from the
# live-ask expiry settle (``channel_handlers._SETTLE_EXPIRED``, which becomes
# the operator-away "engagement paused" copy under F-EXPIRE): a boot-orphaned
# question has no live operator-away episode, so the plain "answer by text
# below" wording still applies.
_OPEN_Q_EXPIRED_SUFFIX = "\n⌛ expired — answer by text below"
# §4 free-text anchor: appended when the next operator message answers it.
_OPEN_Q_ANSWERED_SUFFIX = "\n✅ answered below"
# §A3(b) terminal finalize: a live anchor stranded by /cancel or /complete is
# settled (never left visually open forever) with an outcome-appropriate copy.
_OPEN_Q_CANCELLED_SUFFIX = "\n🛑 engagement ended — this question is closed"
# v0.84.0 (round-4 §D6, Sol r15-4): the re-anchor MOVED markers — pinned exact
# text. REPLACES the step-4 OLD-copy edit's previous "↪ question re-posted
# below ↓" suffix, which retained the full duplicated body (read as "asked
# twice"); the marker text below REPLACES the body entirely, never appends to
# it. The SAME open form is also what every stale-settle path
# (``_settle_ledger_entry`` + boot reconciliation) renders for a ``stale_mids``
# entry recorded ``kind="reanchored"`` — the terminal form is used once that
# copy itself is finally settled (answered/expired/cancelled). Deliberately
# named OUTSIDE the ``_OPEN_Q_*``/``_SETTLE_*`` prefixes ``ask_lifecycle_
# suffixes``'s drift test (tests/test_ask_body_limit.py) enumerates: this is a
# standalone REPLACEMENT form, never a body SUFFIX.
_REANCHOR_MOVED_OPEN_FMT = "⤵ MOVED Q{n} — answer the current copy below"
_REANCHOR_MOVED_TERMINAL_FMT = "⤵ MOVED Q{n} — resolved below"


def _reanchor_moved_open(n: "int | None") -> str:
    """The re-anchor step-4 OLD-copy edit + a retained ``reanchored`` stale
    entry's OPEN-form settle (both point forward to the live current copy)."""
    return _REANCHOR_MOVED_OPEN_FMT.format(n=n)


def _reanchor_moved_terminal(n: "int | None") -> str:
    """A ``reanchored`` stale entry's TERMINAL-form settle (rendered once that
    tracked copy is itself finally settled — never the duplicated body)."""
    return _REANCHOR_MOVED_TERMINAL_FMT.format(n=n)


def ask_lifecycle_suffixes(
    number: "int | None", options: list, multi: bool,
) -> list[str]:
    """v0.84.0 (round 4, D1 bullets 3 & 6, Task A3) — enumerate EVERY terminal
    suffix Telegram lifecycle form THIS ask can render, exactly as the
    settle/boot/terminal paths will, for the render-and-measure per-ask
    body-limit validator (``channels.channel_handlers``'s ``ask`` handler):
    ``len(body) + max(len(s) for s in ask_lifecycle_suffixes(...)) <= 4096``.

    ``number`` is accepted for call-site symmetry with
    :func:`channels.channel_handlers.render_ask_body` (the validator renders
    the body and the suffixes for the SAME allocated/approximated number in
    one breath) — no suffix form below actually interpolates the Q-number, so
    it is otherwise unused here.

    Forms enumerated:
      * live answered — the free-text-anchor fixed copy
        (``_SETTLE_ANSWERED_BELOW``) when ``options`` is empty; otherwise the
        BOUNDED POSITIONAL settle copy (v0.84.0 D1 bullet 3 — never the
        chosen full label(s)): single-select worst case is the LAST option
        position; multi worst case is EVERY option selected, rendered
        exactly (``✅ Options 1, 2, …, N`` — every index, no elision);
      * expired / cancelled / superseded / internal-error (live-ask settle
        copies owned by ``channels.channel_handlers``);
      * boot-reconcile answered / expired (``_OPEN_Q_ANSWERED_SUFFIX`` /
        ``_OPEN_Q_EXPIRED_SUFFIX``);
      * terminal cancellation (``_OPEN_Q_CANCELLED_SUFFIX`` — the engagement
        ended while this question was open).

    Every ``_OPEN_Q_*`` body-suffix constant is now enumerated above (the drift
    test's allowlist is empty as of Task D3): the old re-anchor persist-failure
    ``_OPEN_Q_SEE_ABOVE`` body suffix is DELETED (r11-1 — the drained unit's
    finite LOCAL persist retry replaces it, never a body edit), and D1 already
    retired ``_OPEN_Q_REPOSTED_BELOW``. The Task D6 moved-marker forms
    (``_REANCHOR_MOVED_OPEN_FMT`` / ``_REANCHOR_MOVED_TERMINAL_FMT``) are
    deliberately named OUTSIDE the ``_OPEN_Q_*`` prefix this drift test scans —
    standalone REPLACEMENT forms, never a body SUFFIX (never appended to a
    rendered ask body), so they need no allowlist entry at all.

    A function-local import of the settle constants avoids a module-level
    cross-package edge between the driver and channel-handler modules
    (neither imports the other at module scope today) — matching this
    codebase's existing local-import convention for the same pair of modules
    (e.g. ``casa_core``'s lazy ``from channels.channel_handlers import ...``).
    """
    from channels.channel_handlers import (
        _SETTLE_ANSWERED_BELOW, _SETTLE_CANCELLED, _SETTLE_EXPIRED,
        _SETTLE_INTERNAL_ERROR, _SETTLE_SUPERSEDED, _positional_settle_suffix,
    )
    suffixes = [
        _SETTLE_EXPIRED,
        _SETTLE_CANCELLED,
        _SETTLE_SUPERSEDED,
        _SETTLE_INTERNAL_ERROR,
        _OPEN_Q_ANSWERED_SUFFIX,
        _OPEN_Q_EXPIRED_SUFFIX,
        _OPEN_Q_CANCELLED_SUFFIX,
    ]
    if not options:
        suffixes.append(_SETTLE_ANSWERED_BELOW)
    elif multi:
        suffixes.append(_positional_settle_suffix(list(range(len(options)))))
    else:
        suffixes.append(_positional_settle_suffix([len(options) - 1]))
    return suffixes


# §A3(b) retry-owner bounded backoff (Sol r13-1): a FAILED latch-consuming pass
# self-reschedules 5s → 30s → 300s (capped, repeated) indefinitely until the
# latch clears via success / promotion / terminal settlement / a boundary win.
_REANCHOR_BACKOFF: tuple[float, ...] = (5.0, 30.0, 300.0)
# v0.84.0 (round-4 §D6 r17-2, Sol): REAL finite BUDGETS on every wire op in the
# drained re-anchor unit — deadlines that bound ONE physical wire await, NOT
# SLAs (no promise the op completes within them). The send gets exactly ONE
# ``wait_for``-bounded attempt with ZERO wire retries (the send wrapper cannot
# distinguish "not sent" from "accepted before the timeout", so any retry could
# stack an untracked duplicate). The old-copy marker edit is finite-attempt,
# each attempt ``wait_for``-bounded and F1-cache idempotent. The in-unit
# ``tg_message_id`` persist is N bounded LOCAL (registry-file) attempts with a
# short backoff — never a wire op. On ANY budget exhaustion the unit releases
# its locks and takes the documented floor (never indefinite lock ownership).
_REANCHOR_SEND_TIMEOUT = 15.0
_REANCHOR_EDIT_TIMEOUT = 10.0
_REANCHOR_PERSIST_ATTEMPTS = 3
_REANCHOR_PERSIST_BACKOFF = 0.5
# F3 (whole-branch gate): after an UNVERIFIED (False/raised) force-suspend
# outcome the once-per-episode backstop re-arms, but must PACE before it may
# fire again — an immediate re-arm lets a doctrine-defying ask→refusal loop
# churn probes/subprocesses at token speed. A monotonic cooldown gates re-firing.
_AWAY_FORCE_COOLDOWN_S = 60.0
_EVICTION_COPY = (
    "⚠️ Dropped in favor of your redirect — resend if still relevant."
)
_PRIORITY_CAP_COPY = "⚠️ Too many pending redirects — this one was dropped."
_SPOOL_FAIL_COPY = "your message could not be recorded — please resend"
# An ordinary message arriving with the ordinary lane full keeps the existing
# dropped-full notice (§3: "an ordinary message at cap keeps the existing
# dropped-full notice").
_ORDINARY_FULL_COPY = (
    f"Your engagement already has {_ORDINARY_LANE_CAP} messages waiting to be "
    "delivered — this one was dropped. Please wait for it to catch up, then "
    "resend."
)


async def _never_deliver() -> bool:
    """A ``write_fifo`` for a drain-only spool view (reconcile): never delivers.
    """
    return False


async def _completion_noop_poster() -> int | None:
    """Poster for the emit_completion CONSUMPTION-DEBT intent (§2 F1(c)): never
    invoked (a debt block is consumed silently, never posted)."""
    return None


def _is_redirect(text: str) -> bool:
    """§3 redirect detection: ``STOP`` as the (case-insensitive) first line, or
    a ``redirect:`` prefix."""
    if not text:
        return False
    stripped = text.strip()
    if stripped.lower().startswith("redirect:"):
        return True
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    return first_line.lower() == "stop"
# P31 (v0.37.10): match a UUID as a complete filename stem — the
# claude CLI names its session-storage files ``<uuid>.jsonl``. Replaces
# v0.37.9's free-text session_id regex (which tailed a log file that
# never gets created in production; see bug-review-2026-05-14-exploration6).
_UUID_REGEX = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}'
    r'-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

TopicSender = Callable[[int, str], Awaitable[Any]]
SessionIdPersister = Callable[[str, str], Awaitable[None]]
"""(engagement_id, session_id) → None — registry persist hook.

Matches engagement_registry.persist_session_id's bound-method signature."""


# Notice sender: (text, reply_to_message_id) -> delivered_ok.
NoticeSender = Callable[[str, "int | None"], Awaitable[bool]]

# Persisted envelope fields (§3 schema) + the impl-only fields the spool needs.
_ENVELOPE_PERSIST_FIELDS = (
    "text", "tg_message_id", "priority", "receipt", "notice", "notice_text",
    "enqueued_at", "delivery_epoch", "state", "seq", "is_initial",
    "answer_anchor_mid",
)


@dataclass
class _Envelope:
    """One durable inbound operator message (§3).

    Persisted schema: ``{text, tg_message_id, priority, receipt, notice,
    enqueued_at, delivery_epoch, state}``. ``seq`` (FIFO order within a lane)
    and ``is_initial`` (suppress interaction-state advance for the initial
    prompt) are impl fields carried on the same line.

    * ``receipt`` ∈ ``not_required | pending | sent`` — at-least-once receipt.
    * ``notice`` ∈ ``none | pending | sent`` — a pending eviction notice.
    * ``state`` ∈ ``queued | delivered | consumed`` — ``consumed`` only on
      turn_start evidence; ``delivered`` but died pre-turn_start ⇒ redelivered.
    """

    text: str
    tg_message_id: int | None
    priority: bool
    receipt: str
    notice: str
    enqueued_at: float
    delivery_epoch: int | None
    state: str
    seq: int
    is_initial: bool = False
    # §3 / F8: the notice copy to send when ``notice == "pending"``. ``None``
    # means the eviction copy (backward-compatible default); capacity-DROP
    # notices (priority-full / ordinary-full) carry their own copy on a
    # notice-only envelope so the drop notice is durable + retried, not
    # fire-and-forget.
    notice_text: str | None = None
    # v0.83.0 (§A3, Sol r7-1): the tg_message_id of the free-text anchor this
    # message ANSWERED, recorded at durable-enqueue promotion. When set,
    # delivery only THREADS the turn's first post to it (the promotion already
    # ran the visual settle) — delivery never re-settles. ``None`` (field
    # absent on legacy spooled envelopes) keeps the delivery-time settle path.
    answer_anchor_mid: int | None = None

    def to_line(self) -> str:
        return json.dumps(
            {k: getattr(self, k) for k in _ENVELOPE_PERSIST_FIELDS},
            ensure_ascii=False,
        )

    @classmethod
    def from_line(cls, line: str) -> "_Envelope | None":
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or "text" not in data:
            return None
        return cls(
            text=data.get("text", ""),
            tg_message_id=data.get("tg_message_id"),
            priority=bool(data.get("priority", False)),
            receipt=data.get("receipt", "not_required"),
            notice=data.get("notice", "none"),
            notice_text=data.get("notice_text"),
            enqueued_at=float(data.get("enqueued_at", 0.0)),
            delivery_epoch=data.get("delivery_epoch"),
            state=data.get("state", "queued"),
            seq=int(data.get("seq", 0)),
            is_initial=bool(data.get("is_initial", False)),
            answer_anchor_mid=data.get("answer_anchor_mid"),
        )


class _InboundSpool:
    """Durable JSON-lines inbound envelope spool per engagement (§3).

    Replaces the ephemeral ``_InboundQueue``/``.inbound_pending`` marker. Every
    operator message is a durable envelope; delivery is REDELIVERY-BY-
    CONSTRUCTION (a message clears to ``consumed`` only on positive turn_start
    evidence, so a process death pre-turn_start redelivers it on the next
    spawn). Receipts and eviction notices are at-least-once with a durable
    tri-state, retried at every spool touchpoint (enqueue, turn start, turn
    end, boot recovery, terminal drain).

    Priority lanes: redirects (``STOP``/``redirect:``) drain ahead of the
    ordinary FIFO. Caps 10 (ordinary) / 3 (priority); a redirect at ordinary
    cap evicts the newest ordinary (threaded notice), a redirect at priority
    cap drops, an ordinary at cap drops.

    Injected primitives keep it unit-testable in isolation:
    ``write_fifo(text) -> ok``, ``send_notice(text, reply_to) -> ok`` (a
    delivered-ok bool so a failed send stays pending for retry),
    ``is_turn_running() -> bool`` (receipt is due iff a turn is running),
    ``current_epoch() -> int|None`` (stamps delivery / correlates consumption),
    and an optional ``sequencer`` (delivery sets the turn's reply-thread target)
    plus ``registry`` (interaction-state advance).
    """

    def __init__(
        self,
        *,
        engagement_id: str,
        spool_path: str,
        write_fifo: Callable[[str], Awaitable[bool]],
        send_notice: NoticeSender,
        is_turn_running: Callable[[], bool] = lambda: False,
        current_epoch: Callable[[], int | None] = lambda: None,
        sequencer: Any = None,
        registry: Any = None,
        supersede_pending_asks: Callable[[], Awaitable[None]] | None = None,
        settle_anchor_on_delivery: (
            Callable[[int | None], Awaitable[int | None]] | None) = None,
        on_operator_enqueued: Callable[[], Awaitable[None]] | None = None,
        promote_answer_on_enqueue: (
            Callable[[], Awaitable[int | None]] | None) = None,
    ) -> None:
        self._engagement_id = engagement_id
        self._spool_path = spool_path
        self._write_fifo = write_fifo
        self._send_notice = send_notice
        self._is_turn_running = is_turn_running
        self._current_epoch = current_epoch
        self._sequencer = sequencer
        self._registry = registry
        # v0.79.0 (§4): fired when a non-initial operator envelope is enqueued
        # while an ask keyboard is still PENDING — casa-main cancels it as
        # ``superseded_by_text`` so the operator's message doesn't dead-wait.
        self._supersede_pending_asks = supersede_pending_asks
        # F-EXPIRE (A2a): on a durable non-initial operator enqueue (a real
        # inbound envelope — NOT a dropped/capacity notice), end any operator-away
        # suspend episode. Fires from the SAME successful-persist path that bumps
        # the generation, so it is exactly "a durably-enqueued operator message
        # exists". The driver wires this to clear ``_operator_away`` + recompute.
        self._on_operator_enqueued = on_operator_enqueued
        # §4: on delivery of an operator message, settle the oldest open
        # free-text anchor (✅ answered below) and thread the turn to it.
        # Returns the anchor's tg_message_id to thread to, or None.
        self._settle_anchor_on_delivery = settle_anchor_on_delivery
        # §A3 (Sol r7-1/r9-1): at durable non-initial enqueue, PROMOTE the
        # answer — mark the oldest unanswered anchor answered, run the visual
        # settle, CONSUME any reservation. UNCONDITIONAL (any delivered
        # operator message answers the one open question). Returns the anchor's
        # tg_message_id (recorded on the envelope so delivery only THREADS),
        # or None when no anchor is open.
        self._promote_answer_on_enqueue = promote_answer_on_enqueue
        self._envelopes: list[_Envelope] = []
        self.reader_ready = False
        self._pump_lock = asyncio.Lock()
        self._next_seq = 0
        # #341: True while the in-memory envelope state has changes the last
        # persist failed to commit — the touchpoint flush retries the write
        # even when no send outcome changed (a recorded-but-unpersisted
        # capacity-drop notice must not wait for a successful send to become
        # durable).
        self._persist_dirty = False
        # v0.79.0 (§4): monotonic operator-message generation — the ask inbound
        # gate reserves this before BROKER.register and re-checks it after
        # posting to close the arrival race.
        self._generation = 0
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = Path(self._spool_path).read_text(encoding="utf-8")
        except OSError:
            return
        for line in raw.splitlines():
            if not line.strip():
                continue
            env = _Envelope.from_line(line)
            if env is not None:
                self._envelopes.append(env)
        self._next_seq = max((e.seq for e in self._envelopes), default=-1) + 1

    def _persist(self) -> None:
        """Atomic whole-file rewrite (temp + os.replace). Raises OSError on
        failure so the enqueue path can surface the spool-write-FAILURE notice.

        #341: the containing directory is fsynced after the replace so a
        newly-created spool file (and the ``queued``-acked turn in it)
        survives a power crash; a failure sets ``_persist_dirty`` so the
        best-effort touchpoints (``_flush_pending``) retry the write even
        when no send outcome changed.
        """
        payload = "".join(e.to_line() + "\n" for e in self._envelopes)
        tmp = f"{self._spool_path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._spool_path)
        except OSError:
            self._persist_dirty = True
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._persist_dirty = False
        fsync_directory(os.path.dirname(self._spool_path) or ".")

    # -- lane / prune helpers ---------------------------------------------

    def _lane_members(self) -> list[_Envelope]:
        """Envelopes still eligible for delivery (queued, not evicted)."""
        return [
            e for e in self._envelopes
            if e.state == "queued" and e.notice == "none"
        ]

    def _ordinary_count(self) -> int:
        return sum(1 for e in self._lane_members() if not e.priority)

    def _priority_count(self) -> int:
        return sum(1 for e in self._lane_members() if e.priority)

    def _next_deliverable(self) -> "_Envelope | None":
        members = self._lane_members()
        if not members:
            return None
        priority = [e for e in members if e.priority]
        pool = priority if priority else members
        return min(pool, key=lambda e: e.seq)

    @staticmethod
    def _should_retain(env: _Envelope) -> bool:
        if env.notice == "pending":
            return True            # eviction notice still owed
        if env.receipt == "pending":
            return True            # receipt still owed (retried at touchpoints)
        # A live delivery candidate (queued/delivered and NOT an evicted
        # envelope whose notice already went out).
        if env.state in ("queued", "delivered") and env.notice != "sent":
            return True
        return False

    def _prune(self) -> None:
        self._envelopes = [e for e in self._envelopes if self._should_retain(e)]

    def has_pending(self) -> bool:
        return any(
            e.receipt == "pending" or e.notice == "pending"
            for e in self._envelopes
        )

    # -- v0.79.0 (§4) inbound-gate reads -----------------------------------

    def generation(self) -> int:
        """Monotonic operator-message generation (bumped on each successful
        enqueue). The ask gate reserves this, posts, then re-checks it — a
        change means an operator message arrived in the race window."""
        return self._generation

    def unread_texts(self) -> list[str]:
        """G4 D4 (v0.96.0): the TEXTS of queued operator envelopes the agent
        has not seen (same population as ``unread_depth`` — queued,
        non-initial, deliverable). ``delivered`` is excluded: it is not
        proof of non-reading (Sol g4-r1-5)."""
        return [e.text for e in self._lane_members() if not e.is_initial]

    def unread_depth(self) -> int:
        """Count of queued operator envelopes the agent has NOT yet seen
        (deliverable, not-yet-delivered, non-initial). ``depth > 0`` at ask
        entry means the operator is waiting — the ask is refused."""
        return sum(1 for e in self._lane_members() if not e.is_initial)

    # -- enqueue -----------------------------------------------------------

    async def enqueue(
        self, text: str, *, tg_message_id: int | None = None,
        is_initial: bool = False,
    ) -> str:
        """Append an operator envelope; return a durable disposition string
        (§3): ``queued`` | ``dropped_full`` | ``evicted_other(<tg_id>)`` |
        ``error`` — reported AFTER the atomic spool write.
        """
        is_redirect = (not is_initial) and _is_redirect(text)
        evicted_tg: int | None = None
        evicted = False
        if is_redirect:
            if self._priority_count() >= _PRIORITY_LANE_CAP:
                await self._record_drop_notice(_PRIORITY_CAP_COPY, tg_message_id)
                return "dropped_full"
            if self._ordinary_count() >= _ORDINARY_LANE_CAP:
                victim = max(
                    (e for e in self._lane_members() if not e.priority),
                    key=lambda e: e.seq, default=None,
                )
                if victim is not None:
                    victim.notice = "pending"       # retained for its notice
                    evicted_tg = victim.tg_message_id
                    evicted = True
        elif self._ordinary_count() >= _ORDINARY_LANE_CAP:
            await self._record_drop_notice(_ORDINARY_FULL_COPY, tg_message_id)
            return "dropped_full"

        receipt = (
            "pending" if (not is_initial and self._is_turn_running())
            else "not_required"
        )
        env = _Envelope(
            text=text, tg_message_id=tg_message_id, priority=is_redirect,
            receipt=receipt, notice="none", enqueued_at=time.time(),
            delivery_epoch=None, state="queued", seq=self._next_seq,
            is_initial=is_initial,
        )
        self._next_seq += 1
        self._envelopes.append(env)
        try:
            self._persist()
        except OSError as exc:
            # Spool-write FAILURE — the ONLY enqueue-time notice (§3, S4 fix).
            logger.warning(
                "engagement %s: inbound spool write failed: %s",
                self._engagement_id[:8], exc,
            )
            self._envelopes.pop()               # roll the in-memory add back
            if evicted:                         # un-evict the victim we touched
                for e in self._envelopes:
                    if e.tg_message_id == evicted_tg and e.notice == "pending":
                        e.notice = "none"
                        break
            await self._send_notice(_SPOOL_FAIL_COPY, tg_message_id)
            return "error"

        # §4: the enqueue succeeded — bump the operator-message generation and,
        # for a real operator turn (not the initial task), supersede any ask
        # keyboard still waiting for a tap (it must not dead-wait behind a
        # message the operator has already sent).
        self._generation += 1
        if not is_initial and self._supersede_pending_asks is not None:
            try:
                await self._supersede_pending_asks()
            except Exception:  # noqa: BLE001 — supersession is best-effort
                logger.debug("supersede_pending_asks failed", exc_info=True)
        # F-EXPIRE (A2a) EXIT: a durably-enqueued operator message ends the
        # operator-away episode (clear the flag + recompute the summary so the
        # ⏸ coercion window closes crisply). Best-effort; the initial task never
        # clears (there is no away state at spawn).
        if not is_initial and self._on_operator_enqueued is not None:
            try:
                await self._on_operator_enqueued()
            except Exception:  # noqa: BLE001 — away-clear is best-effort
                logger.debug("on_operator_enqueued failed", exc_info=True)

        # §A3 (Sol r7-1/r9-1): PROMOTE the answer at durable enqueue — the
        # message is now durably spooled, so the agent WILL receive it and the
        # question is answered. Promotion marks the oldest unanswered anchor
        # answered + runs its visual settle + CONSUMES any reservation, and
        # records the anchor mid on THIS envelope so delivery only threads
        # (never re-settles). Runs BEFORE ``_pump`` so an immediate delivery
        # sees the recorded mid. Best-effort: a failure only degrades the
        # delivery reply-thread (pinned advisory).
        if not is_initial and self._promote_answer_on_enqueue is not None:
            try:
                anchor_mid = await self._promote_answer_on_enqueue()
                if anchor_mid is not None:
                    env.answer_anchor_mid = anchor_mid
                    self._persist_quiet()
            except Exception:  # noqa: BLE001 — promotion settle is advisory
                logger.debug("promote_answer_on_enqueue failed", exc_info=True)

        await self._flush_pending()
        await self._pump()
        if evicted:
            return f"evicted_other({evicted_tg})"
        return "queued"

    # -- receipt / notice at-least-once flush ------------------------------

    async def _record_drop_notice(
        self, copy: str, tg_message_id: int | None,
    ) -> None:
        """F8: record a DURABLE capacity-drop notice (priority-full / ordinary-
        full). The dropped operator envelope itself is gone, but the notice must
        NOT be fire-and-forget — a failed send has to survive and retry. A
        notice-only envelope (``state=consumed``, ``notice=pending`` carrying its
        own ``notice_text``) is appended + persisted; it rides the same
        at-least-once retry lane as eviction notices (flushed here now, and again
        at every touchpoint until it sends). ``has_pending()`` stays True while
        the send has not succeeded."""
        env = _Envelope(
            text="", tg_message_id=tg_message_id, priority=False,
            receipt="not_required", notice="pending", notice_text=copy,
            enqueued_at=time.time(), delivery_epoch=None, state="consumed",
            seq=self._next_seq, is_initial=False,
        )
        self._next_seq += 1
        self._envelopes.append(env)
        self._persist_quiet()
        await self._flush_pending()

    async def _flush_pending(self) -> None:
        """Retry every pending receipt/notice (§3 touchpoint). Notice-first
        suppression: an envelope with BOTH pending sends ONLY the notice, then
        flips its receipt to ``not_required`` (one operator message, never two).
        """
        changed = False
        for env in self._envelopes:
            if env.notice == "pending":
                ok = await self._send_notice(
                    env.notice_text or _EVICTION_COPY, env.tg_message_id)
                if ok:
                    env.notice = "sent"
                    if env.receipt == "pending":
                        env.receipt = "not_required"   # notice-first suppression
                    changed = True
            elif env.receipt == "pending":
                ok = await self._send_notice(_RECEIPT_COPY, env.tg_message_id)
                if ok:
                    env.receipt = "sent"
                    changed = True
        # #341: also persist when a PRIOR write failed (``_persist_dirty``) —
        # otherwise a recorded notice/receipt whose first write failed stays
        # memory-only until some send succeeds, and a crash loses it.
        if changed:
            self._prune()
        if changed or self._persist_dirty:
            try:
                self._persist()
            except OSError as exc:                 # best-effort; retried next tick
                logger.warning(
                    "engagement %s: spool persist after flush failed: %s",
                    self._engagement_id[:8], exc,
                )

    def mark_all_settled(self) -> None:
        """Boot-reconciliation WARN-drop (§3): topic is gone — settle every
        pending receipt/notice so it stops retrying, WITHOUT sending."""
        for env in self._envelopes:
            if env.notice == "pending":
                env.notice = "sent"
            if env.receipt == "pending":
                env.receipt = "not_required"
        self._prune()

    # -- lifecycle touchpoints --------------------------------------------

    async def on_spawn(self) -> None:
        """A ``spawn`` control frame — arm the reader, REDELIVER any envelope
        still ``delivered`` (a prior epoch that never reached turn_start), pump.
        """
        self.reader_ready = True
        reverted = False
        for env in self._envelopes:
            if env.state == "delivered":
                env.state = "queued"
                env.delivery_epoch = None
                reverted = True
        if reverted:
            self._persist_quiet()
        await self._pump()

    async def on_turn_start(self) -> None:
        """turn_start evidence: the envelope delivered THIS epoch is now
        ``consumed``. Retry pending receipts/notices.

        #341: consumed envelopes owing nothing are PRUNED before the persist
        — they used to be retained forever (``_prune`` only ran when a
        pending send succeeded), growing the spool without bound and making
        every later whole-file rewrite larger and likelier to fail."""
        epoch = self._current_epoch()
        changed = False
        for env in self._envelopes:
            if env.state == "delivered" and env.delivery_epoch == epoch:
                env.state = "consumed"
                changed = True
        if changed:
            self._prune()
            self._persist_quiet()
        await self._flush_pending()

    async def on_turn_end(self) -> None:
        """result / turn boundary: retry pending receipts/notices, prune."""
        await self._flush_pending()

    async def recover(self) -> None:
        """Boot recovery (replaces the zero-with-uncertainty notice path):
        revert stale ``delivered`` envelopes to ``queued`` (redelivery), retry
        pending receipts/notices, pump if already armed."""
        reverted = False
        for env in self._envelopes:
            if env.state == "delivered":
                env.state = "queued"
                env.delivery_epoch = None
                reverted = True
        if reverted:
            self._persist_quiet()
        await self._flush_pending()
        await self._pump()

    async def drain(self) -> None:
        """Terminal pre-close drain (§3): flush pending receipts/notices while
        the topic is still open."""
        await self._flush_pending()

    def _persist_quiet(self) -> None:
        try:
            self._persist()
        except OSError as exc:
            # #341: remember the unpersisted state so the touchpoint flush
            # retries the write (belt-and-suspenders beside _persist's own
            # flag — this also covers a persist injected/wrapped in tests).
            self._persist_dirty = True
            logger.warning(
                "engagement %s: spool persist failed: %s",
                self._engagement_id[:8], exc,
            )

    async def _pump(self) -> None:
        async with self._pump_lock:
            while self.reader_ready:
                env = self._next_deliverable()
                if env is None:
                    break
                text = env.text
                if env.priority:
                    text = f"{_REDIRECT_PREFIX}\n{env.text}"
                ok = await self._write_fifo(text)
                if not ok:
                    # Retain + stay armed — retry on the next spawn / enqueue.
                    break
                env.state = "delivered"
                env.delivery_epoch = self._current_epoch()
                self.reader_ready = False           # one message per FIFO EOF
                self._persist_quiet()
                # §4/§A3: thread this turn to the ANCHOR the message answered,
                # not the operator's own message. Promotion (at durable enqueue)
                # already ran the visual settle and recorded the anchor mid on
                # the envelope — so delivery only THREADS to it (no second
                # settle edit). A LEGACY envelope spooled before ``answer_anchor
                # _mid`` existed carries no mid → fall back to the delivery-time
                # settle path.
                thread_to = env.tg_message_id
                if not env.is_initial:
                    if env.answer_anchor_mid is not None:
                        thread_to = env.answer_anchor_mid
                    elif self._settle_anchor_on_delivery is not None:
                        try:
                            anchor_id = await self._settle_anchor_on_delivery(
                                env.tg_message_id)
                            if anchor_id is not None:
                                thread_to = anchor_id
                        except Exception:  # noqa: BLE001 — anchor settle advisory
                            logger.debug("settle_anchor_on_delivery failed",
                                         exc_info=True)
                # Delivery context: thread this turn's first sequencer post to
                # the operator's message (§3) — or the anchor it answered (§4).
                if self._sequencer is not None:
                    try:
                        self._sequencer.set_turn_reply_to(thread_to)
                    except Exception:  # noqa: BLE001 — advisory threading only
                        logger.debug("set_turn_reply_to failed", exc_info=True)
                # Advance interaction state for ordinary operator turns only.
                if not env.is_initial and self._registry is not None:
                    fn = getattr(
                        self._registry, "advance_interaction_state", None,
                    )
                    if fn is not None:
                        await fn(self._engagement_id, "operator_turn")


class UidDropRefused(Exception):
    """Raised by ``_preflight_uid_drop`` when the preconditions for launching
    an engagement's CLI subprocess under its allocated uid (via setpriv)
    cannot be verified. The caller must refuse — never plant/start the s6
    service and never fall back to a root spawn (containment stage 2,
    Task 7)."""


def _preflight_uid_drop(rec: EngagementRecord, ws: str) -> None:
    """Fail-closed gate, run BEFORE the s6 service is planted: verify the
    ``setpriv`` uid drop that ``render_run_script`` bakes into the run
    script (Task 6) can actually succeed at spawn time. Every precondition
    checked here is made true by a LATER task — the chown + passwd append
    (Task 8) and plugin-artifact mode normalization (Task 11) — so this
    check is correct now and simply starts passing once those tasks land;
    it must never be loosened to "skip if not yet true".

    Raises ``UidDropRefused`` on the first failing check; callers must not
    swallow it and plant the service anyway.
    """
    if shutil.which("setpriv") is None:
        raise UidDropRefused("setpriv not found on PATH — cannot drop uid")

    # GHSA-569r-7crq-xr43: dropping to a uid that can then read a Supervisor
    # bearer token is worse than not dropping at all — it hands the engagement
    # every add-on's stored secrets. Checked HERE, at the point of use, and
    # re-stated from the filesystem on every launch rather than read from a
    # boot-time latch: a fresh launch happens long after boot, and a latch would
    # have to be carried, cleared, and consulted by every future start path.
    offenders = private_state.credential_modes_ok()
    if offenders:
        raise UidDropRefused(
            "private runtime state is readable beyond root ("
            + ", ".join(offenders)
            + ") — refusing to start a uid-dropped engagement"
        )

    uid = rec.allocated_uid
    if uid < UID_BASE:
        raise UidDropRefused(
            f"allocated_uid {uid} is below UID_BASE ({UID_BASE}) — "
            "unallocated or otherwise invalid"
        )

    try:
        ws_stat = os.stat(ws)
    except OSError as exc:
        raise UidDropRefused(
            f"cannot stat workspace {ws!r}: {exc}"
        ) from exc
    if ws_stat.st_uid != uid:
        raise UidDropRefused(
            f"workspace {ws!r} is owned by uid {ws_stat.st_uid}, not the "
            f"allocated uid {uid} — the uid-drop chown has not landed"
        )

    try:
        pwd.getpwuid(uid)
    except KeyError as exc:
        raise UidDropRefused(
            f"no passwd entry for uid {uid} — NSS identity not provisioned"
        ) from exc

    _check_plugin_dirs_readable(uid, getattr(rec, "plugin_artifacts", ()))


def _check_plugin_dirs_readable(uid: int, plugin_artifacts) -> None:
    """Shared by :func:`_preflight_uid_drop` (fresh launch) and the boot-replay
    migration loop (``casa_core.replay_undergoing_engagements``) — containment
    stage 2 parity: BOTH paths render a run script that passes each pinned
    plugin artifact to the CLI as a ``--plugin-dir`` flag, so both must verify
    the subprocess, running AS *uid*, can actually traverse into and read it
    before planting/starting the service. ``os.access()`` is not a reliable
    check here: run as root (both callers always are), it reports what root
    could do, not what the target uid could do. So check mode bits directly:
    either the uid owns the dir (its own artifacts), or the "other" bits
    grant read+execute (o+rx == 0o005) — traverse (x) to enter, read (r) to
    list/open entries beneath it. Raises ``UidDropRefused`` on the first
    failing artifact; callers must not swallow it and plant/start the
    service anyway."""
    for artifact in plugin_artifacts or ():
        path = artifact["path"]
        try:
            pa_stat = os.stat(path)
        except OSError as exc:
            raise UidDropRefused(
                f"cannot stat plugin dir {path!r}: {exc}"
            ) from exc
        if pa_stat.st_uid == uid:
            continue
        if (pa_stat.st_mode & 0o005) != 0o005:
            raise UidDropRefused(
                f"plugin dir {path!r} is not world-readable+traversable "
                f"and not owned by uid {uid} (mode {oct(pa_stat.st_mode)})"
            )


class ClaudeCodeDriver(DriverProtocol):
    """s6-rc orchestrator. Does not manage subprocesses directly."""

    def __init__(
        self,
        *,
        engagements_root: str,
        send_to_topic: TopicSender,
        casa_framework_mcp_url: str,
        persist_session_id: SessionIdPersister | None = None,
        edit_topic_message: Callable[[int, int, str], Awaitable[bool]] | None = None,
        delete_topic_message: Callable[[int, int], Awaitable[bool]] | None = None,
        send_topic_message_markup: Callable[..., Awaitable[int | None]] | None = None,
        edit_topic_message_markup: Callable[..., Awaitable[bool]] | None = None,
        send_to_topic_paged: TopicSender | None = None,
        pin_topic_message: Callable[[int, int], Awaitable[bool]] | None = None,
        registry: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        reanchor_retry_sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        executor_defn_lookup: Callable[[str], Any] | None = None,
    ) -> None:
        self._engagements_root = engagements_root
        # #369: (executor_type) -> definition | None, wired by casa_core to
        # executor_registry.definition_any — rebuild_fresh_context re-renders
        # CLAUDE.md after a clearance downgrade and needs the defn's template.
        self._executor_defn_lookup = executor_defn_lookup
        # F3 (whole-branch gate): injectable monotonic clock for the force-suspend
        # re-arm cooldown (tests advance it deterministically; never patch a
        # shared module attribute).
        self._monotonic = monotonic
        # W-R1 (v0.81.0): injectable clock for the confirmed-edit settle retry
        # (``confirmed_settle_edit``). Kept injectable so tests stay fast and
        # NEVER patch ``<module>.asyncio.sleep`` (the shared module attribute).
        self._sleep = sleep
        # §A3(b) retry owner: a SEPARATE injectable clock for the re-anchor retry
        # backoff, so a test can record its schedule without polluting the
        # settle-gate's ``_sleep`` recorder. Defaults to ``sleep``.
        self._reanchor_retry_sleep = reanchor_retry_sleep or sleep
        # ``send_to_topic`` doubles as the relay's ``send_message`` primitive:
        # casa_core wires it to return the posted Telegram message_id (the relay
        # needs it to edit the rolling message), while notice/warning callers
        # ignore the return.
        self._send_to_topic = send_to_topic
        # v0.109.0 (G5): paged rich sender for the ONE-SHOT terminal completion
        # post — injected into each sequencer as its ``send_paged`` primitive.
        self._send_to_topic_paged = send_to_topic_paged
        self._edit_topic_message = edit_topic_message
        self._delete_topic_message = delete_topic_message
        # A9 (v0.83.0): markup-capable topic send/edit primitives, injected into
        # the per-engagement OutputSequencer so post_discrete/edit_discrete drive
        # keyboard-bearing writes through the single writer.
        self._send_topic_message_markup = send_topic_message_markup
        self._edit_topic_message_markup = edit_topic_message_markup
        # v0.79.0 (§5): best-effort pin of the live summary message. Takes
        # ``(topic_id, message_id)`` and returns pin-ok. None (or an ungranted
        # can_pin_messages) leaves the summary unpinned but still live.
        self._pin_topic_message = pin_topic_message
        self._casa_framework_mcp_url = casa_framework_mcp_url
        self._persist_session_id = persist_session_id
        # ``advance_interaction_state`` (Task 7) lives on this registry; the
        # inbound queue reaches for it via getattr (no-op until it exists).
        self._registry = registry
        # Per-engagement background tasks (respawn poller, session-id capture,
        # ALWAYS-on live topic-stream relay, DEBUG log relay).
        self._tasks: dict[str, list[asyncio.Task]] = {}
        self._last_turn_ts: dict[str, float] = {}
        # v0.79.0 (§3): durable inbound envelope spool + per-turn reply-text set
        # (relay reply de-dup) + current spawn epoch pending a result
        # (abnormal-exit probe) + per-engagement turn-running flag (receipt is
        # due iff a turn is running — set at turn_start, cleared at result/spawn).
        self._inbound: dict[str, _InboundSpool] = {}
        self._reply_texts: dict[str, set[str]] = {}
        self._epoch_pending: dict[str, int | None] = {}
        self._turn_running: dict[str, bool] = {}
        # W2/Sol B9 (Task 7): at most ONE in-topic violation notice per
        # engagement — guards the mutating_tool seam in _on_stream_event.
        # B4 (Sol r1): the notice-post and the flag-persist are tracked
        # INDEPENDENTLY; each marker is set only AFTER its own effect succeeds,
        # so a transient failure of one retries on the next mutating_tool frame
        # instead of permanently skipping both.
        self._violation_notified: set[str] = set()
        self._violation_flagged: set[str] = set()
        # v0.79.0 (§2 Primitive A): ONE per-engagement OUTPUT SEQUENCER (the
        # single serialized topic writer that owns the high-water mark, the
        # no-op edit gate and the relay-mediated discrete-posting intent
        # registry). Shared between this engagement's topic-stream relay and the
        # discrete ingresses — the ask/reply/permission/emit_completion ingress
        # adapters (T2/T3) reach it through this driver's intent-registration
        # API (register_send_intent / arm_send_intent / cancel_send_intent),
        # resolving the driver via ``agent.active_claude_code_driver`` exactly
        # as tools.emit_completion already does.
        self._sequencers: dict[str, "OutputSequencer"] = {}
        # v0.79.0 (§5): ONE per-engagement live-SUMMARY controller (owns the
        # pinned first topic message; consumers submit desired state and it
        # coalesces/throttles edits through the sequencer). Dropped on teardown.
        self._summaries: dict[str, "SummaryController"] = {}
        # v0.79.0 (§4): consecutive ask-inbound-gate refusals in the CURRENT
        # turn (reset at turn_start). From the 3rd the refusal copy escalates +
        # a WARN counter is logged (soft anti-livelock — no hard force-end).
        self._ask_refusals: dict[str, int] = {}
        # G4 (v0.96.0): completion-gate state
        self._completion_refusals: dict[str, int] = {}
        self._inbound_reservations: dict[str, int] = {}
        # v0.83.0 (§A3, Sol r6-4 + r7-2): in-memory ANSWERED overlay. When
        # ``mark_question_answered``'s STRICT persist raises (the durable envelope
        # is already spooled, so the agent WILL get the answer — the question must
        # not keep gating), the caller records the number here so the live process
        # treats the question answered immediately. It is UNIONED onto the
        # persisted flag by ``_effective_open_question_numbers`` (gates/summary/
        # re-anchor honor overlay ∪ persisted) and each later settle attempt
        # RETRIES the strict persist; crash convergence is owned by boot
        # reconciliation (the overlay is memory-only, dropped at teardown).
        self._answered_overlay: dict[str, set[int]] = {}
        # v0.83.0 (§A3, Sol r7-1/r8-1/r9-1): the answered-RESERVATION token map,
        # ``engagement_id -> (token, question_n)``. Set at Telegram handler entry
        # (same synchronous section as the high-water advance) for the oldest
        # unanswered anchor, carrying a unique per-message uuid token — so any
        # finalize that observed the answer's high-water also observes the
        # reservation. A reserved-but-unpromoted question counts as ANSWERED for
        # the effective/union view (gates/summary/re-anchor). ROLLBACK is
        # token-CAS'd (a later message's reservation is never clobbered);
        # PROMOTION at durable enqueue is UNCONDITIONAL and CONSUMES it.
        # Memory-only (crash convergence is boot reconciliation's job).
        self._answer_reservations: dict[str, tuple[str, int]] = {}
        # v0.83.0 (§A3(a)+(c), Sol r2-8/r3-6/r4-4/5): the per-engagement
        # ASK-MAINTENANCE lock + the ingress-reservation ``ask_inflight`` marker.
        #
        # PINNED LOCK DISCIPLINE: ``ask_maintenance_lock`` is taken ONLY on
        # driver/handler tasks with the sequencer lock NOT held — no
        # sequencer→maintenance edge exists. The relay-invoked poster runs UNDER
        # the sequencer lock and therefore NEVER acquires it (it clears the marker
        # lock-free instead — asyncio run-to-completion makes the synchronous
        # marker→durable-ownership handoff gap-free). The lock serializes the ask
        # ingress reservation (check pending predicate + set marker) against the
        # reply gate's check, so two concurrent asks can never both pass their
        # gates before one reaches durable ownership.
        self._ask_maint_locks: dict[str, asyncio.Lock] = {}
        # ``engagement_id -> request_id`` of an ask that passed its gates but has
        # not yet reached durable ownership (broker register for buttons /
        # add_open_question for anchors). Set under the maintenance lock at
        # ingress; cleared SYNCHRONOUSLY (no lock) at durable ownership and on
        # every terminal failure path. Memory-only, dropped at teardown.
        self._ask_inflight: dict[str, str] = {}
        # v0.83.0 (§A3(b), Sol r10-1/r11-1/r13-1): the per-engagement re-anchor-due
        # LATCH — the standing OBLIGATION to keep the oldest unanswered anchor LAST.
        # SET when a re-anchor pass fails/aborts or is suppressed by a reservation
        # that then rolls back; CONSUMED (checked + cleared, only on a True pass)
        # at EVERY turn boundary (result-after-on_turn_end, spawn-without-result,
        # terminal finalize, and the rollback/non-delivery completion itself).
        self._reanchor_due: set[str] = set()
        # ONE retry task per engagement (Sol r13-1 note 1): a FAILED latch-consuming
        # pass is its own retry owner — it self-reschedules with bounded backoff
        # (injectable) until the latch clears (success / promotion / terminal
        # settlement) or a boundary consumer beats it. Double-arm is a no-op;
        # CancelledError terminates WITHOUT rescheduling; cancelled at teardown.
        self._reanchor_retry_tasks: dict[str, asyncio.Task] = {}
        # v0.84.0 (round-4 §D6, Sol r18-2/r19-3/r28-2): the process-lifetime
        # per-engagement CONFIRMED-PAIR record ``{engagement_id: {q -> new_mid}}``.
        # Each entry owns a re-anchor copy that IS on the wire (mid confirmed) but
        # whose LOCAL persist never committed. Retained by the DRIVER, independent
        # of any retry task (a task-local pairing would drop on owner retirement,
        # re-opening the wire-bearing pass — Sol r19-3). A record exists ONLY while
        # unpersisted (Sol r28-2): it retires the moment its transaction commits
        # (the durable ``reanchored`` stale entry the commit created owns the rest)
        # OR on a confirmed settlement marker-edit of the orphan. Memory-only — a
        # crash drops the whole map (the accepted at-least-once crash residual, one
        # orphan per retained pair). While a pair is unpersisted NO new send is
        # permitted for that question (the boundary consults this FIRST).
        self._confirmed_pairs: dict[str, dict[int, int]] = {}
        # The UNIFIED HISTORICAL SCHEDULER rotation cursor per engagement (Sol
        # r24-2): advanced after EVERY step, success or failure — oldest-first
        # would let a permanently-failing pair starve every younger pair's cleanup.
        self._historical_cursor: dict[str, int] = {}
        # §A3(b) boot reconciliation owner (Sol r6-3): readiness-gated reconcile
        # tasks for refused/terminal-with-questions records (attached records
        # retain theirs in ``self._tasks``). Retained here so they are not GC'd
        # before the Telegram-readiness barrier lifts; each self-removes on done.
        self._boot_reconcile_tasks: list[asyncio.Task] = []
        # F-EXPIRE (v0.83.0, A2a): operator-away suspend state. ``_operator_away``
        # is SET on ask expiry (generation-CAS, ``note_operator_away``) and
        # CLEARED on the next durable inbound operator envelope; while set, every
        # further ask is refused and the summary coerces to ⏸ paused.
        # ``_away_refusals`` counts consecutive away-refusals in the current
        # away-episode (Task 5's force-turn-boundary backstop reads it; reset when
        # away clears). In-memory only — a Casa restart lands in the same
        # FIFO-blocked suspended state anyway, so nothing is persisted.
        self._operator_away: dict[str, bool] = {}
        self._away_refusals: dict[str, int] = {}
        # F-EXPIRE (v0.83.0, A2b): the HARD backstop. On the 2nd away-refusal in
        # an episode the driver force-ends the CLI turn ONCE (``_away_suspend_fired``
        # gates re-firing; reset when away clears). Before signalling, the current
        # spawn epoch is stamped into ``_forced_suspend_epochs`` so the ensuing
        # respawn's abnormal-exit log reads "forced suspend (operator away)" at
        # INFO instead of the scary WARN. ``_force_turn_boundary`` is the injected
        # kill callable (defaults to the verified group-kill in s6_rc).
        self._forced_suspend_epochs: dict[str, int | None] = {}
        self._away_suspend_fired: set[str] = set()
        # F3 (whole-branch gate): monotonic deadline BEFORE which a re-armed
        # backstop may NOT re-fire (set after an unverified False/raised outcome;
        # cleared when the away episode ends). Paces probe/subprocess churn.
        self._away_force_cooldown_until: dict[str, float] = {}
        self._force_turn_boundary = s6_rc.force_turn_boundary
        # A2b (Sol A2 review): the in-flight force-suspend task, held per
        # engagement so ``_clear_operator_away`` (operator returned) can CANCEL a
        # backstop kill still verifying group extinction — the turn boundary the
        # operator's own message will now provide makes the forced one moot.
        self._force_tasks: dict[str, asyncio.Task] = {}
        # A2b (Sol A2 wave-3, Finding 2): handed-off post-SIGTERM cleanup tasks.
        # When a force-suspend task is cancelled after SIGTERM was delivered,
        # ``force_turn_boundary`` hands its shielded extinction-poll + SIGKILL
        # escalation to ``_register_force_cleanup``. Those tasks are bounded
        # (≤ ~7 s) and MUST run to completion — teardown NEVER cancels this set
        # (unlike ``_tasks``). Tracked in a DEDICATED set (not ``_tasks[eng_id]``,
        # which teardown pops+cancels and which a post-teardown handoff would
        # otherwise resurrect as a stale entry); each task self-retires via an
        # ``add_done_callback`` so completion leaves no reference.
        self._force_cleanups: set[asyncio.Task] = set()

    # -- DriverProtocol ---------------------------------------------------

    async def start(
        self, engagement: EngagementRecord, prompt: str, options: Any,
        expected_generation: int | None = None,
    ) -> None:
        """options is the ExecutorDefinition — see DriverProtocol.start docstring.

        Bug 13 (v0.14.6): if any step from provision_workspace through
        start_service fails, roll back the partial state (workspace,
        service dir, s6-rc compile) so the engagement registry / sweeper
        don't end up with a permanent UNDERGOING ghost. The exception is
        re-raised so engage_executor's caller surfaces the failure.
        """
        defn = options
        # Workspace path is deterministic — compute it up front so the
        # rollback path can rmtree even if provision_workspace raises
        # before returning the assignment.
        ws_path = str(Path(self._engagements_root) / engagement.id)
        service_dir_written = False
        async with s6_rc._compile_lock:
            try:
                # M4: precompute executor_memory if the executor opts in.
                # Forward-compat — no claude_code executor opts in today, but
                # threading the slot now means a future memory-enabled
                # claude_code executor (e.g. claude_code-flavoured
                # configurator) works without further plumbing. Lazy import
                # of tools avoids a top-level cycle (drivers ← agent ← drivers);
                # _fetch_executor_archive lazily imports agent itself.
                executor_memory_block = ""
                if defn.memory.enabled:
                    from tools import _fetch_executor_archive
                    executor_memory_block = await _fetch_executor_archive(
                        task=engagement.task,
                        origin_channel=engagement.origin.get("channel", "telegram"),
                        token_budget=defn.memory.token_budget,
                        # #369/#336: filter at the record's own markers, as the
                        # in_casa launch does — omitting them fell through to
                        # channel-keyed clearance (telegram ⇒ private), handing
                        # a low-clearance creator the operator's private tier.
                        origin_route=engagement.origin.get("_origin_route"),
                        origin_clearance=engagement.origin.get(
                            "_origin_clearance"),
                    )

                # §3.3: a workspace-template/ (e.g. plugin-developer) selects
                # the template render path — independent of plugin assignment.
                exec_dir = Path(defn.prompt_template_path).parent
                template_root = exec_dir / "workspace-template"

                # 1. Provision workspace (CLAUDE.md, .mcp.json, FIFO, meta).
                ws = await provision_workspace(
                    engagements_root=self._engagements_root,
                    engagement_id=engagement.id,
                    # #335: bake the record's secret into .mcp.json — the id
                    # header alone no longer binds any authority.
                    engagement_auth_token=engagement.auth_token,
                    defn=defn,
                    # W3 (Task 8): the CLAUDE.md {task} carries the full brief
                    # envelope (acceptance criteria + verbatim process
                    # requirements + completion accounting), derived from the
                    # RAW origin["brief"]; falls back to engagement.task when
                    # the engagement has no brief.
                    task=brief_task_for(engagement, defn),
                    context=engagement.origin.get("context", ""),
                    world_state_summary=engagement.origin.get("world_state_summary", ""),
                    casa_framework_mcp_url=self._casa_framework_mcp_url,
                    workspace_template_root=(
                        template_root if template_root.is_dir() else None
                    ),
                    executor_memory=executor_memory_block,
                    # Task 8 (containment stage 2): the allocated uid doubles
                    # as the gid here too, same as render_run_script below —
                    # provision_workspace chowns the whole tree to this
                    # identity as its LAST write, after every root-side file
                    # above has landed.
                    uid=engagement.allocated_uid, gid=engagement.allocated_uid,
                )
                write_casa_meta(
                    workspace_path=ws,
                    engagement_id=engagement.id,
                    executor_type=defn.type,
                    status="UNDERGOING",
                    created_at=_iso_now(),
                    finished_at=None, retention_until=None,
                    # §3.8: record the pinned artifacts with the workspace meta.
                    plugin_artifacts=list(
                        getattr(engagement, "plugin_artifacts", ()) or ()),
                    # Task 8: persisted so the workspace sweeper (no registry
                    # access) can prune this uid's passwd/group entry once
                    # retention deletes the workspace.
                    allocated_uid=engagement.allocated_uid,
                )

                # 1.5. Task 7 (containment stage 2): verify the uid drop the
                # run script below will attempt can actually succeed BEFORE
                # planting the service — a refusal here must never leave a
                # service that `set -e`-crash-loops under setpriv, nor fall
                # back to a root spawn.
                _preflight_uid_drop(engagement, ws)

                # 2. Write the s6 service pair (sibling logger service
                #    captures the CLI's stdout — see s6_rc.write_service_dir).
                # v0.14.9: GITHUB_TOKEN is set at addon boot via
                # setup-configs.sh → /run/s6/container_environment/GITHUB_TOKEN, and
                # s6-overlay merges it into every supervised service's env. Engagement
                # subprocesses inherit it without per-engagement plumbing.
                # #429: the declared-but-unresolved empty-string overlay is
                # derived inside render_run_script from plugin_dirs — one
                # owner, so this path and boot reconciliation cannot diverge.
                extra_env: dict[str, str] = {}
                run_script = render_run_script(
                    engagement_id=engagement.id,
                    permission_mode=defn.permission_mode or "acceptEdits",
                    extra_dirs=list(defn.extra_dirs),
                    extra_env=extra_env or None,
                    # §3.8: load the pinned artifacts via --plugin-dir flags.
                    plugin_dirs=[pa["path"] for pa in
                                 getattr(engagement, "plugin_artifacts", ()) or ()],
                    # Task 6 (containment stage 2): the allocated uid doubles
                    # as the gid — render refuses the UNALLOCATED_UID
                    # sentinel, so a not-yet-allocated record fails closed
                    # here rather than launching a setpriv-less/root CLI.
                    uid=engagement.allocated_uid, gid=engagement.allocated_uid,
                )
                log_script = render_log_run_script(engagement_id=engagement.id)
                s6_rc.write_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=engagement.id,
                    run_script=run_script,
                    depends_on=["init-setup-configs"],
                    log_run_script=log_script,
                )
                service_dir_written = True

                # 3. Compile + update + change — lock held, inner helper.
                await s6_rc._compile_and_update_locked()
                # v0.79.0 (§5): post the pinned live SUMMARY and persist its id
                # BEFORE the subprocess starts. A post FAILURE aborts the launch
                # (rolled back by the handler below), so the operator never sees
                # an engagement running without its summary anchor.
                await self._post_initial_summary(engagement)
                await s6_rc.start_service(engagement_id=engagement.id)
            except BaseException as start_exc:  # noqa: BLE001 — rollback is
                # opportunistic. Sol r2 (#363 family): BaseException, not
                # Exception — a task CANCELLATION mid-launch (compile, summary
                # post, service start) must run the same rollback instead of
                # abandoning half-written service dirs; the trailing `raise`
                # re-raises the CancelledError unchanged. (Rollback awaits run
                # normally — a cancellation is delivered once, at the await
                # that was pending when cancel() landed.)
                logger.warning(
                    "claude_code start failed for engagement %s: %s; rolling back",
                    engagement.id[:8], start_exc,
                )
                # Task 7 (containment stage 2): a uid-drop refusal gets its
                # own terminal kind (not the generic "driver_start_failed"
                # a caller further up would otherwise assign) so it's
                # distinguishable in the registry/telemetry. mark_error is
                # idempotent-guarded (only the first winner flips the
                # record), so a caller upstream re-marking with a generic
                # kind afterwards is a harmless no-op. The service was never
                # planted (this raises before write_service_dir), so
                # ensure_service_down is best-effort/likely a no-op — kept
                # anyway in case a prior partial state lingers.
                if isinstance(start_exc, UidDropRefused):
                    try:
                        await s6_rc.ensure_service_down(
                            engagement_id=engagement.id)
                    except Exception:  # noqa: BLE001 — best-effort teardown
                        logger.warning(
                            "uid-drop refusal: ensure_service_down failed "
                            "for %s", engagement.id[:8], exc_info=True,
                        )
                    if self._registry is not None:
                        try:
                            await self._registry.mark_error(
                                engagement.id, kind="refuse_uid_drop_failed",
                                message=str(start_exc),
                            )
                        except Exception:  # noqa: BLE001 — best-effort mark
                            logger.warning(
                                "uid-drop refusal: mark_error failed for "
                                "%s", engagement.id[:8], exc_info=True,
                            )
                # Best-effort rollback. Each step swallows its own errors so
                # one rollback failure doesn't mask the original cause.
                # v0.64.0: ALWAYS attempt dir removal — write_service_dir can
                # raise midway (pair half-written), and remove_service_dir is
                # idempotent. Recompile only when the dirs were fully written
                # (before that, the live db never saw them).
                try:
                    s6_rc.remove_service_dir(
                        svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                        engagement_id=engagement.id,
                    )
                except Exception as rb_exc:  # noqa: BLE001
                    logger.warning(
                        "rollback remove_service_dir failed: %s", rb_exc,
                    )
                if service_dir_written:
                    try:
                        await s6_rc._compile_and_update_locked()
                    except Exception as rb_exc:  # noqa: BLE001
                        logger.warning(
                            "rollback compile_and_update failed: %s", rb_exc,
                        )
                # Always attempt to remove the workspace tree at the
                # deterministic path — provision_workspace may have raised
                # AFTER creating partial state.
                try:
                    shutil.rmtree(ws_path, ignore_errors=True)
                except Exception as rb_exc:  # noqa: BLE001
                    logger.warning(
                        "rollback rmtree(%s) failed: %s", ws_path, rb_exc,
                    )
                # Task 4: the control dir is provisioned alongside the
                # workspace (provision_control_dir, called from within
                # provision_workspace) — remove it on the same rollback so a
                # failed launch never leaves an orphaned root-only directory.
                ctl_path = control_dir(engagement.id)
                try:
                    shutil.rmtree(ctl_path, ignore_errors=True)
                except Exception as rb_exc:  # noqa: BLE001
                    logger.warning(
                        "rollback rmtree(%s) failed: %s", ctl_path, rb_exc,
                    )
                # Task 8 (containment stage 2): a failed launch may still
                # have gotten far enough for provision_workspace to call
                # ensure_identity() before whatever raised — best-effort
                # prune here so a repeatedly-failing launch (or a launch
                # that fails after Task 8's own chown step) doesn't leave a
                # stale passwd/group entry for a uid whose workspace is
                # about to be deleted above. A no-op if identity was never
                # created.
                real_uid = owner_uid_or_none(engagement.allocated_uid)
                if real_uid is not None:
                    try:
                        prune_identity(real_uid)
                    except Exception as rb_exc:  # noqa: BLE001
                        logger.warning(
                            "rollback prune_identity(%s) failed: %s",
                            real_uid, rb_exc,
                        )
                    # Task 11 (containment stage 2): the uid's private
                    # outbox dir follows the workspace on this rollback path
                    # too — provision_workspace may have gotten far enough
                    # to create it before whatever raised. Best-effort, same
                    # guard/shape as the sibling teardown call in
                    # tools.delete_engagement_workspace.
                    try:
                        plugin_outbox.teardown_engagement_outbox(real_uid)
                    except Exception as rb_exc:  # noqa: BLE001
                        logger.warning(
                            "rollback engagement outbox teardown(%s) "
                            "failed: %s", real_uid, rb_exc,
                        )
                raise

        # 4. Kick off the background tasks (outside lock): respawn poller,
        #    session-id capture, and (at DEBUG) the log relay.
        self._spawn_background_tasks(engagement)

        # #369 (Sol design r2): LAST-instant gate before the initial prompt is
        # enqueued — a clearance clamp landing during the service-start awaits
        # above means `prompt` was rendered from pre-clamp materials and must
        # not reach the fresh process. Abort; the launcher's error path rolls
        # the engagement back.
        _lookup = getattr(self._registry, "get", None)
        latest = _lookup(engagement.id) if callable(_lookup) else None
        # Terra diff-gate r2: the pending flag alone cannot see a clamp→
        # rebuild cycle that COMPLETED while the service-start awaits above
        # were suspended (the rebuild cleared it) — the monotonic generation
        # captured at prompt-source time is compared too.
        if latest is not None and (
            getattr(latest, "context_rebuild_pending", False)
            or (expected_generation is not None
                and getattr(latest, "context_generation", 0)
                != expected_generation)
        ):
            from drivers.driver_protocol import StaleLaunchError
            # Terra diff-gate r4: this gate sits AFTER service start and
            # outside the Bug-13 rollback scope — raising alone would leave a
            # live service supervising a CLI whose workspace CLAUDE.md was
            # rendered from pre-clamp materials. Tear the service and the
            # in-memory machinery down first — but ONLY while the rebuild is
            # still pending (Terra r5): the s6 service is shared by
            # engagement id, so once a rebuild has completed (pending
            # cleared, generation-only mismatch) the running service is the
            # REBUILT floor one and must be left alone; this launch then
            # aborts without touching it.
            if getattr(latest, "context_rebuild_pending", False):
                try:
                    await s6_rc.ensure_service_down(
                        engagement_id=engagement.id)
                except Exception:  # noqa: BLE001 — best-effort; logged
                    logger.warning(
                        "stale-launch teardown: ensure_service_down failed "
                        "for %s", engagement.id[:8], exc_info=True)
                try:
                    await self.cancel(engagement)
                except Exception:  # noqa: BLE001 — best-effort; logged
                    logger.warning(
                        "stale-launch teardown: driver cancel failed for %s",
                        engagement.id[:8], exc_info=True)
            raise StaleLaunchError(
                f"engagement {engagement.id[:8]} was clearance-clamped "
                "during launch; aborting the pre-clamp prompt",
                record_live=not getattr(
                    latest, "context_rebuild_pending", False))

        # 5. Enqueue the initial prompt (is_initial=True) — the first spawn
        #    arms the reader and delivers it. Enqueue is instant while the
        #    reader is unarmed, so no background task is needed.
        if prompt:
            spool = self._inbound.get(engagement.id)
            if spool is not None:
                await spool.enqueue(prompt, is_initial=True)
            else:
                # Background tasks disabled (e.g. a unit test) — legacy direct
                # write so start() still delivers the prompt.
                await self._write_to_fifo(engagement, prompt)

        logger.info("claude_code engagement %s started", engagement.id[:8])

    async def send_user_turn(
        self, engagement: EngagementRecord, text: str,
        *, tg_message_id: int | None = None,
    ) -> str | None:
        """Enqueue an operator turn; return the durable enqueue DISPOSITION
        (§A3, Sol r10-2) — ``queued`` / ``evicted_other(<tg>)`` (accepted, the
        promotion already ran) or ``dropped_full`` / ``error`` (rejected — the
        caller rolls back the answer reservation). ``None`` when there is no
        spool (legacy direct write)."""
        spool = self._inbound.get(engagement.id)
        if spool is not None:
            return await spool.enqueue(text, tg_message_id=tg_message_id)
        await self._write_to_fifo(engagement, text)
        return None

    async def invalidate_session(self, engagement: EngagementRecord) -> None:
        """#369: tear down a session whose clearance was downgraded — the live
        CLI process (whose in-memory conversation holds pre-clamp context) is
        stopped, the control-dir session pointer is removed (the run template
        starts a FRESH session when it is absent), the cached executor-memory
        archive is blanked (it was fetched at the old tier and boot-replay's
        CLAUDE.md refresh would otherwise re-render it), and the media outbox
        is re-provisioned empty. The service is left DOWN;
        :meth:`rebuild_fresh_context` brings it back up. Raises on the
        teardown steps that matter — the caller fails closed on the durable
        ``context_rebuild_pending`` flag either way."""
        from drivers.workspace import executor_memory_path, session_id_path
        import plugin_outbox

        # Sol diff-gate r1: ensure_service_down REPORTS success — a False
        # means the old process may still be alive with its pre-clamp
        # conversation in memory, and proceeding would let the rebuild clear
        # the fence around a surviving session. Raise; the flag stays set and
        # every delivery keeps being refused until a later attempt confirms
        # extinction.
        if not await s6_rc.ensure_service_down(engagement_id=engagement.id):
            raise RuntimeError(
                f"engagement {engagement.id[:8]}: service did not confirm "
                "down — old session may survive; rebuild refused")
        # In-memory spool/task state for the old process.
        for t in self._tasks.pop(engagement.id, []):
            t.cancel()
        self._inbound.pop(engagement.id, None)
        self._epoch_pending.pop(engagement.id, None)
        self._turn_running.pop(engagement.id, None)
        mem_path = Path(executor_memory_path(engagement.id))
        if mem_path.is_file():
            mem_path.write_text("", encoding="utf-8")
        Path(session_id_path(engagement.id)).unlink(missing_ok=True)
        real_uid = owner_uid_or_none(engagement.allocated_uid)
        if real_uid is not None:
            plugin_outbox.provision_engagement_outbox(real_uid, fresh=True)

    async def rebuild_fresh_context(self, engagement: EngagementRecord) -> None:
        """#369: establish the post-downgrade FLOOR context — re-render the
        workspace CLAUDE.md from the (now clearance-withheld) record and the
        (now blank) archive cache, then start the service, which spawns a
        fresh session (no ``.session_id``). Raises on failure so the caller
        keeps ``context_rebuild_pending`` set and refuses delivery."""
        from drivers.workspace import refresh_claude_md

        # Sol diff-gate r1: re-run the (idempotent) invalidation first — this
        # path is also reached with a crash-recovered pending flag whose
        # teardown never ran, and a rebuild must never proceed over a live
        # pre-clamp process or a surviving session pointer. Raises on
        # unconfirmed teardown, keeping the flag set.
        await self.invalidate_session(engagement)
        defn = (
            self._executor_defn_lookup(engagement.role_or_type)
            if self._executor_defn_lookup is not None else None
        )
        if defn is None:
            raise RuntimeError(
                f"cannot rebuild engagement {engagement.id[:8]}: executor "
                f"definition {engagement.role_or_type!r} unavailable")
        ws_path = str(Path(self._engagements_root) / engagement.id)
        await asyncio.to_thread(
            refresh_claude_md, ws_path, defn=defn, rec=engagement)
        await s6_rc.start_service(engagement_id=engagement.id)
        self._spawn_background_tasks(engagement)

    async def cancel(self, engagement: EngagementRecord) -> None:
        """Teardown for a terminal transition (cancelled or completed).

        The durable spool FILE is intentionally left on disk: any pending
        receipts/notices are drained pre-close by ``_finalize_engagement`` and,
        if that drain crashed, the terminal boot-reconciliation owner picks
        them up. Only the in-memory spool object is dropped here."""
        # Cancel background tasks
        for t in self._tasks.pop(engagement.id, []):
            t.cancel()
        self._last_turn_ts.pop(engagement.id, None)
        self._inbound.pop(engagement.id, None)
        self._completion_refusals.pop(engagement.id, None)
        self._inbound_reservations.pop(engagement.id, None)
        self._reply_texts.pop(engagement.id, None)
        self._epoch_pending.pop(engagement.id, None)
        self._turn_running.pop(engagement.id, None)
        self._violation_notified.discard(engagement.id)
        self._violation_flagged.discard(engagement.id)
        self._ask_refusals.pop(engagement.id, None)
        # v0.83.0 (§A3): drop the in-memory answered overlay + answer
        # reservation (memory-only; crash convergence is owned by boot
        # reconciliation, not these).
        self._answered_overlay.pop(engagement.id, None)
        self._answer_reservations.pop(engagement.id, None)
        self._ask_inflight.pop(engagement.id, None)
        # §A3(b): drop the re-anchor-due latch + cancel any retry-owner task
        # (CancelledError terminates it without rescheduling).
        self._reanchor_due.discard(engagement.id)
        retry_task = self._reanchor_retry_tasks.pop(engagement.id, None)
        if retry_task is not None and not retry_task.done():
            retry_task.cancel()
        # wb4-3 (whole-branch gate wave 4): DRAIN the cancelled re-anchor owner
        # BEFORE dropping D6 state. A cancelled re-anchor owner DRAINS its child
        # to completion (by design — ``_reanchor_pass_locked``); that child keeps
        # running after ``cancel()`` returns and re-creates ``_confirmed_pairs``
        # AFTER teardown popped it — an unpumped, unlogged D6 record. So here:
        # (1) await the retry owner so its drained child finishes; (2) take the
        # ask-maintenance lock as a BARRIER — the drained child (this owner's OR
        # a boundary consumer's, e.g. one settle already cancelled) holds that
        # lock for its whole drain, so acquiring the EXISTING lock waits for the
        # child to complete and re-create any pair BEFORE we drop + log it. Both
        # are bounded — the drained unit is finite (§D6). Acquire the existing
        # lock BEFORE popping its dict slot (a popped slot would mint a NEW free
        # lock and skip the barrier).
        if retry_task is not None:
            await asyncio.gather(retry_task, return_exceptions=True)
        maint_lock = self._ask_maint_locks.get(engagement.id)
        if maint_lock is not None:
            async with maint_lock:
                pass
        # §A3(c): drop the ask-maintenance lock + ingress marker on teardown.
        self._ask_maint_locks.pop(engagement.id, None)
        # v0.84.0 (round-4 §D6, Sol r20-1/r21-1/r22-1): drop the confirmed-pair
        # map on teardown and LOG one residual PER remaining PAIR. A pair still
        # present at TERMINAL teardown is a double failure (registry outage AND
        # repeated settlement-edit failures); the accepted floor is one inert,
        # obviously-stale duplicate PER QUESTION in an already-answered/terminal
        # topic — logged, never resurrected (a teardown-surviving owner would
        # rebuild exactly the machinery r17 deleted). A crash drops the map the
        # same way (at most one orphan per retained pair, potentially several).
        pairs = self._confirmed_pairs.pop(engagement.id, None)
        if pairs:
            for q_n, orphan_mid in pairs.items():
                logger.info(
                    "engagement %s: terminal teardown drops unconfirmed "
                    "re-anchor pair (q=%s orphan_mid=%s) — accepted stale "
                    "duplicate residual (topic is answered/terminal)",
                    engagement.id[:8], q_n, orphan_mid)
        self._historical_cursor.pop(engagement.id, None)
        # F-EXPIRE: drop the operator-away suspend state on teardown.
        self._operator_away.pop(engagement.id, None)
        self._away_refusals.pop(engagement.id, None)
        # A2b: drop the force-end backstop state on teardown.
        self._forced_suspend_epochs.pop(engagement.id, None)
        self._away_suspend_fired.discard(engagement.id)
        self._away_force_cooldown_until.pop(engagement.id, None)  # F3
        # Whole-branch gate r3: cancel IN PLACE — never pop-then-cancel. The
        # owner must stay VISIBLE in ``_force_tasks`` until its done callback
        # retires it, because its shielded post-SIGTERM cleanup only registers
        # in ``_force_cleanups`` when the cancellation actually runs; a
        # popped-but-not-yet-done owner would be invisible to
        # ``drain_force_cleanups``'s snapshot on BOTH surfaces.
        force_task = self._force_tasks.get(engagement.id)
        if force_task is not None and not force_task.done():
            force_task.cancel()
        # Sol A2 wave-3, Finding 2: ``_force_cleanups`` (handed-off post-SIGTERM
        # kill sequences) is DELIBERATELY NOT cancelled here — those bounded tasks
        # must complete their SIGKILL escalation + extinction verification. They
        # self-retire via their own done-callback; teardown just leaves them.
        # v0.79.0 (§2): drop the sequencer (its watcher task is cancelled above).
        # wb3-3: TERMINALIZE it FIRST — abort any unresolved intent + prune the
        # registry (firing every ``on_retire``) so the wb2-4 validation-gate pins
        # release even on a teardown path that bypassed
        # ``settle_all_open_questions`` (a terminal transition before the relay
        # ever processed ``result``). Idempotent — a no-op if settle already
        # terminalized. Must complete BEFORE the pop, so the pruned registry is
        # the one being dropped.
        seq = self._sequencers.get(engagement.id)
        if seq is not None:
            try:
                await seq.terminalize()
            except Exception:  # noqa: BLE001 — teardown hygiene, never abort
                logger.debug(
                    "engagement %s: sequencer terminalize at teardown failed",
                    engagement.id[:8], exc_info=True)
        self._sequencers.pop(engagement.id, None)
        # v0.79.0 (§5): drop the summary controller and cancel its elapsed tick
        # (not in self._tasks — the controller owns it).
        ctrl = self._summaries.pop(engagement.id, None)
        if ctrl is not None:
            ctrl.shutdown()

        async with s6_rc._compile_lock:
            # Stop is tolerant of "already down" — log and continue.
            try:
                await s6_rc.stop_service(engagement_id=engagement.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stop_service(%s) failed: %s",
                               engagement.id[:8], exc)
            # v0.64.0: also stop the sibling logger service explicitly so the
            # recompile below never has to down a still-live service. No-op
            # for legacy engagements without one.
            try:
                await s6_rc.stop_log_service(engagement_id=engagement.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stop_log_service(%s) failed: %s",
                               engagement.id[:8], exc)
            try:
                s6_rc.remove_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=engagement.id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("remove_service_dir(%s) failed: %s",
                               engagement.id[:8], exc)
            try:
                await s6_rc._compile_and_update_locked()
            except Exception as exc:  # noqa: BLE001
                logger.warning("compile_and_update after remove failed: %s", exc)

    async def resume(self, engagement: EngagementRecord, session_id: str) -> None:
        """Effectively a no-op under s6 — the run script reads .session_id on
        its next spawn. Included for DriverProtocol completeness."""
        return

    def is_alive(self, engagement: EngagementRecord) -> bool:
        """Synchronous probe — schedules an async s6-svstat call and waits.

        Called from sweep code that is already async; use is_alive_async
        if you need awaitable form. This sync wrapper exists only to
        match DriverProtocol.is_alive signature.
        """
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Can't block the running loop; return True optimistically.
            # Callers in async context should use is_alive_async().
            return engagement.id in self._tasks
        return loop.run_until_complete(self.is_alive_async(engagement))

    async def is_alive_async(self, engagement: EngagementRecord) -> bool:
        pid = await s6_rc.service_pid(engagement_id=engagement.id)
        return pid is not None

    # -- internal ---------------------------------------------------------

    async def _write_to_fifo(
        self, engagement: EngagementRecord, text: str,
        *, timeout_s: float = 20.0, poll_s: float = 0.25,
        retained: bool = False,
    ) -> bool:
        """Write one newline-terminated line to the engagement FIFO.

        Returns ``True`` iff the WHOLE line was written (the inbound queue
        keys one-message-per-spawn delivery + retention on this). Any
        no-reader / stall / broken-pipe / missing-FIFO outcome returns
        ``False`` so the caller retains the item for the next spawn.

        #322: ``retained`` declares what the CALLER does with a ``False``
        return. The spool retains the envelope and auto-redelivers on the
        next spawn, so its no-reader notice must promise the retry — the old
        one-size "Try again" copy made the operator resend a turn that was
        about to redeliver anyway, and the agent did the work twice. The
        legacy no-spool direct writes retain nothing; "Try again" stays their
        honest copy.
        """
        # M13: a blocking open(fifo, "a") parks a pooled executor thread
        # FOREVER when no reader exists (crash-looping/downed s6 service).
        # asyncio.to_thread threads are uncancellable, so a handful of stuck
        # writes starve all subprocess orchestration app-wide. Open + write
        # non-blocking with a bounded deadline instead — no thread at all.
        # Task 4 (containment stage 2): the FIFO lives in the root-only
        # control dir, not the uid-owned workspace.
        fifo = Path(fifo_path(engagement.id))
        if not fifo.exists():
            logger.warning("FIFO missing for engagement %s", engagement.id[:8])
            return False
        data = (text + "\n").encode("utf-8")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        fd: int | None = None
        try:
            # O_NONBLOCK open raises ENXIO while no reader exists — retry until
            # a reader appears or the deadline passes (covers the ~1s s6
            # respawn pause without ever parking a thread).
            while fd is None:
                try:
                    fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    if exc.errno != errno.ENXIO:
                        logger.warning(
                            "FIFO open failed for engagement %s: %s",
                            engagement.id[:8], exc,
                        )
                        return False
                    if loop.time() >= deadline:
                        logger.warning(
                            "engagement %s: no FIFO reader after %.0fs — "
                            "dropping turn", engagement.id[:8], timeout_s,
                        )
                        # F3 (Sol r3): route the no-reader notice THROUGH the
                        # single writer (post_platform_notice: seals narration +
                        # advances high-water), not a direct send that bypasses
                        # the sequencer on an abnormal respawn. ``post_topic_notice``
                        # falls back to a direct send only when no live sequencer
                        # exists.
                        if retained:
                            # #322: the spool keeps this envelope and
                            # redelivers it on the next spawn — a resend
                            # would duplicate the turn.
                            copy = (
                                "The engagement isn't accepting input right "
                                "now — your message is queued and will be "
                                "delivered automatically once it recovers. "
                                "/cancel if it stays unresponsive."
                            )
                        else:
                            copy = (
                                "The engagement isn't accepting input right "
                                "now — your message was not delivered. Try "
                                "again, or /cancel if it stays unresponsive."
                            )
                        await self.post_topic_notice(engagement, copy)
                        return False
                    await asyncio.sleep(poll_s)
            # Reader exists; write non-blocking under the same deadline. Turns
            # are far below the 64KB pipe buffer, so the first write virtually
            # always completes fully.
            view = memoryview(data)
            while view:
                try:
                    n = os.write(fd, view)
                    view = view[n:]
                except BlockingIOError:
                    if loop.time() >= deadline:
                        logger.warning(
                            "engagement %s: FIFO write stalled — dropping "
                            "remainder of turn", engagement.id[:8],
                        )
                        return False
                    await asyncio.sleep(poll_s)
                except BrokenPipeError:
                    logger.warning(
                        "engagement %s: FIFO reader vanished mid-write",
                        engagement.id[:8],
                    )
                    return False
        finally:
            if fd is not None:
                os.close(fd)
        self._last_turn_ts[engagement.id] = time.time()
        return True

    def _spawn_background_tasks(
        self, engagement: EngagementRecord, *,
        reconcile_snapshot: list[dict] | None = None,
        reconcile_claimed: set[str] | None = None,
        telegram_ready: "asyncio.Event | None" = None,
    ) -> None:
        # Sol r2-B6: boot replay calls this DIRECTLY (not start), so the inbound
        # spool, reply-text set, epoch tracker AND the spool recovery all live
        # here — a resumed engagement gets the same wiring as a fresh one.
        self._reply_texts.setdefault(engagement.id, set())
        self._epoch_pending.setdefault(engagement.id, None)
        self._turn_running.setdefault(engagement.id, False)
        # v0.79.0 (§2): the shared per-engagement OUTPUT SEQUENCER + its late/
        # timeout discrete-post watcher task. Created BEFORE the relay so a
        # discrete ingress that registers an intent early always finds it, and
        # BEFORE the spool so delivery can set the turn's reply-thread target.
        sequencer = self._ensure_sequencer(engagement)
        # v0.79.0 (§5): the live-SUMMARY controller. Adopts the summary message
        # id posted at boot (fresh engagement) or persisted across a restart
        # (resumed engagement); its edits flow through the sequencer above.
        summary = self._ensure_summary(engagement)
        summary.adopt_message_id(
            getattr(engagement, "summary_message_id", None))
        # v0.79.0 (§3): the durable inbound envelope spool. Loads any surviving
        # spool file so undelivered turns redeliver and pending receipts/notices
        # retry (recovery scheduled below).
        self._inbound[engagement.id] = _InboundSpool(
            engagement_id=engagement.id,
            spool_path=inbound_spool_path(engagement.id),
            # #322: the spool RETAINS on a False return (auto-redelivery on
            # the next spawn) — its no-reader notice promises the retry.
            write_fifo=lambda text: self._write_to_fifo(
                engagement, text, retained=True),
            send_notice=lambda text, reply_to: self._spool_send_notice(
                engagement, text, reply_to),
            is_turn_running=lambda: self._turn_running.get(engagement.id, False),
            current_epoch=lambda: self._epoch_pending.get(engagement.id),
            sequencer=sequencer,
            registry=self._registry,
            supersede_pending_asks=lambda: self._supersede_pending_asks(
                engagement.id),
            settle_anchor_on_delivery=lambda op_mid: self._settle_open_anchor(
                engagement, op_mid),
            on_operator_enqueued=lambda: self._clear_operator_away(
                engagement.id),
            promote_answer_on_enqueue=lambda: self._promote_answer_on_enqueue(
                engagement),
        )

        tasks = [
            asyncio.create_task(self._poll_respawns(engagement)),
            asyncio.create_task(
                sequencer.run_watcher(),
                name=f"seq_watcher:{engagement.id[:8]}"),
            # P31 (v0.37.10): capture the SDK session_id by watching the
            # claude CLI's own session-storage directory. Persists the
            # UUID to the control dir's ``.session_id`` (Task 4) so the run script's
            # resume plumbing (UUID-validated single ``--resume=<uuid>``
            # token since v0.131.0) picks up after a Casa restart.
            asyncio.create_task(self._capture_session_id(engagement)),
            # W1: the LIVE topic-stream relay is spawned ALWAYS, regardless of
            # LOG_LEVEL — it is the operator's live window on the engagement,
            # not a debug aid. It fans ``on_turn_event`` into _on_stream_event
            # (arm the inbound queue on spawn, abnormal-exit correlation,
            # reply-text reset, Task-7 seams).
            asyncio.create_task(
                self._run_topic_relay(engagement),
                name=f"topic_relay:{engagement.id[:8]}"),
            # v0.79.0 (§5): initial best-effort pin attempt for the live summary
            # (retried on every lifecycle flush by the controller). No-op when
            # there is no pin primitive / no message id.
            asyncio.create_task(
                summary.ensure_pinned(),
                name=f"summary_pin:{engagement.id[:8]}"),
        ]
        # Phase 4b G5: ALSO relay every raw s6-log line into Casa's logger at
        # DEBUG so operators have one greppable namespace for both drivers' CLI
        # subprocess output. Spawned only when DEBUG-enabled: the tailer
        # re-opens and reads the file at 10 Hz, and at INFO every line would be
        # discarded. A LOG_LEVEL flip requires an add-on restart, which
        # respawns these tasks anyway. (Distinct from the always-on relay
        # above, which drives the operator-visible topic stream.)
        if logging.getLogger("subprocess_cli").isEnabledFor(logging.DEBUG):
            log_path = os.path.join(
                engagement_log_dir(engagement.id), "current")
            tasks.append(asyncio.create_task(
                self._relay_log_lines(engagement, log_path=log_path)))
        # v0.79.0 (§3): spool recovery replaces the zero-with-uncertainty
        # notice. A surviving spool redelivers undelivered turns by construction
        # and retries any pending receipts/notices — no "please resend" guess.
        # Scheduled only when a spool file survives (a fresh engagement has
        # nothing to recover), mirroring the old conditional marker reconcile.
        spool = self._inbound.get(engagement.id)
        if spool is not None and Path(inbound_spool_path(engagement.id)).exists():
            tasks.append(asyncio.create_task(spool.recover()))

        # v0.79.0 (§4): boot reconciliation of open questions. The broker /
        # finish hooks / reply anchors are memory-only, so a restart can leave a
        # question visibly open with a stale keyboard nobody can settle. Any
        # open_questions entry with no LIVE broker record settles here (expired
        # copy, keyboard cleared) while next_question_number is preserved.
        # Snapshot the open_questions SYNCHRONOUSLY here (attach time): every
        # entry on disk is by definition PRIOR-PROCESS (the in-memory broker
        # starts empty), so the reconcile settles this exact snapshot
        # unconditionally. Snapshotting at schedule time (not inside the task)
        # keeps a fresh same-process ask — which registers a NEW numbered entry
        # concurrently — out of the settle set (review I1: a live ask must not
        # suppress settling genuinely-stale prior-process keyboards).
        #
        # §A3(b) boot reconciliation owner (Sol r6-3/r7-3/4/r10-3): when casa_core
        # passes a PRE-SERVICE snapshot + shared claimed-set + the Telegram
        # readiness event, this attached record CLAIMS itself (exactly one
        # reconciler per record per boot) and its reconcile EXECUTION is gated on
        # channel readiness — so its confirmed settle edits never fire against a
        # ``None`` bot. Direct/legacy callers (driver unit tests, fresh start with
        # no snapshot) keep the immediate attach-time read + ungated schedule.
        # B3 — explicit snapshot contract: a replay context passes an EXPLICIT
        # per-record snapshot (possibly ``[]``). A snapshot that is not None means
        # replay ran — settle EXACTLY that pre-service snapshot (``[]`` ⇒ nothing
        # prior-process; a fresh same-process ask created between the snapshot and
        # this attachment is NOT in it and must NOT be expired). ``None`` means NO
        # replay context (legacy / direct callers, driver unit tests) → keep the
        # immediate attach-time fresh read + ungated schedule.
        if reconcile_snapshot is not None:
            rt = self.schedule_boot_reconcile(
                engagement, reconcile_snapshot, telegram_ready,
                claimed=reconcile_claimed)
            if rt is not None:
                tasks.append(rt)
        elif reconcile_claimed is not None or telegram_ready is not None:
            # Boot context but no per-record snapshot supplied (not produced by
            # casa_core post-B3; kept for a caller that gates without a snapshot):
            # fresh attach-time read, still claimed + readiness-gated.
            snap = list(getattr(engagement, "open_questions", ()) or ())
            rt = self.schedule_boot_reconcile(
                engagement, snap, telegram_ready, claimed=reconcile_claimed)
            if rt is not None:
                tasks.append(rt)
        else:
            attach_open_qs = list(
                getattr(engagement, "open_questions", ()) or ())
            if attach_open_qs:
                tasks.append(asyncio.create_task(
                    self.reconcile_open_questions(engagement, attach_open_qs)))

        self._tasks[engagement.id] = tasks

    def schedule_boot_reconcile(
        self, engagement: EngagementRecord, snapshot: list[dict],
        telegram_ready: "asyncio.Event | None", *,
        claimed: set[str] | None = None,
    ) -> "asyncio.Task | None":
        """§A3(b) boot reconciliation owner: schedule a readiness-gated reconcile
        of the record's PRE-SERVICE open-questions ``snapshot``. CLAIMS the record
        via the shared ``claimed`` set (exactly one reconciler per record per
        boot — a record already claimed returns ``None``). An empty snapshot
        needs no reconcile (returns ``None``). Retains the task so it isn't GC'd
        before the readiness barrier lifts; each self-removes on done. Used by
        BOTH the attached path (``_spawn_background_tasks``) and the casa_core
        refused/terminal-with-questions path."""
        eng_id = engagement.id
        if claimed is not None:
            if eng_id in claimed:
                return None
            claimed.add(eng_id)
        if not snapshot:
            return None
        task = asyncio.create_task(
            self._reconcile_after_ready(engagement, list(snapshot), telegram_ready),
            name=f"boot_reconcile:{eng_id[:8]}")
        self._boot_reconcile_tasks.append(task)
        task.add_done_callback(self._on_boot_reconcile_done)
        return task

    def _on_boot_reconcile_done(self, task: asyncio.Task) -> None:
        try:
            self._boot_reconcile_tasks.remove(task)
        except ValueError:
            pass

    async def _reconcile_after_ready(
        self, engagement: EngagementRecord, snapshot: list[dict],
        telegram_ready: "asyncio.Event | None",
    ) -> None:
        """§A3(b) channel-readiness barrier (Sol r9-2/r10-3): wait for the
        Telegram channel's first successful ``_rebuild`` before running the
        reconcile's confirmed settle edits — otherwise every edit fails closed
        against a ``None`` bot and the SAME ordering repeats next boot (settle
        would never happen). The snapshot was taken PRE-SERVICE; execution is
        deferred here, not in replay (which must not block on the channel)."""
        if telegram_ready is not None:
            await telegram_ready.wait()
        await self.reconcile_open_questions(engagement, snapshot)

    async def _spool_send_notice(
        self, engagement: EngagementRecord, text: str, reply_to: int | None,
    ) -> bool:
        """Send a spool receipt/notice into the topic, threaded to the operator
        message when given. Returns delivered-ok so a failed send stays pending
        for at-least-once retry (§3).

        v0.79.0 (§2 F1(b)): a receipt/notice is a DISCRETE platform-origin send
        and MUST go through the single writer — a receipt slipping in below open
        narration while ``edit_narration_if_latest`` still returns APPLIED is the
        exact ordering violation §2 forbids. It has no subprocess frame, so it
        registers no intent; instead the sequencer's ``post_platform_notice``
        seals open narration, posts, and advances the high-water mark under the
        one serialization lock. Falls back to a direct send only when no live
        sequencer exists (terminal boot reconciliation of a torn-down topic)."""
        try:
            seq = self._sequencers.get(engagement.id)
            if seq is not None:
                mid = await seq.post_platform_notice(text, reply_to=reply_to)
                return mid is not None
            # #341: the direct-send fallback must apply the SAME delivery
            # evidence as the sequencer path — a ``None`` message id (e.g.
            # Telegram unavailable during terminal boot reconciliation) is a
            # FAILED send, and returning True here dropped the pending
            # receipt/notice instead of retrying it at the next touchpoint.
            if reply_to is not None:
                mid = await self._send_to_topic(
                    engagement.topic_id, text, reply_to_message_id=reply_to)
            else:
                mid = await self._send_to_topic(engagement.topic_id, text)
            return mid is not None
        except Exception as exc:  # noqa: BLE001 — retried at the next touchpoint
            logger.warning(
                "engagement %s: spool notice send failed (retried): %s",
                engagement.id[:8], exc,
            )
            return False

    async def post_topic_notice(
        self, engagement: EngagementRecord, text: str,
        reply_to: int | None = None,
    ) -> bool:
        """§2 F2: post a platform-origin notice (a Telegram command reply, a
        resume error) into the engagement topic THROUGH the single writer.

        Command replies used to post via a direct ``send_to_topic`` that
        bypassed the sequencer — a reply slipping in below open narration while
        ``edit_narration_if_latest`` still returns APPLIED is the exact ordering
        violation §2 forbids. Delegates to the same ``post_platform_notice``
        seam receipts use (seals narration + advances high-water under the one
        lock; direct send only when no live sequencer)."""
        return await self._spool_send_notice(engagement, text, reply_to)

    async def drain_inbound_spool(self, engagement: EngagementRecord) -> None:
        """§3 terminal pre-close drain: flush pending receipts/notices while
        the topic is still open (called by ``_finalize_engagement`` BEFORE the
        terminal commit + topic close)."""
        spool = self._inbound.get(engagement.id)
        if spool is not None:
            await spool.drain()

    async def finalize_completion_post(
        self, engagement: EngagementRecord, summary_text: str,
    ) -> bool:
        """§2 F1(c): post the engagement COMPLETION text through the single
        writer, but only AFTER draining the sequencer.

        Completion may not overtake its causal block: first flush every pending
        narration + parked/armed intent (``flush_armed_intents`` resolves late
        armed intents; sealing closes open narration), THEN post the completion
        text through ``post_platform_notice`` (single writer, seals + advances
        high-water). Returns ``True`` if a live sequencer posted it (the caller
        skips its own direct ``send_to_topic``), ``False`` if there is no live
        sequencer (caller does the pre-v0.79 direct send)."""
        seq = self._sequencers.get(engagement.id)
        if seq is None:
            return False
        # F3: DRAIN the relay to the completion block FIRST — wait until the
        # relay consumes the emit_completion consumption debt (⇒ every PRIOR
        # frame has been processed) so lagging prior-frame narration can never
        # post BELOW the completion message. Bounded (slot budget); on timeout
        # WARN and proceed. ``register_completion_consumption`` uses this same
        # request_id, so an emit_completion-driven finalize has a debt to await;
        # a cancel/error finalize has none and this returns immediately.
        rid = f"emit_completion:{engagement.id}"
        drained = await seq.await_completion_drain(rid)
        if not drained:
            logger.warning(
                "engagement %s: completion drain timed out — posting completion "
                "without a full relay drain (prior narration may lag)",
                engagement.id[:8],
            )
        await seq.flush_armed_intents()
        await seq.seal_narration()
        # wb4-2: the completion text is the TERMINAL message — post it through the
        # dedicated completion seam, the ONLY writer permitted post-terminal
        # (``post_platform_notice`` now discards under the terminal latch, so a
        # lagging mutating-tool violation notice can never land below completion).
        await seq.post_completion_notice(summary_text)
        return True

    def register_completion_consumption(
        self, engagement_id: str, args: dict,
    ) -> None:
        """§2 F1(c): register the emit_completion send INTENT (svc_casa_mcp
        ingress, hash = identity over raw args) as a one-block CONSUMPTION DEBT
        so the relay, reaching the emit_completion tool_use block, consumes it
        silently instead of emitting stray narration or binding a later
        same-hash intent. Best-effort — no live sequencer ⇒ no-op."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return
        from channels.output_sequencer import (
            EMIT_COMPLETION_TOOL, projection_hash as _pj,
        )
        rid = f"emit_completion:{engagement_id}"
        phash = _pj(EMIT_COMPLETION_TOOL, args if isinstance(args, dict) else {})
        res = seq.register_intent(
            request_id=rid, tool_name=EMIT_COMPLETION_TOOL,
            projection_hash=phash, poster=_completion_noop_poster,
        )
        # wb4-1: the engagement already terminalized (register returns the
        # terminal sentinel, not a tuple) — no debt to leave; finalize's own
        # terminalize already drove settlement. Best-effort, so just return.
        if not isinstance(res, tuple):
            return
        intent, _created = res
        # Mark the debt directly (no lock needed — a plain dataclass toggle; the
        # relay reads it under the lock at block-match time).
        intent.state = "posted"
        intent.timeout_posted = True
        intent.consumed = False

    async def reconcile_terminal_spool(self, engagement: EngagementRecord) -> None:
        """§3 terminal boot-reconciliation: a TERMINAL engagement whose spool
        file still holds pending receipts/notices (a drain that crashed / a
        send that failed pre-terminal) is drained here — posting to the topic
        if it still exists, else WARN-dropping (the topic is gone)."""
        spool_path = Path(inbound_spool_path(engagement.id))
        if not spool_path.exists():
            return
        spool = _InboundSpool(
            engagement_id=engagement.id,
            spool_path=str(spool_path),
            write_fifo=lambda text: _never_deliver(),
            send_notice=lambda text, reply_to: self._spool_send_notice(
                engagement, text, reply_to),
        )
        if not spool.has_pending():
            return
        if engagement.topic_id is None:
            logger.warning(
                "engagement %s: terminal spool has pending receipts/notices "
                "but its topic is gone — dropping", engagement.id[:8],
            )
            spool.mark_all_settled()
            spool._persist_quiet()
            return
        await spool.drain()

    # -- v0.79.0 (§5) live-summary controller -------------------------------

    @staticmethod
    def _summary_goal_line(engagement: EngagementRecord) -> str:
        """The summary's stable title — the persisted SHORT topic title (W-R6,
        the SAME source the topic-name state edit reads). Legacy engagements
        with no persisted title fall back to the derived concise_task label so
        old records never crash."""
        title = getattr(engagement, "topic_title", "") or ""
        if title:
            return title
        try:
            from channels.state_emoji import concise_task
            return concise_task(engagement.task or "")
        except Exception:  # noqa: BLE001 — a goal line is best-effort
            return (engagement.task or "").strip()

    async def _post_initial_summary(self, engagement: EngagementRecord) -> int | None:
        """§5 boot: post the pinned live summary and persist its id. The pin
        itself is owned by ``ensure_pinned()`` (T4 F-4 — see below). Raises
        ``RuntimeError`` on a post failure so ``start`` aborts.

        A resumed/replayed engagement never calls this (it already has a
        persisted ``summary_message_id`` that the controller adopts on attach).
        """
        if engagement.topic_id is None:
            return None
        from drivers.summary_controller import STATUS_WORKING, render_summary
        text = render_summary(
            goal_line=self._summary_goal_line(engagement),
            status=STATUS_WORKING,
        )
        try:
            mid = await self._send_to_topic(engagement.topic_id, text)
        except Exception as exc:  # noqa: BLE001 — abort the launch
            raise RuntimeError(
                f"summary post failed for engagement {engagement.id[:8]}: {exc}"
            ) from exc
        if mid is None:
            raise RuntimeError(
                f"summary post returned no message id for engagement "
                f"{engagement.id[:8]}"
            )
        # Persist durably FIRST (so a restart resumes it), THEN set the
        # in-memory field. F7 (Sol r2): snapshot-BEFORE-mutate. The registry's
        # strict setter snapshots the record's PRE-mutation summary_message_id to
        # roll back on a persist failure. If we assigned ``engagement.summary_
        # message_id = mid`` HERE first — and ``engagement`` IS the registry's
        # own record object — the setter would snapshot the ALREADY-mutated new
        # id, making its rollback a no-op (a forced persist failure would leave
        # the new id in memory instead of the true prior value). So we let the
        # setter own the mutation+snapshot and only touch the in-memory field
        # AFTER a successful persist.
        setter = getattr(self._registry, "set_summary_message_id", None)
        if setter is not None:
            # §5 / F6: strict persistence — a summary posted but NOT persisted
            # cannot be resumed after a restart (§5's invariant would break), so
            # a persist failure ABORTS the launch (post-failure-aborts rule).
            try:
                await setter(engagement.id, mid)
            except Exception as exc:  # noqa: BLE001 — abort the launch
                raise RuntimeError(
                    f"summary_message_id persist failed for engagement "
                    f"{engagement.id[:8]}: {exc}"
                ) from exc
        # Set the in-memory record's field only after a durable persist (a no-op
        # when ``engagement`` already IS the registry record the setter mutated;
        # required when it is a distinct object or there is no registry setter).
        engagement.summary_message_id = mid
        # T4 F-4 (review): the initial pin attempt is NOT made here — the
        # per-engagement SummaryController doesn't exist yet at this point in
        # ``start()`` (``_ensure_summary`` only runs later, from
        # ``_spawn_background_tasks``), so any success here could never be
        # recorded on a controller and ``ensure_pinned()`` would immediately
        # redo the pin anyway once the controller is built. ``ensure_pinned()``
        # (scheduled as a task in ``_spawn_background_tasks``, retried on every
        # lifecycle flush) owns the pin attempt for both fresh and
        # resumed/replayed engagements alike.
        return mid

    async def adopt_summary_if_missing(self, engagement: EngagementRecord) -> None:
        """§5 / F7 adopt-on-attach migration: a LEGACY pre-v0.79 ACTIVE
        engagement replayed at boot has ``summary_message_id is None`` (it
        predates the pinned summary). Post + persist a summary NOW, on attach,
        so §5's invariant — no running engagement without a summary — holds
        before/immediately-at service start. Best-effort per record (boot replay
        isolates failures); a fresh v0.79 engagement (id already persisted) or a
        topic-less record is a no-op."""
        if (getattr(engagement, "summary_message_id", None) is not None
                or engagement.topic_id is None):
            return
        await self._post_initial_summary(engagement)

    def _ensure_summary(self, engagement: EngagementRecord) -> "SummaryController":
        """Build (or return) the per-engagement live-summary controller (§5)."""
        from drivers.summary_controller import SummaryController

        ctrl = self._summaries.get(engagement.id)
        if ctrl is None:
            reg = self._registry
            eid = engagement.id
            ctrl = SummaryController(
                engagement_id=eid,
                sequencer=self._ensure_sequencer(engagement),
                goal_line=self._summary_goal_line(engagement),
                # §A3: the pinned summary's ``Open questions:`` line reads the
                # EFFECTIVE unanswered set (persisted ``answered`` flag ∪ overlay).
                open_question_numbers=(lambda: self._effective_open_question_numbers(eid)),
                pin_message=(
                    (lambda mid: self._pin_topic_message(engagement.topic_id, mid))
                    if self._pin_topic_message is not None
                    and engagement.topic_id is not None
                    else None
                ),
                message_id=getattr(engagement, "summary_message_id", None),
            )
            self._summaries[eid] = ctrl
        return ctrl

    async def _summary_status_transition(
        self, engagement_id: str, status: str,
    ) -> None:
        """Acquire the next monotonic revision from the engagement-wide
        allocator and submit a lifecycle STATUS to the controller (§5). A
        lifecycle source (turn lifecycle / interaction_state / ask registry)
        calls this so the three sources are totally ordered."""
        ctrl = self._summaries.get(engagement_id)
        if ctrl is None:
            return
        rev: int | None = None
        alloc = getattr(self._registry, "allocate_summary_revision", None)
        if alloc is not None:
            try:
                rev = await alloc(engagement_id)
            except Exception as exc:  # noqa: BLE001 — degrade to no revision
                # T4 F-1 (review): this used to log at DEBUG only, so the drop
                # below (submit_status silently no-ops on revision=None) was
                # invisible in production. WARN — no fallback revision is
                # synthesized; the status transition for THIS call is dropped,
                # matching submit_status's existing "no revision, no update"
                # contract.
                logger.warning(
                    "engagement %s: allocate_summary_revision failed — "
                    "dropping this summary status transition (status=%s): %s",
                    engagement_id[:8], status, exc,
                )
        # F-EXPIRE (A2a) COERCION — the ONE funnel every lifecycle status source
        # already goes through (Sol r1-2). While operator-away, a working/waiting
        # submission is coerced to ⏸ paused so a compliant agent ending its turn
        # (``result`` → ⏳) cannot overwrite the paused line. Terminal statuses
        # bypass this funnel (finalize). A DIRECT STATUS_PAUSED submission
        # (``note_operator_away`` entry, Finding 3) passes through untouched —
        # the coercion only rewrites WAITING/WORKING.
        #
        # PINNED INVARIANT (Sol r2-3): sample ``operator_away`` AFTER the revision
        # allocation await returns. Sampling BEFORE it would let an inbound clear
        # + recompute land a correct status, only for THIS older suspended
        # coroutine to obtain a NEWER revision and write ⏸ back; sampling after
        # allocation guarantees any later clear allocates a strictly-newer
        # corrective revision that wins.
        from drivers.summary_controller import (
            STATUS_PAUSED, STATUS_WAITING_REPLY, STATUS_WORKING,
        )
        if (self._operator_away.get(engagement_id)
                and status in (STATUS_WAITING_REPLY, STATUS_WORKING)):
            status = STATUS_PAUSED
        await ctrl.submit_status(status, rev)

    async def note_ask_waiting(self, engagement_id: str) -> None:
        """W-R2: a successfully POSTED ask/anchor means the ball is now with the
        operator → ⏳ waiting for your reply. Driven from the ask LIFECYCLE (not
        the turn ``result``), because ``ask()`` blocks the subprocess for the
        whole time the operator owns the turn — so without this the summary
        would read ⚙️ working the entire wait. Acquires the next monotonic
        revision so it totally-orders against the settlement recompute below."""
        from drivers.summary_controller import STATUS_WAITING_REPLY
        await self._summary_status_transition(engagement_id, STATUS_WAITING_REPLY)

    # -- v0.83.0 (§A3) answered-overlay + effective open-question accessor ----
    def mark_answered_overlay(self, engagement_id: str, number: int) -> None:
        """Record an in-memory ANSWERED mark (§A3, Sol r6-4). Set by the answer
        path when ``mark_question_answered``'s strict persist RAISES — the live
        process then treats the question answered immediately (unioned onto the
        persisted flag) while later settle attempts retry the durable write."""
        self._answered_overlay.setdefault(engagement_id, set()).add(number)

    def _overlay_answered(self, engagement_id: str, number: int | None) -> bool:
        if number is None:
            return False
        return number in self._answered_overlay.get(engagement_id, set())

    def _effective_open_question_numbers(self, engagement_id: str) -> list[int]:
        """The A3 gates / pinned summary / recompute read UNANSWERED questions as
        ``persisted-unanswered MINUS overlay-answered MINUS reserved`` (Sol r6-4 +
        r7-1): a question whose ``answered`` write failed but is overlay-marked —
        or one whose answer is still only RESERVED (handler entry, pre-promotion)
        — stops gating immediately, before the durable flag lands."""
        reg = self._registry
        if reg is None or not hasattr(reg, "open_question_numbers"):
            return []
        try:
            nums = reg.open_question_numbers(engagement_id)
        except Exception:  # noqa: BLE001 — degrade to "no open questions"
            return []
        excluded = set(self._answered_overlay.get(engagement_id) or ())
        reserved_n = self._reserved_question_number(engagement_id)
        if reserved_n is not None:
            excluded.add(reserved_n)
        if not excluded:
            return list(nums)
        return [n for n in nums if n not in excluded]

    # -- v0.83.0 (§A3) answered-reservation token lifecycle -------------------
    def _reserved_question_number(self, engagement_id: str) -> int | None:
        """The question number currently RESERVED for this engagement (counts as
        answered for the effective/union view), or ``None``."""
        res = self._answer_reservations.get(engagement_id)
        return res[1] if res is not None else None

    def _oldest_unanswered_anchor(
        self, engagement_id: str, *, exclude_reserved: bool,
    ) -> dict | None:
        """The oldest open free-text anchor that is not yet answered — where
        "answered" is ``persisted flag ∪ overlay`` and, when ``exclude_reserved``,
        also ``∪ reserved``. Returns the RAW entry dict or ``None``.

        ``exclude_reserved=True`` is the RESERVE selection (never re-reserve an
        already-reserved anchor). ``exclude_reserved=False`` is the PROMOTION
        selection (a reserved anchor is exactly the one promotion consumes)."""
        reg = self._registry
        if reg is None:
            return None
        entries_getter = getattr(reg, "open_question_entries", None)
        if entries_getter is None:  # legacy registry without the raw accessor
            getter = getattr(reg, "oldest_open_anchor", None)
            return getter(engagement_id) if getter is not None else None
        reserved_n = (
            self._reserved_question_number(engagement_id)
            if exclude_reserved else None
        )
        anchors = [
            q for q in entries_getter(engagement_id)
            if q.get("kind") == "anchor"
            and not q.get("answered", False)
            and not self._overlay_answered(engagement_id, q.get("n"))
            and q.get("n") != reserved_n
        ]
        if not anchors:
            return None
        return min(anchors, key=lambda q: q.get("n", 0))

    # -- v0.83.0 (§A3(a)+(c)) ask-maintenance lock + ingress marker ----------
    def ask_maintenance_lock(self, engagement_id: str) -> asyncio.Lock:
        """The per-engagement ASK-MAINTENANCE lock (create-on-demand). PINNED
        DISCIPLINE: only ever taken on driver/handler tasks with the SEQUENCER
        lock NOT held — the relay-invoked poster (which runs under the sequencer
        lock) NEVER acquires it. It serializes the ask ingress reservation
        (pending-predicate check + marker set) against the reply gate's check."""
        lock = self._ask_maint_locks.get(engagement_id)
        if lock is None:
            lock = asyncio.Lock()
            self._ask_maint_locks[engagement_id] = lock
        return lock

    def ask_inflight(self, engagement_id: str) -> str | None:
        """The request_id of an ask that passed its gates but is not yet durable
        (broker-registered / add_open_question'd), or ``None``. Read under the
        maintenance lock by the ingress reservation + reply gate predicates."""
        return self._ask_inflight.get(engagement_id)

    def set_ask_inflight(self, engagement_id: str, request_id: str) -> None:
        """Claim the ingress marker for ``request_id`` (called under the
        maintenance lock, right after the pending predicate passes)."""
        self._ask_inflight[engagement_id] = request_id

    def clear_ask_inflight(
        self, engagement_id: str, request_id: str | None = None,
    ) -> None:
        """Clear the ingress marker — CAS on ``request_id`` (clear only when the
        marker still belongs to this request, so a terminal-failure backstop can
        never clobber a newer ask's marker). ``request_id=None`` clears
        unconditionally. Cleared WITHOUT the maintenance lock (asyncio
        run-to-completion makes the sync marker→durable handoff gap-free)."""
        if request_id is None or self._ask_inflight.get(engagement_id) == request_id:
            self._ask_inflight.pop(engagement_id, None)

    def effective_open_anchor(self, engagement_id: str) -> dict | None:
        """The oldest UNANSWERED free-text anchor (effective view — persisted
        ``answered`` ∪ overlay ∪ reserved excluded), or ``None``. The A3 reply /
        stacking gates' unanswered-anchor clause."""
        return self._oldest_unanswered_anchor(engagement_id, exclude_reserved=True)

    async def mark_send_intent_compensated(
        self, engagement_id: str, request_id: str, message_id: int,
    ) -> Any:
        """§A3(c) (Sol r5-5/r6-1): the ``mark_send_intent_posted``-adjacent
        COMPENSATED seam. The initial-anchor poster calls this on an
        ``add_open_question`` failure AFTER the wire post: the sequencer advances
        high-water to ``message_id`` and resolves the intent exactly once as
        ``{"ok": False, "message_id": message_id, "compensated": True}`` — the
        physical write is accounted, the logical result is failure."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return None
        return await seq.mark_intent_compensated(request_id, message_id)

    async def settle_answered_anchor(
        self, engagement_id: str, number: int,
    ) -> int | None:
        """§A3(c) post-add generation re-check: an operator envelope that arrived
        between the ingress reservation and ``add_open_question`` IS this anchor's
        answer. Mark it answered (strict; overlay on raise — Task-6 policy) and
        run the shared visual settle. NOT maintenance-locked — the caller is the
        relay poster under the sequencer lock (settle stays lock-free here)."""
        reg = self._registry
        if reg is None:
            return None
        engagement = reg.get(engagement_id)
        if engagement is None:
            return None
        mark = getattr(reg, "mark_question_answered", None)
        if mark is not None:
            try:
                await mark(engagement_id, number)
            except Exception:  # noqa: BLE001 — overlay covers the live process
                self.mark_answered_overlay(engagement_id, number)
        # DEADLOCK NOTE (B2): this runs on the relay poster task UNDER the
        # sequencer lock, so it MUST stay lock-free — acquiring the maintenance
        # lock here would deadlock a concurrent re-anchor that holds maintenance
        # and is blocked on the sequencer lock at ``post_discrete``. Safe without
        # the lock: the entry was JUST marked answered above, so a racing
        # re-anchor's step-2 revalidate (which itself blocks on the sequencer lock
        # WE hold) declines and never posts a competing copy. Re-read fresh by
        # number and settle the CURRENT copy.
        amid = await self._settle_anchor_entry_locked(engagement, number)
        try:
            await self.recompute_engagement_status(engagement_id)
        except Exception:  # noqa: BLE001 — status recompute is advisory
            logger.debug("recompute after answered-anchor settle failed",
                         exc_info=True)
        return amid

    def reserve_answer(self, engagement_id: str) -> str | None:
        """§A3 (Sol r7-1): reserve the oldest UNANSWERED anchor as answered by
        the current operator message, carrying a unique token. Returns the token,
        or ``None`` when no unanswered anchor is open (nothing to reserve — the
        message still enqueues; promotion is unconditional). Called SYNCHRONOUSLY
        at Telegram handler entry, in the same section as the high-water advance
        (no await between), so a concurrent result-finalize that observed the
        answer's high-water also observes the reservation."""
        anchor = self._oldest_unanswered_anchor(
            engagement_id, exclude_reserved=True)
        if anchor is None:
            return None
        n = anchor.get("n")
        if n is None:
            return None
        token = uuid.uuid4().hex
        self._answer_reservations[engagement_id] = (token, n)
        return token

    async def rollback_answer_reservation(
        self, engagement_id: str, token: str | None,
        *, suppress_reanchor: bool = False,
    ) -> bool:
        """§A3 (Sol r7-1/r9-1): CAS-roll-back a reservation — clear it ONLY when
        the CURRENT reservation still carries ``token`` (never clobber a later
        message's reservation) AND it was not already promoted (promotion deletes
        the entry, so a consumed reservation is simply absent → CAS fails).
        Returns ``True`` on a successful clear. On success calls the Task-9 seam
        ``_on_reservation_rolled_back`` (a last-reservation-clearing rollback
        schedules a compensating re-anchor pass there).

        F2 (whole-branch gate): a TERMINAL command (``/cancel``/``/complete``)
        rolls back the reservation and then FINALIZES the engagement, whose
        ``settle_all_open_questions`` owns every open anchor. Firing the
        fourth-consumer re-anchor pass here would post a redundant anchor copy
        that terminal settlement immediately settles. When ``suppress_reanchor``
        is set, the CAS clear still happens but the re-anchor latch is NEITHER
        set NOR consumed — the imminent terminal settle owns the entries."""
        if token is None:
            return False
        res = self._answer_reservations.get(engagement_id)
        if res is None or res[0] != token:
            return False
        del self._answer_reservations[engagement_id]
        if not suppress_reanchor:
            await self._on_reservation_rolled_back(engagement_id)
        return True

    async def _on_reservation_rolled_back(self, engagement_id: str) -> None:
        """§A3(b) consumer (d) — the FOURTH turn boundary (Sol r10-1/r12-1): a CAS
        rollback that cleared a still-un-promoted reservation lands here. An IDLE
        engagement whose operator message did NOT become a delivered turn
        (``/silent``, a rejected originator-only command, a handler cancellation,
        a spool-rejected message) gets no later ``result``, abnormal spawn, or
        terminal finalize — so the rollback path itself, AFTER any platform notice
        it already posted (Sol r12-1 ordering), directly runs the same idempotent,
        revalidated, maintenance-locked re-anchor pass so the anchor still ends up
        LAST. SET the latch first (case b: a rollback CAS cleared the last
        reservation) so a failed pass leaves the obligation armed for the retry
        owner / a racing boundary consumer (harmlessly — one finds it consumed)."""
        reg = self._registry
        engagement = reg.get(engagement_id) if reg is not None else None
        if engagement is None:
            return
        self.set_reanchor_due(engagement_id)
        await self._consume_reanchor(engagement)

    async def _promote_answer_on_enqueue(
        self, engagement: EngagementRecord,
    ) -> int | None:
        """§A3 (Sol r9-1): PROMOTE the answer at a durable non-initial enqueue.
        UNCONDITIONAL — any delivered operator message answers the one open
        question, regardless of which token (if any) holds the reservation:
        mark the oldest unanswered anchor answered (``mark_question_answered``
        strict; on raise → overlay per Task-6 policy), run the SAME visual settle
        the delivery-time path uses, and CONSUME the reservation. Returns the
        anchor's tg_message_id (recorded on the envelope so delivery only
        threads), or ``None`` when no anchor is open."""
        eid = engagement.id
        anchor = self._oldest_unanswered_anchor(eid, exclude_reserved=False)
        # Consume the reservation regardless of whether an anchor was found —
        # promotion is the reservation's terminal state (Sol r9-1).
        self._answer_reservations.pop(eid, None)
        # §A3(b) latch-clear discipline (Sol r13-1): PROMOTION (the question got
        # answered) is one of the three latch-clearing events — discharge the
        # re-anchor obligation. §D6 r29-2 PUMP (D4 review): retire the retry
        # owner ONLY when no historical items remain — mirrors the guard in
        # ``_consume_reanchor``. A confirmed-pair memory record surviving
        # persist exhaustion (or a durable ``reanchored`` stale entry) still
        # needs the standing pump's marker-edit cleanup; retiring
        # unconditionally here would strand that cleanup on the next
        # unrelated output boundary instead.
        self._reanchor_due.discard(eid)
        if not self._historical_items(eid):
            self._retire_reanchor_retry(eid)
        if anchor is None:
            return None
        n = anchor.get("n")
        reg = self._registry
        mark = getattr(reg, "mark_question_answered", None) if reg else None
        if mark is not None and n is not None:
            try:
                await mark(eid, n)
            except Exception:  # noqa: BLE001 — strict persist failed
                # §A3 answered-persist-failure policy (Sol r6-4/r7-2): the
                # envelope is already durably spooled, so the question must not
                # keep gating. The overlay covers the live process; the strict
                # write retries at each later settle attempt.
                self.mark_answered_overlay(eid, n)
        # Visual settle for THIS anchor — shared with the delivery-time path.
        return await self._settle_open_anchor(engagement, anchor=anchor)

    async def _settle_ledger_entry(
        self, rec: EngagementRecord, q: dict, *, answered_suffix: bool,
        override_suffix: str | None = None,
    ) -> int | None:
        """Settle ONE open-question entry's CURRENT copy plus every staged
        ``stale_mids`` copy, honoring the entry-removal invariant (§A3, Sol r5-3):
        the entry is REMOVED only when the current settle edit is CONFIRMED AND no
        stale copy remains; otherwise it PERSISTS (its ``answered`` flag keeps it
        invisible to the gates/summary) with each confirmed stale copy un-staged.

        ``answered_suffix`` picks the terminal copy (``✅ answered below`` vs
        ``⌛ expired``). Independently, when the entry is overlay-answered but its
        durable ``answered`` flag never landed, the strict persist is RETRIED here
        (Sol r6-4/r7-2 — convergence at each later settle attempt). Returns the
        entry's current ``tg_message_id`` (for anchor reply-threading)."""
        reg = self._registry
        n = q.get("n")
        # Converge the answered flag when an earlier strict persist failed
        # (overlay covers the live process; retry before the visual settle).
        if (reg is not None and n is not None
                and not q.get("answered", False)
                and self._overlay_answered(rec.id, n)):
            mark = getattr(reg, "mark_question_answered", None)
            if mark is not None:
                try:
                    await mark(rec.id, n)
                except Exception:  # noqa: BLE001 — retry again at the next settle
                    logger.debug(
                        "mark_question_answered retry failed (n=%s)", n,
                        exc_info=True)
        if override_suffix is not None:
            suffix = override_suffix
        else:
            suffix = (
                _OPEN_Q_ANSWERED_SUFFIX if answered_suffix
                else _OPEN_Q_EXPIRED_SUFFIX
            )
        display = q.get("text") or (f"Q{n}:" if n is not None else "")
        text = f"{display}{suffix}"
        cur_mid = q.get("tg_message_id")
        current_confirmed = await self._confirm_settle_mid(rec, cur_mid, text, n)
        # A8 · Q1-settle observability: one INFO line per CONFIRMED settle (a real
        # keyboard-clearing edit landed). The outcome mirrors the settle copy.
        if current_confirmed and cur_mid is not None:
            if override_suffix == _OPEN_Q_CANCELLED_SUFFIX:
                _outcome = "cancelled"
            elif answered_suffix:
                _outcome = "answered"
            else:
                _outcome = "expired"
            logger.info(
                "ask settle CONFIRMED (eng=%s q=%s mid=%s outcome=%s)",
                rec.id[:8], n if n is not None else "-", cur_mid, _outcome)
        # Every staged stale copy is settled with the same confirmed-gate; a
        # confirmed one is un-staged, an unconfirmed one keeps the entry present.
        # v0.84.0 (round-4 §D6): each stale entry's RENDERING is chosen by its
        # normalized ``kind`` — a "reanchored" copy settles to the pinned
        # marker-only terminal text, NEVER the duplicated full body ``text``
        # every other (current + "plain" stale) copy uses; legacy bare-int
        # entries normalize to "plain" (today's rendering, unchanged).
        remaining_stale: list[Any] = []
        unstage = getattr(reg, "unstage_stale_mid", None) if reg is not None else None
        for raw_stale in list(q.get("stale_mids") or []):
            stale = normalize_stale_mid_entry(raw_stale)
            smid = stale["mid"]
            stale_text = (
                _reanchor_moved_terminal(n) if stale["kind"] == "reanchored"
                else text
            )
            if await self._confirm_settle_mid(rec, smid, stale_text, n):
                if unstage is not None and n is not None:
                    try:
                        await unstage(rec.id, n, smid)
                    except Exception:  # noqa: BLE001
                        # M4: a confirmed stale-copy edit whose STRICT unstage
                        # persist RAISES must NOT let the entry close with a
                        # durable stale_mid still staged — keep the mid in
                        # ``remaining_stale`` so no close happens this pass and
                        # boot/settle reconciliation retries it.
                        logger.warning(
                            "engagement %s: unstage_stale_mid persist failed "
                            "(n=%s mid=%s) — retaining entry", rec.id[:8], n,
                            smid, exc_info=True)
                        remaining_stale.append(raw_stale)
            else:
                remaining_stale.append(raw_stale)
        # Entry-removal invariant: remove ONLY when the current copy is confirmed
        # AND no stale copy remains; otherwise the entry persists.
        if current_confirmed and not remaining_stale:
            close = getattr(reg, "close_open_question", None) if reg is not None else None
            if close is not None and n is not None:
                try:
                    await close(rec.id, n)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "engagement %s: close_open_question failed (n=%s)",
                        rec.id[:8], n, exc_info=True)
        # v0.84.0 (§D6 r20-1): answer/terminal SETTLEMENT also consults the
        # confirmed-pair record — an UNPERSISTED orphan copy (nothing durable
        # tracks it) is marker-edited to the terminal form here, retiring the
        # record ONLY on a CONFIRMED edit (an unconfirmed edit KEEPS the record so
        # the unified historical scheduler re-attempts while the engagement lives;
        # at terminal teardown an unconfirmed record becomes the logged residual).
        orphan_mid = self._confirmed_pair_mid(rec.id, n)
        if orphan_mid is not None and n is not None:
            if await self._orphan_marker_edit(rec, orphan_mid, n):
                self._retire_confirmed_pair(rec.id, n)
        return cur_mid

    async def _confirm_settle_mid(
        self, rec: EngagementRecord, mid: int | None, text: str, n: int | None,
    ) -> bool:
        """Confirmed-settle ONE message id (§A3). ``True`` when there is nothing to
        settle (``mid`` is None) OR the edit is confirmed; ``False`` fail-closed
        when a message-backed copy has no edit primitive, or the bounded retry
        stays unconfirmed — leaving the ledger entry intact for a later pass."""
        if mid is None:
            return True
        if self._edit_topic_message is None:
            logger.warning(
                "engagement %s: open-question settle has a message id but no edit "
                "primitive (n=%s) — leaving ledger entry INTACT", rec.id[:8], n)
            return False
        settled = await confirmed_settle_edit(
            lambda mid=mid, text=text: self._edit_topic_message(
                rec.topic_id, mid, text, clear_keyboard=True),
            sleep=self._sleep,
        )
        if not settled:
            logger.warning(
                "engagement %s: open-question settle UNCONFIRMED after retries "
                "(n=%s) — leaving ledger entry INTACT", rec.id[:8], n)
        return settled

    async def recompute_engagement_status(self, engagement_id: str) -> None:
        """W-R2: on ask/anchor SETTLEMENT, recompute the summary status from the
        REMAINING open questions — stay ⏳ waiting while any question is still
        open; return to ⚙️ working only when none remain AND the turn is still
        running. A terminal status stays absolute (``submit_status`` rejects any
        later transition). Each transition acquires a fresh revision, so the
        linearization pin (registration → waiting → THEN settlement) guarantees
        a fast tap during the post window can never leave the summary
        stuck-waiting: the recompute's revision is always allocated after the
        waiting submission's."""
        from drivers.summary_controller import (
            STATUS_WAITING_REPLY, STATUS_WORKING,
        )
        # §A3: read the EFFECTIVE unanswered set (persisted flag ∪ overlay), so an
        # answered-but-unconfirmed-settle question stops holding the summary ⏳.
        open_qs = self._effective_open_question_numbers(engagement_id)
        if open_qs:
            await self._summary_status_transition(
                engagement_id, STATUS_WAITING_REPLY)
        elif self._turn_running.get(engagement_id):
            await self._summary_status_transition(
                engagement_id, STATUS_WORKING)
        # F1 (Sol diff gate): the open-questions SET may have changed with NO
        # status-class transition — one of several questions settled while the
        # summary stays ⏳ waiting, or none remain but the turn already ended
        # (neither branch above fires). Force a summary refresh so the pinned
        # open-questions line reflects the remaining set; the no-op gate elides
        # a redundant edit when a transition above already reflowed it.
        ctrl = self._summaries.get(engagement_id)
        if ctrl is not None:
            try:
                await ctrl.refresh()
            except Exception:  # noqa: BLE001 — summary refresh is advisory
                logger.debug(
                    "summary refresh after status recompute failed",
                    exc_info=True)

    async def finalize_summary(
        self, engagement: EngagementRecord, outcome: str,
    ) -> None:
        """§5 engagement finalize: set the TERMINAL summary status (absolute),
        cancel the tick and perform the mandatory finalize flush. Called by
        ``_finalize_engagement`` while the topic is still open."""
        from drivers.summary_controller import OUTCOME_STATUS, STATUS_ERROR
        ctrl = self._summaries.get(engagement.id)
        if ctrl is None:
            return
        await ctrl.finalize(OUTCOME_STATUS.get(outcome, STATUS_ERROR))

    def _ensure_sequencer(self, engagement: EngagementRecord) -> "OutputSequencer":
        """Build (or return) the per-engagement OUTPUT SEQUENCER (§2).

        Wraps the driver's own relay send/edit primitives so the sequencer, the
        topic-stream relay and the discrete ingresses all drive ONE serialized
        writer with a single high-water mark and intent registry.
        """
        from channels.output_sequencer import OutputSequencer

        seq = self._sequencers.get(engagement.id)
        if seq is None:
            seq = OutputSequencer(
                engagement_id=engagement.id,
                topic_id=engagement.topic_id,
                send_message=self._relay_send_message,
                edit_message=self._relay_edit_message,
                # A9: markup-capable wire for post_discrete/edit_discrete.
                send_message_markup=self._relay_send_message_markup,
                edit_message_markup=self._relay_edit_message_markup,
                # v0.109.0 (G5): paged sender for the terminal completion post.
                send_paged=self._send_to_topic_paged,
            )
            self._sequencers[engagement.id] = seq
        return seq

    # -- v0.79.0 (§2) discrete-posting intent-registration API (T2/T3 seam) --

    def register_send_intent(
        self, *, engagement_id: str, request_id: str, tool_name: str,
        projection_hash: str, poster: Any, on_retire: Any = None,
    ) -> Any:
        """Register (or idempotently reattach to) a discrete-send INTENT (§2(1)).

        The T2/T3 ingress adapters call this at fence entry. ``poster`` is an
        ``async () -> int | None`` that performs the actual keyboard/text post
        (the sequencer invokes it when the relay reaches the matching content
        block). Returns ``(intent, created)`` or ``None`` if the engagement has
        no live sequencer. A same-``request_id`` call reattaches idempotently
        and the caller can read the recorded outcome via ``send_intent_outcome``.
        """
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return None
        return seq.register_intent(
            request_id=request_id, tool_name=tool_name,
            projection_hash=projection_hash, poster=poster,
            on_retire=on_retire,
        )

    def set_send_intent_poster(
        self, engagement_id: str, request_id: str, poster: Any,
    ) -> Any:
        """Install the REAL relay-invoked poster on a registered intent (§2(3),
        T3). The ask/reply ingress registers early (reattach idempotency), then
        sets the poster and arms; the relay posts it at its tool_use block."""
        seq = self._sequencers.get(engagement_id)
        return seq.set_intent_poster(request_id, poster) if seq is not None else None

    def arm_send_intent(self, engagement_id: str, request_id: str) -> Any:
        """Move a pending intent to ``armed`` — the point of no return (§2(2))."""
        seq = self._sequencers.get(engagement_id)
        return seq.arm_intent(request_id) if seq is not None else None

    def cancel_send_intent(self, engagement_id: str, request_id: str) -> Any:
        """Cancel a pending/armed intent → tombstone (§2(2))."""
        seq = self._sequencers.get(engagement_id)
        return seq.cancel_intent(request_id) if seq is not None else None

    def record_send_intent_refusal(
        self, engagement_id: str, request_id: str, outcome: dict,
    ) -> Any:
        """A2 (Finding 1): tombstone the intent AND record a refusal outcome so a
        same-request_id retry reattaches to the SAME refusal. See
        ``OutputSequencer.record_intent_refusal``."""
        seq = self._sequencers.get(engagement_id)
        return seq.record_intent_refusal(request_id, outcome) if seq is not None else None

    def record_send_intent_cancelled_nowait(
        self, engagement_id: str, request_id: str, outcome: dict,
    ) -> str:
        """A3 · F-ORDER (Sol A3 wave 4): FULLY SYNCHRONOUS transport-cancellation
        cleanup against an in-flight relay post — NO await, so a second
        ``Task.cancel()`` cannot interrupt it mid-flight (the double-cancel window
        the awaited wave-3 predecessor left open). Reads ``posting`` synchronously
        and returns a TRI-STATE (wb1-1): ``"post_won"`` when a relay post is in
        flight / resolved (the cancel LOSES — the poster owns the marker + outcome,
        never clobbered), ``"cancelled"`` when it tombstoned a still-cancellable
        intent (the caller then clears the ingress marker), ``"absent"`` when there
        is no such intent (nothing to post — the caller may clear). See
        ``OutputSequencer.record_intent_cancelled_nowait``."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return "absent"
        return seq.record_intent_cancelled_nowait(request_id, outcome)

    def send_intent_outcome(self, engagement_id: str, request_id: str) -> Any:
        """Recorded outcome (incl. posted message id) for a reattaching retry."""
        seq = self._sequencers.get(engagement_id)
        return seq.intent_outcome(request_id) if seq is not None else None

    def consume_turn_reply_to(self, engagement_id: str) -> int | None:
        """#332: fetch-and-clear the sequencer's one-shot turn reply target
        (v0.79.0 §3) — the ask/reply ingress posters call this so whichever
        output posts FIRST this turn threads to the operator's message."""
        seq = self._sequencers.get(engagement_id)
        return seq.consume_turn_reply_to() if seq is not None else None

    def restore_turn_reply_to(
        self, engagement_id: str, message_id: int | None,
    ) -> None:
        """#332 failure arm: re-arm a consumed-but-unsent turn reply target
        (never clobbers a newer one — see the sequencer method)."""
        seq = self._sequencers.get(engagement_id)
        if seq is not None:
            seq.restore_turn_reply_to(message_id)

    async def mark_send_intent_posted(
        self, engagement_id: str, request_id: str, message_id: int | None,
    ) -> Any:
        """Record a discrete ingress' out-of-band post (§2(5) consumption debt +
        reattach outcome). See ``OutputSequencer.mark_intent_posted``."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return None
        return await seq.mark_intent_posted(request_id, message_id)

    async def await_send_intent(
        self, engagement_id: str, request_id: str, timeout: float | None = None,
    ) -> Any:
        """F3 fail-closed: block until the deferred intent posts (ok) or fails
        (ok:false), bounded by the sequencer's transport budget. Returns the
        recorded outcome dict, or ``None`` if there is no live sequencer /
        intent (the caller treats that as a non-post). See
        ``OutputSequencer.await_intent_resolution``."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return None
        return await seq.await_intent_resolution(request_id, timeout)

    async def advance_topic_high_water_for_inbound(
        self, engagement_id: str, operator_msg_id: int | None = None,
    ) -> None:
        """§2: an inbound operator message advances the high-water mark and
        SEALS open narration. (The inbound-spool call site is wired by T2.)"""
        seq = self._sequencers.get(engagement_id)
        if seq is not None:
            await seq.advance_high_water_for_inbound(operator_msg_id)

    # -- v0.79.0 (§4) ask inbound-gate reads + refusal escalation ----------

    def inbound_generation(self, engagement_id: str) -> int:
        """§4 gate: the current operator-message generation for the ask
        race-close re-check. 0 when no spool exists (degraded / no driver)."""
        spool = self._inbound.get(engagement_id)
        return spool.generation() if spool is not None else 0

    def inbound_unread_texts(self, engagement_id: str) -> list[str]:
        """G4 D4: queued-unseen operator texts ([] when no spool)."""
        spool = self._inbound.get(engagement_id)
        return spool.unread_texts() if spool is not None else []

    def reserve_inbound(self, engagement_id: str) -> None:
        """G4 D2: SYNCHRONOUS ingress reservation — taken by the trusted
        Telegram handler under the topic lock BEFORE the background
        delivery task exists, so an accepted-but-not-yet-spooled message
        counts as unread at completion time."""
        self._inbound_reservations[engagement_id] = (
            self._inbound_reservations.get(engagement_id, 0) + 1)

    def release_inbound_reservation(self, engagement_id: str) -> None:
        n = self._inbound_reservations.get(engagement_id, 0) - 1
        if n <= 0:
            self._inbound_reservations.pop(engagement_id, None)
        else:
            self._inbound_reservations[engagement_id] = n

    def inbound_reservations(self, engagement_id: str) -> int:
        return self._inbound_reservations.get(engagement_id, 0)

    def record_completion_refusal(self, engagement_id: str) -> int:
        """G4 D3: bump + return the count of consecutive unread_inbound
        completion refusals. Reset at turn_start and teardown. From the
        2nd, the caller escalates via the forced turn boundary so the
        pending envelope actually pumps (Sol g4-r1-4)."""
        n = self._completion_refusals.get(engagement_id, 0) + 1
        self._completion_refusals[engagement_id] = n
        if n >= 3:
            logger.warning(
                "engagement %s: %d consecutive completion refusals — "
                "unread operator inbound persists across retries",
                engagement_id[:8], n)
        return n

    async def force_completion_turn_boundary(self, engagement) -> None:
        """G4 D3 (v0.96.0): escalation for repeated ``unread_inbound``
        completion refusals — force a REAL turn boundary so the queued
        operator envelope actually pumps at the respawn (delivery re-arms
        only at ``on_spawn``; doctrine alone can livelock a completion-bent
        model — Sol g4-r1-4). Reuses the operator-away verified group-kill;
        s6 respawns the run script, which resumes the session and receives
        the pending envelope.

        #341 (v0.138.0): the kill is EPOCH-GUARDED and SINGLE-FLIGHT.
        ``emit_completion`` handlers dispatch without a per-engagement lock,
        so overlapping refusals (or a delayed escalation) used to signal with
        ``expected_epoch=None`` — the helper's epoch guard was disabled and a
        late kill could land on a FRESHLY RESPAWNED turn that had already
        consumed a queued operator envelope (consumed ⇒ never redelivered ⇒
        operator-turn loss). Now: (a) an in-flight force task (completion- or
        away-triggered) owns the boundary — a second call defers to it;
        (b) the CURRENT spawn epoch is stamped and passed so the guard (and
        the pid-stability re-probe behind it) aborts against any newer
        generation; (c) no known live epoch ⇒ nothing attributable to kill ⇒
        no signal (the refusal reply alone must do the work)."""
        eng_id = engagement.id
        existing = self._force_tasks.get(eng_id)
        if existing is not None and not existing.done():
            logger.info(
                "engagement %s: completion-gate boundary — kill already in "
                "flight, deferring to it", eng_id[:8])
            return
        epoch = self._epoch_pending.get(eng_id)
        if epoch is None:
            logger.info(
                "engagement %s: completion-gate boundary — no live spawn "
                "epoch, not signalling", eng_id[:8])
            return
        # Stamp the epoch expected-terminated so the ensuing respawn's
        # spawn-without-result is logged as an expected forced end, and so
        # the guard kills ONLY this generation.
        self._forced_suspend_epochs[eng_id] = epoch
        task = asyncio.create_task(
            self._force_turn_boundary(
                engagement_id=eng_id,
                # Task 4: ``.spawn_epoch`` moved to the control dir — this
                # kwarg is s6_rc's read-target for the epoch guard, not
                # literally "the workspace" anymore.
                workspace_dir=control_dir(eng_id),
                expected_epoch=epoch,
                track_task=(lambda t, eid=eng_id:
                            self._register_force_cleanup(eid, t)),
            ),
            name=f"force_completion:{eng_id[:8]}")
        self._force_tasks[eng_id] = task
        task.add_done_callback(
            lambda t, eid=eng_id: (
                self._force_tasks.pop(eid, None)
                if self._force_tasks.get(eid) is t else None))
        # Awaiting the task keeps the caller's semantics (the tool handler
        # logs best-effort); an outer cancel stops the WAIT, never the kill —
        # the task stays owned by ``_force_tasks`` / the teardown drain.
        # Sol r8: shield() ALSO raises CancelledError when the INNER task is
        # cancelled by its owner (operator re-engagement clears the away
        # kill; teardown cancels the shared ``_force_tasks`` entry) — that is
        # not OUR cancellation, and letting it out would kill the
        # emit_completion handler (which catches only Exception) instead of
        # returning its documented refusal. Inner cancel ⇒ the escalation is
        # simply over — return quietly; only a genuine outer cancellation
        # propagates.
        try:
            ok = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                logger.info(
                    "engagement %s: completion-gate boundary cancelled by "
                    "its owner (operator returned / teardown)", eng_id[:8])
                return
            raise
        logger.info(
            "engagement %s: completion-gate forced turn boundary (ok=%s)",
            eng_id[:8], ok)

    def inbound_unread_depth(self, engagement_id: str) -> int:
        """§4 gate: number of unseen queued operator messages. ``> 0`` refuses
        the ask. 0 when no spool exists."""
        spool = self._inbound.get(engagement_id)
        return spool.unread_depth() if spool is not None else 0

    def record_ask_refusal(self, engagement_id: str) -> int:
        """§4: bump + return the count of consecutive ask refusals THIS turn.
        From the 3rd, log a WARN counter (observability for a future hard
        turn-end primitive — OUT of scope here; the escalated copy is the only
        mechanism)."""
        n = self._ask_refusals.get(engagement_id, 0) + 1
        self._ask_refusals[engagement_id] = n
        if n >= 3:
            logger.warning(
                "engagement %s: %d consecutive ask refusals this turn — the "
                "agent keeps asking while an operator message is unread "
                "(no hard force-end primitive exists; soft escalation only)",
                engagement_id[:8], n,
            )
        return n

    # -- F-EXPIRE (A2a) operator-away suspend state ------------------------

    async def note_operator_away(self, engagement_id: str, gen: int) -> bool:
        """F-EXPIRE ENTER (generation-CAS, Sol r1-3/r2-2): mark the engagement
        operator-away ONLY IF the inbound generation still equals ``gen`` — the
        value sampled at the ask's entry and carried in the broker meta as
        ``inbound_gen``. A racing inbound that already bumped the generation
        FAILS the CAS, so a lost-response retry reattaching after an inbound
        cleared the away state can never re-wedge it with a fresher generation.
        Returns whether the flag was set. In-memory only (no persistence).

        Sol A2 review (Finding 3): on a SUCCESSFUL CAS, DRIVE the paused summary
        DIRECTLY — the timeout settle edit that would otherwise recompute the
        status may be unconfirmed (``_close_question``'s recompute never runs),
        leaving the engagement showing ⏳ forever while suspended. Submitting
        STATUS_PAUSED through the ONE ``_summary_status_transition`` funnel (a
        fresh monotonic revision) makes ⏸ appear regardless; the funnel's
        away-coercion + monotonic revision make a redundant/racing submit
        idempotent."""
        if self.inbound_generation(engagement_id) != gen:
            return False
        self._operator_away[engagement_id] = True
        from drivers.summary_controller import STATUS_PAUSED
        try:
            await self._summary_status_transition(engagement_id, STATUS_PAUSED)
        except Exception:  # noqa: BLE001 — the away flag stands regardless
            logger.debug(
                "paused-status submit after operator-away entry failed",
                exc_info=True)
        return True

    def operator_away_active(self, engagement_id: str) -> bool:
        """F-EXPIRE gate: True while the engagement is SUSPENDED waiting for the
        operator (set on ask expiry, cleared on the next durable inbound)."""
        return self._operator_away.get(engagement_id, False)

    def record_away_refusal(self, engagement_id: str) -> int:
        """F-EXPIRE backstop counter: bump + return the count of consecutive
        operator-away ask refusals in the current away-episode. Task 5's
        force-turn-boundary backstop CONSUMES this count; nothing here
        force-ends. Reset when the away state clears (``_clear_operator_away``).

        A2b HARD BACKSTOP: refusals are instant, so a doctrine-defying agent
        could loop ask→refusal at token speed. On the 2nd refusal in an episode
        (``_away_suspend_fired`` gates re-firing) the driver force-ends the CLI
        turn ONCE via the verified group-kill. The trigger lives here — the
        handler-side gate already routes every refusal through this method, so
        channel_handlers needs no further change."""
        n = self._away_refusals.get(engagement_id, 0) + 1
        self._away_refusals[engagement_id] = n
        # F3: after an unverified outcome the guard re-arms but a monotonic
        # cooldown must elapse before it may re-fire — else an ask→refusal loop
        # churns probes/subprocesses at token speed.
        cooldown_until = self._away_force_cooldown_until.get(engagement_id, 0.0)
        if (
            n >= 2
            and engagement_id not in self._away_suspend_fired
            and self._monotonic() >= cooldown_until
        ):
            self._away_suspend_fired.add(engagement_id)
            self._trigger_force_suspend(engagement_id)
        return n

    def _trigger_force_suspend(self, engagement_id: str) -> None:
        """A2b: mark the current epoch expected-terminated BEFORE signalling (so
        ``_log_abnormal_exit`` annotates the ensuing respawn), then spawn the
        verified group-kill as a tracked background task. Degrades to a no-op if
        no event loop is running (a sync fake context)."""
        # Whole-branch gate r3: SINGLE-FLIGHT — never overwrite a live owner
        # (a cancelled-but-not-yet-done predecessor must stay visible to the
        # drain until its done callback retires it; overwriting would hide it
        # from both drain surfaces for one loop turn). Checked BEFORE creating
        # a duplicate task so no side-effecting coroutine ever starts.
        existing = self._force_tasks.get(engagement_id)
        if existing is not None and not existing.done():
            logger.debug(
                "force_turn_boundary: kill already in flight for %s — "
                "deferring to it", engagement_id[:8])
            return
        # Stamp the epoch first — the kill races the respawn's spawn event.
        self._forced_suspend_epochs[engagement_id] = self._epoch_pending.get(
            engagement_id)
        try:
            task = asyncio.create_task(
                self._run_force_suspend(engagement_id),
                name=f"force_suspend:{engagement_id[:8]}")
        except RuntimeError:  # no running loop — cannot schedule the kill
            logger.debug(
                "force_turn_boundary: no running loop; skipping force-end "
                "for %s", engagement_id[:8])
            # Sol A2 wave-3, Finding 1: nothing fired → RE-ARM so a later refusal
            # (with a loop) can retry rather than staying permanently latched.
            self._away_suspend_fired.discard(engagement_id)
            return
        # A2b (Sol A2 review): hold a DEDICATED per-engagement handle (replacing
        # the anonymous ``_tasks`` append) so ``_clear_operator_away`` can cancel
        # this specific kill on operator re-engagement. Retire the handle when
        # the task completes so a stale/cancelled reference is never re-cancelled.
        self._force_tasks[engagement_id] = task
        task.add_done_callback(
            lambda t, eid=engagement_id: (
                self._force_tasks.pop(eid, None)
                if self._force_tasks.get(eid) is t else None))

    def _register_force_cleanup(
        self, engagement_id: str, task: asyncio.Task,
    ) -> None:
        """Adopt ``force_turn_boundary``'s shielded post-signal cleanup task when
        the force-suspend task is cancelled mid-kill (operator returned after
        SIGTERM was sent).

        Sol A2 wave-3, Finding 2: the cleanup is CANCEL-EXEMPT — it must complete
        its extinction poll + SIGKILL escalation (bounded ≤ ~7 s) even under
        engagement teardown, or a SIGTERM-resistant MCP/tool child survives while
        verification is skipped. It therefore goes in the DEDICATED
        ``_force_cleanups`` set that ``cancel()`` NEVER cancels — NOT
        ``_tasks[eng_id]`` (wave-2's bug): ``cancel()`` iterates+cancels
        ``_tasks`` and, having already popped ``_tasks[eng_id]``, a handoff onto
        it would (a) risk cancelling the kill and (b) resurrect a stale, never-
        reaped ``_tasks`` entry after teardown. The ``add_done_callback`` retires
        the task from the set on completion so no reference lingers.

        Process shutdown bounded-awaits these via :meth:`drain_force_cleanups`
        (F1 whole-branch gate) so a resistant engagement subprocess is verified
        extinct before the process exits; each is otherwise self-limiting."""
        self._force_cleanups.add(task)
        task.add_done_callback(self._force_cleanups.discard)

    async def drain_force_cleanups(self, timeout: float = 10.0) -> bool:
        """F1 (whole-branch gate): bounded, LOOP-until-stable shutdown drain of
        the driver's force-suspend machinery across BOTH surfaces:

          * ``_force_tasks`` — the still-running ``_run_force_suspend`` OWNERS. A
            post-SIGTERM cleanup normally lives INSIDE its owner (shield-awaited
            there); it only MIGRATES to ``_force_cleanups`` when the owner is
            cancelled (operator returned mid-kill). So an in-flight owner is
            itself an un-drained cleanup and must be awaited here.
          * ``_force_cleanups`` — the CANCEL-EXEMPT shielded extinction-poll +
            SIGKILL escalation handed off by ``force_turn_boundary`` (≤ ~7 s each).

        These cleanups are exempt from ``cancel()`` precisely so a SIGTERM-
        resistant MCP/tool child is verified gone (or SIGKILL-escalated) rather
        than orphaned — but process shutdown must actually WAIT for that
        verification, or teardown races the SIGKILL and the resistant subprocess
        outlives the container.

        The drain LOOPS within a single ``timeout`` budget: each iteration
        RE-SNAPSHOTS both surfaces and awaits whatever is pending. Re-snapshotting
        is what makes it stable — an owner cancelled mid-drain hands a fresh
        cleanup off AFTER an earlier snapshot, and the loop's next iteration
        catches it instead of letting it slip past. It settles when both surfaces
        are empty (``True``) or the budget is exhausted (``False``).

        casa_core's shutdown sequence calls this AFTER channel + HTTP ingress
        teardown so the drain point is INGRESS-QUIESCENT — no inbound can spawn a
        fresh force-suspend or fire an operator-away clear that hands a new
        cleanup off once the loop has settled.

        Returns ``True`` when both surfaces drained within ``timeout``; ``False``
        on budget exhaustion (WARN with the still-pending owner + cleanup counts)
        so shutdown proceeds truthfully rather than blocking forever. NEVER
        raises — a drain failure must not wedge shutdown."""
        deadline = self._monotonic() + timeout
        while True:
            owners = [t for t in self._force_tasks.values() if not t.done()]
            cleanups = [t for t in self._force_cleanups if not t.done()]
            pending = owners + cleanups
            if not pending:
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                logger.warning(
                    "drain_force_cleanups: %d force-suspend owner(s) + %d "
                    "cleanup(s) did not finish within %.1fs — proceeding with "
                    "shutdown", len(owners), len(cleanups), timeout)
                return False
            try:
                await asyncio.wait(pending, timeout=remaining)
            except Exception:  # noqa: BLE001 — shutdown must complete regardless
                logger.warning(
                    "drain_force_cleanups: wait raised", exc_info=True)
                return False
            # Loop: re-snapshot to catch a handoff that landed during this wait.

    async def _run_force_suspend(self, engagement_id: str) -> None:
        """A2b: await the injected verified group-kill and log the truthful
        outcome. A False (unverified) is WARN-logged; the kill helper never
        touches s6 wanted-state, so s6 auto-respawns the run script into the
        FIFO-blocked suspended state either way."""
        try:
            ok = await self._force_turn_boundary(
                engagement_id=engagement_id,
                # Task 4: see the twin call site's comment above.
                workspace_dir=control_dir(engagement_id),
                expected_epoch=self._forced_suspend_epochs.get(engagement_id),
                track_task=(
                    lambda t, eid=engagement_id:
                    self._register_force_cleanup(eid, t)),
            )
        except asyncio.CancelledError:
            # A2b: operator returned mid-kill (``_clear_operator_away`` cancelled
            # this task). Sol A2 wave-2 Finding 3: if SIGTERM was already sent,
            # ``force_turn_boundary`` has already handed its shielded post-signal
            # cleanup to ``_register_force_cleanup`` so the SIGKILL escalation
            # still completes under teardown ownership; a pre-signal cancel left
            # nothing running. Either way the operator's own message provides the
            # turn boundary — nothing to log, propagate the cancellation.
            raise
        except Exception:  # noqa: BLE001 — the backstop must never wedge the driver
            logger.warning(
                "engagement %s: force_turn_boundary raised", engagement_id[:8],
                exc_info=True)
            # Sol A2 wave-3, Finding 1: an unverified (raised) outcome RE-ARMS so
            # the NEXT away-refusal retries — a transient failure must not
            # permanently disable the once-per-episode backstop. F3: pace the
            # retry behind a monotonic cooldown so the loop cannot churn.
            self._away_force_cooldown_until[engagement_id] = (
                self._monotonic() + _AWAY_FORCE_COOLDOWN_S)
            self._away_suspend_fired.discard(engagement_id)
            return
        if ok:
            # Verified suspended → LATCH (leave the guard set) so the episode does
            # not force-end again.
            logger.info(
                "engagement %s: forced turn boundary (operator away, 2nd "
                "refusal) — verified suspended", engagement_id[:8])
        else:
            logger.warning(
                "engagement %s: forced turn boundary NOT verified — agent may "
                "still be looping", engagement_id[:8])
            # Sol A2 wave-3, Finding 1: an unverified (False) outcome RE-ARMS the
            # once-per-episode guard so a subsequent away-refusal fires again
            # (e.g. a transient ``unknown`` probe cleared on the next attempt).
            # F3: pace the re-fire behind a monotonic cooldown to bound churn.
            self._away_force_cooldown_until[engagement_id] = (
                self._monotonic() + _AWAY_FORCE_COOLDOWN_S)
            self._away_suspend_fired.discard(engagement_id)

    async def _clear_operator_away(self, engagement_id: str) -> None:
        """F-EXPIRE EXIT: a durably-enqueued operator message ends the away
        episode. Clear the flag + the away-refusal counter, THEN recompute the
        summary status so the ⏸ coercion window closes crisply — the recompute
        allocates a strictly-newer revision that lands the correct ⚙️/⏳. A no-op
        (no recompute) when the engagement was not away, so an ordinary operator
        message never triggers a spurious summary edit."""
        was_away = self._operator_away.pop(engagement_id, False)
        self._away_refusals.pop(engagement_id, None)
        # F3: a fresh away-episode starts with no cooldown so its first backstop
        # fires immediately.
        self._away_force_cooldown_until.pop(engagement_id, None)
        # A2b: reset the once-per-episode force-end guard so a fresh away-episode
        # can fire again. The epoch mark self-clears when _log_abnormal_exit
        # consumes it, so it is NOT dropped here (the respawn's spawn event may
        # arrive after this clear).
        self._away_suspend_fired.discard(engagement_id)
        # A2b (Sol A2 review): cancel an in-flight force-suspend kill — the
        # operator's own inbound is now the turn boundary, so a still-verifying
        # SIGTERM/SIGKILL ladder against a possibly-already-respawned generation
        # is both moot and racy. force_turn_boundary tolerates cancellation at
        # any await (nothing half-signalled). Whole-branch gate r3: cancel IN
        # PLACE (no pop) — the done callback retires the handle, keeping the
        # owner visible to ``drain_force_cleanups`` until its shielded cleanup
        # has been handed off or finished.
        force_task = self._force_tasks.get(engagement_id)
        if force_task is not None and not force_task.done():
            force_task.cancel()
        if not was_away:
            return
        try:
            await self.recompute_engagement_status(engagement_id)
        except Exception:  # noqa: BLE001 — status recompute is advisory
            logger.debug(
                "recompute after operator-away clear failed", exc_info=True)

    async def reconcile_open_questions(
        self, engagement: EngagementRecord, snapshot: list[dict] | None = None,
    ) -> None:
        """§4 boot reconciliation: settle the ATTACH-TIME snapshot of open
        questions. Each stale keyboard/anchor is edited to the expired copy with
        the keyboard cleared, then removed from the ledger;
        ``next_question_number`` is NEVER rewound. Tested for button, free-text,
        and the commit-then-kill window.

        Review I1: the entries reconciled are the attach-time snapshot, which is
        by definition PRIOR-PROCESS (the in-memory broker starts empty at boot,
        so no snapshot entry can have a live broker record). We therefore settle
        them UNCONDITIONALLY — no blanket "any live ask ⇒ skip all" gate, which
        would let a fresh same-process ask (registered concurrently with this
        task) suppress settling genuinely-stale prior-process keyboards. A fresh
        ask allocates a NEW question number and a NEW ledger entry not present in
        the snapshot, so it is never touched here. Callers that omit ``snapshot``
        (legacy / direct invocation) fall back to a fresh read of the record."""
        rec = engagement
        open_qs = (
            list(snapshot) if snapshot is not None
            else list(getattr(rec, "open_questions", ()) or ())
        )
        if not open_qs:
            return

        # §A3 (Sol r5-3): settle EVERY tracked copy (current + each ``stale_mids``
        # copy) behind the confirmed-edit gate, removing the entry only when the
        # current copy is confirmed AND no stale copy remains. The reconcile copy
        # is chosen by the ``answered`` flag (∪ the live overlay): an answered
        # entry reads ``✅ answered below`` — R1 recovery unchanged, an ordinary
        # prior-process open question reads ``⌛ expired``.
        #
        # DEADLOCK AUDIT (B2, Sol r5-3): take the engagement's maintenance lock
        # around the per-entry work and RE-READ each entry FRESH by number in-lock
        # — the pre-service SNAPSHOT only determines WHICH questions are
        # prior-process; their CURRENT mids/state come from the in-lock re-read, so
        # a settle/re-anchor concurrent with boot serializes here instead of racing
        # a captured snapshot. Lock order maintenance → registry; the settle's wire
        # edits use the RAW edit primitive (never the sequencer lock) — no cycle.
        # This task runs at boot with no sequencer lock held.
        async with self.ask_maintenance_lock(rec.id):
            for q in open_qs:
                n = q.get("n")
                fresh = (self._reread_open_question(rec.id, n)
                         if n is not None else None)
                if fresh is None and n is not None:
                    # B3 (wave 2): a NUMBERED snapshot entry ABSENT on the in-lock
                    # fresh re-read was ALREADY resolved (settled + removed) between
                    # snapshot capture and this readiness-gated pass. SKIP — never
                    # fall back to the stale snapshot, whose ``answered=False`` would
                    # ⌛-overwrite a ✅ settle on a message that is already done.
                    logger.debug(
                        "boot reconcile: Q%s absent on fresh re-read (already "
                        "resolved) — skipping (eng=%s)", n, rec.id[:8])
                    continue
                # A legacy no-number snapshot entry (``n is None``) cannot be
                # re-read; settle it from the snapshot as before.
                entry = fresh if fresh is not None else q
                answered = bool(entry.get("answered", False)) or \
                    self._overlay_answered(rec.id, n)
                await self._settle_ledger_entry(
                    rec, entry, answered_suffix=answered)

        # F1 (Sol diff gate): entries were closed above without touching the
        # pinned summary — refresh it so its open-questions line reflects the
        # post-reconcile set (the open-question accessor reads live, so refresh
        # picks up exactly the entries that closed).
        ctrl = self._summaries.get(rec.id)
        if ctrl is not None:
            try:
                await ctrl.refresh()
            except Exception:  # noqa: BLE001 — summary refresh is advisory
                logger.debug(
                    "summary refresh after open-question reconcile failed",
                    exc_info=True)

    def _select_oldest_anchor_entry(self, engagement_id: str) -> dict | None:
        """The oldest free-text anchor from the RAW ledger (answered or not) —
        the answer-lifecycle may already have flagged it answered (invisible to
        ``oldest_open_anchor``), and visual settlement iterates raw entries."""
        reg = self._registry
        if reg is None:
            return None
        entries_getter = getattr(reg, "open_question_entries", None)
        if entries_getter is not None:
            anchors = [
                q for q in entries_getter(engagement_id)
                if q.get("kind") == "anchor"
            ]
            if anchors:
                return min(anchors, key=lambda q: q.get("n", 0))
            return None
        getter = getattr(reg, "oldest_open_anchor", None)  # legacy registry
        return getter(engagement_id) if getter is not None else None

    def _reread_open_question(
        self, engagement_id: str, n: int | None,
    ) -> dict | None:
        """FRESH registry re-read of one open-question entry by number (§A3, B2).
        Returns the CURRENT entry (its live ``tg_message_id`` + ``stale_mids``) or
        ``None``. Callers use this INSIDE the maintenance lock so a settle never
        acts on a pre-lock snapshot that a concurrent re-anchor has superseded."""
        reg = self._registry
        if reg is None or n is None:
            return None
        getter = getattr(reg, "open_question_entries", None)
        if getter is None:
            return None
        for q in getter(engagement_id):
            if q.get("n") == n:
                return q
        return None

    async def _settle_anchor_entry_locked(
        self, engagement: EngagementRecord, n: int | None,
        *, fallback: dict | None = None,
    ) -> int | None:
        """Settle ONE anchor entry, re-read FRESH by number (§A3, B2). Assumes the
        caller either holds the maintenance lock (the ordinary answer path) OR is
        the relay poster running lock-free under the sequencer lock (the entry is
        already answered there, so a racing re-anchor's revalidate declines). A
        legacy no-number entry (``n is None``) settles the ``fallback`` dict."""
        entry = self._reread_open_question(engagement.id, n) if n is not None else None
        if entry is None:
            if n is not None:
                # B3 (wave 2): a NUMBERED entry ABSENT on the fresh re-read was
                # ALREADY resolved (settled + removed) — SKIP, never re-edit from
                # the captured ``fallback`` snapshot (its ``answered=False`` would
                # ⌛-overwrite a ✅ settle). The ``fallback`` is ONLY for a legacy
                # no-number entry (``n is None``), which cannot be re-read.
                logger.debug(
                    "settle: anchor Q%s absent on fresh re-read (already "
                    "resolved) — skipping (eng=%s)", n, engagement.id[:8])
                return None
            entry = fallback
        if entry is None:
            return None
        return await self._settle_ledger_entry(
            engagement, entry, answered_suffix=True)

    async def _settle_open_anchor(
        self, engagement: EngagementRecord, operator_msg_id: int | None = None,
        *, anchor: dict | None = None,
    ) -> int | None:
        """§4/§A3: settle a free-text anchor — edit ``✅ answered below`` over it,
        remove its ledger entry (per the §A3 entry-removal invariant — only when
        the current settle is confirmed AND no ``stale_mids`` copy remains), and
        return the anchor's tg_message_id so the turn threads to the QUESTION it
        answers. Returns ``None`` when no anchor is open.

        The enqueue-time PROMOTION and the delivery-time settle share this one
        implementation: promotion passes the explicit ``anchor`` (only to pick the
        question NUMBER); the delivery-time path passes none and the oldest raw
        anchor number is selected. Every staged stale copy is settled behind the
        same confirmed-gate.

        DEADLOCK AUDIT (B2, Sol interleaving): this acquires the per-engagement
        ``ask_maintenance_lock`` and RE-READS the entry FRESH by number INSIDE the
        lock — never a pre-lock snapshot — so an answer settlement can no longer
        edit a stale OLD copy and close the entry while a concurrent re-anchor
        persisted a NEW current copy. Lock order is maintenance → registry (the
        SAME order re-anchor uses); the settle's wire edits use the RAW edit
        primitive, never the sequencer lock, so no maintenance→sequencer→
        maintenance cycle can form. MUST NOT be called while holding the sequencer
        lock — the relay poster's ``settle_answered_anchor`` path uses the
        lock-free ``_settle_anchor_entry_locked`` helper for exactly that reason."""
        n = None
        if anchor is not None:
            n = anchor.get("n")
        else:
            sel = self._select_oldest_anchor_entry(engagement.id)
            if sel is None:
                return None
            n = sel.get("n")
            anchor = sel
        async with self.ask_maintenance_lock(engagement.id):
            amid = await self._settle_anchor_entry_locked(
                engagement, n, fallback=anchor)
        # W-R2: recompute the summary status from the remaining open questions
        # (still ⏳ waiting while another question is open; ⚙️ working once none
        # remain and the turn is running). Done OUTSIDE the maintenance lock.
        try:
            await self.recompute_engagement_status(engagement.id)
        except Exception:  # noqa: BLE001 — status recompute is advisory
            logger.debug("recompute after anchor settle failed", exc_info=True)
        return amid

    # -- v0.83.0 (§A3(b)) turn-end re-anchor: staged flow + latch + retry owner -
    def set_reanchor_due(self, engagement_id: str) -> None:
        """SET the re-anchor-due latch (idempotent). The standing OBLIGATION to
        keep the oldest unanswered anchor LAST, consumed at every turn boundary
        (§A3(b), Sol r10-1/r11-1). Set by the reservation-rollback consumer and
        by any boundary pass that fails/aborts."""
        self._reanchor_due.add(engagement_id)

    async def _consume_reanchor(self, engagement: EngagementRecord) -> None:
        """Run the re-anchor pass at a turn boundary and reconcile the latch +
        retry owner (§A3(b), Sol r13-1/r17-1). A True pass (obligation met —
        nothing owed, already-last, a successful staged re-anchor, or a consumed
        cosmetic floor) CLEARS the latch and RETIRES the retry owner; a False pass
        (a STAGE failure that left NOTHING on the wire) leaves them armed. The
        pass is idempotent and fully revalidated, so a concurrent boundary
        consumer racing it is harmless — one of them finds the obligation already
        discharged.

        v0.84.0 (§D6 r17-1): the obligation is ARMED BEFORE the pass's first
        cancellable await — the pre-r17 latch-AFTER-return arming bypassed the
        latch when a cancellation landed inside the pass's drained staging write
        (``stage_stale_mid``), and the ``/silent`` rollback path has no later
        boundary to recover the obligation at. On a True pass we retire what we
        just armed; a cancellation propagating out of the pass therefore LEAVES
        the obligation armed for a later boundary / the retry owner."""
        eng_id = engagement.id
        self._reanchor_due.add(eng_id)
        self._arm_reanchor_retry(engagement)
        ok = await self._reanchor_pass(engagement)
        if ok:
            self._reanchor_due.discard(eng_id)
            # §D6 r29-2 PUMP: retire the owner ONLY when NO historical items
            # remain. A memory commit at the LAST natural boundary leaves a
            # durable ``reanchored`` entry the sweep must still marker-clean with
            # no further output — keep the (already-armed) owner running so it
            # re-runs one item per pass with backoff.
            if not self._historical_items(eng_id):
                self._retire_reanchor_retry(eng_id)

    async def _reanchor_pass(self, engagement: EngagementRecord) -> bool:
        """§A3(b) staged turn-end re-anchor — ANCHORS ONLY. Keep the oldest
        UNANSWERED anchor the LAST item in the topic. Returns ``True`` when the
        latch MAY clear (nothing owed / obligation met), ``False`` when a retry is
        owed. Runs under the per-engagement ask-maintenance lock (shared with
        ``_settle_open_anchor`` and the ingress reservation; NEVER acquired while
        holding the sequencer lock)."""
        eng_id = engagement.id
        async with self.ask_maintenance_lock(eng_id):
            return await self._reanchor_pass_locked(engagement)

    async def _reanchor_pass_locked(self, engagement: EngagementRecord) -> bool:
        # v0.84.0 (§D6 r17-1/r27-1): the UNIFIED HISTORICAL SCHEDULER's one
        # selected step AND the current-question stage+post+persist+marker-edit
        # unit run inside ONE strongly-retained, repeatedly drained CHILD TASK
        # created here under the ask-maintenance lock (held by our caller
        # ``_reanchor_pass`` for the child's whole lifetime). Both call the strict
        # registry helpers whose shielded write survives only ONE cancellation
        # (``engagement_registry``); run naked under the lock a second cancellation
        # could escape with the lock released while persistence continues. So on
        # our own ``CancelledError`` we DRAIN the child to completion FIRST (a
        # re-await loop that absorbs REPEATED cancellation via ``shield`` — the
        # same task-ownership shape as ``engagement_registry`` transactions, but
        # ``shield`` not ``gather`` because our child is a live coroutine a
        # gather-cancel WOULD cancel), releasing the lock only after, then
        # re-raise. The drain is safe because the body is FINITE (one historical
        # step, then a unit whose every wire op is ``wait_for``-bounded and whose
        # persist is N local attempts). "No extra tasks" (Sol r27-1) means no
        # SEPARATE detached cleanup owner — the historical step is NOT one.
        child = asyncio.ensure_future(self._reanchor_pass_body(engagement))
        try:
            return await asyncio.shield(child)
        except asyncio.CancelledError:
            while not child.done():
                try:
                    await asyncio.shield(child)
                except asyncio.CancelledError:
                    continue
            raise

    async def _reanchor_pass_body(self, engagement: EngagementRecord) -> bool:
        """The drained pass BODY (§D6): first the UNIFIED HISTORICAL SCHEDULER's
        ONE rotation-selected step (the stale sweep, BEFORE the current-question
        work and before the already-last/nothing-owed exits), then the current
        question's re-anchor unit. Returns the latch bool of the current-question
        work — ``True`` when the obligation is met/consumed, ``False`` ONLY on a
        current-unit STAGE failure (nothing on the wire)."""
        eng_id = engagement.id
        # AT MOST ONE historical step per pass, on the one rotation-selected item,
        # BEFORE current-question work — the selected item is thereby excluded
        # from any other same-pass action (Sol r28-1).
        await self._run_historical_step(engagement)

        # Select the oldest UNANSWERED, unreserved anchor (effective view: not
        # answered ∪ overlay, not reserved). No such anchor ⇒ nothing owed.
        anchor = self._oldest_unanswered_anchor(eng_id, exclude_reserved=True)
        if anchor is None:
            return True
        n = anchor.get("n")
        # §D6 r18-2: while a CONFIRMED pair is unpersisted for this question, NO
        # new send is permitted — the memory-pair scheduler step (above) owns
        # re-driving its LOCAL transaction. Consult the record FIRST, before any
        # send path; the obligation for this question is met by the scheduler.
        if n is not None and self._confirmed_pair_mid(eng_id, n) is not None:
            return True
        old_mid = anchor.get("tg_message_id")
        seq = self._sequencers.get(eng_id)
        if seq is None:
            # No sequencer to measure high-water / post through — cannot
            # re-anchor. Treat as nothing we can do (degraded); the boot
            # reconciler is the eventual backstop.
            return True
        # wb5-1 (whole-branch gate wave 5): REFUSE the re-anchor SEND once EITHER
        # terminal condition holds — the engagement RECORD flipped terminal
        # (``_record_is_terminal``, reused from wave 2) or the sequencer LATCHED
        # (``seq.is_terminal()``). Terminal ``settle_all_open_questions`` owns the
        # retained stale copy (an unconfirmed settle edit keeps the ledger entry);
        # a relay ``result``/rollback boundary consumer that acquired the
        # ask-maintenance lock AFTER the sole settlement pass must NOT repost the
        # full question and persist it live on a CLOSED engagement — ``cancel()``
        # never re-runs settlement to clean it up. Checked at pass entry HERE AND
        # again inside the locked ``_revalidate`` immediately before the send, so a
        # pass crossing terminalization mid-flight still declines. The obligation
        # is CONSUMED (settlement discharged it), so return True to clear the
        # latch, matching the ``already-last`` / ``nothing-owed`` exits above.
        if self._record_is_terminal(eng_id) or seq.is_terminal():
            return True
        hw = seq.high_water
        # Already LAST (nothing posted below it this turn) ⇒ nothing owed. A
        # ``None`` high-water means the sequencer recorded no post at/after the
        # anchor, so it is trivially last too.
        if old_mid is not None and (hw is None or old_mid >= hw):
            return True

        body = anchor.get("text") or (f"Q{n}:" if n is not None else "")
        return await self._reanchor_unit(engagement, n, old_mid, body, seq)

    async def _reanchor_unit(
        self, engagement: EngagementRecord, n: int | None,
        old_mid: int | None, body: str, seq: Any,
    ) -> bool:
        """The drained re-anchor CRITICAL SECTION (§D6 r17-1). Runs as a
        strongly-retained child task under the caller's held ask-maintenance
        lock: stage(plain) → ONE ``wait_for``-bounded plain send (ZERO wire
        retries) → in-unit LOCAL persist (N bounded attempts) →
        ``wait_for``-bounded marker edit of the OLD copy (finite attempts).

        Returns ``True`` whenever the obligation is DISCHARGED or CONSUMED for
        this pass (a successful re-anchor, an answer that won the revalidation
        race, an ambiguous send, or a persist-exhaustion floor); ``False`` ONLY
        when a STAGE failure left NOTHING on the wire — the only outcome safe to
        wire-retry, because no send happened and no duplicate can exist."""
        eng_id = engagement.id
        reg = self._registry

        # Step 1 — STAGE (strict) plain. A raise aborts with NOTHING on the wire
        # (return False — safe to retry). §D6/D2: ALWAYS stages "plain"; the
        # atomic flip to "reanchored" is ``update_question_mid``'s transaction.
        if old_mid is not None and n is not None:
            stage = getattr(reg, "stage_stale_mid", None) if reg is not None else None
            if stage is not None:
                try:
                    await stage(eng_id, n, old_mid, kind="plain")
                except Exception:  # noqa: BLE001 — strict stage failed
                    logger.warning(
                        "engagement %s: re-anchor stage_stale_mid failed (n=%s) "
                        "— aborting, nothing on wire", eng_id[:8], n,
                        exc_info=True)
                    return False

        # Step 2 — ONE ``wait_for``-bounded plain send (ZERO wire retries). The
        # ``revalidate`` hook re-checks, under the sequencer lock immediately
        # before the send, that the anchor is STILL the oldest unanswered+
        # unreserved one (Sol r8-2): an answer that landed during the awaited
        # step-1 stage DECLINES the send.
        declined = False

        def _revalidate() -> bool:
            nonlocal declined
            # wb5-1: terminalization can land between the pass-entry guard and
            # this locked pre-send check (a settlement pass running concurrently
            # with our boundary consumer). Decline the send if EITHER terminal
            # condition now holds — treated exactly like an answer winning the
            # revalidation race (nothing owed), so no copy lands on a closing/
            # closed engagement.
            if self._record_is_terminal(eng_id) or seq.is_terminal():
                declined = True
                return False
            cur = self._oldest_unanswered_anchor(eng_id, exclude_reserved=True)
            still = cur is not None and cur.get("n") == n
            if not still:
                declined = True
            return still

        try:
            new_mid = await seq.post_discrete(
                body, revalidate=_revalidate,
                wire_timeout=_REANCHOR_SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # The production send wrapper catches everything and returns None
            # (Sol r17-3); a raising send here (or under the timeout task's
            # cancellation) is likewise INDISTINGUISHABLE from an accepted copy,
            # so treat it as an ambiguous send — ZERO wire retries.
            logger.warning(
                "engagement %s: re-anchor send raised (n=%s) — treating as "
                "ambiguous, no wire retry, obligation consumed", eng_id[:8], n,
                exc_info=True)
            await self._best_effort_unstage(eng_id, n, old_mid)
            return True
        if new_mid is None:
            # Un-stage best-effort (a failed un-stage leaves an overlap-tolerant
            # stale_mid — reconciliation tolerates the overlap).
            await self._best_effort_unstage(eng_id, n, old_mid)
            if declined:
                # The answer won the revalidation race — nothing owed.
                return True
            # AMBIGUOUS send (Sol r17-3): the send wrapper returns None for BOTH
            # a wire failure AND "Telegram accepted but returned no mid" / a
            # wait_for timeout — indistinguishable, so a wire retry could stack
            # an untracked duplicate. TERMINAL cosmetic outcome for THIS pass: no
            # wire retry, obligation CONSUMED, old copy stays current; the next
            # re-anchor obligation arises naturally from the next output event
            # (round-3 semantics). Residual: at most ONE potential untracked copy
            # per ambiguous send, logged.
            #
            # D4 review (Minor): NO confirmed-pair record is created here — a
            # confirmed pair requires a KNOWN mid, which an ambiguous send never
            # yields (Sol r18-2). So nothing blocks a later boundary from doing a
            # SECOND send for this question; that cross-pass second send is WITHIN
            # the accepted at-least-once floor (each ambiguous send risks at most
            # one untracked copy — the same direction D5 pinned for buffered
            # prose), NOT a regression the confirmed-pair machinery must prevent.
            logger.warning(
                "engagement %s: re-anchor send ambiguous (n=%s) — exactly ONE "
                "attempt, no wire retry, obligation consumed for this pass",
                eng_id[:8], n)
            return True

        # Step 3 — IN-UNIT LOCAL persist of tg_message_id = new_mid: N bounded
        # attempts, short backoff, registry-file writes ONLY (never a wire op).
        # This same transaction atomically flips the staged stale entry
        # "plain" → "reanchored" (D2, inside ``update_question_mid``).
        if not await self._reanchor_persist_in_unit(engagement, n, new_mid):
            # v0.84.0 (§D6 r18-2/r28-2): persist exhaustion after a CONFIRMED send
            # (``new_mid`` is known). RECORD the confirmed pair so the UNIFIED
            # HISTORICAL SCHEDULER owns re-driving the LOCAL transaction (never a
            # new send — rule (b)) across later boundaries, and answer/terminal
            # settlement can marker-edit the orphan. NO body edit (the '↪ see
            # above' hack is DELETED, r11-1); the obligation is CONSUMED for this
            # pass. The old copy stays durably current + tracked (persist never
            # committed, so tg_message_id and the "plain" stale entry are
            # unchanged); ``new_mid`` is an unpersisted orphan the record now owns
            # (mid-agnostic routing means answering it still works meanwhile).
            self._record_confirmed_pair(eng_id, n, new_mid)
            logger.warning(
                "engagement %s: re-anchor persist exhausted after %d attempts "
                "(n=%s new_mid=%s) — confirmed-pair recorded, scheduler owns it",
                eng_id[:8], _REANCHOR_PERSIST_ATTEMPTS, n, new_mid)
            return True

        # Step 4 — ``wait_for``-bounded, finite-attempt marker edit of the OLD
        # copy to the MOVED-open marker (§D6, Sol r1-7): REPLACES the full
        # duplicated body with marker-ONLY text so the old copy no longer reads
        # as "asked twice". ONLY on a confirmed edit un-stage old_mid; an
        # unconfirmed edit PRESERVES the durable "reanchored" stale entry (the
        # D4 stale-sweep re-drives it). The OBLIGATION is already met (the
        # question is now LAST at new_mid), so this returns True regardless.
        #
        # wb2-2 (whole-branch gate wave 2): the marker edit + unstage is made
        # LINEARIZABLE against an answer reservation / terminal flip that can land
        # during either awaited step — see ``_release_old_copy_lifecycle_aware``.
        await self._release_old_copy_lifecycle_aware(engagement, n, old_mid)
        return True

    async def _reanchor_persist_in_unit(
        self, engagement: EngagementRecord, n: int | None, new_mid: int,
    ) -> bool:
        """In-unit LOCAL persist of the re-anchored ``tg_message_id`` (§D6
        r17-2): up to ``_REANCHOR_PERSIST_ATTEMPTS`` strict
        ``update_question_mid`` attempts with a short backoff between them —
        registry-file writes only, no wire op. Returns ``True`` on a committed
        transaction (or when there is no persist primitive — legacy registry),
        ``False`` on exhaustion."""
        reg = self._registry
        update = (getattr(reg, "update_question_mid", None)
                  if reg is not None else None)
        if update is None or n is None:
            return True
        for attempt in range(_REANCHOR_PERSIST_ATTEMPTS):
            try:
                await update(engagement.id, n, new_mid)
                return True
            except Exception:  # noqa: BLE001 — strict persist failed
                logger.warning(
                    "engagement %s: re-anchor persist attempt %d/%d failed "
                    "(n=%s new_mid=%s)", engagement.id[:8], attempt + 1,
                    _REANCHOR_PERSIST_ATTEMPTS, n, new_mid, exc_info=True)
                if attempt + 1 < _REANCHOR_PERSIST_ATTEMPTS:
                    await self._sleep(_REANCHOR_PERSIST_BACKOFF)
        return False

    async def _reanchor_marker_edit(
        self, engagement: EngagementRecord, old_mid: int, n: int | None,
        *, terminal: bool = False,
    ) -> bool:
        """``wait_for``-bounded, finite-attempt marker edit of the OLD copy
        through the sequencer's ``edit_discrete`` (§D6 r17-2/r17-4): each attempt
        is bounded by ``_REANCHOR_EDIT_TIMEOUT`` and idempotent via the F1 edit
        cache (a repeated identical edit no-ops). Returns ``True`` on a confirmed
        edit. Renders D1's marker-ONLY text, never the duplicated body.

        wb1-2: ``terminal=True`` renders the ``resolved below`` marker instead of
        the ``answer the current copy below`` OPEN marker — the durable step uses
        it once answer/terminal settlement has begun so the stale copy never
        looks live in the pre-settlement window."""
        seq = self._sequencers.get(engagement.id)
        if seq is None:
            return False
        from channels.output_sequencer import MARKUP_EMPTY
        marker = _reanchor_moved_terminal(n) if terminal else _reanchor_moved_open(n)
        return await confirmed_settle_edit(
            lambda: seq.edit_discrete(
                old_mid, text=marker, markup=MARKUP_EMPTY,
                wire_timeout=_REANCHOR_EDIT_TIMEOUT),
            sleep=self._sleep,
        )

    async def _best_effort_unstage(
        self, engagement_id: str, n: int | None, mid: int | None,
    ) -> None:
        """Best-effort strict un-stage of a stale mid (§A3(b) overlap-tolerance):
        a failure leaves a stale_mid pointing at a message that may ALSO still be
        the live ``tg_message_id`` — reconciliation tolerates the overlap."""
        reg = self._registry
        if reg is None or n is None or mid is None:
            return
        unstage = getattr(reg, "unstage_stale_mid", None)
        if unstage is None:
            return
        try:
            await unstage(engagement_id, n, mid)
        except Exception:  # noqa: BLE001 — overlap-tolerant
            logger.debug(
                "engagement %s: re-anchor un-stage failed (n=%s mid=%s)",
                engagement_id[:8], n, mid, exc_info=True)

    # -- v0.84.0 (round-4 §D6) confirmed-pair record + unified historical --------
    #    scheduler (Sol r18-2/r19-3/r23-1/r24-2/r27-x/r28-x/r29-x) --------------
    def _record_confirmed_pair(
        self, engagement_id: str, n: int | None, new_mid: int,
    ) -> None:
        """Record a CONFIRMED but UNPERSISTED re-anchor pair (§D6 r18-2): a copy
        on the wire at ``new_mid`` whose LOCAL persist never committed. Owned by
        the driver, retained INDEPENDENTLY of any retry task."""
        if n is None:
            return
        self._confirmed_pairs.setdefault(engagement_id, {})[n] = new_mid

    def _confirmed_pair_mid(self, engagement_id: str, n: int | None) -> int | None:
        """The unpersisted orphan mid recorded for question ``n``, or ``None``."""
        if n is None:
            return None
        return (self._confirmed_pairs.get(engagement_id) or {}).get(n)

    def _retire_confirmed_pair(self, engagement_id: str, n: int | None) -> None:
        """Retire a confirmed pair (§D6 r28-2): on the transaction COMMIT (the
        durable ``reanchored`` entry the commit created owns the rest) or on a
        confirmed orphan settlement edit."""
        pairs = self._confirmed_pairs.get(engagement_id)
        if pairs is None:
            return
        pairs.pop(n, None)
        if not pairs:
            self._confirmed_pairs.pop(engagement_id, None)

    def _settlement_begun(self, engagement_id: str, n: int | None) -> bool:
        """§D6 r29-1: ``True`` once answer/terminal SETTLEMENT has begun for
        question ``n`` — its ledger entry is ABSENT (closed) OR effectively
        ANSWERED (persisted flag ∪ overlay ∪ reservation). A memory pair whose
        question has begun settling must NEVER run a late ``update_question_mid``
        (a closed ledger returns ``False`` and strands the orphan un-edited; a
        current mid installed AFTER the sole settlement pass is left unowned) —
        the memory step marker-edits the orphan instead."""
        if n is None:
            return True
        entry = self._reread_open_question(engagement_id, n)
        if entry is None:
            return True
        if entry.get("answered", False) or self._overlay_answered(engagement_id, n):
            return True
        return self._reserved_question_number(engagement_id) == n

    def _record_is_terminal(self, engagement_id: str) -> bool:
        """wb1-2 (whole-branch gate wave 1): ``True`` once the engagement RECORD
        has flipped terminal (``completed``/``cancelled``/``error``). The
        authoritative terminal flip (``try_transition_terminal``) commits BEFORE
        finalize's broker-hook + summary awaits and BEFORE
        ``settle_all_open_questions`` cancels the retry owner — a GAP in which a
        scheduler pump pass could edit a stale copy to the OPEN marker and unstage
        it, stranding it live-looking (settlement then only sees the current
        copy). The scheduler consults this to defer to the imminent settlement:
        render the TERMINAL marker + leave staged, and stop pumping/arming."""
        reg = self._registry
        getter = getattr(reg, "get", None) if reg is not None else None
        if getter is None:
            return False
        try:
            rec = getter(engagement_id)
        except Exception:  # noqa: BLE001 — a read failure must never wedge the pass
            return False
        return rec is not None and getattr(rec, "status", None) in (
            "completed", "cancelled", "error")

    def _historical_items(
        self, engagement_id: str,
    ) -> list[tuple[str, int, int]]:
        """The UNIFIED HISTORICAL SCHEDULER's item set (§D6): unpersisted memory
        pairs ∪ open durable ``reanchored`` stale entries, as deterministically
        ordered ``(kind, n, mid)`` tuples (memory items carry ``mid == -1``).
        Answered/closed questions' ``reanchored`` entries are EXCLUDED — their
        terminal-form settle is owned by ``_settle_ledger_entry``, not the
        open-form sweep. Deterministic order keeps the rotation cursor fair."""
        items: list[tuple[str, int, int]] = []
        for n in (self._confirmed_pairs.get(engagement_id) or {}):
            items.append(("memory", n, -1))
        reg = self._registry
        entries_getter = (getattr(reg, "open_question_entries", None)
                          if reg is not None else None)
        if entries_getter is not None:
            for q in entries_getter(engagement_id):
                n = q.get("n")
                if n is None:
                    continue
                if q.get("answered", False) or self._overlay_answered(
                        engagement_id, n):
                    continue
                for raw in (q.get("stale_mids") or []):
                    s = normalize_stale_mid_entry(raw)
                    if s["kind"] == "reanchored":
                        items.append(("durable", n, s["mid"]))
        # (n, kind, mid): "durable" sorts before "memory" for the same question.
        items.sort(key=lambda it: (it[1], it[0], it[2]))
        return items

    def _reanchor_work_pending(self, engagement_id: str) -> bool:
        """§D6 r29-2 PUMP predicate: the re-anchor retry owner stays armed while
        the latch is set OR the scheduler's item set is non-empty (so a durable
        cleanup lands even when the committing boundary was the last natural one
        — no second owner mechanism)."""
        return (engagement_id in self._reanchor_due
                or bool(self._historical_items(engagement_id)))

    async def _run_historical_step(
        self, engagement: EngagementRecord,
    ) -> tuple[str, int, int] | None:
        """Perform AT MOST ONE rotation-selected historical step (§D6 Sol
        r23-1/r24-2/r28-1). Runs under the caller's held maintenance lock, INSIDE
        the drained pass body (its strict registry helpers survive only ONE
        cancellation). The cursor advances after EVERY step, success or failure —
        never K sequential steps for K items (the current question stays
        unblockable by historical cosmetic failures). Returns the selected item
        or ``None`` when the set is empty."""
        eng_id = engagement.id
        items = self._historical_items(eng_id)
        if not items:
            return None
        cursor = self._historical_cursor.get(eng_id, 0)
        sel = items[cursor % len(items)]
        self._historical_cursor[eng_id] = cursor + 1  # advance after EVERY step
        kind, n, mid = sel
        if kind == "memory":
            await self._historical_memory_step(engagement, n)
        else:
            await self._historical_durable_step(engagement, n, mid)
        return sel

    async def _historical_memory_step(
        self, engagement: EngagementRecord, n: int,
    ) -> None:
        """LIFECYCLE-AWARE memory-pair step (§D6 r29-1). While the question is
        LIVE and UNANSWERED: exactly ONE strict ``update_question_mid`` attempt
        (no in-pass retry loop — the cross-pass rotation spreads retries); on
        commit the record RETIRES and the durable ``reanchored`` entry (created by
        the atomic flip) enters the item set. Once settlement HAS BEGUN: switch
        PERMANENTLY to a bounded terminal marker-edit of the orphan, retiring ONLY
        on a confirmed edit (nothing durable can rediscover this mid)."""
        eng_id = engagement.id
        new_mid = self._confirmed_pair_mid(eng_id, n)
        if new_mid is None:
            return
        # wb1-2: a terminal record (finalize's pre-settle gap) is settling too —
        # a late ``update_question_mid`` would install a current mid AFTER the
        # sole settlement pass with nothing owning it; marker-edit the orphan
        # terminal instead.
        if self._settlement_begun(eng_id, n) or self._record_is_terminal(eng_id):
            if await self._orphan_marker_edit(engagement, new_mid, n):
                self._retire_confirmed_pair(eng_id, n)
            return
        reg = self._registry
        update = (getattr(reg, "update_question_mid", None)
                  if reg is not None else None)
        if update is None:
            return
        try:
            await update(eng_id, n, new_mid)
        except Exception:  # noqa: BLE001 — one attempt, pair retained for rotation
            logger.warning(
                "engagement %s: historical memory-pair persist attempt failed "
                "(n=%s new_mid=%s) — pair retained", eng_id[:8], n, new_mid,
                exc_info=True)
            return
        # Committed: the durable ``reanchored`` entry now owns the marker-edit +
        # unstage cleanup — retire the memory record (Sol r28-2).
        self._retire_confirmed_pair(eng_id, n)

    async def _historical_durable_step(
        self, engagement: EngagementRecord, n: int, mid: int,
    ) -> None:
        """COMPOUND durable-entry step (§D6): one ``wait_for``-bounded OLD-copy
        marker edit, and on edit success the one ``unstage_stale_mid`` write.
        Retire on unstage success; a failed unstage leaves the durable entry
        present, so the next selection re-runs the compound step — the repeated
        identical edit no-ops via the F1 edit cache (idempotent, no new durable
        'edited' flag needed).

        wb1-2: REVALIDATE lifecycle before choosing the marker text AND again
        before unstaging (both reads under the caller's held maintenance lock).
        Once answer/terminal settlement has BEGUN — D4's ``_settlement_begun``
        (answered/reserved/closed) OR the engagement RECORD has flipped terminal
        (``_record_is_terminal`` — the finalize gap before ``settle_all`` cancels
        the pump) — the OPEN "answer the current copy below" marker would strand
        the stale copy looking live because settlement then only sees the current
        copy. Render the TERMINAL marker and LEAVE the entry staged so the
        settlement path (which renders reanchored stale copies terminal) owns the
        final state — NEVER unstage it out from under settlement."""
        # wb2-2: the compound marker-edit + unstage is LINEARIZABLE against a
        # reservation / terminal flip landing during EITHER awaited step (the
        # OPEN-marker edit OR the unstage persist) — the shared helper below.
        await self._release_old_copy_lifecycle_aware(engagement, n, mid)

    async def _release_old_copy_lifecycle_aware(
        self, engagement: EngagementRecord, n: int | None, old_mid: int | None,
    ) -> None:
        """wb2-2 (whole-branch gate wave 2): marker-edit the OLD anchor copy and
        release its staged stale entry, LINEARIZABLE against an answer reservation
        / terminal record flip that can land during EITHER awaited step — the
        ``wait_for``-bounded OPEN-marker edit OR the awaited ``unstage_stale_mid``
        persist. Shared by the current-question re-anchor unit (step 4) and the
        unified historical scheduler's durable step.

        Answer/terminal settlement (``_settle_ledger_entry``) renders a still-
        staged ``reanchored`` stale copy to the TERMINAL ``resolved below`` marker.
        So the invariant kept here is: once settlement has BEGUN for this question,
        the old copy must be LEFT (or restored) staged so settlement terminalizes
        it — never unstaged out from under settlement, stranding it permanently on
        the OPEN ``answer the current copy below`` marker (the bug wb1-2's single
        pre-unstage check still allowed, because the reservation can install WHILE
        the unstage write is awaiting persistence).

        Three lifecycle reads, each under the caller's held maintenance lock:
        (1) BEFORE the edit — already begun ⇒ render TERMINAL, leave staged;
        (2) AFTER the OPEN edit — a reservation may have landed while it was in
        flight ⇒ leave staged, skip the unstage;
        (3) AFTER the unstage persist — a reservation can install between check (2)
        and the persisted removal ⇒ RE-STAGE the stale entry ``reanchored`` so
        settlement finds and terminalizes it."""
        eng_id = engagement.id
        if old_mid is None or n is None:
            return
        if self._settlement_begun(eng_id, n) or self._record_is_terminal(eng_id):
            await self._reanchor_marker_edit(engagement, old_mid, n, terminal=True)
            return
        if not await self._reanchor_marker_edit(engagement, old_mid, n):
            return
        if self._settlement_begun(eng_id, n) or self._record_is_terminal(eng_id):
            return
        await self._best_effort_unstage(eng_id, n, old_mid)
        if self._settlement_begun(eng_id, n) or self._record_is_terminal(eng_id):
            await self._restage_reanchored(eng_id, n, old_mid)

    async def _restage_reanchored(
        self, engagement_id: str, n: int | None, mid: int | None,
    ) -> None:
        """wb2-2: RE-STAGE ``mid`` as a ``reanchored`` stale copy after a lifecycle
        race unstaged it just as settlement was beginning — so the settlement path
        (which renders ``reanchored`` stale copies to the terminal marker) owns the
        old copy's final state instead of stranding it on the OPEN marker.
        Best-effort: a failed re-stage leaves the old copy on the OPEN marker (the
        pre-fix outcome for this one message — strictly no worse), logged."""
        reg = self._registry
        if reg is None or n is None or mid is None:
            return
        stage = getattr(reg, "stage_stale_mid", None)
        if stage is None:
            return
        try:
            await stage(engagement_id, n, mid, kind="reanchored")
        except Exception:  # noqa: BLE001 — best-effort restoration
            logger.warning(
                "engagement %s: re-stage after unstage race failed (n=%s mid=%s)",
                engagement_id[:8], n, mid, exc_info=True)

    async def _orphan_marker_edit(
        self, engagement: EngagementRecord, orphan_mid: int, n: int | None,
    ) -> bool:
        """§D6 r20-1: bounded, finite-attempt TERMINAL marker-edit of a confirmed
        pair's orphan mid (the settlement form — the question is by now
        answered/terminal), routed through the sequencer's ``edit_discrete`` (F1
        no-op idempotent, so the scheduler and settlement path can each re-run the
        identical edit). Returns ``True`` on a confirmed edit."""
        seq = self._sequencers.get(engagement.id)
        if seq is None:
            return False
        from channels.output_sequencer import MARKUP_EMPTY
        marker = _reanchor_moved_terminal(n)
        return await confirmed_settle_edit(
            lambda: seq.edit_discrete(
                orphan_mid, text=marker, markup=MARKUP_EMPTY,
                wire_timeout=_REANCHOR_EDIT_TIMEOUT),
            sleep=self._sleep,
        )

    def _arm_reanchor_retry(self, engagement: EngagementRecord) -> None:
        """Arm the ONE retry-owner task for this engagement (§A3(b), Sol r13-1).
        A double-arm while a task already runs is a NO-OP. The task self-retires
        (removing its dict entry) via a done-callback.

        wb1-2: NO-OP once the record is terminal — ``settle_all_open_questions``
        cancels the pump before it iterates stale mids, and a re-arm here would
        restart the very scheduler the terminal quiesce just stopped."""
        eng_id = engagement.id
        if self._record_is_terminal(eng_id):
            return
        existing = self._reanchor_retry_tasks.get(eng_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._reanchor_retry_loop(engagement),
            name=f"reanchor_retry:{eng_id[:8]}")
        self._reanchor_retry_tasks[eng_id] = task
        task.add_done_callback(
            lambda t, eid=eng_id: self._on_reanchor_retry_done(eid, t))

    def _on_reanchor_retry_done(
        self, engagement_id: str, task: asyncio.Task,
    ) -> None:
        # Remove the dict entry only if it still points at THIS task (a fresh
        # arm may have replaced it). A completed task leaves no reference.
        if self._reanchor_retry_tasks.get(engagement_id) is task:
            self._reanchor_retry_tasks.pop(engagement_id, None)

    def _retire_reanchor_retry(self, engagement_id: str) -> None:
        """Cancel + drop the retry owner (a boundary consumer / promotion /
        terminal settle discharged the obligation)."""
        task = self._reanchor_retry_tasks.pop(engagement_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _reanchor_retry_loop(self, engagement: EngagementRecord) -> None:
        """Bounded-backoff self-rescheduling retry owner (§A3(b), Sol r13-1):
        loop until a True pass clears the latch (success / the question got
        answered elsewhere). ``CancelledError`` terminates WITHOUT rescheduling
        (engagement teardown / a boundary consumer beat it). The pass is
        idempotent + revalidated, so overlap with a boundary consumer is
        harmless."""
        eng_id = engagement.id
        idx = 0
        try:
            # §D6 r29-2 PUMP: loop while the latch is set OR the scheduler's item
            # set is non-empty — one item per pass with backoff — so a durable
            # cleanup lands even after the committing boundary was the last one.
            while self._reanchor_work_pending(eng_id):
                delay = _REANCHOR_BACKOFF[min(idx, len(_REANCHOR_BACKOFF) - 1)]
                idx += 1
                await self._reanchor_retry_sleep(delay)
                if not self._reanchor_work_pending(eng_id):
                    break
                ok = await self._reanchor_pass(engagement)
                if ok:
                    self._reanchor_due.discard(eng_id)
                    # Break ONLY when no historical items remain; otherwise keep
                    # pumping the sweep (a True pass with a durable entry still
                    # present must not retire the owner).
                    if not self._historical_items(eng_id):
                        break
        except asyncio.CancelledError:
            raise

    async def settle_all_open_questions(
        self, engagement: EngagementRecord, outcome: str,
    ) -> None:
        """§A3(b) consumer (c) — terminal engagement finalize SETTLES every
        remaining open-question entry (raw view) instead of re-anchoring, closing
        the latent gap where ``/cancel``/``/complete`` left a live free-text
        anchor visually open forever. Answered entries get the ✅ copy; unanswered
        ones an outcome-appropriate copy (🛑 ended for cancelled/error, ⌛ expired
        otherwise). The entry-removal invariant is honored. Clears the latch +
        cancels the retry owner (the terminal settle IS the obligation's
        discharge). Best-effort per entry — never raises into the finalize funnel.
        Called getattr-tolerantly by ``tools._finalize_engagement``."""
        eng_id = engagement.id
        # wb3-1/wb3-2/wb3-3: LATCH the sequencer TERMINAL before settling. This
        # runs BEFORE this method enumerates the open-question ledger AND before
        # the later ``finalize_completion_post`` flush, so: (a) any still-armed
        # anchor poster is aborted here and never posts + ledgers a question the
        # settle pass would then miss (BLOCKER wb3-1); (b) an in-flight poster's
        # ledger entry has already landed (the writer lock serializes it ahead of
        # us) and IS included in the settle pass below; (c) the intent registry
        # is pruned, firing every ``on_retire`` so the wb2-4 validation-gate pins
        # release even if the relay never processes ``result`` (wb3-3); and (d)
        # late relay narration is discarded (wb3-2). Idempotent + serialized
        # against posters via the writer lock.
        seq = self._sequencers.get(eng_id)
        if seq is not None:
            await seq.terminalize()
        # Discharge the re-anchor obligation: nothing to keep last once terminal.
        self._reanchor_due.discard(eng_id)
        self._retire_reanchor_retry(eng_id)
        reg = self._registry
        entries_getter = (getattr(reg, "open_question_entries", None)
                          if reg is not None else None)
        if entries_getter is None:
            return
        cancelled = outcome in ("cancelled", "error", "failed")
        async with self.ask_maintenance_lock(eng_id):
            for q in list(entries_getter(eng_id)):
                answered = bool(q.get("answered", False)) or self._overlay_answered(
                    eng_id, q.get("n"))
                try:
                    if answered:
                        await self._settle_ledger_entry(
                            engagement, q, answered_suffix=True)
                    else:
                        await self._settle_ledger_entry(
                            engagement, q, answered_suffix=False,
                            override_suffix=(
                                _OPEN_Q_CANCELLED_SUFFIX if cancelled
                                else _OPEN_Q_EXPIRED_SUFFIX),
                        )
                except Exception:  # noqa: BLE001 — never abort finalize
                    logger.warning(
                        "engagement %s: terminal open-question settle failed "
                        "(n=%s)", eng_id[:8], q.get("n"), exc_info=True)

    def sequencer_is_terminal(self, engagement_id: str) -> bool:
        """wb3-1: ``True`` once the engagement's sequencer has TERMINALIZED (the
        persistent latch). The anchor poster consults this under the writer lock
        (in the same locked section as its cancel-latch re-read) so it never
        posts + ledgers a question on a closing/closed engagement. ``False`` when
        there is no live sequencer."""
        seq = self._sequencers.get(engagement_id)
        return seq is not None and seq.is_terminal()

    def set_engagement_reply_anchor(
        self, engagement_id: str, message_id: int,
    ) -> None:
        """§4 causal handoff: a button answer continues the SAME CLI turn — the
        telegram commit helper sets this one-shot anchor so the turn's FIRST
        sequencer output threads its reply to the ask message. SYNCHRONOUS (no
        await) — the caller relies on the pre-resumption guarantee."""
        seq = self._sequencers.get(engagement_id)
        if seq is not None:
            seq.set_turn_reply_to(message_id)

    async def edit_ask_keyboard(
        self, engagement_id: str, message_id: int, markup: Any,
        *, revalidate: Any = None,
    ) -> bool:
        """A5 · F-MULTI: markup-only redraw of a live multi-select keyboard
        through the sequencer's ``edit_discrete`` (§A9) so it serializes on the
        same single-writer lock as the settle edit. ``revalidate`` (the toggle
        terminal-race guard) runs under the lock immediately before the wire
        edit. Returns False (no edit) when the engagement has no live sequencer."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return False
        return await seq.edit_discrete(
            message_id, markup=markup, revalidate=revalidate)

    async def settle_ask_keyboard(
        self, engagement_id: str, message_id: int, text: str,
    ) -> bool:
        """A5 · F-MULTI: the multi ask's terminal settle edit — set the settle
        text AND clear the keyboard, routed through the SAME ``edit_discrete``
        primitive as the toggle redraw so a stale redraw can never land after
        (and resurrect) a settled keyboard. Returns False when the engagement
        has no live sequencer (the finish hook then leaves the ledger intact for
        boot reconciliation)."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return False
        from channels.output_sequencer import MARKUP_EMPTY
        return await seq.edit_discrete(
            message_id, text=text, markup=MARKUP_EMPTY)

    async def _supersede_pending_asks(self, engagement_id: str) -> None:
        """§4 live-ask supersession: a fresh operator message resolves any
        PENDING engagement_ask keyboard as ``superseded_by_text`` (broker cancel
        path) so it settles immediately instead of dead-waiting its timeout. The
        keyboard's finish hook renders the superseded copy + clears the buttons.
        Free-text anchors register no broker request, so they are untouched."""
        from verdict_broker import BROKER
        BROKER.cancel_scope(
            namespace="engagement_ask", scope=engagement_id,
            reason="superseded_by_text",
        )

    async def _run_topic_relay(self, engagement: EngagementRecord) -> None:
        """Drive the always-on live topic-stream relay for one engagement.

        The relay reads the engagement's NDJSON s6-log to the live end then
        returns; each claude_code turn is a fresh CLI spawn that appends a
        burst then exits, so we re-run on a short poll — the crash-safe cursor
        (control-dir ``.stream_cursor.json``, Task 4) resumes exactly where
        the last run left off, and REPLAY-mode side-effect suppression keeps
        re-runs idempotent.
        """
        from drivers.topic_stream import TopicStreamRelay

        # §D5 ("Successful-anchor identity comes from the DRIVER"): the SAME
        # truth source the A3 reply/stacking gates use — persisted ``answered``
        # ∪ overlay ∪ reservation excluded — reused verbatim as the relay's
        # injected seam.
        # wb2-1 (whole-branch gate wave 2): report the anchor's SOURCE HASH (the
        # projection hash of the ask that produced it — recorded on the ledger
        # entry by the ask handler) so the relay can bind a candidate POSITIVELY to
        # the anchor its OWN ask produced, never a prior / co-existing open anchor.
        def _open_anchor_state() -> "tuple[int, int, str | None] | None":
            entry = self.effective_open_anchor(engagement.id)
            if not entry:
                return None
            return (entry["n"], entry["tg_message_id"], entry.get("source_hash"))

        relay = TopicStreamRelay(
            engagement_id=engagement.id,
            topic_id=engagement.topic_id,
            log_dir=engagement_log_dir(engagement.id),
            # Task 4: crash-safe cursor moved to the control dir.
            cursor_path=stream_cursor_path(engagement.id),
            send_message=self._relay_send_message,
            edit_message=self._relay_edit_message,
            delete_message=self._relay_delete_message,
            on_turn_event=(
                lambda kind, payload: self._on_stream_event(
                    engagement, kind, payload)
            ),
            reply_texts=lambda: self._reply_texts.get(engagement.id, set()),
            # v0.79.0 (§2): the SHARED per-engagement sequencer, so the relay
            # and the discrete ingresses agree on ordering + high-water.
            sequencer=self._ensure_sequencer(engagement),
            open_anchor_state=_open_anchor_state,
            # wb2-3: terminal-lifecycle seam — a terminal closure DISCARDS held
            # narration (never flushes a sign-off below the terminal completion).
            engagement_terminal=lambda: self._record_is_terminal(engagement.id),
        )
        while True:
            try:
                await relay.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — relay is best-effort
                logger.warning(
                    "topic relay for engagement %s errored (will retry): %s",
                    engagement.id[:8], exc,
                )
            await asyncio.sleep(0.5)

    # -- relay-injected Telegram primitives -------------------------------

    async def _relay_send_message(
        self, topic_id: int, text: str, reply_to: int | None = None,
    ) -> int | None:
        # v0.79.0 (§3): the sequencer threads the turn's first post to the
        # inbound operator message (reply-quoting). Only pass the kwarg when a
        # target exists so the common no-thread path stays 2-arg.
        if reply_to is not None:
            return await self._send_to_topic(
                topic_id, text, reply_to_message_id=reply_to)
        return await self._send_to_topic(topic_id, text)

    async def _relay_edit_message(
        self, topic_id: int, message_id: int, text: str,
    ) -> bool:
        if self._edit_topic_message is None:
            return False
        return await self._edit_topic_message(topic_id, message_id, text)

    async def _relay_delete_message(
        self, topic_id: int, message_id: int,
    ) -> bool:
        if self._delete_topic_message is None:
            return False
        return await self._delete_topic_message(topic_id, message_id)

    async def _relay_send_message_markup(
        self, topic_id: int, text: str, markup, reply_to: int | None = None,
    ) -> int | None:
        # A9: markup-capable discrete send (post_discrete). Returns None when the
        # primitive is un-injected so the sequencer records a failed post.
        if self._send_topic_message_markup is None:
            return None
        return await self._send_topic_message_markup(
            topic_id, text, markup, reply_to=reply_to)

    async def _relay_edit_message_markup(
        self, topic_id: int, message_id: int, text, markup,
    ) -> bool:
        # A9: markup-capable discrete edit (edit_discrete).
        if self._edit_topic_message_markup is None:
            return False
        return await self._edit_topic_message_markup(
            topic_id, message_id, text, markup)

    # -- stream-event fan-out ---------------------------------------------

    def record_reply_text(self, engagement_id: str, text: str) -> None:
        """Record a ``reply()`` text for the relay's per-turn de-dup.

        Called by the /internal/channel/send_to_topic handler (the engagement
        reply path). The set is cleared on each ``turn_start`` (see
        ``_on_stream_event``)."""
        if text:
            self._reply_texts.setdefault(engagement_id, set()).add(text)

    async def _on_stream_event(
        self, engagement: EngagementRecord, kind: str, payload: dict,
    ) -> None:
        """Fan the relay's ordered ``on_turn_event`` kinds into driver state.

        ``spawn`` → arm the inbound spool (redeliver survivors) + epoch/
        abnormal-exit correlation; ``turn_start`` → reset the reply-text de-dup
        set + mark the turn running + consume the delivered envelope (§3
        turn_start evidence) + retry receipts; ``mutating_tool``
        → W2/Sol B9 (Task 7): while the engagement's ``interaction_state``
        is ``awaiting_operator``, post ONE in-topic violation notice (per
        engagement) and flag ``rec.origin["interaction_violated"]`` via
        ``registry.set_interaction_violated`` — ``_finalize_engagement``
        surfaces it in the completion summary. A ``reply``/``ask``/
        ``set_progress`` control tool-use never reaches here —
        ``topic_stream.is_mutating_tooluse`` excludes them. ``result`` →
        clear the epoch pending a result (normal turn boundary)."""
        eng_id = engagement.id
        if kind == "spawn":
            epoch = payload.get("epoch")
            prev = self._epoch_pending.get(eng_id)
            # #341 (v0.138.0): TopicStream delivers frames at-least-once. A
            # replayed spawn carrying the SAME epoch is THIS generation seen
            # again, not a new one — processing it would log a phantom
            # abnormal exit and (worse) ``on_spawn`` would revert the
            # already-delivered envelope to queued, producing a duplicate
            # operator turn at the next real spawn. Full no-op.
            if prev is not None and epoch is not None and prev == epoch:
                logger.debug(
                    "engagement %s: replayed spawn frame for epoch %s — "
                    "ignored", eng_id[:8], epoch)
                return
            if prev is not None:
                # A previous epoch spawned but never emitted a result before
                # this new spawn — abnormal exit. ``result`` is at-most-once,
                # so a spawn-without-result is an equally valid turn boundary.
                self._log_abnormal_exit(engagement, prev)
            # A new spawn means the prior turn ended (result) or died
            # (abnormal) — no turn is running until turn_start.
            self._turn_running[eng_id] = False
            self._epoch_pending[eng_id] = epoch
            spool = self._inbound.get(eng_id)
            if spool is not None:
                await spool.on_spawn()
            if prev is not None:
                # §A3(b) consumer (b): spawn-without-result — the prior turn died
                # abnormally (an equally valid turn boundary). Consume the
                # re-anchor latch AFTER on_spawn so a redelivered survivor never
                # lands below the re-anchored question.
                await self._consume_reanchor(engagement)
        elif kind == "turn_start":
            # Fresh turn — drop the prior turn's reply-text de-dup set, mark the
            # turn running (receipts are now due for new inbound), and consume
            # the envelope this turn carried (§3 turn_start evidence).
            self._reply_texts[eng_id] = set()
            self._turn_running[eng_id] = True
            # §4: a fresh turn resets the consecutive-ask-refusal escalation.
            self._ask_refusals[eng_id] = 0
            # G4 D3: a fresh turn resets the completion-refusal escalation
            # (unconditional — the counter must clear even spool-less).
            self._completion_refusals.pop(eng_id, None)
            spool = self._inbound.get(eng_id)
            if spool is not None:
                await spool.on_turn_start()
            # §5: a running turn ⇒ ⚙️ working. Reset the elapsed base + start the
            # tick FIRST (so the working-status flush already reflects it).
            summary = self._summaries.get(eng_id)
            if summary is not None:
                from drivers.summary_controller import STATUS_WORKING
                await summary.note_turn_start()
                await self._summary_status_transition(eng_id, STATUS_WORKING)
        elif kind == "mutating_tool":
            logger.debug(
                "engagement %s mutating tool during turn: %s",
                eng_id[:8], payload.get("tool"),
            )
            reg = self._registry
            rec = reg.get(eng_id) if reg is not None else None
            if (rec is not None
                    and getattr(rec, "interaction_state", "") == "awaiting_operator"):
                # B4 (Sol r1): each effect is attempted and marked INDEPENDENTLY.
                # A failure is swallowed (logged) so the relay keeps consuming
                # and the NEXT mutating_tool frame retries the un-marked effect
                # — the marker is set only after success, so at most one
                # SUCCESSFUL notice + one flag-persist ever fire per engagement.
                if eng_id not in self._violation_notified:
                    try:
                        # F2 (Sol r2): the violation notice is a PLATFORM notice
                        # and MUST go through the single writer — a direct
                        # send_to_topic posts it AROUND the sequencer, landing
                        # below open narration while ``edit_narration_if_latest``
                        # still returns APPLIED (the exact §2 ordering violation).
                        # Route through ``post_platform_notice`` (seals narration
                        # + advances high-water under the one lock); direct send
                        # only when no live sequencer.
                        notice = (
                            "The agent took an action while waiting for your "
                            "reply — flagging this engagement for review."
                        )
                        seq = self._sequencers.get(eng_id)
                        if seq is not None:
                            posted = await seq.post_platform_notice(notice)
                            if posted is None:
                                raise RuntimeError("notice post returned no id")
                        else:
                            await self._send_to_topic(engagement.topic_id, notice)
                        self._violation_notified.add(eng_id)
                    except Exception:  # noqa: BLE001 — retry on the next frame
                        logger.warning(
                            "engagement %s: violation notice post failed; "
                            "will retry on next mutating tool",
                            eng_id[:8], exc_info=True,
                        )
                if eng_id not in self._violation_flagged:
                    set_violated = getattr(reg, "set_interaction_violated", None)
                    if set_violated is not None:
                        try:
                            await set_violated(eng_id)
                            self._violation_flagged.add(eng_id)
                        except Exception:  # noqa: BLE001 — retry on the next frame
                            logger.warning(
                                "engagement %s: set_interaction_violated failed; "
                                "will retry on next mutating tool",
                                eng_id[:8], exc_info=True,
                            )
        elif kind == "tool_use":
            # §5: EVERY tool_use block drives the summary's activity + plan
            # progress (NEVER status). Skips the engagement-channel CONTROL
            # tools (ask/reply/set_progress) — they are lifecycle-status
            # signals, not work activity.
            summary = self._summaries.get(eng_id)
            if summary is not None:
                name = payload.get("tool") or ""
                if not name.startswith("mcp__casa-engagement-channel__"):
                    from drivers.summary_controller import (
                        activity_for_tool, extract_plan,
                    )
                    activity = activity_for_tool(name)
                    plan = extract_plan(name, payload.get("input") or {})
                    if plan is not None:
                        # §5 P1-B (r6 fix): apply the activity AND the plan under
                        # ONE lock with ONE flush, so the tool_use that carries a
                        # TodoWrite renders its checklist in the SAME flush (a
                        # separate activity flush would otherwise consume the
                        # throttle window and starve the plan render). ``seq`` is
                        # the relay's monotonic per-turn tool-event counter,
                        # forwarded VERBATIM so the controller rejects a
                        # stale/duplicate plan frame.
                        await summary.submit_activity_and_plan(
                            activity, **plan, seq=payload.get("seq"))
                    else:
                        await summary.submit_activity(activity)
        elif kind == "result":
            self._epoch_pending[eng_id] = None
            self._turn_running[eng_id] = False
            spool = self._inbound.get(eng_id)
            if spool is not None:
                await spool.on_turn_end()
            # §A3(b) consumer (a) — the ordinary turn-end re-anchor pass, pinned
            # to run AFTER ``spool.on_turn_end()`` (Sol r13-2): a notice the spool
            # flushes at turn end must land BEFORE a just-re-anchored question, so
            # the re-anchored anchor stays LAST.
            await self._consume_reanchor(engagement)
            # §5: a finished turn ⇒ the ball is with the operator (⏳ waiting for
            # your reply). Stop the elapsed tick + mandatory turn-end flush.
            summary = self._summaries.get(eng_id)
            if summary is not None:
                from drivers.summary_controller import STATUS_WAITING_REPLY
                await self._summary_status_transition(
                    eng_id, STATUS_WAITING_REPLY)
                await summary.note_turn_end()

    def _log_abnormal_exit(
        self, engagement: EngagementRecord, epoch: int | None,
    ) -> None:
        short = engagement.id[:8]
        # A2b: an epoch we force-ended as the operator-away backstop is NOT a
        # scary abnormal exit — log the expected forced suspend at INFO and
        # consume the mark (a later, genuinely-abnormal exit still WARNs).
        if (epoch is not None
                and self._forced_suspend_epochs.get(engagement.id) == epoch):
            self._forced_suspend_epochs.pop(engagement.id, None)
            logger.info(
                "engagement %s: epoch %s ended by forced suspend "
                "(operator away)", short, epoch,
            )
            return
        tail = self._read_epoch_stderr_tail(engagement, epoch)
        if tail is None:
            logger.warning(
                "engagement %s: epoch %s exited without a result frame "
                "(abnormal); stderr diagnostics unavailable", short, epoch,
            )
        else:
            logger.warning(
                "engagement %s: epoch %s exited without a result frame "
                "(abnormal); stderr tail:\n%s", short, epoch, tail,
            )

    def _read_epoch_stderr_tail(
        self, engagement: EngagementRecord, epoch: int | None,
        *, max_bytes: int = 4000,
    ) -> str | None:
        """Read the UNIQUE per-epoch stderr ring (Sol r5-B2).

        The filename carries the epoch, so no sidecar / ownership check is
        needed — a lingering ringlog consumer only ever writes ITS OWN epoch's
        file. Reads ``.stderr.<epoch>.log.1`` (older rotated chunk) then
        ``.stderr.<epoch>.log`` (newest), returning the tail; both absent
        (never created / already pruned) → ``None`` (diagnostics unavailable,
        never misattributed to a reused slot)."""
        if epoch is None:
            return None
        # Task 4: the ring lives in the control dir now.
        base = Path(stderr_path(engagement.id, epoch))
        chunks: list[str] = []
        for p in (base.with_name(base.name + ".1"), base):
            try:
                chunks.append(
                    p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        if not chunks:
            return None
        return "".join(chunks)[-max_bytes:]

    async def _relay_log_lines(
        self, engagement: EngagementRecord, *, log_path: str,
    ) -> None:
        """Tail the per-engagement s6-log file and emit each line at DEBUG.

        Phase 4b G5 — companion to Bug 4's stderr callback. Stderr from the
        in_casa-driver path lands on the ``subprocess_cli`` logger via the SDK
        callback (sdk_logging.make_stderr_logger).

        v0.75.0/JC3: claude_code's CLI subprocess NO LONGER merges stderr into
        s6-log — the run script redirects stderr into a bounded per-epoch ring
        (``exec 2> >(ringlog.sh .stderr.<EPOCH>.log ...)``), so s6-log/current
        carries only the CLI's NDJSON stdout. This DEBUG relay simply mirrors
        that stdout stream (``_tail_file`` with ``from_end=True``, inode
        rotation handled) into the ``subprocess_cli`` logger, staying
        DEBUG-only: prod operators see nothing, a single LOG_LEVEL=DEBUG flip
        surfaces everything. It is INDEPENDENT of the always-on
        ``TopicStreamRelay`` (which parses the same NDJSON into the operator's
        live topic window and is spawned regardless of LOG_LEVEL). Per-epoch
        stderr is surfaced separately by the abnormal-exit correlation in
        ``_on_stream_event`` / ``_read_epoch_stderr_tail``.

        v0.64.0 removed the sibling ``_capture_url`` task: headless claude
        auto-degrades to one-shot --print mode on non-TTY stdout and never
        prints a remote-control URL line, so there is nothing to capture
        (live-verified; see the 2026-07-10 remote-control-honesty design).
        """
        short = engagement.id[:8]
        relay_logger = logging.getLogger("subprocess_cli")
        async for line in _tail_file(log_path, from_end=True):
            relay_logger.debug(
                "stdout %s", line.rstrip("\n"),
                extra={"engagement_id": short},
            )

    async def _capture_session_id(
        self, engagement: EngagementRecord, *,
        poll_interval_s: float = 0.5,
    ) -> None:
        """P31 (v0.37.10): watch the claude CLI's own session-storage
        directory for the first ``<uuid>.jsonl`` file. The filename
        (minus extension) IS the SDK session UUID. Persist to the control
        dir's ``.session_id`` (Task 4) so a boot-replay resumes the
        conversation (the run script validates the file's content as an
        exact UUID and passes a single ``--resume=<uuid>`` argv token —
        v0.131.0 hardening).

        Replaces v0.37.9's s6-log tailing approach, which was
        non-functional at the time: until v0.64.0 the s6-rc log pipeline
        was never compiled (nested log/ subdir — see
        ``s6_rc.write_service_dir``), so the log file did not exist.
        Watching the CLI's own session storage is retained even now that
        the log pipeline works: it observes the authoritative artifact
        directly. Bug-review:
        ``docs/bug-review-2026-05-14-exploration6.md::O-5``.

        Claude CLI session storage layout (HOME=<ws>/.home, CWD=<ws>):

            <ws>/.home/.claude/projects/-data-engagements-<id>/<uuid>.jsonl

        The directory-name encoding replaces ``/`` with ``-`` in the
        workspace path (claude CLI native behavior).

        One-shot: returns after the first UUID-named .jsonl is found.
        Re-spawns on s6 restart see the persisted file and resume
        cleanly — see ``engagement_run_template.sh``.

        Atomic write: temp-file + ``os.replace`` so a Casa crash
        mid-write cannot leave a half-truncated ``.session_id``.
        """
        short = engagement.id[:8]
        ws = Path(self._engagements_root) / engagement.id
        # Task 4: the WRITE target moves to the control dir. The scan below
        # still reads the CLI's own session-storage directory under the
        # workspace's .home. Task 5: that scan is now routed through
        # ``safe_fs`` — rooted at ``<ws>/.home`` and never following a
        # symlink component (e.g. a sibling engagement planting
        # ``.home/.claude/projects/<name>`` as a symlink to ITS OWN projects
        # dir, which would otherwise leak its session UUID here).
        target = Path(session_id_path(engagement.id))
        tmp = target.with_name(target.name + ".tmp")
        ws_home = ws / ".home"
        rel_projects_dir = (
            f".claude/projects/-data-engagements-{engagement.id}"
        )
        owner_uid = owner_uid_or_none(engagement.allocated_uid)
        while True:
            try:
                sid = self._scan_projects_dir_for_sid(
                    ws_home, rel_projects_dir, owner_uid=owner_uid)
            except SymlinkRefused as exc:
                logger.warning(
                    "engagement %s session capture refused: symlinked "
                    "projects dir (%s) — no session id will be adopted",
                    short, exc,
                )
                return
            if sid is not None:
                try:
                    tmp.write_text(sid + "\n", encoding="utf-8")
                    os.replace(tmp, target)
                except OSError as exc:
                    logger.warning(
                        "engagement %s session_id persist failed: %s",
                        short, exc,
                    )
                    return
                logger.info(
                    "engagement %s captured sdk session_id %s",
                    short, sid[:8],
                )
                if self._persist_session_id is not None:
                    try:
                        await self._persist_session_id(engagement.id, sid)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "engagement %s persist_session_id callback "
                            "failed: %s", short, exc,
                        )
                return
            await asyncio.sleep(poll_interval_s)

    @staticmethod
    def _scan_projects_dir_for_sid(
        ws_home: Path, rel_projects_dir: str, *, owner_uid: int | None,
    ) -> str | None:
        """Return the oldest UUID-named .jsonl in the CLI's session-projects
        dir (``<ws_home>/<rel_projects_dir>``), or None if not present yet.

        Task 5: listed via ``safe_fs.list_dir_beneath`` rooted at
        ``ws_home`` — refuses to follow a symlinked ``rel_projects_dir``
        (e.g. one planted pointing at a SIBLING engagement's own projects
        dir) rather than reading whatever it points to. Raises
        ``SymlinkRefused`` up to the caller in that case: the caller treats
        a refused DIRECTORY as "no session capture, ever" for this
        watcher — never silently adopting a sibling's UUID.

        A candidate FILE that is itself a symlink (finer-grained than the
        directory case above) is instead just skipped — same effect as if
        it were absent, since only the directory-level refusal needs to
        abort the whole watch.

        Sort by mtime ascending so the first session file (the one
        spawned by the initial CLI start) wins over any later ones the
        CLI might write on a resume retry.
        """
        try:
            names = list_dir_beneath(
                str(ws_home), rel_projects_dir, owner_uid=owner_uid)
        except SymlinkRefused:
            raise
        except OSError:
            return None
        candidates: list[tuple[float, str]] = []
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            stem = name[: -len(".jsonl")]
            if _UUID_REGEX.match(stem) is None:
                continue
            rel_file = f"{rel_projects_dir}/{name}"
            try:
                fd = open_beneath(
                    str(ws_home), rel_file, owner_uid=owner_uid)
            except SymlinkRefused:
                # This specific entry is a symlink — skip it (do not adopt
                # its target's mtime/content), but the directory itself
                # resolved cleanly so the watch continues.
                continue
            except OSError:
                continue
            try:
                mtime = os.fstat(fd).st_mtime
            finally:
                os.close(fd)
            candidates.append((mtime, stem))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    async def _poll_respawns(
        self, engagement: EngagementRecord, *, interval_s: float = 5.0,
    ) -> None:
        """Emit subprocess_respawn bus events when s6-svstat shows a new PID."""
        last_pid: int | None = None
        while True:
            await asyncio.sleep(interval_s)
            pid = await s6_rc.service_pid(engagement_id=engagement.id)
            if pid is None:
                continue
            if last_pid is not None and pid != last_pid:
                await self._publish_bus_event({
                    "event": "subprocess_respawn",
                    "engagement_id": engagement.id,
                    "previous_pid": last_pid,
                    "new_pid": pid,
                    "ts": time.time(),
                })
            last_pid = pid

    async def _publish_bus_event(self, event: dict) -> None:
        """Overridable (tests inject). Default no-op at driver layer —
        casa_core wires a real bus sink in at construction time (see Phase E)."""
        logger.debug("bus event (no sink wired): %s", event)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _tail_file(log_path: str, *, from_end: bool = False):
    """Yield new lines from a file as they appear. Terminates on task cancel.

    Bug 11 (v0.14.6): tracks the file's inode so rotation is handled.
    s6-log rotates ``current`` at 1 MB by renaming it to ``@<timestamp>.s``
    and creating a fresh ``current``. Pre-fix the loop kept seeking to
    the OLD pos in the new (smaller) file, so all lines below the prior
    cutoff were silently dropped. Now: when ``st_ino`` changes, reset
    ``pos`` to 0 so the new file is read from its start. We also reset
    if the file shrinks below ``pos`` (truncate-in-place pattern).

    v0.64.0 (file is now real in production):
      - ``from_end=True`` starts at the file's current end when it already
        exists at first sight — boot replay re-attaches without re-yielding
        up to 1 MB of history. A file that appears later (fresh engagement)
        is still read from its start.
      - A transient OSError mid-cycle (rotation renames ``current`` between
        ``exists()`` and ``open()``) retries next tick instead of killing
        the (unobserved) consumer task.
    """
    path = Path(log_path)
    pos = 0
    last_inode: int | None = None
    first_sight = True
    while True:
        try:
            exists = path.exists()
            if first_sight:
                first_sight = False
                if exists and from_end:
                    try:
                        pos = path.stat().st_size
                    except OSError:
                        pos = 0
            if exists:
                try:
                    current_inode = path.stat().st_ino
                except OSError:
                    current_inode = None
                if last_inode is not None and current_inode != last_inode:
                    pos = 0
                last_inode = current_inode

                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(0, 2)            # SEEK_END
                    end = fh.tell()
                    if pos > end:
                        pos = 0
                    fh.seek(pos)
                    while True:
                        line = fh.readline()
                        if not line:
                            pos = fh.tell()
                            break
                        yield line
        except OSError:
            pass                             # transient — retry next tick
        await asyncio.sleep(0.1)
