"""Tests for ``drivers.summary_controller`` — the per-engagement live SUMMARY
controller (v0.79.0 §5, engagement-topic UX).

Every §5 sentence is binding. Time is INJECTED (``_now``/``_sleep``) so the
elapsed tick and the ≥10s throttle are deterministic and we never patch the
global ``asyncio.sleep`` (the OOM memory-cage rule).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest

from channels.output_sequencer import APPLIED, FAILED
from drivers.summary_controller import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_WAITING_APPROVAL,
    STATUS_WAITING_REPLY,
    STATUS_WORKING,
    SummaryController,
    activity_for_tool,
    extract_plan,
    format_elapsed,
    render_summary,
    sanitize_plan_subject,
)

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class FakeSequencer:
    """Records ``edit_summary`` calls; returns APPLIED (or FAILED on demand)."""

    def __init__(self) -> None:
        self.edits: list[tuple[int, str]] = []
        self.fail_next = 0

    @asynccontextmanager
    async def serialized(self):
        # GLOBAL LOCK-ORDER: the controller's ``_writing`` takes this OUTER lock
        # before the summary lock. A no-op CM suffices for these single-task tests.
        yield

    async def edit_summary(self, msg_id: int, text: str) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            return FAILED
        self.edits.append((msg_id, text))
        return APPLIED


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


async def _park(_dt: float) -> None:
    """A ``_sleep`` that parks forever (until the tick task is cancelled) — so
    the background tick loop never spins; we drive ``_tick_body`` directly."""
    await asyncio.Event().wait()


def _make(
    seq: FakeSequencer | None = None,
    *,
    clock: Clock | None = None,
    goal_line: str = "fix login bug",
    open_qs=(),
    pin=None,
    message_id: int | None = 500,
    throttle_s: float = 10.0,
    tick_s: float = 60.0,
) -> SummaryController:
    seq = seq or FakeSequencer()
    clock = clock or Clock()
    return SummaryController(
        engagement_id="eng123",
        sequencer=seq,
        goal_line=goal_line,
        open_question_numbers=lambda: list(open_qs),
        pin_message=pin,
        message_id=message_id,
        _now=clock.now,
        _sleep=_park,
        throttle_s=throttle_s,
        tick_s=tick_s,
    )


# ---------------------------------------------------------------------------
# Pure helpers — activity mapping, plan, elapsed, layout.
# ---------------------------------------------------------------------------


class TestActivityMapping:
    @pytest.mark.parametrize(
        "tool,expected",
        [
            ("Read", "reading files"),
            ("Glob", "reading files"),
            ("Grep", "reading files"),
            ("Write", "editing files"),
            ("Edit", "editing files"),
            ("NotebookEdit", "editing files"),
            ("Bash", "running commands"),
            ("Task", "planning"),
            ("TaskOutput", "planning"),
            ("TodoWrite", "planning"),
            ("WebFetch", "researching"),
            ("WebSearch", "researching"),
            ("mcp__ha-control__call_service", "using ha-control tools"),
            ("mcp__casa-framework__emit_completion", "using casa-framework tools"),
            ("SomethingElse", "working"),
            ("", "working"),
        ],
    )
    def test_coarse_mapping(self, tool, expected):
        assert activity_for_tool(tool) == expected

    def test_mcp_server_name_capped_at_32(self):
        # P1-B r4: the mcp server substring is capped at 32 chars AT SOURCE so
        # the status line cannot blow the 4096 wire bound.
        long = "s" * 100
        out = activity_for_tool(f"mcp__{long}__do")
        assert out == "using " + "s" * 32 + " tools"


class TestPlanSanitization:
    def test_unsafe_text_rejected(self):
        # A newline (C0) is UNSAFE — would break the single-line layout.
        assert sanitize_plan_subject("do a\nthing") == ""
        # Bidi override is UNSAFE.
        assert sanitize_plan_subject("safe‮text") == ""

    def test_sixty_char_cap(self):
        subj = "x" * 200
        assert len(sanitize_plan_subject(subj)) == 60

    def test_plain_subject_passthrough(self):
        assert sanitize_plan_subject("refactor auth") == "refactor auth"

    def test_non_str_and_empty(self):
        assert sanitize_plan_subject(None) == ""
        assert sanitize_plan_subject("") == ""

    def test_strips_markers_before_cap(self):
        # P1-B r3 pipeline order: control/bidi → strip ``*``/backticks → 60-cap.
        # ``'*'*60+'OAuth'`` must strip to ``OAuth`` (NOT be capped to 60 stars).
        assert sanitize_plan_subject("*" * 60 + "OAuth") == "OAuth"
        assert sanitize_plan_subject("`code`") == "code"
        # Marker-only text empties.
        assert sanitize_plan_subject("***") == ""

    def test_todowrite_counts_and_current(self):
        # P1-B r4: TodoWrite now yields the full-scan/bounded-window fragment —
        # counts, per-item window entries (label from ``content``), per-region
        # hidden buckets and the ``is_todo`` authority flag.
        plan = extract_plan(
            "TodoWrite",
            {
                "todos": [
                    {"content": "a", "status": "completed"},
                    {"content": "b", "status": "in_progress",
                     "activeForm": "doing b"},
                    {"content": "c", "status": "pending"},
                ]
            },
        )
        assert plan == {
            "done": 1,
            "total": 3,
            "subject": "b",
            "items": [
                {"ordinal": 1, "status": "done", "label": "a"},
                {"ordinal": 2, "status": "active", "label": "b"},
                {"ordinal": 3, "status": "pending", "label": "c"},
            ],
            "hidden_before": {"done": 0, "active": 0, "pending": 0},
            "hidden_after": {"done": 0, "active": 0, "pending": 0},
            "is_todo": True,
        }

    def test_todowrite_sanitizes_current(self):
        # An unsafe (newline) label empties → None (still counted); subject "".
        plan = extract_plan(
            "TodoWrite",
            {"todos": [{"content": "bad\nsubject", "status": "in_progress"}]},
        )
        assert plan["done"] == 0
        assert plan["total"] == 1
        assert plan["subject"] == ""
        assert plan["items"] == [
            {"ordinal": 1, "status": "active", "label": None}
        ]
        assert plan["is_todo"] is True

    def test_task_subject_only(self):
        # Task* is a display FALLBACK only (is_todo False) — no counts/items.
        plan = extract_plan("Task", {"description": "spin up a subagent"})
        assert plan == {"subject": "spin up a subagent", "is_todo": False}

    def test_non_plan_tool_returns_none(self):
        assert extract_plan("Bash", {"command": "ls"}) is None


class TestTodoWindowExtraction:
    """P1-B r4: full-scan / bounded-window TodoWrite extraction."""

    def test_active_item_visible_with_true_ordinal_of_100(self):
        # 100 todos: 0-78 completed, 79 in_progress, 80-99 pending. The active
        # item at ordinal 80 (index 79) is visible with its TRUE ordinal, and
        # retained state is O(window), not O(100).
        todos = (
            [{"content": f"t{i}", "status": "completed"} for i in range(79)]
            + [{"content": "the active one", "status": "in_progress"}]
            + [{"content": f"t{i}", "status": "pending"} for i in range(80, 100)]
        )
        plan = extract_plan("TodoWrite", {"todos": todos})
        assert plan["total"] == 100
        assert plan["done"] == 79
        ords = [it["ordinal"] for it in plan["items"]]
        # Window is [anchor-1 .. anchor+6] over index 79 → indices 78..85.
        assert ords == [79, 80, 81, 82, 83, 84, 85, 86]
        active = [it for it in plan["items"] if it["status"] == "active"]
        assert active == [{"ordinal": 80, "status": "active",
                           "label": "the active one"}]
        # Hidden regions: 78 done before (indices 0..77), 14 pending after.
        assert plan["hidden_before"] == {"done": 78, "active": 0, "pending": 0}
        assert plan["hidden_after"] == {"done": 0, "active": 0, "pending": 14}
        # Retained window is bounded regardless of plan size.
        assert len(plan["items"]) == 8

    def test_unknown_status_normalizes_to_pending(self):
        todos = [
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "weird"},
            {"content": "c"},  # missing status
        ]
        plan = extract_plan("TodoWrite", {"todos": todos})
        assert [it["status"] for it in plan["items"]] == [
            "done", "pending", "pending",
        ]
        # Anchor = first pending (the normalized 'weird' item at index 1).
        assert plan["subject"] == "b"

    def test_multiple_in_progress_sum_in_hidden_buckets(self):
        # Two in_progress far apart: the FIRST is the anchor; the second falls
        # into the hidden-after 'active' bucket (per-region attribution).
        todos = (
            [{"content": "a0", "status": "in_progress"}]
            + [{"content": f"p{i}", "status": "pending"} for i in range(20)]
            + [{"content": "a21", "status": "in_progress"}]
        )
        plan = extract_plan("TodoWrite", {"todos": todos})
        assert plan["items"][0] == {"ordinal": 1, "status": "active",
                                    "label": "a0"}
        assert plan["hidden_after"]["active"] == 1

    def test_all_pending_anchor_is_first(self):
        todos = [{"content": f"p{i}", "status": "pending"} for i in range(5)]
        plan = extract_plan("TodoWrite", {"todos": todos})
        # Anchor = first pending (index 0); window covers all 5.
        assert plan["subject"] == "p0"
        assert plan["items"][0] == {"ordinal": 1, "status": "pending",
                                    "label": "p0"}
        assert len(plan["items"]) == 5

    def test_all_completed_anchor_is_last(self):
        todos = [{"content": f"c{i}", "status": "completed"} for i in range(5)]
        plan = extract_plan("TodoWrite", {"todos": todos})
        assert plan["done"] == 5
        # Anchor = the TRUE last item (index 4), not a truncation point.
        assert plan["subject"] == "c4"
        assert plan["items"][-1] == {"ordinal": 5, "status": "done",
                                     "label": "c4"}

    def test_pending_before_active_promotes_anchor(self):
        # A pending precedes the first in_progress far apart: the single-pass
        # extraction PROMOTES the provisional pending anchor to the active one,
        # and the now-before entries are re-bucketed (r6 single-pass rewrite).
        todos = (
            [{"content": "p0", "status": "pending"}]
            + [{"content": f"d{i}", "status": "completed"} for i in range(1, 10)]
            + [{"content": "the active", "status": "in_progress"}]
        )
        plan = extract_plan("TodoWrite", {"todos": todos})
        # Anchor = the in_progress at index 10 → window [9 .. 10] (clamped end).
        assert plan["subject"] == "the active"
        ords = [it["ordinal"] for it in plan["items"]]
        assert ords == [10, 11]
        active = [it for it in plan["items"] if it["status"] == "active"]
        assert active == [{"ordinal": 11, "status": "active",
                           "label": "the active"}]
        # Before region: the pending (index 0) + dones 1..8 (index 9 is the
        # window head). 8 done + 1 pending.
        assert plan["hidden_before"] == {"done": 8, "active": 0, "pending": 1}
        assert plan["hidden_after"] == {"done": 0, "active": 0, "pending": 0}

    def test_pending_then_adjacent_active_keeps_both_in_window(self):
        # [pending, active]: promotion keeps the pending as the anchor−1 head.
        todos = [
            {"content": "first", "status": "pending"},
            {"content": "second", "status": "in_progress"},
        ]
        plan = extract_plan("TodoWrite", {"todos": todos})
        assert plan["subject"] == "second"
        assert plan["items"] == [
            {"ordinal": 1, "status": "pending", "label": "first"},
            {"ordinal": 2, "status": "active", "label": "second"},
        ]
        assert plan["hidden_before"] == {"done": 0, "active": 0, "pending": 0}

    def test_marker_only_label_becomes_none_but_still_counts(self):
        todos = [
            {"content": "***", "status": "completed"},   # sanitizes to empty
            {"content": "real", "status": "in_progress"},
        ]
        plan = extract_plan("TodoWrite", {"todos": todos})
        assert plan["total"] == 2
        assert plan["done"] == 1  # the marker-only DONE item still counts
        assert plan["items"][0] == {"ordinal": 1, "status": "done",
                                    "label": None}

    def test_empty_todos_is_zero_total(self):
        plan = extract_plan("TodoWrite", {"todos": []})
        assert plan["total"] == 0
        assert plan["items"] == []
        assert plan["is_todo"] is True


class TestElapsedFormat:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0s"),
            (5, "5s"),
            (59, "59s"),
            (60, "1m 00s"),
            (150, "2m 30s"),
            (3600, "1h 00m"),
            (3660, "1h 01m"),
        ],
    )
    def test_format(self, seconds, expected):
        assert format_elapsed(seconds) == expected


class TestLayoutExact:
    def test_status_only(self):
        assert render_summary(goal_line="", status=STATUS_WORKING) == "⚙️ working"

    def test_goal_and_status(self):
        # W-R2: STATUS FIRST, then the short title.
        assert render_summary(goal_line="fix bug", status=STATUS_WORKING) == (
            "⚙️ working\nfix bug"
        )

    def test_all_lines(self):
        # W-R2 exact layout: status FIRST with activity + elapsed MERGED
        # (` — <activity> · <elapsed>`), short title second, then Plan / Open.
        out = render_summary(
            goal_line="Gmail plugin",
            status=STATUS_WORKING,
            plan_done=2,
            plan_total=5,
            plan_subject="OAuth setup",
            activity="planning",
            elapsed_str="1m 00s",
            open_qs=(11,),
        )
        assert out == (
            "⚙️ working — planning · 1m 00s\n"
            "Gmail plugin\n"
            "Plan: 2/5 — current: OAuth setup\n"
            "Open questions: Q11"
        )

    def test_waiting_layout_exact(self):
        # W-R2 waiting block: status line carries NO activity/elapsed.
        out = render_summary(
            goal_line="Gmail plugin",
            status=STATUS_WAITING_REPLY,
            plan_done=2,
            plan_total=5,
            plan_subject="OAuth setup",
            open_qs=(11,),
        )
        assert out == (
            "⏳ waiting for your reply\n"
            "Gmail plugin\n"
            "Plan: 2/5 — current: OAuth setup\n"
            "Open questions: Q11"
        )

    def test_empty_lines_omitted(self):
        # No plan, no activity, no open questions → only status + title.
        out = render_summary(goal_line="fix bug", status=STATUS_WAITING_REPLY)
        assert out == "⏳ waiting for your reply\nfix bug"

    def test_plan_without_subject_omits_current(self):
        out = render_summary(
            goal_line="", status=STATUS_WORKING, plan_done=2, plan_total=5,
        )
        assert out == "⚙️ working\nPlan: 2/5"

    def test_activity_without_elapsed_merges_bare(self):
        # W-R2: activity merges onto the status line even without elapsed; no
        # ` · <elapsed>` tail and NO separate `Now:` line.
        out = render_summary(
            goal_line="", status=STATUS_WORKING, activity="reading files",
        )
        assert out == "⚙️ working — reading files"
        assert "Now:" not in out


class TestChecklistRender:
    """P1-B r4: the pinned-summary plan checklist render."""

    def _items(self, *specs):
        return [
            {"ordinal": o, "status": s, "label": lbl} for (o, s, lbl) in specs
        ]

    def test_marks_and_true_ordinals(self):
        out = render_summary(
            goal_line="Gmail plugin",
            status=STATUS_WORKING,
            plan_done=1,
            plan_total=3,
            plan_items=self._items(
                (1, "done", "OAuth setup"),
                (2, "active", "wire callback"),
                (3, "pending", "write tests"),
            ),
        )
        assert out == (
            "⚙️ working\n"
            "Gmail plugin\n"
            "Plan: 1/3\n"
            "☑ 1. OAuth setup\n"
            "▶ 2. wire callback\n"
            "☐ 3. write tests"
        )
        # The redundant ` — current:` clause is dropped when the checklist renders.
        assert "current:" not in out

    def test_none_label_renders_dash(self):
        out = render_summary(
            goal_line="",
            status=STATUS_WORKING,
            plan_done=0,
            plan_total=2,
            plan_items=self._items((1, "active", None), (2, "pending", "next")),
        )
        assert "▶ 1. —" in out
        assert "☐ 2. next" in out

    def test_presence_keyed_on_total_not_labels(self):
        # A marker-only plan (all labels None) still renders — presence keys on
        # total > 0, never on labels.
        out = render_summary(
            goal_line="g",
            status=STATUS_WORKING,
            plan_done=0,
            plan_total=2,
            plan_items=self._items((1, "active", None), (2, "pending", None)),
        )
        assert "Plan: 0/2" in out
        assert "▶ 1. —" in out
        assert "☐ 2. —" in out

    def test_hidden_count_lines_with_breakdown(self):
        out = render_summary(
            goal_line="",
            status=STATUS_WORKING,
            plan_done=80,
            plan_total=100,
            plan_items=self._items(
                (79, "done", "a"), (80, "active", "b"), (81, "pending", "c"),
            ),
            plan_hidden_before={"done": 78, "active": 0, "pending": 0},
            plan_hidden_after={"done": 0, "active": 1, "pending": 18},
        )
        assert "… 78 earlier — 78 done, 0 pending" in out
        # The active bucket renders when nonzero.
        assert "… 19 more — 0 done, 1 active, 18 pending" in out

    def test_small_plan_no_hidden_lines(self):
        out = render_summary(
            goal_line="",
            status=STATUS_WORKING,
            plan_done=1,
            plan_total=2,
            plan_items=self._items((1, "done", "a"), (2, "active", "b")),
            plan_hidden_before={"done": 0, "active": 0, "pending": 0},
            plan_hidden_after={"done": 0, "active": 0, "pending": 0},
        )
        assert "earlier" not in out
        assert "more" not in out

    def test_goal_line_capped_at_200(self):
        out = render_summary(
            goal_line="G" * 5000, status=STATUS_WORKING,
        )
        goal = out.splitlines()[1]
        assert goal == "G" * 200 + "…"

    def test_open_questions_collapse_beyond_three(self):
        out = render_summary(
            goal_line="", status=STATUS_WAITING_REPLY,
            open_qs=(1, 2, 3, 4, 5),
        )
        assert "⏳ 5 open questions" in out
        assert "Open questions:" not in out
        # Three or fewer stay verbatim.
        out3 = render_summary(
            goal_line="", status=STATUS_WAITING_REPLY, open_qs=(1, 2, 3),
        )
        assert "Open questions: Q1, Q2, Q3" in out3

    def test_goal_flood_does_not_evict_checklist(self):
        out = render_summary(
            goal_line="G" * 9000,
            status=STATUS_WORKING,
            plan_done=1,
            plan_total=3,
            plan_items=self._items(
                (1, "done", "a"), (2, "active", "b"), (3, "pending", "c"),
            ),
        )
        assert len(out) <= 4096
        assert "▶ 2. b" in out  # checklist survives the goal flood

    def test_unconditional_4096_truncation(self):
        # An adversarially long activity is not capped at source here, so the
        # final whole-payload truncation is the hard wire bound.
        out = render_summary(
            goal_line="g", status=STATUS_WORKING, activity="x" * 10000,
        )
        assert len(out) <= 4096


# ---------------------------------------------------------------------------
# Authority model (§5).
# ---------------------------------------------------------------------------


class TestAuthorityModel:
    async def test_newer_revision_lowers_rank(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        assert c._status == STATUS_WAITING_REPLY
        # A LATER answer legitimately drops the rank waiting → working.
        await c.submit_status(STATUS_WORKING, 6)
        assert c._status == STATUS_WORKING

    async def test_older_or_equal_never_overrides(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        await c.submit_status(STATUS_WORKING, 5)   # equal
        assert c._status == STATUS_WAITING_REPLY
        await c.submit_status(STATUS_WORKING, 3)   # older
        assert c._status == STATUS_WAITING_REPLY

    async def test_late_replayed_frame_cannot_overwrite_newer_waiting(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        # A stale/late working transition (revision 2) must NOT win.
        await c.submit_status(STATUS_WORKING, 2)
        assert c._status == STATUS_WAITING_REPLY

    async def test_terminal_absolute(self):
        c = _make()
        await c.finalize(STATUS_COMPLETED)
        assert c._status == STATUS_COMPLETED
        await c.submit_status(STATUS_WORKING, 999)
        assert c._status == STATUS_COMPLETED

    async def test_none_revision_rejected(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        await c.submit_status(STATUS_WORKING, None)
        assert c._status == STATUS_WAITING_REPLY

    async def test_activity_never_changes_status(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        await c.note_turn_start()  # turn running, but status is set by lifecycle
        await c.submit_activity("running commands")
        assert c._status == STATUS_WAITING_REPLY
        c.shutdown()


# ---------------------------------------------------------------------------
# Throttle + immediate status flush + lifecycle flush (§5).
# ---------------------------------------------------------------------------


class TestThrottleAndFlush:
    async def test_status_change_flushes_immediately_despite_throttle(self):
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()
        await c.submit_activity("reading files")  # edit #1 (last_edit=-inf)
        n = len(seq.edits)
        clock.t = 1.0  # only 1s later — inside the 10s throttle window
        await c.submit_status(STATUS_WAITING_REPLY, 5)  # status change → force
        assert len(seq.edits) == n + 1
        # W-R2: status is now the FIRST line.
        assert seq.edits[-1][1].splitlines()[0] == "⏳ waiting for your reply"
        c.shutdown()

    async def test_activity_coalesces_within_throttle_window(self):
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()
        await c.submit_activity("reading files")   # edit #1
        clock.t = 2.0
        await c.submit_activity("running commands")  # throttled (2s < 10s)
        clock.t = 5.0
        await c.submit_activity("editing files")     # throttled
        assert len(seq.edits) == 1  # at most one edit per throttle window
        c.shutdown()

    async def test_turn_end_forces_lifecycle_flush(self):
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()
        await c.submit_activity("reading files")  # edit #1
        clock.t = 1.0
        await c.note_turn_end()  # mandatory lifecycle flush (force)
        # The Now line is dropped (turn no longer running).
        assert "Now:" not in seq.edits[-1][1]
        assert len(seq.edits) == 2
        c.shutdown()

    async def test_failed_edit_not_cached_as_rendered(self):
        seq = FakeSequencer()
        seq.fail_next = 1
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()
        await c.submit_status(STATUS_WAITING_REPLY, 5)  # force → FAILED
        assert seq.edits == []            # nothing recorded (the edit failed)
        assert c._last_rendered is None   # not cached → a retry will re-attempt
        c.shutdown()

    async def test_failed_edit_does_not_consume_throttle_window(self):
        """#334: a FAILED edit put nothing on the wire, so it must not start a
        throttle window — the next non-forced flush is immediately due."""
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()
        await c.submit_activity("reading files")   # edit #1 lands
        clock.t = 11.0                             # past the throttle window
        seq.fail_next = 1
        await c.submit_activity("running commands")  # attempt FAILS (transient)
        assert len(seq.edits) == 1
        clock.t = 12.0                             # only 1s after the failure
        await c.submit_activity("editing files")   # must retry NOW, not at t=21
        assert len(seq.edits) == 2
        c.shutdown()

    async def test_no_edit_without_message_id(self):
        seq = FakeSequencer()
        c = _make(seq, message_id=None)
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        assert seq.edits == []
        c.shutdown()


# ---------------------------------------------------------------------------
# Elapsed tick lifecycle (§5 B1).
# ---------------------------------------------------------------------------


class TestTickLifecycle:
    async def test_tick_starts_on_working_turn(self):
        c = _make()
        await c.note_turn_start()  # default status working + turn running
        assert c._tick_task is not None
        c.shutdown()

    async def test_tick_stops_on_turn_end(self):
        c = _make()
        await c.note_turn_start()
        await c.note_turn_end()
        assert c._tick_task is None

    async def test_tick_stops_when_status_leaves_working(self):
        c = _make()
        await c.note_turn_start()
        assert c._tick_task is not None
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        assert c._tick_task is None
        c.shutdown()

    async def test_tick_stops_on_finalize(self):
        c = _make()
        await c.note_turn_start()
        await c.finalize(STATUS_COMPLETED)
        assert c._tick_task is None

    async def test_tick_advances_elapsed_no_spam(self):
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()            # base = 0
        await c.submit_activity("running commands")  # edit @ elapsed 0s
        first = seq.edits[-1][1]
        # W-R2: activity + elapsed are MERGED onto the status line.
        assert "⚙️ working — running commands · 0s" in first
        # One productive tick per 60s.
        clock.t = 60.0
        assert await c._tick_body() is True
        assert "⚙️ working — running commands · 1m 00s" in seq.edits[-1][1]
        n = len(seq.edits)
        # A second tick at the SAME clock produces no new edit (elapsed
        # unchanged + throttle) — no edit spam.
        assert await c._tick_body() is True
        assert len(seq.edits) == n
        # Next minute → one more edit.
        clock.t = 120.0
        assert await c._tick_body() is True
        assert "⚙️ working — running commands · 2m 00s" in seq.edits[-1][1]
        assert len(seq.edits) == n + 1
        c.shutdown()

    async def test_tick_body_not_eligible_when_not_working(self):
        c = _make()
        await c.note_turn_start()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        assert await c._tick_body() is False
        c.shutdown()


# ---------------------------------------------------------------------------
# Pin probe / WARN / retry (§5).
# ---------------------------------------------------------------------------


class TestPin:
    async def test_pin_success(self):
        calls = []

        async def pin(mid):
            calls.append(mid)
            return True

        c = _make(pin=pin)
        await c.ensure_pinned()
        assert calls == [500]
        assert c._pinned is True

    async def test_pin_failure_warns_once_then_retries_on_lifecycle_flush(
        self, caplog,
    ):
        results = [False, True]

        async def pin(mid):
            return results.pop(0)

        c = _make(pin=pin)
        with caplog.at_level(logging.WARNING):
            await c.ensure_pinned()          # fails → WARN once
        assert c._pinned is False
        assert c._pin_warned is True
        warns = [r for r in caplog.records if "could not pin" in r.message]
        assert len(warns) == 1
        # A lifecycle (force) flush retries the pin — now succeeds.
        await c.note_turn_end()
        assert c._pinned is True
        c.shutdown()

    async def test_pin_warns_only_once_across_retries(self, caplog):
        async def pin(mid):
            return False

        c = _make(pin=pin)
        with caplog.at_level(logging.WARNING):
            await c.ensure_pinned()
            await c.note_turn_end()
            await c.note_turn_end()
        warns = [r for r in caplog.records if "could not pin" in r.message]
        assert len(warns) == 1
        c.shutdown()

    async def test_no_pin_primitive_is_noop(self):
        c = _make(pin=None)
        await c.ensure_pinned()  # must not raise
        assert c._pinned is False
        c.shutdown()


# ---------------------------------------------------------------------------
# Open-questions rendering from the T3 accessor (§5).
# ---------------------------------------------------------------------------


class TestOpenQuestions:
    async def test_open_questions_rendered_from_accessor(self):
        seq = FakeSequencer()
        nums = [4, 6]
        c = SummaryController(
            engagement_id="e",
            sequencer=seq,
            goal_line="g",
            open_question_numbers=lambda: list(nums),
            message_id=7,
            _now=Clock().now,
            _sleep=_park,
        )
        await c.submit_status(STATUS_WAITING_REPLY, 1)
        assert "Open questions: Q4, Q6" in seq.edits[-1][1]
        c.shutdown()

    async def test_no_open_questions_line_when_empty(self):
        seq = FakeSequencer()
        c = _make(seq, open_qs=())
        await c.submit_status(STATUS_WAITING_REPLY, 1)
        assert "Open questions:" not in seq.edits[-1][1]
        c.shutdown()


class TestF1SameStatusOpenQuestionRefresh:
    """F1 (Sol diff gate): a SAME-status revision bump whose OPEN-QUESTIONS set
    changed must still re-render + flush the pinned summary — the no-op gate is
    keyed on the RENDERED TEXT, not the status enum."""

    async def test_settle_one_of_two_reflows_open_questions(self):
        seq = FakeSequencer()
        nums = [11, 12]
        c = _make(seq, message_id=500)
        c._open_question_numbers = lambda: list(nums)
        await c.submit_status(STATUS_WAITING_REPLY, 1)
        assert "Open questions: Q11, Q12" in seq.edits[-1][1]
        # Q11 settles; still ⏳ waiting on Q12 (same status, higher revision).
        nums[:] = [12]
        await c.submit_status(STATUS_WAITING_REPLY, 2)
        assert "Open questions: Q12" in seq.edits[-1][1]
        assert "Q11" not in seq.edits[-1][1]
        c.shutdown()

    async def test_add_question_while_waiting_reflows(self):
        seq = FakeSequencer()
        nums = [11]
        c = _make(seq, message_id=500)
        c._open_question_numbers = lambda: list(nums)
        await c.submit_status(STATUS_WAITING_REPLY, 1)
        assert "Open questions: Q11" in seq.edits[-1][1]
        # A new question is registered while already ⏳ waiting.
        nums[:] = [11, 12]
        await c.submit_status(STATUS_WAITING_REPLY, 2)
        assert "Open questions: Q11, Q12" in seq.edits[-1][1]
        c.shutdown()

    async def test_refresh_reflows_without_status_transition(self):
        # The explicit refresh path (driver recompute / boot reconcile) reflows
        # the open-questions line with NO status change at all.
        seq = FakeSequencer()
        nums = [11, 12]
        c = _make(seq, message_id=500)
        c._open_question_numbers = lambda: list(nums)
        await c.submit_status(STATUS_WAITING_REPLY, 1)
        nums[:] = []  # all settled
        await c.refresh()
        assert "Open questions:" not in seq.edits[-1][1]
        c.shutdown()


# ---------------------------------------------------------------------------
# Waiting-for-approval status copy is available (ask-registry source).
# ---------------------------------------------------------------------------


async def test_waiting_for_approval_copy():
    seq = FakeSequencer()
    c = _make(seq)
    await c.submit_status(STATUS_WAITING_APPROVAL, 1)
    # W-R2: status is the FIRST line. (The approval-status ask WIRING stays
    # deferred in v0.81 — this only asserts the copy constant is renderable.)
    assert seq.edits[-1][1].splitlines()[0] == "🔐 waiting for your approval"
    c.shutdown()


async def test_terminal_copies():
    assert render_summary(goal_line="", status=STATUS_CANCELLED) == "🛑 cancelled"
    assert render_summary(goal_line="", status=STATUS_ERROR) == "⚠️ error"
    assert render_summary(goal_line="", status=STATUS_COMPLETED) == "✅ completed"


# ---------------------------------------------------------------------------
# P1-B: plan checklist — Todo authority latch, ordering watermark, end-to-end.
# ---------------------------------------------------------------------------


def _todo_frag(**over):
    """A minimal accepted-TodoWrite fragment as produced by ``extract_plan``."""
    frag = {
        "done": 0,
        "total": 2,
        "subject": "a",
        "items": [
            {"ordinal": 1, "status": "active", "label": "a"},
            {"ordinal": 2, "status": "pending", "label": "b"},
        ],
        "hidden_before": {"done": 0, "active": 0, "pending": 0},
        "hidden_after": {"done": 0, "active": 0, "pending": 0},
        "is_todo": True,
    }
    frag.update(over)
    return frag


class TestTodoAuthorityLatch:
    async def test_todo_latch_blocks_task_subject_mutation(self):
        c = _make()
        await c.submit_plan(**_todo_frag(subject="from todo"))
        # A later Task* (fallback) fragment must NOT mutate the latched plan.
        await c.submit_plan(subject="from task", is_todo=False)
        assert c._plan_subject == "from todo"
        assert c._plan_total == 2
        c.shutdown()

    async def test_task_subject_used_only_while_unlatched(self):
        c = _make()
        # No TodoWrite yet → a Task* subject IS the display fallback.
        await c.submit_plan(subject="task subj", is_todo=False)
        assert c._plan_subject == "task subj"
        c.shutdown()

    async def test_latch_blocks_task_after_empty_todowrite(self):
        c = _make()
        await c.submit_plan(**_todo_frag())
        # A later TodoWrite([]) keeps the latch (is_todo True), and a Task*
        # afterward still cannot mutate.
        await c.submit_plan(
            done=0, total=0, subject="", items=[],
            hidden_before={"done": 0, "active": 0, "pending": 0},
            hidden_after={"done": 0, "active": 0, "pending": 0},
            is_todo=True,
        )
        await c.submit_plan(subject="sneaky", is_todo=False)
        assert c._plan_subject == ""
        assert c._plan_total == 0
        c.shutdown()


class TestPlanOrderingWatermark:
    async def test_stale_relay_coordinate_rejected(self):
        # Relay path (r6 fix): seq = monotonic per-turn tool-event counter.
        c = _make()
        await c.submit_plan(**_todo_frag(subject="new"), seq=4)
        await c.submit_plan(**_todo_frag(subject="stale"), seq=2)
        assert c._plan_subject == "new"  # stale-after-new rejected
        c.shutdown()

    async def test_two_todowrite_blocks_one_frame_distinct_seq(self):
        # Two TodoWrite blocks in ONE assistant frame get distinct, strictly
        # increasing counter seqs (r6 fix); the SECOND wins.
        c = _make()
        await c.submit_plan(**_todo_frag(subject="first"), seq=1)
        await c.submit_plan(**_todo_frag(subject="second"), seq=2)
        assert c._plan_subject == "second"
        c.shutdown()

    async def test_stale_in_casa_counter_rejected(self):
        # In-casa driver path: seq is a monotonic per-turn integer counter —
        # SAME reject-≤ contract.
        c = _make()
        await c.submit_plan(**_todo_frag(subject="c5"), seq=5)
        await c.submit_plan(**_todo_frag(subject="c3"), seq=3)
        assert c._plan_subject == "c5"
        c.shutdown()

    async def test_watermark_resets_at_turn_boundary(self):
        # The watermark resets with the plan lifecycle at the turn boundary, so
        # a fresh turn's first frame (any seq) is accepted.
        c = _make()
        await c.submit_plan(**_todo_frag(subject="t1"), seq=99)
        await c.note_turn_start()  # fresh turn → watermark + latch reset
        await c.submit_plan(**_todo_frag(subject="t2"), seq=1)
        assert c._plan_subject == "t2"
        c.shutdown()

    async def test_turn_boundary_resets_latch(self):
        c = _make()
        await c.submit_plan(**_todo_frag(subject="latched"), seq=1)
        await c.note_turn_start()  # fresh turn → latch cleared
        await c.submit_plan(subject="task now allowed", is_todo=False, seq=2)
        assert c._plan_subject == "task now allowed"
        c.shutdown()


class TestChecklistEndToEnd:
    async def test_todowrite_driven_checklist_across_two_frames(self):
        # Two TodoWrite frames through the REAL controller + REAL renderer: the
        # ☑/▶ marks and true ordinals reach the pinned summary edit.
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock, message_id=500)
        # Frame 1: item 1 active.
        plan1 = extract_plan("TodoWrite", {"todos": [
            {"content": "OAuth setup", "status": "in_progress"},
            {"content": "wire callback", "status": "pending"},
        ]})
        await c.submit_plan(**plan1, seq=1)
        assert "▶ 1. OAuth setup" in seq.edits[-1][1]
        # Frame 2 (past the throttle window): item 1 done, item 2 active.
        clock.t += 11.0
        plan2 = extract_plan("TodoWrite", {"todos": [
            {"content": "OAuth setup", "status": "completed"},
            {"content": "wire callback", "status": "in_progress"},
        ]})
        await c.submit_plan(**plan2, seq=2)
        rendered = seq.edits[-1][1]
        assert "☑ 1. OAuth setup" in rendered
        assert "▶ 2. wire callback" in rendered
        c.shutdown()

    async def test_hostile_markup_through_real_render(self):
        # `'*'*60+'OAuth'` sanitizes to `OAuth` end-to-end; no stray markers in
        # the rendered checklist label.
        seq = FakeSequencer()
        c = _make(seq, message_id=500)
        plan = extract_plan("TodoWrite", {"todos": [
            {"content": "*" * 60 + "OAuth", "status": "in_progress"},
        ]})
        await c.submit_plan(**plan, seq=1)
        rendered = seq.edits[-1][1]
        assert "▶ 1. OAuth" in rendered
        assert "*" not in rendered
        c.shutdown()


class TestActivityAndPlanSameFlush:
    async def test_todowrite_renders_checklist_in_same_flush(self):
        # r6 fix: a tool_use carrying a TodoWrite applies BOTH the activity and
        # the plan under ONE lock with ONE flush — the checklist is visible in
        # the SAME (first) edit, not deferred to a later event/tick. Earlier, a
        # prior activity flush consumed the throttle window and starved it.
        seq = FakeSequencer()
        c = _make(seq, message_id=500)
        plan = extract_plan("TodoWrite", {"todos": [
            {"content": "OAuth setup", "status": "in_progress"},
        ]})
        await c.submit_activity_and_plan("planning", **plan, seq=1)
        assert len(seq.edits) == 1  # ONE flush
        rendered = seq.edits[-1][1]
        assert "▶ 1. OAuth setup" in rendered  # checklist in the SAME flush
        c.shutdown()

    async def test_stale_plan_still_applies_activity(self):
        # A stale plan seq is rejected, but the activity ALWAYS applies (the
        # combined op never drops the activity update).
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock, message_id=500)
        await c.submit_plan(**_todo_frag(subject="latched"), seq=5)
        first_subject = c._plan_subject
        clock.t += 11.0  # clear the throttle so the next flush lands
        await c.submit_activity_and_plan(
            "running commands", **_todo_frag(subject="stale"), seq=2)
        assert c._plan_subject == first_subject  # stale plan rejected
        assert c._activity == "running commands"  # activity still applied
        c.shutdown()
