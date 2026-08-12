"""Per-engagement live SUMMARY controller (v0.79.0 §5 — engagement-topic UX).

R1 (operator ruling): the FIRST topic message is a living, pinned SUMMARY;
everything else flows below it as an append-only causal log. This module owns
that one summary message for one ``claude_code`` engagement.

ONE serialized controller (an :class:`asyncio.Lock` guards every state mutation
and flush) owns the summary. Consumers submit DESIRED STATE — status, activity,
plan progress, elapsed base, open questions — and the controller coalesces +
throttles the resulting edits, posting them through the per-topic OUTPUT
SEQUENCER as NON-narration edits (the R1 exception: summary edits never seal the
open narration, are never themselves sealed, and never advance the narration
high-water mark — see :meth:`channels.output_sequencer.OutputSequencer.edit_summary`).

AUTHORITY MODEL (§5, Sol r2-8/r3-3): activity/plan/elapsed frames NEVER submit
status. Only LIFECYCLE sources do — the driver turn lifecycle, ``interaction_state``
and the ask registry — and each acquires a monotonic REVISION from ONE
engagement-wide atomic allocator (persisted with the engagement record) at
transition time, so the sources are totally ordered and collision-free. A NEWER
revision may LOWER the status rank (waiting → working after an answer is
legitimate); an OLDER or EQUAL revision never overrides; a TERMINAL status is
absolute (nothing overrides it once set).

Clocks are injectable (``_now`` / ``_sleep``); no code here patches the global
``asyncio.sleep`` (the module-local / injected-clock rule, CLAUDE.md memory cage).
The elapsed TICK (§5 B1) is THE single sanctioned timer in this design.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from channels.output_sequencer import FAILED
from text_util import is_unsafe_text

logger = logging.getLogger(__name__)

# -- Status copy (EXACT — §5; no parse_mode) --------------------------------
STATUS_WORKING = "⚙️ working"
STATUS_WAITING_REPLY = "⏳ waiting for your reply"
STATUS_WAITING_APPROVAL = "🔐 waiting for your approval"
# F-EXPIRE (v0.83.0, A2a): the engagement is SUSPENDED — a question expired
# unanswered and Casa is waiting for the operator to return (no further asks,
# no live activity). NON-terminal (a returning operator clears it). Rendered
# status-first like the others; because it is not ``STATUS_WORKING`` the
# activity/elapsed merge in ``_render_locked`` never fires while paused.
STATUS_PAUSED = "⏸ paused — waiting for the operator"
STATUS_COMPLETED = "✅ completed"
STATUS_CANCELLED = "🛑 cancelled"
STATUS_ERROR = "⚠️ error"

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_ERROR}
)

# Terminal ``outcome`` (finalize) → status line.
OUTCOME_STATUS: dict[str, str] = {
    "completed": STATUS_COMPLETED,
    "cancelled": STATUS_CANCELLED,
    "error": STATUS_ERROR,
    "failed": STATUS_ERROR,
}

# -- tunables (module-local so tests can shrink them) -----------------------
_THROTTLE_S = 10.0        # §5: edits ≥10s apart EXCEPT status-class changes.
_TICK_S = 60.0            # §5 B1: one edit-eligible elapsed tick per 60s.
_PLAN_SUBJECT_CAP = 60    # §5 B2: 60-char cap on agent-authored plan subjects.

# -- P1-B (v0.91.0): pinned-summary plan checklist --------------------------
_MCP_SERVER_CAP = 32      # §5 P1-B: cap the mcp server substring AT SOURCE.
_GOAL_LINE_CAP = 200      # §5 P1-B: goal line capped at render.
_OPEN_QS_INLINE_MAX = 3   # §5 P1-B: collapse open-questions beyond 3 entries.
_SUMMARY_HARD_CAP = 4096  # §5 P1-B: the unconditional final wire bound.
_WINDOW_BEFORE = 1        # §5 P1-B: window is [anchor−1 … anchor+6].
_WINDOW_AFTER = 6
# Per-status checklist marks (done → ☑, active → ▶, pending → ☐).
_ITEM_MARK: dict[str, str] = {"done": "☑", "active": "▶", "pending": "☐"}
_ZERO_BUCKETS: dict[str, int] = {"done": 0, "active": 0, "pending": 0}


# ---------------------------------------------------------------------------
# Pure helpers (activity mapping, plan extraction, elapsed, rendering).
# ---------------------------------------------------------------------------

# §5 S5: coarse tool_use → activity mapping.
_ACTIVITY_READING = frozenset({"Read", "Glob", "Grep"})
_ACTIVITY_EDITING = frozenset({"Write", "Edit", "NotebookEdit"})
_ACTIVITY_RESEARCH = frozenset({"WebFetch", "WebSearch"})


def activity_for_tool(tool_name: str) -> str:
    """Coarse activity phrase for a ``tool_use`` block (§5 S5).

    Read/Glob/Grep → ``reading files``; Write/Edit/NotebookEdit → ``editing
    files``; Bash → ``running commands``; Task*/TodoWrite → ``planning``;
    WebFetch/WebSearch → ``researching``; ``mcp__<server>__…`` → ``using
    <server> tools``; anything else → ``working``.
    """
    name = tool_name or ""
    if name in _ACTIVITY_READING:
        return "reading files"
    if name in _ACTIVITY_EDITING:
        return "editing files"
    if name == "Bash":
        return "running commands"
    if name.startswith("Task") or name == "TodoWrite":
        return "planning"
    if name in _ACTIVITY_RESEARCH:
        return "researching"
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 and parts[1] else "mcp"
        # §5 P1-B: cap the (unbounded) server substring AT SOURCE so the status
        # line can never blow the 4096 wire bound.
        return f"using {server[:_MCP_SERVER_CAP]} tools"
    return "working"


def sanitize_plan_subject(subject: object) -> str:
    """Sanitize an agent-authored plan subject / checklist label (§5 B2, P1-B).

    FIXED pipeline order (§5 P1-B r3 — summary edits ride the v0.89.0 R2a rich
    closure): (1) reject UNSAFE-TEXT (v0.78 predicate — control/bidi codepoints
    incl. newlines) outright (returns ``""``); (2) strip the rich marker chars
    (``*``, backticks) so a mid-marker cut can never unbalance the rich edit;
    (3) THEN cap at 60 characters. An all-marker subject empties to ``""`` after
    step (2). ``'*'*60+'OAuth'`` → ``OAuth`` (NOT capped to 60 stars).
    """
    if not isinstance(subject, str) or not subject:
        return ""
    if is_unsafe_text(subject):
        return ""
    stripped = subject.replace("*", "").replace("`", "")
    return stripped[:_PLAN_SUBJECT_CAP]


def _todo_status(todo: object) -> str:
    """Normalize a todo's status to ``done``/``active``/``pending`` (§5 P1-B r4;
    unknown or missing status normalizes to ``pending``)."""
    status = todo.get("status") if isinstance(todo, dict) else None
    if status == "completed":
        return "done"
    if status == "in_progress":
        return "active"
    return "pending"


def _todo_label(todo: object) -> str | None:
    """Sanitized checklist label for a todo, or ``None`` when sanitization
    empties it (§5 P1-B: the ``content`` — the stable task title — falls back to
    ``activeForm``; empties to ``None`` so the render shows ``—``)."""
    if not isinstance(todo, dict):
        return None
    text = todo.get("content") or todo.get("activeForm") or ""
    return sanitize_plan_subject(text) or None


def _todo_item(idx: int, status: str, todo: object) -> dict:
    """A rendered checklist entry ``{ordinal, status, label|None}`` (§5 P1-B)."""
    return {"ordinal": idx + 1, "status": status, "label": _todo_label(todo)}


def _extract_todo_plan(todos: list) -> dict:
    """SINGLE-PASS full-scan / bounded-window TodoWrite extraction (§5 P1-B r4;
    r6 single-pass rewrite).

    Scans the COMPLETE ``todos`` list ONCE (no cap, no O(n) auxiliary list) to
    compute ``total``, the exact ``done`` count and the TRUE anchor (first
    ``in_progress``, else first ``pending``, else the LAST item — the true last,
    never a truncation point), RETAINING only bounded state: the window entries
    ``[{ordinal, status, label|None}]`` for ``[anchor−1 … anchor+6]`` clamped,
    the per-region ``hidden_before``/``hidden_after`` status buckets, and a small
    fixed lookback so the anchor's window can be reconstructed even when the
    anchor is discovered late.

    The two-level anchor priority (a later ``in_progress`` outranks an earlier
    ``pending``) is handled with at-most-ONE promotion: a first ``pending`` opens
    a provisional window; a later ``active`` PROMOTES to the final anchor and the
    now-before entries are re-bucketed. ``prev`` (the immediately preceding item)
    always supplies the promoted anchor's ``anchor−1`` slot. Retained state is
    O(window) regardless of plan size.
    """
    done = 0
    before = dict(_ZERO_BUCKETS)
    after = dict(_ZERO_BUCKETS)
    items: list[dict] = []
    anchor: int | None = None
    anchor_final = False          # True once the anchor is a first ``active``
    # Lookback while still SEARCHING (no anchor yet): the last few items, so an
    # anchor found late (incl. all-completed → last) still gets its window. Only
    # ``_WINDOW_BEFORE + 1`` items can ever enter the window from behind.
    hold: deque = deque(maxlen=_WINDOW_BEFORE + 1)
    prev: tuple | None = None     # (idx, status, todo) of the previous item
    total = 0

    for i, todo in enumerate(todos):
        total = i + 1
        s = _todo_status(todo)
        if s == "done":
            done += 1

        if anchor is None:
            if s == "done":
                # Not an anchor: retire the oldest held item to ``before``.
                if len(hold) == hold.maxlen:
                    o_idx, o_s, _o = hold[0]
                    before[o_s] += 1
                hold.append((i, s, todo))
            else:
                # First non-done item OPENS the window. ``active`` finalizes the
                # anchor; ``pending`` is provisional (a later active promotes).
                anchor = i
                anchor_final = s == "active"
                start = max(0, i - _WINDOW_BEFORE)
                for h_idx, h_s, h_todo in hold:
                    if h_idx < start:
                        before[h_s] += 1
                    else:
                        items.append(_todo_item(h_idx, h_s, h_todo))
                items.append(_todo_item(i, s, todo))
        elif anchor_final:
            if i <= anchor + _WINDOW_AFTER:
                items.append(_todo_item(i, s, todo))
            else:
                after[s] += 1
        elif s == "active":
            # PROMOTE the provisional pending anchor to this first active. Fold
            # every collected window entry and every after-bucket count into
            # ``before`` (all sit strictly before the new anchor), then correct
            # for ``prev`` (index i−1, counted once above) which becomes the new
            # anchor's ``anchor−1`` window head.
            for it in items:
                before[it["status"]] += 1
            for k in before:
                before[k] += after[k]
                after[k] = 0
            p_idx, p_s, p_todo = prev  # type: ignore[misc]
            before[p_s] -= 1
            items = [_todo_item(p_idx, p_s, p_todo), _todo_item(i, s, todo)]
            anchor = i
            anchor_final = True
        else:
            # Provisional pending anchor still collecting its window.
            if i <= anchor + _WINDOW_AFTER:
                items.append(_todo_item(i, s, todo))
            else:
                after[s] += 1

        prev = (i, s, todo)

    if anchor is None and total:
        # All completed → the TRUE last item is the anchor; the tail lookback
        # holds exactly ``[anchor−1 … anchor]``.
        anchor = total - 1
        start = max(0, anchor - _WINDOW_BEFORE)
        for h_idx, h_s, h_todo in hold:
            if h_idx < start:
                before[h_s] += 1
            else:
                items.append(_todo_item(h_idx, h_s, h_todo))

    subject = ""
    if anchor is not None:
        for it in items:
            if it["ordinal"] == anchor + 1:
                subject = it["label"] or ""
                break
    return {
        "done": done,
        "total": total,
        "subject": subject,
        "items": items,
        "hidden_before": before,
        "hidden_after": after,
        "is_todo": True,
    }


def extract_plan(tool_name: str, tool_input: dict) -> dict | None:
    """Derive a plan-progress DESIRED-STATE fragment from a Task*/TodoWrite
    tool_use payload (§5 B2, P1-B), or ``None`` when the tool carries no plan.

    ``TodoWrite`` → the full-scan/bounded-window fragment (``is_todo=True``):
    ``{done, total, subject, items, hidden_before, hidden_after}`` — the
    AUTHORITATIVE display source. ``Task*`` → a ``{subject}`` FALLBACK fragment
    (``is_todo=False``) only; while a TodoWrite has latched the plan this turn,
    the controller ignores Task* mutations. ``subject``/labels are sanitized.
    """
    name = tool_name or ""
    inp = tool_input if isinstance(tool_input, dict) else {}
    if name == "TodoWrite":
        todos = inp.get("todos")
        if not isinstance(todos, list):
            return None
        return _extract_todo_plan(todos)
    if name.startswith("Task"):
        subject = inp.get("description") or inp.get("prompt") or ""
        return {"subject": sanitize_plan_subject(subject), "is_todo": False}
    return None


def format_elapsed(seconds: float) -> str:
    """Human-readable elapsed string (``45s`` / ``2m 30s`` / ``1h 05m``)."""
    total = int(seconds) if seconds and seconds > 0 else 0
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _render_item_line(item: dict) -> str:
    """One checklist line: ``<mark> <ordinal>. <label|—>`` (§5 P1-B r4)."""
    mark = _ITEM_MARK.get(item.get("status"), "☐")
    label = item.get("label")
    return f"{mark} {item.get('ordinal')}. {label if label else '—'}"


def _hidden_count_line(bucket: dict | None, word: str) -> str:
    """A framing hidden-count line with the per-region status breakdown, or
    ``""`` when the region is empty (§5 P1-B r4). The active bucket renders only
    when nonzero: ``… N earlier — k done, p pending`` (or ``… N more — k done,
    a active, p pending``)."""
    if not bucket:
        return ""
    done = bucket.get("done", 0)
    active = bucket.get("active", 0)
    pending = bucket.get("pending", 0)
    n = done + active + pending
    if n <= 0:
        return ""
    parts = [f"{done} done"]
    if active > 0:
        parts.append(f"{active} active")
    parts.append(f"{pending} pending")
    return f"… {n} {word} — " + ", ".join(parts)


def render_summary(
    *,
    goal_line: str,
    status: str,
    plan_done: int | None = None,
    plan_total: int | None = None,
    plan_subject: str = "",
    plan_items: tuple | list = (),
    plan_hidden_before: dict | None = None,
    plan_hidden_after: dict | None = None,
    activity: str | None = None,
    elapsed_str: str = "",
    open_qs: tuple[int, ...] = (),
) -> str:
    """Render the EXACT summary layout (W-R2; no parse_mode, omit empty lines).

    STATUS FIRST, with activity + elapsed MERGED onto the status line; the short
    title (W-R6 ``goal_line``) second; then Plan / Open-questions (each omitted
    when empty). There is NO separate ``Now:`` line — activity/elapsed live on
    the status line and are only supplied while working (the controller passes
    ``activity=None`` when waiting/between turns).

    ::

        ⚙️ working — planning · 1m 00s
        Gmail plugin
        Plan: 2/5 — current: OAuth setup
        Open questions: Q11

    Waiting (no activity/elapsed on the status line)::

        ⏳ waiting for your reply
        Gmail plugin
        Plan: 2/5 — current: OAuth setup
        Open questions: Q11
    """
    lines: list[str | None] = []
    status_line = status
    if activity:
        status_line += f" — {activity}"
        if elapsed_str:
            status_line += f" · {elapsed_str}"
    lines.append(status_line)
    if goal_line:
        # §5 P1-B: cap the (unbounded) goal line at render.
        gl = goal_line
        if len(gl) > _GOAL_LINE_CAP:
            gl = gl[:_GOAL_LINE_CAP] + "…"
        lines.append(gl)
    # §5 P1-B r4: the plan block. Checklist presence keys on ``total > 0`` (never
    # on labels — a marker-only plan still renders). When window entries are
    # present they REPLACE the redundant ` — current:` clause (the ▶ anchor line
    # carries the current item); a bare plan fragment keeps the legacy line.
    item_line_idxs: list[int] = []
    if plan_total:
        if plan_items:
            lines.append(f"Plan: {plan_done or 0}/{plan_total}")
            hb = _hidden_count_line(plan_hidden_before, "earlier")
            if hb:
                lines.append(hb)
            for item in plan_items:
                item_line_idxs.append(len(lines))
                lines.append(_render_item_line(item))
            ha = _hidden_count_line(plan_hidden_after, "more")
            if ha:
                lines.append(ha)
        else:
            subj = f" — current: {plan_subject}" if plan_subject else ""
            lines.append(f"Plan: {plan_done or 0}/{plan_total}{subj}")
    if open_qs:
        # §5 P1-B: collapse an oversized open-questions set.
        if len(open_qs) > _OPEN_QS_INLINE_MAX:
            lines.append(f"⏳ {len(open_qs)} open questions")
        else:
            lines.append(
                "Open questions: " + ", ".join(f"Q{n}" for n in open_qs)
            )

    def _join() -> str:
        return "\n".join(ln for ln in lines if ln is not None)

    text = _join()
    # §5 P1-B r4 (checklist-preserving priority): the unbounded inputs are
    # already bounded above (activity server-name at source, goal at 200,
    # open-questions collapsed), so the checklist is the LAST content sacrificed
    # — drop item lines only if STILL over, bottom-up, before the unconditional
    # whole-payload truncation. All of this operates on the RAW text BEFORE the
    # rich closure, so this ≤4096 bound is the wire bound.
    if len(text) > _SUMMARY_HARD_CAP:
        for idx in reversed(item_line_idxs):
            lines[idx] = None
            text = _join()
            if len(text) <= _SUMMARY_HARD_CAP:
                break
    if len(text) > _SUMMARY_HARD_CAP:
        text = text[:_SUMMARY_HARD_CAP]
    return text


# ---------------------------------------------------------------------------
# The controller.
# ---------------------------------------------------------------------------


async def _default_sleep(seconds: float) -> None:  # pragma: no cover - trivial
    await asyncio.sleep(seconds)


class SummaryController:
    """One serialized live-summary controller for one engagement (§5)."""

    def __init__(
        self,
        *,
        engagement_id: str,
        sequencer,
        goal_line: str,
        open_question_numbers: Callable[[], list[int]] = lambda: [],
        pin_message: Callable[[int], Awaitable[bool]] | None = None,
        message_id: int | None = None,
        _now: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], Awaitable[None]] | None = None,
        throttle_s: float = _THROTTLE_S,
        tick_s: float = _TICK_S,
    ) -> None:
        self.engagement_id = engagement_id
        self._sequencer = sequencer
        self._goal_line = goal_line
        self._open_question_numbers = open_question_numbers
        self._pin_message = pin_message
        self._message_id = message_id
        self._now = _now
        self._sleep = _sleep or _default_sleep
        self._throttle_s = throttle_s
        self._tick_s = tick_s

        self._lock = asyncio.Lock()
        # DESIRED STATE.
        self._status = STATUS_WORKING
        # ``_status_rev`` is the revision at which the current status was set; a
        # submission wins iff its revision is strictly greater (older/equal never
        # overrides). Starts below zero so the first revision-0 lifecycle
        # submission wins.
        self._status_rev = -1
        self._activity: str | None = None
        self._plan_done: int | None = None
        self._plan_total: int | None = None
        self._plan_subject = ""
        # §5 P1-B: the bounded checklist window + per-region hidden buckets.
        self._plan_items: tuple = ()
        self._plan_hidden_before: dict = dict(_ZERO_BUCKETS)
        self._plan_hidden_after: dict = dict(_ZERO_BUCKETS)
        # §5 P1-B r3: Todo-plan authority latch — set on the first accepted
        # TodoWrite fragment this turn, reset with the plan lifecycle at the turn
        # boundary. While latched, Task* fragments never mutate the plan.
        self._todo_plan_seen_this_turn = False
        # §5 P1-B r4: plan-frame ordering watermark. ``submit_plan`` rejects a
        # frame whose sequence is ≤ the watermark (a stale/duplicate frame);
        # reset with the plan lifecycle at the turn boundary. Any orderable
        # sequence works — the relay coordinate ``(segment, offset, ordinal)``
        # OR the in-casa driver's per-turn integer counter.
        self._plan_seq_watermark: object | None = None
        self._turn_running = False
        self._turn_base: float | None = None  # monotonic base for elapsed
        # Edit throttle + last-rendered (coalescing / no-op).
        self._last_edit_ts = float("-inf")
        self._last_rendered: str | None = None
        # Pin state (best-effort; retried on lifecycle flush).
        self._pinned = False
        self._pin_warned = False
        # The SINGLE sanctioned timer (§5 B1).
        self._tick_task: asyncio.Task | None = None

    # -- serialization (GLOBAL LOCK-ORDER: sequencer OUTER, summary INNER) ---
    @asynccontextmanager
    async def _writing(self):
        """Acquire the writer locks in the ONE sanctioned order: the OUTPUT
        SEQUENCER lock OUTER, then this controller's summary lock INNER.

        INVARIANT (Sol diff gate r3): *no code may hold ``self._lock`` (the summary
        lock) while acquiring the OutputSequencer lock.* Every summary mutator that
        can reach :meth:`_flush_locked` / ``edit_summary`` / :meth:`_maybe_pin_locked`
        enters through here, so the sequencer's writer lock is ALWAYS taken first —
        reentrantly for the owning task, so an ask poster the sequencer already
        awaits under its held writer lock re-enters rather than deadlocking.

        Why this kills the cross-task AB-BA deadlock: the two paths that used to
        acquire the two locks in OPPOSITE orders —

        * ``post_for_block`` → armed ask poster (holds the sequencer lock) →
          ``note_ask_waiting`` → :meth:`submit_status` (wants the summary lock), and
        * a summary mutator (holds the summary lock) → :meth:`_flush_locked` →
          ``edit_summary`` (wants the sequencer lock) —

        now share ONE order. Because holding the summary lock REQUIRES first holding
        the sequencer lock, and only one task holds the sequencer lock at a time, a
        second task can never hold the summary lock while the first holds the
        sequencer lock. ``edit_summary``, reached from under the summary lock inside
        :meth:`_flush_locked`, is therefore always already under the sequencer lock
        (reentrant for this task). :meth:`_maybe_pin_locked`'s ``pin_message``
        primitive does NOT touch the sequencer, so it adds no third ordering.
        """
        async with self._sequencer.serialized():   # OUTER (reentrant if held)
            async with self._lock:                  # INNER
                yield

    # -- message-id adoption ------------------------------------------------
    def adopt_message_id(self, message_id: int | None) -> None:
        """Bind the summary Telegram message id (posted at boot, persisted in
        the engagement record; a resumed engagement adopts it on attach)."""
        self._message_id = message_id

    @property
    def message_id(self) -> int | None:
        return self._message_id

    # -- lifecycle status (authority-checked) -------------------------------
    async def submit_status(self, status: str, revision: int | None) -> None:
        """Submit a lifecycle STATUS at *revision* (§5 authority model).

        Accepted iff the current status is not terminal AND *revision* is
        strictly greater than the revision the current status was set at. The
        flush is FORCED (F1, Sol diff gate): a status-class change obviously
        re-renders, but a SAME-status revision bump can still change the
        rendered text — most commonly the open-questions set shrinking or
        growing while the status stays ⏳ waiting — and that change must reach
        the pinned summary. The rendered-text no-op gate in ``_flush_locked``
        suppresses a redundant wire edit when nothing actually moved.
        """
        async with self._writing():
            if self._status in _TERMINAL_STATUSES:
                return  # terminal absolute
            if revision is None or revision <= self._status_rev:
                return  # older/equal never overrides
            self._status = status
            self._status_rev = revision
            self._reconcile_tick_locked()
            await self._flush_locked(force=True)

    async def finalize(self, status: str) -> None:
        """Set the TERMINAL status (§5 — terminal absolute), cancel the tick and
        perform the mandatory engagement-finalize flush. Ignores the revision
        ordering: a terminal status wins unconditionally and, once set,
        :meth:`submit_status` rejects every later submission."""
        async with self._writing():
            self._status = status
            self._turn_running = False
            self._cancel_tick_locked()
            await self._flush_locked(force=True)

    # -- open-questions / pulled-input refresh (never status) ---------------
    async def refresh(self) -> None:
        """Force a re-render + flush so a change in a PULLED input that carries
        NO status transition still reaches the pinned summary (F1, Sol diff
        gate). The open-questions set is read live from ``_open_question_numbers``
        at render time, so when it shrinks/grows during ask settlement or boot
        reconciliation — without a status-class change — the driver calls this
        to reflow the open-questions line. The rendered-text no-op gate in
        ``_flush_locked`` suppresses a redundant wire edit when nothing moved."""
        async with self._writing():
            await self._flush_locked(force=True)

    # -- activity / plan (never status) -------------------------------------
    async def submit_activity(self, activity: str) -> None:
        """Submit the current ACTIVITY (§5 S5). DESIRED-STATE coalescing: the
        edit is throttled, so alternating tools yield at most one edit per
        throttle window (rate limiting, not just dedup)."""
        async with self._writing():
            self._activity = activity
            await self._flush_locked(force=False)

    def _merge_plan_locked(
        self,
        *,
        done: int | None,
        total: int | None,
        subject: str | None,
        items: list | tuple | None,
        hidden_before: dict | None,
        hidden_after: dict | None,
        is_todo: bool,
        seq: object | None,
    ) -> bool:
        """Merge a plan-progress fragment into the controller state (lock held,
        no flush). Returns ``False`` when *seq* is stale (≤ the watermark) so the
        caller can skip a redundant flush; ``True`` when the frame was applied.

        ORDERING (r4): a stale/duplicate frame (``seq`` ≤ watermark) mutates
        nothing. AUTHORITY (r3 latch): a TodoWrite fragment (``is_todo``) is the
        authoritative display source — it overwrites the counts, subject,
        checklist window and hidden buckets, and LATCHES the plan for the turn.
        While latched, a Task* fragment (``is_todo`` False) NEVER mutates the
        plan; unlatched, a Task* ``subject`` is the display fallback only.
        """
        if (
            seq is not None
            and self._plan_seq_watermark is not None
            and seq <= self._plan_seq_watermark
        ):
            return False
        if is_todo:
            self._todo_plan_seen_this_turn = True
            self._plan_done = done
            self._plan_total = total
            self._plan_subject = subject or ""
            self._plan_items = tuple(items or ())
            self._plan_hidden_before = hidden_before or dict(_ZERO_BUCKETS)
            self._plan_hidden_after = hidden_after or dict(_ZERO_BUCKETS)
        elif not self._todo_plan_seen_this_turn:
            # Task* fallback — display-only, and only while unlatched.
            if subject is not None:
                self._plan_subject = subject
        if seq is not None:
            self._plan_seq_watermark = seq
        return True

    async def submit_plan(
        self,
        *,
        done: int | None = None,
        total: int | None = None,
        subject: str | None = None,
        items: list | tuple | None = None,
        hidden_before: dict | None = None,
        hidden_after: dict | None = None,
        is_todo: bool = False,
        seq: object | None = None,
    ) -> None:
        """Merge a plan-progress fragment (§5 B2, P1-B).

        ORDERING (r4): if *seq* is supplied and is ≤ the ordering watermark, the
        frame is a stale/duplicate and is rejected outright (no mutation, no
        flush). An accepted frame advances the watermark.

        AUTHORITY (r3 latch): see :meth:`_merge_plan_locked`.
        """
        async with self._writing():
            if self._merge_plan_locked(
                done=done, total=total, subject=subject, items=items,
                hidden_before=hidden_before, hidden_after=hidden_after,
                is_todo=is_todo, seq=seq,
            ):
                await self._flush_locked(force=False)

    async def submit_activity_and_plan(
        self,
        activity: str,
        *,
        done: int | None = None,
        total: int | None = None,
        subject: str | None = None,
        items: list | tuple | None = None,
        hidden_before: dict | None = None,
        hidden_after: dict | None = None,
        is_todo: bool = False,
        seq: object | None = None,
    ) -> None:
        """Apply an ACTIVITY update AND a plan-progress fragment atomically —
        under ONE writer lock with ONE flush (§5 P1-B, r6 fix).

        A tool_use block carries both signals; issuing ``submit_activity`` then
        ``submit_plan`` as two calls let the activity flush consume the throttle
        window, so the TodoWrite checklist render then waited for the next event
        or tick. Coalescing them means the same tool_use that carries a TodoWrite
        renders its checklist in the SAME flush. The activity ALWAYS applies (a
        stale/duplicate plan ``seq`` still updates the activity line); the plan
        merges only when not stale.
        """
        async with self._writing():
            self._activity = activity
            self._merge_plan_locked(
                done=done, total=total, subject=subject, items=items,
                hidden_before=hidden_before, hidden_after=hidden_after,
                is_todo=is_todo, seq=seq,
            )
            await self._flush_locked(force=False)

    # -- turn lifecycle (elapsed base + tick) -------------------------------
    async def note_turn_start(self) -> None:
        """A CLI turn started: reset the elapsed base and (re)start the tick if
        the status is working."""
        async with self._writing():
            self._turn_running = True
            self._turn_base = self._now()
            # §5 P1-B r3/r4: a fresh turn re-opens the plan authority window —
            # reset the Todo-plan latch and the ordering watermark so the new
            # turn's first accepted fragment re-establishes authority and no
            # prior-turn sequence blocks it. (The plan COUNTS/items persist —
            # the checklist stays visible until the next TodoWrite overwrites
            # it — mirroring the existing plan_done/total lifecycle; no reset
            # site is added.)
            self._todo_plan_seen_this_turn = False
            self._plan_seq_watermark = None
            self._reconcile_tick_locked()

    async def note_turn_end(self) -> None:
        """A CLI turn ended: stop the tick and perform the mandatory
        turn-end lifecycle flush (§5)."""
        async with self._writing():
            self._turn_running = False
            self._reconcile_tick_locked()
            await self._flush_locked(force=True)

    # -- pin (best-effort) --------------------------------------------------
    async def ensure_pinned(self) -> None:
        """Attempt to pin the summary (best-effort; §5). Retried on every
        lifecycle flush until it succeeds."""
        async with self._writing():
            await self._maybe_pin_locked()

    async def _maybe_pin_locked(self) -> None:
        if self._pin_message is None or self._message_id is None or self._pinned:
            return
        try:
            ok = bool(await self._pin_message(self._message_id))
        except Exception as exc:  # noqa: BLE001 — pin is best-effort
            ok = False
            logger.debug("summary pin raised (engagement %s): %s",
                         self.engagement_id, exc)
        if ok:
            self._pinned = True
        elif not self._pin_warned:
            self._pin_warned = True
            logger.warning(
                "engagement %s: could not pin the live summary message "
                "(best-effort; will retry on the next lifecycle flush)",
                self.engagement_id,
            )

    # -- tick (THE single sanctioned timer — §5 B1) -------------------------
    def _tick_should_run(self) -> bool:
        return self._status == STATUS_WORKING and self._turn_running

    def _reconcile_tick_locked(self) -> None:
        """Start/stop the elapsed tick to match the working+turn-running
        predicate (§5 B1). Caller holds the lock."""
        if self._tick_should_run():
            if self._tick_task is None or self._tick_task.done():
                self._tick_task = asyncio.create_task(self._tick_loop())
        else:
            self._cancel_tick_locked()

    def _cancel_tick_locked(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            self._tick_task = None

    async def _tick_body(self) -> bool:
        """One elapsed-refresh tick (§5 B1): submit ONLY an elapsed refresh
        (never status), routed through the same throttle/no-op gate. Returns
        ``False`` when no longer eligible (working+turn-running lapsed)."""
        async with self._writing():
            if not self._tick_should_run():
                return False
            await self._flush_locked(force=False)
            return True

    async def _tick_loop(self) -> None:  # pragma: no cover - exercised via _tick_body
        """THE single sanctioned timer in this design (§5 B1): one edit-eligible
        elapsed tick per ``tick_s`` (60s) while working and a turn is running.
        Uses the INJECTED ``_sleep`` — never the global ``asyncio.sleep`` (the
        module-local / injected-clock rule, CLAUDE.md memory cage)."""
        try:
            while True:
                await self._sleep(self._tick_s)
                if not await self._tick_body():
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "summary elapsed tick error (engagement %s): %s",
                self.engagement_id, exc,
            )

    def shutdown(self) -> None:
        """Cancel the tick (driver teardown). Idempotent."""
        self._cancel_tick_locked()

    # -- render + flush -----------------------------------------------------
    def _render_locked(self) -> str:
        elapsed_str = ""
        activity = None
        # Elapsed + the Now line are shown ONLY while working (a turn is
        # actively running); between turns / waiting there is no live activity.
        if self._status == STATUS_WORKING and self._turn_running and self._activity:
            activity = self._activity
            if self._turn_base is not None:
                elapsed_str = format_elapsed(self._now() - self._turn_base)
        open_qs = tuple(self._open_question_numbers() or ())
        return render_summary(
            goal_line=self._goal_line,
            status=self._status,
            plan_done=self._plan_done,
            plan_total=self._plan_total,
            plan_subject=self._plan_subject,
            plan_items=self._plan_items,
            plan_hidden_before=self._plan_hidden_before,
            plan_hidden_after=self._plan_hidden_after,
            activity=activity,
            elapsed_str=elapsed_str,
            open_qs=open_qs,
        )

    async def _flush_locked(self, *, force: bool) -> None:
        """Coalesce → throttle → edit (caller holds the lock).

        Edits are ≥``throttle_s`` apart EXCEPT when *force* (a status-class
        change or a mandatory lifecycle flush). Routes through the sequencer's
        NON-narration ``edit_summary`` (F1 no-op gate lives there too). On a
        FORCE flush the best-effort pin is retried (§5)."""
        if self._message_id is not None:
            text = self._render_locked()
            now = self._now()
            due = force or (now - self._last_edit_ts) >= self._throttle_s
            if due and text != self._last_rendered:
                res = await self._sequencer.edit_summary(self._message_id, text)
                if res != FAILED:
                    # #334: a FAILED edit reached nothing on the wire — leave
                    # the throttle timestamp untouched so the next non-forced
                    # flush retries immediately instead of sitting out a full
                    # window.
                    self._last_edit_ts = now
                    self._last_rendered = text
        if force:
            await self._maybe_pin_locked()
