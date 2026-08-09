"""Task 9 / v0.83.0 §A3(b) — anchor re-anchor: staged flow, boundary-complete
latch, retry owner, terminal-finalize settle, boot reconcile owner (F-ORDER).

Enforces the operator invariant *"once asked, the free-text question stays the
LAST item until answered"*. A doctrine-defying agent keeps narrating below its
own open anchor; at every turn boundary the driver re-anchors the oldest
unanswered anchor via the 4-step staged flow (stage → post_discrete → persist
mid → settle old copy). A per-engagement latch carries the obligation across
abnormal / idle boundaries; a bounded-backoff retry owner completes it when a
boundary is the last event; terminal finalize SETTLES instead of re-anchoring;
and a boot reconciliation owner settles refused/terminal records after Telegram
readiness.

REAL registry over a tmp tombstone + REAL OutputSequencer (fake wire fns) +
REAL driver methods. Injected clocks; NEVER patches ``<module>.asyncio.sleep``
(the shared attribute — the memory-cage rule)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------


def _anchor_tmp_allocator(tmp_path):
    """A reconstructed ``UidAllocator`` on throwaway tmp counter/passwd/group
    files (Containment Stage 2, Task 10) so boot replay can allocate a real uid
    for the resumed claude_code record without touching the real ``/etc``."""
    import os
    from engagement_uids import UidAllocator

    d = tmp_path / "uidalloc"
    d.mkdir(exist_ok=True)
    passwd = str(d / "passwd")
    group = str(d / "group")
    open(passwd, "w").close()
    open(group, "w").close()
    alloc = UidAllocator(
        str(d / "counter.json"), passwd_path=passwd, group_path=group)
    alloc.reconstruct(known_uids=[], dir_owner_uids=[])
    return alloc


class _Wire:
    """One ordered fake wire backing BOTH the text send (``send_to_topic`` /
    platform notices) and the markup send (``post_discrete``), so message
    ordering is a single monotonic mid sequence. ``markup_ok=False`` makes
    ``post_discrete`` fail (wire down); ``edit_ok=False`` makes every settle
    edit transiently fail (unconfirmed).

    v0.84.0 (D3): counts physical send/edit calls (``send_markup_calls`` /
    ``edit_markup_calls``) and exposes optional per-stage GATES
    (``send_block`` / ``edit_block`` — ``asyncio.Event``\\ s awaited BEFORE the
    op proceeds) so the drained-unit cancellation + blocked-wire tests can
    suspend a real wire op at a controlled point. ``send_raises`` records the
    acceptance THEN raises (the accepted-send-then-raises floor)."""

    def __init__(self, *, markup_ok: bool = True, edit_ok: bool = True,
                 start: int = 1000):
        self._n = start
        self.posts: list[tuple[str, int, str]] = []   # (kind, mid, text)
        self.edits: list[tuple[str, int, str]] = []    # (kind, mid, text)
        self.markup_ok = markup_ok
        self.edit_ok = edit_ok
        self.send_markup_calls = 0
        self.edit_markup_calls = 0
        self.send_block: asyncio.Event | None = None
        self.edit_block: asyncio.Event | None = None
        self.send_raises = False

    def _mid(self) -> int:
        self._n += 1
        return self._n

    async def send_text(self, topic, text, **kw) -> int:
        mid = self._mid()
        self.posts.append(("text", mid, text))
        return mid

    async def send_markup(self, topic, text, markup, reply_to=None):
        self.send_markup_calls += 1
        if self.send_block is not None:
            await self.send_block.wait()
        if self.send_raises:
            # Accepted-send-then-raises: the copy IS on the wire (recorded),
            # then the wrapper read fails — an AMBIGUOUS outcome.
            self.posts.append(("markup", self._mid(), text))
            raise RuntimeError("wire ack read failed after send")
        if not self.markup_ok:
            return None
        mid = self._mid()
        self.posts.append(("markup", mid, text))
        return mid

    async def edit_text(self, topic, mid, text, clear_keyboard=False) -> bool:
        self.edits.append(("text", mid, text))
        return self.edit_ok

    async def edit_markup(self, topic, mid, text, markup) -> bool:
        self.edit_markup_calls += 1
        if self.edit_block is not None:
            await self.edit_block.wait()
        self.edits.append(("markup", mid, text))
        return self.edit_ok

    def post_mids(self) -> list[int]:
        return [mid for _, mid, _ in self.posts]

    def edit_mids(self) -> list[int]:
        return [mid for _, mid, _ in self.edits]


async def _noop_sleep(_delay: float) -> None:
    return None


async def _make_registry(tmp_path: Path, *, status: str = "active"):
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 77}, topic_id=555)
    if status != "active":
        # Drive a terminal/idle status directly for the boot-owner tests.
        rec.status = status
    return reg, rec


async def _add_anchor(reg, rec, *, mid, text="Q1: DB name?"):
    n = await reg.allocate_question_number(rec.id)
    await reg.add_open_question(rec.id, n, mid, text=text, kind="anchor")
    return n


def _make_driver(tmp_path: Path, reg, wire, *, retry_sleep=None):
    from drivers.claude_code_driver import ClaudeCodeDriver

    return ClaudeCodeDriver(
        engagements_root=str(tmp_path / "eng"),
        send_to_topic=wire.send_text,
        casa_framework_mcp_url="http://x",
        edit_topic_message=wire.edit_text,
        send_topic_message_markup=wire.send_markup,
        edit_topic_message_markup=wire.edit_markup,
        registry=reg,
        sleep=_noop_sleep,
        reanchor_retry_sleep=retry_sleep,
    )


def _entry(reg, rec, n):
    for q in reg.open_question_entries(rec.id):
        if q.get("n") == n:
            return q
    return None


def _norm(entry):
    from engagement_registry import normalize_stale_mid_entry
    return normalize_stale_mid_entry(entry)


# ===========================================================================
# 1. the staged 4-step re-anchor happy path
# ===========================================================================


class TestStagedFlowHappyPath:
    async def test_reanchor_moves_old_copy_marker_only_and_settles(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600  # narration posted below the anchor this turn

        ok = await drv._reanchor_pass(rec)

        assert ok is True
        # New copy posted through the sequencer (markup wire), high-water advanced.
        assert len(wire.posts) == 1 and wire.posts[0][0] == "markup"
        new_mid = wire.posts[0][1]
        assert seq.high_water == new_mid
        # Ledger now tracks the new copy; stale_mids emptied after the confirmed
        # old-copy settle.
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == new_mid
        assert e["stale_mids"] == []
        # v0.84.0 (round-4 §D6): the old copy settles to the EXACT MOVED-open
        # marker — marker-ONLY, never the duplicated question body.
        moved_edits = [
            text for _, mid, text in wire.edits if mid == 500
        ]
        assert moved_edits == [f"⤵ MOVED Q{n} — answer the current copy below"]

    async def test_already_last_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=700)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 700  # anchor IS the last item

        ok = await drv._reanchor_pass(rec)

        assert ok is True
        assert wire.posts == [] and wire.edits == []

    async def test_no_open_anchor_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 900

        assert await drv._reanchor_pass(rec) is True
        assert wire.posts == []


# ===========================================================================
# 2. per-step persist-failure injection (spec §A3(b) steps 1-4)
# ===========================================================================


class TestStagedFlowPerStepFailures:
    async def _setup(self, tmp_path, **wire_kw):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire(**wire_kw)
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        return reg, rec, n, wire, drv, seq

    async def test_step1_stage_failure_nothing_on_wire(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        reg.stage_stale_mid = AsyncMock(side_effect=RuntimeError("disk"))

        ok = await drv._reanchor_pass(rec)

        assert ok is False               # retry owed
        assert wire.posts == []          # nothing on the wire
        assert _entry(reg, rec, n)["tg_message_id"] == 500  # original intact

    async def test_step2_ambiguous_send_consumes_obligation_no_retry(self, tmp_path):
        # v0.84.0 (§D6 r17-3): a wire failure returns None, INDISTINGUISHABLE
        # from "accepted but no mid returned" — so it is an AMBIGUOUS send:
        # exactly ONE attempt, NO wire retry (a retry could stack an untracked
        # copy), obligation CONSUMED for this pass (True). The old copy stays
        # current + tracked; the staged mid is best-effort un-staged.
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path, markup_ok=False)

        ok = await drv._reanchor_pass(rec)

        assert ok is True                     # obligation consumed (no retry)
        assert wire.posts == []               # post_discrete returned None
        assert wire.send_markup_calls == 1    # exactly ONE physical send attempt
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == 500      # old copy stays current
        assert e["stale_mids"] == []          # staged mid un-staged

    async def test_step3_persist_exhaustion_no_body_edit_orphan_accepted(
            self, tmp_path):
        # v0.84.0 (§D6 r11-1/r17): the '↪ see above' body edit is DELETED. On
        # persist exhaustion (N in-unit attempts all fail) the D4 stopgap fires:
        # NO body edit (the new copy's body is untouched), obligation CONSUMED
        # (True). The old copy stays durably current + tracked; new_mid is an
        # accepted untracked orphan.
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        from drivers import claude_code_driver as ccd
        persist_calls = 0
        orig_update = reg.update_question_mid

        async def _always_fail(*a, **k):
            nonlocal persist_calls
            persist_calls += 1
            raise RuntimeError("registry disk down")

        reg.update_question_mid = _always_fail

        ok = await drv._reanchor_pass(rec)

        assert ok is True                     # obligation consumed (D4 stopgap)
        assert persist_calls == ccd._REANCHOR_PERSIST_ATTEMPTS   # N attempts
        new_mid = wire.posts[0][1]
        # NO body edit of the new copy at all (see-above hack gone), and NO
        # marker edit of the old copy (step 4 never reached without a commit).
        assert wire.edits == []
        assert "see the question above" not in " ".join(t for _, _, t in wire.edits)
        # Persist never committed: the ledger still points at the old copy, and
        # the staged entry stays "plain" (the flip lives inside the same failed
        # transaction).
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == 500
        assert e["stale_mids"] == [{"mid": 500, "kind": "plain"}]

    async def test_step4_unconfirmed_retains_stale_mid_but_obligation_met(
            self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path, edit_ok=False)

        ok = await drv._reanchor_pass(rec)

        assert ok is True                # obligation met (question now LAST)
        new_mid = wire.posts[0][1]
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == new_mid      # step-3 persisted
        # step-4 unconfirmed → retained; D2's atomic flip in
        # update_question_mid already marked it "reanchored" at step 3.
        assert e["stale_mids"] == [{"mid": 500, "kind": "reanchored"}]


# ===========================================================================
# 3. unconfirmed step-4 → boot reconcile settles BOTH mids
# ===========================================================================


class TestUnconfirmedStep4Reconcile:
    async def test_reconcile_settles_current_and_stale(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire(edit_ok=False)
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        await drv._reanchor_pass(rec)             # step-4 unconfirmed
        new_mid = wire.posts[0][1]
        assert _entry(reg, rec, n)["stale_mids"] == [{"mid": 500, "kind": "reanchored"}]

        # "Boot": the transport recovers, reconcile settles every tracked copy.
        wire.edit_ok = True
        snapshot = reg.open_question_entries(rec.id)
        await drv.reconcile_open_questions(rec, snapshot)

        settled = set(wire.edit_mids())
        assert 500 in settled and new_mid in settled   # both mids settled
        assert reg.open_question_entries(rec.id) == []  # entry removed


# ===========================================================================
# 4. mid-re-anchor answer → revalidate declines → un-stage, no post
# ===========================================================================


class TestMidReanchorAnswer:
    async def test_answer_during_stage_declines_send(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600

        # The operator answer lands DURING the awaited step-1 stage: reserve the
        # anchor right after the stale-mid is staged.
        orig_stage = reg.stage_stale_mid

        async def _staging(*a, **k):
            r = await orig_stage(*a, **k)
            drv.reserve_answer(rec.id)   # answer arrives mid-stage
            return r

        reg.stage_stale_mid = _staging

        ok = await drv._reanchor_pass(rec)

        assert ok is True                # the answer won — nothing owed
        assert wire.posts == []          # revalidation declined the send
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == 500          # original copy intact
        assert e["stale_mids"] == []              # staged mid un-staged
        # The question stays reserved/answered (invisible to gates/re-anchor).
        assert drv._effective_open_question_numbers(rec.id) == []


# ===========================================================================
# 4b. B2 — the Sol interleaving: an answer settlement racing a re-anchor caught
#     between step-2 (new copy posted) and step-3 (mid persisted) must serialize
#     behind the maintenance lock and settle the NEW current copy; the entry
#     never closes with a live untracked copy.
# ===========================================================================


class TestB2SettleInterleaving:
    async def test_answer_settle_serializes_after_reanchor_step3(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600   # content posted below the anchor this turn

        # Event-gate step 3 (update_question_mid): re-anchor gets caught between
        # POSTING the new copy (step 2 accepted) and PERSISTING its mid (step 3).
        gate = asyncio.Event()
        orig_update = reg.update_question_mid

        async def _gated_update(eid, num, new_mid):
            await gate.wait()
            return await orig_update(eid, num, new_mid)

        reg.update_question_mid = _gated_update

        ra = asyncio.ensure_future(drv._reanchor_pass(rec))
        await asyncio.sleep(0.02)
        # Step 2 accepted: the NEW copy is on the wire (markup), high-water moved.
        assert len(wire.posts) == 1 and wire.posts[0][0] == "markup"
        new_mid = wire.posts[0][1]
        assert seq.high_water == new_mid

        # The operator answer arrives + settles — must BLOCK on the maintenance
        # lock re-anchor still holds (it is mid-step-3), not race an old snapshot.
        settle = asyncio.ensure_future(drv._promote_answer_on_enqueue(rec))
        await asyncio.sleep(0.02)
        assert not settle.done()          # serialized BEHIND the re-anchor

        gate.set()
        assert await ra is True
        await settle

        # The settle edited the NEW current copy (post step-3) with ✅ answered,
        # and the entry closed with NO live untracked copy left behind.
        assert _entry(reg, rec, n) is None
        assert any(mid == new_mid and "answered below" in text
                   for _, mid, text in wire.edits)
        # The OLD copy was re-anchor-settled (re-posted-below), never left live.
        assert any(mid == 500 for _, mid, _ in wire.edits)


# ===========================================================================
# 4c. M4 — entry-removal invariant: a failed unstage / failed close RETAINS the
#     entry (never closes with a durable stale_mid; strict close rolls back).
# ===========================================================================


class TestM4EntryRemovalInvariant:
    async def test_failed_unstage_retains_entry_with_mid_staged(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        await reg.stage_stale_mid(rec.id, n, 400)   # a staged OLD copy
        wire = _Wire()                              # edit_ok=True → both confirm
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        async def _boom(*a, **k):
            raise RuntimeError("unstage persist down")

        reg.unstage_stale_mid = _boom

        snapshot = reg.open_question_entries(rec.id)
        await drv.reconcile_open_questions(rec, snapshot)

        # M4: the confirmed stale copy's strict un-stage RAISED, so the entry must
        # NOT close — it is retained with the stale mid still staged for retry.
        e = _entry(reg, rec, n)
        assert e is not None
        assert 400 in [s["mid"] for s in (e.get("stale_mids") or [])]

    async def test_failed_close_retains_entry(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        # The STRICT close's tombstone write fails → rollback + raise; the driver
        # treats the raise as RETAINED.
        orig = reg._write_tombstone_locked

        async def _fail_strict(*, strict=False):
            if strict:
                raise RuntimeError("disk full")
            return await orig(strict=strict)

        reg._write_tombstone_locked = _fail_strict

        snapshot = reg.open_question_entries(rec.id)
        await drv.reconcile_open_questions(rec, snapshot)

        assert _entry(reg, rec, n) is not None      # retained, not dropped

    async def test_close_open_question_strict_rolls_back_and_raises(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        orig = reg._write_tombstone_locked

        async def _fail_strict(*, strict=False):
            if strict:
                raise RuntimeError("disk full")
            return await orig(strict=strict)

        reg._write_tombstone_locked = _fail_strict

        with pytest.raises(RuntimeError):
            await reg.close_open_question(rec.id, n)
        # Rolled back full-tuple: the entry survives in memory (never split).
        assert _entry(reg, rec, n) is not None


# ===========================================================================
# 5. step-2→3 crash residual: tracked copies settled, orphan documented
# ===========================================================================


class TestCrashResidual:
    async def test_crash_between_post_and_persist_leaves_one_orphan(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)

        # Simulate the exact step-1 + step-2 partial: stale staged, new copy on
        # the wire, then a crash BEFORE update_question_mid persists.
        await reg.stage_stale_mid(rec.id, n, 500)
        orphan_mid = await seq.post_discrete("Q1: DB name?")
        assert orphan_mid is not None
        # (update_question_mid never runs — crash)

        # Boot reconciliation settles the TRACKED copies (current 500 + stale 500);
        # the orphan (new copy) is never edited.
        snapshot = reg.open_question_entries(rec.id)
        await drv.reconcile_open_questions(rec, snapshot)

        assert 500 in wire.edit_mids()
        assert orphan_mid not in wire.edit_mids()   # orphan left as one stale line
        # The orphan mid was NEVER tracked anywhere (its mid died with the crash):
        # not as a current tg_message_id, not as a stale_mid. Ledger clean.
        assert reg.open_question_entries(rec.id) == []


# ===========================================================================
# 5c. D3 — the DRAINED re-anchor unit (spec §D6 r17): obligation armed before
# the unit's first await; cancellation during EACH stage (staging/send/persist/
# edit) + the /silent no-later-boundary path drains the unit to completion and
# releases the lock only AFTER; repeated cancellation during the drain is
# absorbed; a blocked wire beyond the send budget is exactly ONE send + the
# ambiguous floor; an accepted-send-then-raises never sends a second time.
# REAL primitives + fake wire gates + injected clocks (tiny real wait_for
# budgets); NEVER patches <module>.asyncio.sleep.
# ===========================================================================


async def _pump(cond, timeout: float = 5.0) -> None:
    """Yield to the loop until *cond()* holds, so a test can observe a real
    wire op suspended at a controlled gate without a fixed sleep.

    Bounded by WALL-CLOCK time (not a fixed iteration count): the registry
    behind ``cond`` does real tmp-file I/O, so on a loaded/slow runner a
    handful of scheduler turns can span much more real time than on a fast
    box. A generous 5s budget keeps this cheap when fast (resolves in a
    handful of turns) and tolerant when slow, without weakening the check —
    it still raises the same AssertionError if the condition never fires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() >= deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0)


async def _park_forever(_d: float) -> None:
    """A retry-owner sleep that never returns — so the owner armed before the
    unit parks on its FIRST sleep and never runs a concurrent pass (the test
    controls the unit directly)."""
    await asyncio.Event().wait()


class TestDrainedReanchorUnit:
    async def _setup(self, tmp_path, **wire_kw):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire(**wire_kw)
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_park_forever)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        return reg, rec, n, wire, drv, seq

    def _block_stage(self, reg):
        gate, reached = asyncio.Event(), asyncio.Event()
        orig = reg.stage_stale_mid

        async def _blocking(*a, **k):
            reached.set()
            await gate.wait()
            return await orig(*a, **k)

        reg.stage_stale_mid = _blocking
        return gate, reached

    def _block_persist(self, reg):
        gate, reached = asyncio.Event(), asyncio.Event()
        orig = reg.update_question_mid

        async def _blocking(*a, **k):
            reached.set()
            await gate.wait()
            return await orig(*a, **k)

        reg.update_question_mid = _blocking
        return gate, reached

    # -- (a) obligation armed BEFORE the unit's first awaited op --------------

    async def test_obligation_armed_before_unit_first_await(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        gate, reached = self._block_stage(reg)

        task = asyncio.ensure_future(drv._consume_reanchor(rec))
        await reached.wait()          # the unit is blocked at staging (1st await)

        # Armed BEFORE the unit touched the wire — the pre-r17 latch-after-return
        # arming would still be UNSET here.
        assert rec.id in drv._reanchor_due
        assert rec.id in drv._reanchor_retry_tasks
        assert wire.posts == []
        assert wire.send_markup_calls == 0

        gate.set()
        await task
        # A True pass RETIRES what it armed.
        assert rec.id not in drv._reanchor_due
        assert rec.id not in drv._reanchor_retry_tasks
        assert _entry(reg, rec, n)["tg_message_id"] == wire.posts[0][1]

    # -- (b) cancel during EACH unit stage → drain completes, lock after ------

    async def _assert_drained_complete(self, reg, rec, n, wire, drv):
        # The unit COMPLETED via the drain: new copy posted (markup), persisted,
        # old copy markered + un-staged, and the maintenance lock is released.
        assert len(wire.posts) >= 1 and wire.posts[-1][0] == "markup"
        new_mid = [m for k, m, _ in wire.posts if k == "markup"][-1]
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == new_mid
        assert e["stale_mids"] == []
        assert any(mid == 500 for _, mid, _ in wire.edits)   # old copy markered
        assert not drv.ask_maintenance_lock(rec.id).locked()

    async def test_cancel_during_staging_drains(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        gate, reached = self._block_stage(reg)

        task = asyncio.ensure_future(drv._reanchor_pass(rec))
        await reached.wait()
        assert drv.ask_maintenance_lock(rec.id).locked()   # held during the unit
        task.cancel()
        await asyncio.sleep(0)        # deliver the cancel → enter the drain
        gate.set()                    # release staging → child completes the unit
        with pytest.raises(asyncio.CancelledError):
            await task
        await self._assert_drained_complete(reg, rec, n, wire, drv)

    async def test_cancel_during_send_drains(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        wire.send_block = asyncio.Event()

        task = asyncio.ensure_future(drv._reanchor_pass(rec))
        await _pump(lambda: wire.send_markup_calls == 1)   # blocked in the send
        task.cancel()
        await asyncio.sleep(0)
        wire.send_block.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert wire.send_markup_calls == 1                 # exactly ONE send
        await self._assert_drained_complete(reg, rec, n, wire, drv)

    async def test_cancel_during_persist_drains(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        gate, reached = self._block_persist(reg)

        task = asyncio.ensure_future(drv._reanchor_pass(rec))
        await reached.wait()          # new copy posted, blocked in persist
        assert len(wire.posts) == 1 and wire.posts[0][0] == "markup"
        task.cancel()
        await asyncio.sleep(0)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await self._assert_drained_complete(reg, rec, n, wire, drv)

    async def test_cancel_during_marker_edit_drains(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        wire.edit_block = asyncio.Event()

        task = asyncio.ensure_future(drv._reanchor_pass(rec))
        await _pump(lambda: wire.edit_markup_calls == 1)   # blocked in the edit
        # Posted + persisted already (edit is the unit's final stage).
        assert _entry(reg, rec, n)["tg_message_id"] == wire.posts[0][1]
        task.cancel()
        await asyncio.sleep(0)
        wire.edit_block.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await self._assert_drained_complete(reg, rec, n, wire, drv)

    # -- (b) /silent rollback path (no later boundary) drains -----------------

    async def test_silent_rollback_path_drains_under_cancellation(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_park_forever)
        seq = drv._ensure_sequencer(rec)
        # /silent posted a command notice below the anchor, then rolls back.
        token = drv.reserve_answer(rec.id)
        await seq.post_platform_notice("Observer quieted.")
        assert seq.high_water > 500
        gate, reached = self._block_stage(reg)

        task = asyncio.ensure_future(
            drv.rollback_answer_reservation(rec.id, token))
        await reached.wait()
        assert rec.id in drv._reanchor_due          # obligation armed
        task.cancel()
        await asyncio.sleep(0)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Unit completed despite /silent having NO later boundary to recover at.
        reanchor_mid = [m for k, m, _ in wire.posts if k == "markup"][-1]
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid
        assert not drv.ask_maintenance_lock(rec.id).locked()
        assert rec.id in drv._reanchor_due          # latch stays armed (no bdy)
        drv._retire_reanchor_retry(rec.id)          # cleanup the parked owner

    # -- (c) repeated cancellation during the drain is absorbed ---------------

    async def test_repeated_cancellation_during_drain_absorbed(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        gate, reached = self._block_stage(reg)

        task = asyncio.ensure_future(drv._reanchor_pass(rec))
        await reached.wait()
        for _ in range(3):            # hammer the drain with repeated cancels
            task.cancel()
            await asyncio.sleep(0)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await self._assert_drained_complete(reg, rec, n, wire, drv)

    # -- (d) blocked wire beyond the send budget → ONE send + ambiguous floor -

    async def test_blocked_send_budget_one_attempt_ambiguous_floor(
            self, tmp_path, monkeypatch):
        from drivers import claude_code_driver as ccd
        monkeypatch.setattr(ccd, "_REANCHOR_SEND_TIMEOUT", 0.02)
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        wire.send_block = asyncio.Event()      # never released → wait_for fires

        ok = await drv._reanchor_pass(rec)     # returns after the tiny budget

        assert ok is True                      # ambiguous floor, obligation used
        assert wire.send_markup_calls == 1     # exactly ONE physical send attempt
        assert wire.posts == []                # timed out before it recorded
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == 500       # old copy stays current
        assert e["stale_mids"] == []           # staged mid un-staged
        assert not drv.ask_maintenance_lock(rec.id).locked()

    # -- (e) accepted-send-then-raises → no second send -----------------------

    async def test_accepted_send_then_raises_no_second_send(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        wire.send_raises = True

        ok = await drv._reanchor_pass(rec)

        assert ok is True                      # ambiguous → consumed
        assert wire.send_markup_calls == 1     # NO second send
        # The accepted copy IS on the wire (recorded) but is untracked.
        assert len(wire.posts) == 1 and wire.posts[0][0] == "markup"
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == 500       # old copy stays current
        assert e["stale_mids"] == []           # staged mid un-staged


# ===========================================================================
# 5d. D4 — confirmed-pair record + UNIFIED HISTORICAL SCHEDULER (spec §D6
# r18-2→r29-2). A confirmed-but-unpersisted re-anchor copy is owned by a
# process-lifetime per-engagement record; ONE rotation-selected historical step
# per pass (memory pair OR open durable reanchored entry) runs BEFORE the
# current-question work; the memory step is lifecycle-aware (one strict persist
# while live, permanent orphan marker-edit once settlement began); the retry
# owner pumps while items remain; answer/terminal settlement consults the
# record; terminal teardown drops + logs one residual per pair. REAL registry +
# REAL sequencer (fake wire) + injected clocks; the retry owner is parked so
# every pass is driven directly. NEVER patches <module>.asyncio.sleep.
# ===========================================================================

_MOVED_OPEN = "⤵ MOVED Q{n} — answer the current copy below"
_MOVED_TERMINAL = "⤵ MOVED Q{n} — resolved below"


class TestConfirmedPairScheduler:
    async def _setup(self, tmp_path, **wire_kw):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire(**wire_kw)
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_park_forever)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        return reg, rec, n, wire, drv, seq

    @staticmethod
    def _fail_persist(reg):
        """Make ``update_question_mid`` always raise; return (orig, calls)."""
        orig = reg.update_question_mid
        calls: list = []

        async def _fail(*a, **k):
            calls.append(a)
            raise RuntimeError("registry disk down")

        reg.update_question_mid = _fail
        return orig, calls

    # -- (a) permanent persist failure → ONE send across boundaries + answer ---

    async def test_a_permanent_failure_one_send_then_orphan_markered(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        _orig, calls = self._fail_persist(reg)

        # Boundary 1: send #1, persist exhausts → confirmed pair recorded.
        assert await drv._reanchor_pass(rec) is True
        assert wire.send_markup_calls == 1
        new_mid = wire.posts[0][1]
        assert drv._confirmed_pair_mid(rec.id, n) == new_mid
        assert _entry(reg, rec, n)["tg_message_id"] == 500   # never committed

        # Further boundaries: NO new send (rule b); scheduler re-tries the local
        # transaction only (still failing) — the pair stays owned.
        seq._high_water = new_mid + 50
        for _ in range(3):
            await drv._reanchor_pass(rec)
        assert wire.send_markup_calls == 1                   # exactly ONE send
        assert drv._confirmed_pair_mid(rec.id, n) == new_mid
        assert len(calls) >= 4                               # one attempt per pass

        # The operator answers → settlement begins → the orphan is marker-edited
        # (terminal form) and the record retires; still never re-sent.
        await drv._promote_answer_on_enqueue(rec)
        assert drv._confirmed_pair_mid(rec.id, n) is None
        orphan_edits = [t for _, mid, t in wire.edits if mid == new_mid]
        assert orphan_edits[-1] == _MOVED_TERMINAL.format(n=n)
        assert wire.send_markup_calls == 1

    # -- (b) commit on the last natural boundary → durable cleanup via pump ----

    async def test_b_commit_last_boundary_pump_cleans_durable(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        orig, _calls = self._fail_persist(reg)
        await drv._reanchor_pass(rec)                        # → confirmed pair
        new_mid = wire.posts[0][1]
        assert drv._confirmed_pair_mid(rec.id, n) == new_mid

        reg.update_question_mid = orig                       # registry heals
        # A boundary consumer: the scheduler commits the pair (its confirmed mid
        # IS high-water, so current-question work takes the already-last exit).
        await drv._consume_reanchor(rec)
        assert drv._confirmed_pair_mid(rec.id, n) is None    # retired on commit
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == new_mid
        assert e["stale_mids"] == [{"mid": 500, "kind": "reanchored"}]  # durable
        # PUMP: a durable item remains → the retry owner stays armed.
        assert rec.id in drv._reanchor_retry_tasks
        assert drv._historical_items(rec.id) == [("durable", n, 500)]

        posts_before = len(wire.posts)
        await drv._reanchor_pass(rec)                        # durable step runs
        assert len(wire.posts) == posts_before               # NO external output
        assert _entry(reg, rec, n)["stale_mids"] == []       # marker-edit+unstage
        marker = [t for _, mid, t in wire.edits if mid == 500]
        assert marker[-1] == _MOVED_OPEN.format(n=n)
        drv._retire_reanchor_retry(rec.id)                   # cleanup parked owner

    # -- (c) same-question A/B: A durable + B memory, both owned, no extra send -

    async def test_c_same_question_A_durable_B_memory_both_owned(self, tmp_path):
        # A commits but its marker edit fails (edit_ok=False) → durable entry for
        # old_mid 500; high-water advances; B posts, B's persist exhausts.
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path, edit_ok=False)
        await drv._reanchor_pass(rec)                        # re-anchor #1 (A)
        mid_A = wire.posts[0][1]
        assert _entry(reg, rec, n)["tg_message_id"] == mid_A
        assert _entry(reg, rec, n)["stale_mids"] == [{"mid": 500, "kind": "reanchored"}]

        seq._high_water = mid_A + 50
        self._fail_persist(reg)
        await drv._reanchor_pass(rec)                        # re-anchor #2 (B)
        mid_B = wire.posts[-1][1]
        assert mid_B != mid_A
        assert wire.send_markup_calls == 2                   # NO extra (3rd) send
        # A owned as a durable reanchored entry, B owned as a memory record.
        assert drv._confirmed_pair_mid(rec.id, n) == mid_B
        reanchored = {
            _norm(m)["mid"] for m in _entry(reg, rec, n)["stale_mids"]
            if _norm(m)["kind"] == "reanchored"
        }
        assert reanchored == {500}

    # -- (d) fair rotation: Q1 permanently failing, Q2 cleaned on a later pass -

    async def test_d_rotation_selects_q2_when_q1_permanently_fails(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_anchor(reg, rec, mid=500, text="Q1")
        n2 = await _add_anchor(reg, rec, mid=510, text="Q2")
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_park_forever)
        drv._ensure_sequencer(rec)
        drv._record_confirmed_pair(rec.id, n1, 900)
        drv._record_confirmed_pair(rec.id, n2, 910)
        orig = reg.update_question_mid

        async def _update(eid, num, mid):
            if num == n1:
                raise RuntimeError("q1 disk down")
            return await orig(eid, num, mid)

        reg.update_question_mid = _update

        await drv._reanchor_pass(rec)                        # cursor 0 → Q1 (fail)
        assert drv._confirmed_pair_mid(rec.id, n1) == 900
        assert drv._confirmed_pair_mid(rec.id, n2) == 910
        await drv._reanchor_pass(rec)                        # cursor 1 → Q2 (ok)
        assert drv._confirmed_pair_mid(rec.id, n2) is None   # Q2 cleaned
        assert drv._confirmed_pair_mid(rec.id, n1) == 900    # Q1 still owned
        assert wire.send_markup_calls == 0                   # no send either pass

    # -- (e) at most ONE historical step per pass across all mechanisms --------

    async def test_e_one_historical_step_per_pass(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_anchor(reg, rec, mid=500, text="Q1")
        n2 = await _add_anchor(reg, rec, mid=510, text="Q2")
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_park_forever)
        seq = drv._ensure_sequencer(rec)
        # Q1: a durable reanchored stale entry (stage plain then flip on commit).
        await reg.stage_stale_mid(rec.id, n1, 500, kind="plain")
        await reg.update_question_mid(rec.id, n1, 505)
        assert _norm(_entry(reg, rec, n1)["stale_mids"][0])["kind"] == "reanchored"
        # Q2: a memory pair.
        drv._record_confirmed_pair(rec.id, n2, 910)

        update_calls: list = []
        orig = reg.update_question_mid

        async def _cnt(eid, num, mid):
            update_calls.append(num)
            return await orig(eid, num, mid)

        reg.update_question_mid = _cnt

        # Pass 1: items [(durable,n1,500),(memory,n2,-1)] → cursor 0 = durable.
        edits0 = wire.edit_markup_calls
        await drv._reanchor_pass(rec)
        did_durable = wire.edit_markup_calls > edits0
        did_memory = n2 in update_calls
        assert did_durable is True and did_memory is False   # exactly ONE
        # Pass 2: durable done → only the memory pair remains → the OTHER mechanism.
        await drv._reanchor_pass(rec)
        assert n2 in update_calls
        assert drv._confirmed_pair_mid(rec.id, n2) is None

    # -- (f) settlement-begun memory record → NO late transaction, marker-edit --

    async def test_f1_answered_retained_marker_edits_orphan_no_update(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        drv._record_confirmed_pair(rec.id, n, 900)
        await reg.mark_question_answered(rec.id, n)          # answered, retained
        _orig, calls = self._fail_persist(reg)               # would raise if called

        await drv._reanchor_pass(rec)
        assert calls == []                                   # NO late transaction
        assert drv._confirmed_pair_mid(rec.id, n) is None    # retired on edit
        orphan = [t for _, mid, t in wire.edits if mid == 900]
        assert orphan[-1] == _MOVED_TERMINAL.format(n=n)

    async def test_f2_absent_ledger_marker_edits_orphan_no_update(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        drv._record_confirmed_pair(rec.id, n, 900)
        await reg.close_open_question(rec.id, n)             # entry gone (closed)
        _orig, calls = self._fail_persist(reg)

        await drv._reanchor_pass(rec)
        assert calls == []
        assert drv._confirmed_pair_mid(rec.id, n) is None
        orphan = [t for _, mid, t in wire.edits if mid == 900]
        assert orphan[-1] == _MOVED_TERMINAL.format(n=n)

    async def test_f_unconfirmed_settlement_edit_keeps_record(self, tmp_path):
        # A still-live engagement: the orphan edit fails → the record is KEPT for
        # the scheduler to re-attempt (retirement is CONDITIONAL on a confirmed
        # edit, since nothing durable can rediscover this mid — Sol r20-1).
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path, edit_ok=False)
        drv._record_confirmed_pair(rec.id, n, 900)
        await reg.mark_question_answered(rec.id, n)
        await drv._reanchor_pass(rec)
        assert drv._confirmed_pair_mid(rec.id, n) == 900     # KEPT (edit failed)

    # -- (g) terminal teardown with an unconfirmed pair → logged residual ------

    async def test_g_terminal_teardown_logs_residual_no_crash(self, tmp_path, caplog):
        import logging
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        drv._record_confirmed_pair(rec.id, n, 900)
        with caplog.at_level(logging.INFO):
            await drv.cancel(rec)                            # never raises
        assert drv._confirmed_pairs.get(rec.id) is None      # map dropped
        assert any(
            "terminal teardown drops unconfirmed re-anchor pair" in r.getMessage()
            for r in caplog.records)

    # -- (h) sequential Q1/Q2 sustained failure THEN crash → per-pair bound -----

    async def test_h_sequential_failure_then_crash_per_pair_bound(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_anchor(reg, rec, mid=500, text="Q1")
        n2 = await _add_anchor(reg, rec, mid=510, text="Q2")
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_park_forever)
        drv._ensure_sequencer(rec)
        drv._record_confirmed_pair(rec.id, n1, 900)
        drv._record_confirmed_pair(rec.id, n2, 910)
        self._fail_persist(reg)

        await drv._reanchor_pass(rec)
        await drv._reanchor_pass(rec)
        # Per-pair bound: BOTH pairs coexist owned (Sol r22-1).
        assert set(drv._confirmed_pairs[rec.id]) == {n1, n2}

        # CRASH = the driver instance is dropped; the map is memory-only, so a
        # fresh instance holds NO pairs. The durable ledger stays consistent —
        # both entries still point at their ORIGINAL mids (persist never committed).
        drv2 = _make_driver(tmp_path, reg, wire)
        assert drv2._confirmed_pairs == {}
        assert _entry(reg, rec, n1)["tg_message_id"] == 500
        assert _entry(reg, rec, n2)["tg_message_id"] == 510

    # -- (i) hung historical write matches the pre-existing registry floor -----

    async def test_i_hung_historical_write_no_new_deadline(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        drv._record_confirmed_pair(rec.id, n, 900)
        gate = asyncio.Event()
        orig = reg.update_question_mid

        async def _hung(eid, num, mid):
            await gate.wait()
            return await orig(eid, num, mid)

        reg.update_question_mid = _hung

        task = asyncio.ensure_future(drv._reanchor_pass(rec))
        await asyncio.sleep(0.02)
        # No new deadline machinery: the historical registry await is awaited
        # exactly like any registry caller's — the pass does NOT return early.
        assert not task.done()
        gate.set()
        await task
        assert drv._confirmed_pair_mid(rec.id, n) is None    # committed once freed

    # -- (j) D4 review: promotion must NOT retire the pump owner while a
    #        confirmed pair remains owned — mirrors the ``_consume_reanchor``
    #        guard. DOUBLE FAILURE: persist exhausts (confirmed pair recorded)
    #        AND the in-call settlement orphan marker-edit ALSO fails (record
    #        retained) — the retry owner must stay armed so the standing pump
    #        keeps retrying, instead of depending on the next unrelated
    #        output boundary.

    async def test_j_promote_keeps_owner_armed_while_pair_retained(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path, edit_ok=False)
        self._fail_persist(reg)

        # Boundary 1: send #1, persist exhausts → confirmed pair recorded.
        assert await drv._reanchor_pass(rec) is True
        new_mid = wire.posts[0][1]
        assert drv._confirmed_pair_mid(rec.id, n) == new_mid

        # A prior boundary armed the standing pump owner (as _consume_reanchor
        # does) — simulate that here.
        drv._arm_reanchor_retry(rec)
        assert rec.id in drv._reanchor_retry_tasks
        owner = drv._reanchor_retry_tasks[rec.id]

        # The operator answers → promotion marks the question answered and
        # settles it; the in-call orphan marker-edit ALSO fails (edit_ok=False)
        # so the confirmed-pair record is retained — historical work remains.
        await drv._promote_answer_on_enqueue(rec)

        assert drv._confirmed_pair_mid(rec.id, n) == new_mid  # KEPT (edit failed)
        assert drv._historical_items(rec.id) != []
        # §D6 r29-2 PUMP: the retry owner must stay armed while historical
        # items remain — it must NOT depend on the next unrelated boundary.
        assert rec.id in drv._reanchor_retry_tasks
        assert drv._reanchor_retry_tasks[rec.id] is owner
        assert not owner.done()

        drv._retire_reanchor_retry(rec.id)   # cleanup parked owner


# ===========================================================================
# 5a2. wb1-2 (whole-branch gate wave 1) — the UNIFIED HISTORICAL SCHEDULER's
#      DURABLE step must REVALIDATE lifecycle before it edits + unstages. Once
#      answer/terminal settlement has BEGUN (the engagement record flipped
#      terminal in finalize's pre-settle gap, OR an answer reservation landed
#      while the OPEN-marker edit was blocked), the OPEN "answer the current copy
#      below" marker would strand the stale copy looking live because settlement
#      then only sees the current copy. The step must render the TERMINAL marker
#      and LEAVE the entry staged for settlement.
# ===========================================================================


class TestWb1_2DurableStepRevalidation:
    async def _durable_entry(self, tmp_path):
        """A durable ``reanchored`` stale entry: old copy 500, current copy 505."""
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_park_forever)
        drv._ensure_sequencer(rec)
        await reg.stage_stale_mid(rec.id, n, 500, kind="plain")
        await reg.update_question_mid(rec.id, n, 505)
        assert _entry(reg, rec, n)["stale_mids"] == [{"mid": 500, "kind": "reanchored"}]
        return reg, rec, n, wire, drv

    async def test_terminal_flip_before_step_renders_terminal_leaves_staged(
        self, tmp_path,
    ):
        reg, rec, n, wire, drv = await self._durable_entry(tmp_path)
        # The authoritative terminal flip lands (finalize's gap, BEFORE
        # settle_all cancels the pump). The pump's durable step must NOT edit the
        # stale copy to the OPEN marker and unstage it — it renders the TERMINAL
        # marker and LEAVES it staged for settlement.
        rec.status = "cancelled"
        await drv._historical_durable_step(rec, n, 500)

        markers = [t for _, mid, t in wire.edits if mid == 500]
        assert markers[-1] == _MOVED_TERMINAL.format(n=n)      # terminal, not open
        # Left staged so the settlement path renders it terminal / removes it.
        assert _entry(reg, rec, n)["stale_mids"] == [
            {"mid": 500, "kind": "reanchored"}]

    async def test_reservation_during_blocked_open_edit_does_not_unstage(
        self, tmp_path,
    ):
        reg, rec, n, wire, drv = await self._durable_entry(tmp_path)
        wire.edit_block = asyncio.Event()   # park the marker edit mid-wire
        step = asyncio.ensure_future(drv._historical_durable_step(rec, n, 500))
        await asyncio.sleep(0.02)           # parked in the blocked marker edit
        # An answer reservation lands while the OPEN-marker edit is blocked →
        # settlement has begun. On unblock the step must RE-CHECK before
        # unstaging and LEAVE the entry staged (unstaging would hide the stale
        # copy from settlement → stranded live-looking).
        assert drv.reserve_answer(rec.id) is not None
        wire.edit_block.set()
        await asyncio.wait_for(step, timeout=1.0)

        assert _entry(reg, rec, n)["stale_mids"] == [
            {"mid": 500, "kind": "reanchored"}]

    async def test_live_unanswered_still_renders_open_and_unstages(self, tmp_path):
        # Regression: with NO settlement begun the durable step keeps its
        # round-3 behaviour — OPEN marker + unstage.
        reg, rec, n, wire, drv = await self._durable_entry(tmp_path)
        await drv._historical_durable_step(rec, n, 500)
        markers = [t for _, mid, t in wire.edits if mid == 500]
        assert markers[-1] == _MOVED_OPEN.format(n=n)
        assert _entry(reg, rec, n)["stale_mids"] == []


# ===========================================================================
# 5b. B3 (wave 2) — an ABSENT fresh re-read is ALREADY RESOLVED: SKIP, never
#     fall back to the captured snapshot (its answered=False would ⌛-overwrite
#     a ✅ settle on a message that is already done).
# ===========================================================================


class TestB3FreshReadOnlyReconcile:
    async def test_reconcile_skips_entry_settled_after_snapshot(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        # Snapshot captured while the entry is live (answered=False).
        snapshot = reg.open_question_entries(rec.id)
        assert snapshot and snapshot[0]["n"] == n

        # The question is ANSWERED + settled + CLOSED (removed) between the
        # snapshot capture and this readiness-gated reconcile.
        await reg.mark_question_answered(rec.id, n)
        await reg.close_open_question(rec.id, n)
        assert reg.open_question_entries(rec.id) == []

        # B3: reconcile must make ZERO edits — the fresh re-read is absent (already
        # resolved), so it SKIPS rather than settling the stale snapshot copy.
        await drv.reconcile_open_questions(rec, snapshot)
        assert wire.edits == []

    async def test_settle_open_anchor_skips_absent_numbered_entry(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        # A captured anchor snapshot whose NUMBERED entry is NOT in the ledger
        # (already settled + removed). The in-lock fresh re-read finds nothing.
        stale = {
            "n": 7, "tg_message_id": 500, "stale_mids": [], "kind": "anchor",
            "answered": False, "text": "Q7: name?",
        }
        # B3: the settle path skips (returns None) rather than re-editing mid 500
        # from the captured fallback dict.
        amid = await drv._settle_open_anchor(rec, anchor=stale)
        assert amid is None
        assert wire.edits == []


# ===========================================================================
# 6. boundary consumers
# ===========================================================================


class _FakeSpool:
    """Minimal spool stub: ``on_turn_end`` flushes a pending platform notice
    through the sequencer (advancing high-water) — used to pin the result-consumer
    ordering (notice BEFORE re-anchor)."""

    def __init__(self, seq, *, notice: str | None = None):
        self.seq = seq
        self.notice = notice
        self.notice_mid = None

    async def on_turn_end(self):
        if self.notice is not None:
            self.notice_mid = await self.seq.post_platform_notice(self.notice)

    async def on_spawn(self):
        return None

    async def on_turn_start(self):
        return None


class TestBoundaryConsumers:
    async def test_result_runs_reanchor_after_on_turn_end(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        drv._inbound[rec.id] = _FakeSpool(seq, notice="pending receipt")

        await drv._on_stream_event(rec, "result", {})

        # The flushed notice (text) posted FIRST, then the re-anchor copy (markup)
        # — the re-anchored question ends up LAST.
        assert [k for k, _, _ in wire.posts] == ["text", "markup"]
        notice_mid, reanchor_mid = wire.post_mids()
        assert notice_mid < reanchor_mid
        assert seq.high_water == reanchor_mid
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid

    async def test_spawn_without_result_consumes(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        # A prior epoch is pending a result → the next spawn is an abnormal
        # (spawn-without-result) boundary.
        drv._epoch_pending[rec.id] = 1

        await drv._on_stream_event(rec, "spawn", {"epoch": 2})

        assert len(wire.posts) == 1 and wire.posts[0][0] == "markup"
        assert _entry(reg, rec, n)["tg_message_id"] == wire.posts[0][1]

    async def test_spawn_with_no_prior_epoch_does_not_reanchor(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        drv._epoch_pending[rec.id] = None   # no prior epoch → normal first spawn

        await drv._on_stream_event(rec, "spawn", {"epoch": 1})

        assert wire.posts == []             # no abnormal boundary → no re-anchor

    async def test_rollback_completion_consumer_reanchors_idle_engagement(
            self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        # An idle engagement: the operator's /silent posted a command notice
        # (below the anchor), then its reservation rolls back — no future spawn.
        token = drv.reserve_answer(rec.id)
        await seq.post_platform_notice("Observer quieted.")   # command notice
        rolled = await drv.rollback_answer_reservation(rec.id, token)

        assert rolled is True
        # The rollback consumer (d) ran the pass → the anchor is LAST again.
        assert any(k == "markup" for k, _, _ in wire.posts)
        reanchor_mid = [mid for k, mid, _ in wire.posts if k == "markup"][-1]
        assert seq.high_water == reanchor_mid
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid


# ===========================================================================
# 7. terminal finalize SETTLES anchors (never re-anchors)
# ===========================================================================


class TestTerminalFinalizeSettle:
    async def test_settle_all_open_questions_cancelled(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        await drv.settle_all_open_questions(rec, "cancelled")

        # The live anchor is SETTLED (closed copy) — never re-anchored (no post).
        assert wire.posts == []
        assert any(mid == 500 and "engagement ended" in text
                   for _, mid, text in wire.edits)
        assert reg.open_question_entries(rec.id) == []

    async def test_answered_entry_gets_check_copy(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        await reg.mark_question_answered(rec.id, n)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        await drv.settle_all_open_questions(rec, "completed")

        assert any(mid == 500 and "answered below" in text
                   for _, mid, text in wire.edits)
        assert reg.open_question_entries(rec.id) == []

    async def test_terminal_settle_clears_latch_and_cancels_retry(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)
        # Arm the latch + a retry owner as if a prior pass had failed.
        drv.set_reanchor_due(rec.id)
        drv._arm_reanchor_retry(rec)
        assert rec.id in drv._reanchor_retry_tasks

        await drv.settle_all_open_questions(rec, "cancelled")

        assert rec.id not in drv._reanchor_due
        assert rec.id not in drv._reanchor_retry_tasks


# ===========================================================================
# F2 (whole-branch gate): a TERMINAL command (/cancel, /complete) rolls back
# the answer reservation with suppress_reanchor=True — the imminent terminal
# finalize owns the open anchors, so the fourth-consumer re-anchor must NOT
# fire (no redundant copy) and the latch must stay UNSET. The REAL driver
# finalizer settle (settle_all_open_questions) then settles the entry once.
# ===========================================================================


class TestF2TerminalCommandSuppressesReanchor:
    async def test_suppress_reanchor_skips_pass_then_terminal_settles_once(
        self, tmp_path,
    ):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        # A command notice posted BELOW the anchor this turn (high-water > 500),
        # so a NON-suppressed rollback WOULD re-anchor.
        await seq.post_platform_notice("Engagement cancelled by originator.")
        assert seq.high_water > 500
        token = drv.reserve_answer(rec.id)
        assert token is not None

        # /cancel path: rollback with suppress_reanchor=True (finalize follows).
        rolled = await drv.rollback_answer_reservation(
            rec.id, token, suppress_reanchor=True)
        assert rolled is True
        # NO re-anchor post — the terminal settle owns the entries.
        assert all(k != "markup" for k, _, _ in wire.posts)
        # Latch NEITHER set NOR consumed.
        assert rec.id not in drv._reanchor_due

        # REAL terminal finalizer settle — settles the (unanswered) entry ONCE.
        await drv.settle_all_open_questions(rec, "cancelled")
        settles = [
            (mid, text) for _, mid, text in wire.edits if mid == 500
        ]
        assert len(settles) == 1
        assert "engagement ended" in settles[0][1]
        assert reg.open_question_entries(rec.id) == []

    async def test_non_suppressed_rollback_still_reanchors(self, tmp_path):
        """Contrast: a NON-terminal rollback (default) DOES re-anchor — proving
        the suppress flag, not the setup, is what silences the pass."""
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        await seq.post_platform_notice("Observer quieted.")
        token = drv.reserve_answer(rec.id)

        rolled = await drv.rollback_answer_reservation(rec.id, token)
        assert rolled is True
        assert any(k == "markup" for k, _, _ in wire.posts)   # re-anchored
        assert _entry(reg, rec, n)["tg_message_id"] == (
            [mid for k, mid, _ in wire.posts if k == "markup"][-1])


# ===========================================================================
# 8. retry owner (Sol §6n note 1)
# ===========================================================================


class TestRetryOwner:
    async def test_first_pass_fails_then_retry_succeeds(self, tmp_path):
        # v0.84.0 (§D6 r17): a STAGE failure is now the ONLY retry-owed (False)
        # outcome — nothing reached the wire, so it is safe to re-drive. Once
        # the registry recovers, the retry owner completes the re-anchor.
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        delays: list[float] = []

        async def _rec_sleep(d):
            delays.append(d)

        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_rec_sleep)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600

        stage_calls = 0
        orig_stage = reg.stage_stale_mid

        async def _flaky_stage(*a, **k):
            nonlocal stage_calls
            stage_calls += 1
            if stage_calls == 1:
                raise RuntimeError("registry disk down")   # first pass fails
            return await orig_stage(*a, **k)

        reg.stage_stale_mid = _flaky_stage

        # A boundary consumer runs the (failing) pass → latch set + retry armed.
        await drv._consume_reanchor(rec)
        assert rec.id in drv._reanchor_due
        assert wire.posts == []                      # stage failed, nothing sent
        task = drv._reanchor_retry_tasks.get(rec.id)
        assert task is not None

        # Registry recovers; drive the retry loop to completion.
        await task

        assert delays == [5.0]                       # one backoff step
        assert rec.id not in drv._reanchor_due       # latch cleared on success
        assert rec.id not in drv._reanchor_retry_tasks  # completed → no entry
        assert _entry(reg, rec, n)["tg_message_id"] == wire.posts[0][1]

    async def test_repeated_failures_walk_backoff_to_cap(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        delays: list[float] = []

        async def _cap_sleep(d):
            delays.append(d)
            if len(delays) >= 4:
                raise asyncio.CancelledError   # simulate teardown at the cap

        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_cap_sleep)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        # Registry stage stays down → every pass fails BEFORE the wire.
        reg.stage_stale_mid = AsyncMock(side_effect=RuntimeError("disk"))

        await drv._consume_reanchor(rec)
        task = drv._reanchor_retry_tasks.get(rec.id)
        with pytest.raises(asyncio.CancelledError):
            await task

        # 5 → 30 → 300 → 300 (capped, repeated), then the CancelledError.
        assert delays == [5.0, 30.0, 300.0, 300.0]
        assert wire.posts == []               # nothing ever reached the wire
        assert rec.id in drv._reanchor_due   # never cleared (never succeeded)

    async def test_double_arm_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)

        gate = asyncio.Event()

        async def _blocking_sleep(_d):
            await gate.wait()

        wire = _Wire(markup_ok=False)
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_blocking_sleep)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        drv.set_reanchor_due(rec.id)

        drv._arm_reanchor_retry(rec)
        first = drv._reanchor_retry_tasks.get(rec.id)
        drv._arm_reanchor_retry(rec)                 # double-arm
        second = drv._reanchor_retry_tasks.get(rec.id)
        assert first is second                       # one task per engagement

        gate.set()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    async def test_teardown_cancels_retry_no_reschedule(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)

        gate = asyncio.Event()
        after_cancel: list[float] = []

        async def _blocking_sleep(_d):
            await gate.wait()
            after_cancel.append(_d)   # must never run again after cancel

        wire = _Wire(markup_ok=False)
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_blocking_sleep)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        drv.set_reanchor_due(rec.id)
        drv._arm_reanchor_retry(rec)
        task = drv._reanchor_retry_tasks.get(rec.id)

        await drv.cancel(rec)   # teardown cancels the retry owner

        assert rec.id not in drv._reanchor_retry_tasks
        with pytest.raises(asyncio.CancelledError):
            await task
        assert after_cancel == []


# ===========================================================================
# 9. boot reconciliation owner (readiness barrier + claimed-set)
# ===========================================================================


class TestBootReconcileOwner:
    async def test_reconcile_waits_for_readiness(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)
        ready = asyncio.Event()
        snapshot = reg.open_question_entries(rec.id)

        task = drv.schedule_boot_reconcile(rec, snapshot, ready)
        # Give the task a chance to run — it MUST block on readiness.
        await asyncio.sleep(0)
        assert wire.edits == []                 # nothing settled before readiness

        ready.set()
        await task
        assert 500 in wire.edit_mids()          # settled only after readiness
        assert reg.open_question_entries(rec.id) == []

    async def test_claimed_set_prevents_double_reconcile(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)
        ready = asyncio.Event()
        ready.set()
        claimed: set[str] = set()
        snapshot = reg.open_question_entries(rec.id)

        t1 = drv.schedule_boot_reconcile(rec, snapshot, ready, claimed=claimed)
        t2 = drv.schedule_boot_reconcile(rec, snapshot, ready, claimed=claimed)
        assert t1 is not None and t2 is None    # second claim refused
        await t1

    async def test_empty_snapshot_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        assert drv.schedule_boot_reconcile(rec, [], asyncio.Event()) is None

    async def test_delayed_readiness_settles_on_first_boot(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)
        ready = asyncio.Event()
        snapshot = reg.open_question_entries(rec.id)
        task = drv.schedule_boot_reconcile(rec, snapshot, ready)

        # Readiness lands late (transport recovered after several rebuild retries).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert wire.edits == []
        ready.set()
        await task
        assert 500 in wire.edit_mids()          # settles on THIS boot, not "never"


# ===========================================================================
# 10. boot replay owner integration (refused / terminal records + snapshot)
# ===========================================================================


class TestBootReplayOwner:
    async def test_terminal_and_fresh_ask_and_no_double(self, tmp_path, monkeypatch):
        import casa_core

        # Two records: a TERMINAL record with a stranded question, and an
        # active/idle undergoing record whose FRESH same-process ask must NOT be
        # captured + expired by the pre-service snapshot.
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        term = await reg.create("executor", "configurator", "claude_code", "t",
                                {"user_id": 1}, topic_id=111)
        term.status = "cancelled"
        await reg.add_open_question(term.id, 1, 900, text="Q1: old?",
                                    kind="anchor")

        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(term)

        ready = asyncio.Event()

        # No undergoing records → the s6 fast-path return fires; the terminal
        # owner MUST still be scheduled (pre-lock). Stub s6 heal surface.
        scheduled: list[str] = []
        orig = drv.schedule_boot_reconcile

        def _spy(rec, snap, tr, *, claimed=None):
            scheduled.append(rec.id)
            return orig(rec, snap, tr, claimed=claimed)

        drv.schedule_boot_reconcile = _spy

        await casa_core.replay_undergoing_engagements(
            registry=reg, driver=drv, executor_registry=None,
            engagements_root=str(tmp_path / "eng"), telegram_ready=ready)

        # Terminal record with a stranded question was claimed for reconcile,
        # gated on readiness (nothing settled yet).
        assert term.id in scheduled
        assert wire.edits == []
        ready.set()
        # Drain the scheduled reconcile task.
        for t in list(drv._boot_reconcile_tasks):
            await t
        assert 900 in wire.edit_mids()
        assert reg.open_question_entries(term.id) == []

    async def test_active_fresh_ask_between_snapshot_and_attach_not_expired(
        self, tmp_path, monkeypatch,
    ):
        # B3: an ACTIVE (undergoing) record with NO open questions at the
        # PRE-SERVICE snapshot. The resumed CLI registers a fresh ask BETWEEN the
        # snapshot and attach (``start_service`` here) — casa_core must pass the
        # per-record snapshot as ``[]`` (NOT missing/None), so ``_spawn_background_
        # tasks`` reconciles NOTHING and the fresh ask is never expired.
        import casa_core
        from drivers import s6_rc
        from engagement_registry import EngagementRegistry
        from unittest.mock import AsyncMock, MagicMock

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
        # Skip the heal machinery: pretend the service pair is complete + current.
        monkeypatch.setattr(s6_rc, "service_pair_complete", lambda **kw: True)
        monkeypatch.setattr(s6_rc, "run_script_is_stale", lambda **kw: False)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
        # Containment Stage 2 (Task 10): replay chowns the resumed workspace to
        # its uid as the last write before start — a non-root test cannot chown
        # to uid 200000+, so no-op it; and the registry needs a uid allocator so
        # create() assigns a real uid (no backfill/refuse on this claude_code
        # record).
        from drivers import workspace as _ws_mod
        monkeypatch.setattr(_ws_mod, "chown_workspace", lambda *a, **k: None)
        # Containment Stage 2 (S1 code-gate fix): the symlink-safe .mcp.json/
        # settings writes owner-check their parent dir against the record's
        # allocated uid. In production an undergoing record with a real
        # persisted uid ALWAYS has a uid-owned workspace (it was chowned when
        # the uid was allocated). This non-root test creates a real uid via
        # create() but cannot chown the workspace to 200000 (same reason the
        # chown above is stubbed), so no-op the owner-uid derivation too —
        # otherwise the write would refuse against a test-user-owned parent.
        import casa_core as _cc
        monkeypatch.setattr(_cc, "owner_uid_or_none", lambda uid: None)

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None,
            uid_allocator=_anchor_tmp_allocator(tmp_path))
        rec = await reg.create("executor", "configurator", "claude_code", "t",
                               {"user_id": 1}, topic_id=333)
        # ACTIVE (default) → undergoing; empty open_questions at snapshot time.

        captured: dict = {}

        def _spawn(r, *, reconcile_snapshot=None, reconcile_claimed=None,
                   telegram_ready=None):
            captured["snapshot"] = reconcile_snapshot

        async def _fake_start(*, engagement_id):
            # The resumed CLI registers a FRESH ask AFTER the pre-service snapshot
            # but BEFORE _spawn_background_tasks (attach) runs.
            nn = await reg.allocate_question_number(engagement_id)
            await reg.add_open_question(engagement_id, nn, 800, text="Q: fresh?",
                                        kind="anchor")

        monkeypatch.setattr(s6_rc, "start_service", _fake_start)

        driver = AsyncMock()
        driver._spawn_background_tasks = _spawn
        driver.adopt_summary_if_missing = AsyncMock()
        # schedule_boot_reconcile is SYNC in production (returns a task/None); the
        # casa_core refused-pass calls it un-awaited, so keep it a sync mock.
        driver.schedule_boot_reconcile = MagicMock(return_value=None)
        # #335: the replay loop reads this URL for the .mcp.json refresh; an
        # AsyncMock auto-attribute is not JSON-serializable.
        driver._casa_framework_mcp_url = "http://127.0.0.1:8100/mcp/casa-framework"

        # #314: provision the workspace — a missing one is now refused before
        # the fast path, and this test's subject is the snapshot contract.
        # The fresh workspace has no .mcp.json, so the #335 migration cycles
        # the service — confirm the stop instead of shelling out to s6-rc.
        (tmp_path / "eng" / rec.id).mkdir(parents=True)
        monkeypatch.setattr(
            s6_rc, "ensure_service_down", AsyncMock(return_value=True))

        # Task 6 (#360): boot replay now resolves every claude_code record
        # via definition_any (settings.json floor regeneration) BEFORE the
        # fast path — an executor_registry is required for the record to
        # attach at all now.
        from config import ExecutorDefinition
        try:
            from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
        except ImportError:
            from role_artifact_stub import STUB_ROLE_ARTIFACT

        class _ExecReg:
            def __init__(self):
                self._defn = ExecutorDefinition(
                    role_artifact=STUB_ROLE_ARTIFACT,
                    type="configurator", description="test", model="haiku",
                    driver="claude_code", enabled=True, tools_allowed=[],
                    tools_disallowed=[], permission_mode="bypassPermissions",
                    mcp_server_names=[], idle_reminder_days=1,
                    prompt_template_path="/nope/prompt.md", hooks_path=None,
                    observer_policy_path=None, doctrine_dir="",
                    extra_dirs=[], mirror_chat_to_topic=False,
                    plugins_dir="",
                )
            def get(self, t):
                return self._defn if t == "configurator" else None
            def definition_any(self, t):
                return self._defn if t == "configurator" else None

        await casa_core.replay_undergoing_engagements(
            registry=reg, driver=driver, executor_registry=_ExecReg(),
            engagements_root=str(tmp_path / "eng"), telegram_ready=None)

        # B3: the empty-oq record was snapshotted as [] (NOT None) — the fresh ask
        # created between snapshot and attach is NOT in the reconcile set.
        assert captured.get("snapshot") == []
        # And the fresh question survives in the ledger (never expired).
        assert reg.open_question_entries(rec.id)


# ===========================================================================
# wb2-2 (whole-branch gate wave 2): the marker-edit → unstage sequence must be
# LINEARIZABLE against an answer reservation / terminal flip that lands during
# EITHER awaited step. wb1-2 added ONE pre-unstage check, but a reservation can
# still install WHILE the unstage write is awaiting persistence (or while the
# OPEN-marker edit is in flight), after which the unstage removes the stale copy
# and promotion settles ONLY the current copy — stranding the old message
# permanently on the OPEN "answer the current copy below" marker. The fix re-
# reads lifecycle after the edit AND after the unstage, leaving/restoring the
# stale entry staged so settlement re-renders it TERMINAL. REAL registry + REAL
# sequencer (fake wire) + injected clocks; NEVER patches <module>.asyncio.sleep.
# ===========================================================================


class TestUnstageLinearizableAgainstReservation:
    async def test_answer_reservation_during_current_unit_marker_edit(self, tmp_path):
        """The current-question re-anchor unit's step-4 OPEN-marker edit is in
        flight when an answer reservation lands. The re-check AFTER the edit must
        LEAVE the stale copy staged so promotion terminalizes it — not unstage it
        and strand the old copy on the OPEN marker."""
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        wire.edit_block = asyncio.Event()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600

        moving = asyncio.create_task(drv._reanchor_pass(rec))
        for _ in range(100):
            if wire.edit_markup_calls:
                break
            await asyncio.sleep(0.001)
        assert wire.edit_markup_calls == 1

        # The new current copy has committed and the old-copy OPEN marker edit is
        # in flight. This is settlement-begun under the driver's own predicate.
        assert drv.reserve_answer(rec.id) is not None
        assert drv._settlement_begun(rec.id, n)
        wire.edit_block.set()
        assert await moving is True

        # Complete the normal enqueue-time answer settlement.
        await drv._promote_answer_on_enqueue(rec)

        old_texts = [text for _, mid, text in wire.edits if mid == 500]
        assert old_texts[-1] == f"⤵ MOVED Q{n} — resolved below"

    async def test_reservation_during_current_unit_unstage(self, tmp_path):
        """The current-question re-anchor unit's step-4 ``unstage_stale_mid`` is
        awaiting persistence when an answer reservation lands. The re-check AFTER
        the unstage must RE-STAGE the stale copy so promotion terminalizes it."""
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600

        entered = asyncio.Event()
        release = asyncio.Event()
        real_unstage = reg.unstage_stale_mid

        async def blocked_unstage(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_unstage(*args, **kwargs)

        reg.unstage_stale_mid = blocked_unstage
        moving = asyncio.create_task(drv._reanchor_pass(rec))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        assert drv.reserve_answer(rec.id) is not None
        release.set()
        assert await moving is True
        await drv._promote_answer_on_enqueue(rec)

        old_texts = [text for _, mid, text in wire.edits if mid == 500]
        assert old_texts[-1] == f"⤵ MOVED Q{n} — resolved below"

    async def test_reservation_after_second_check_during_durable_unstage(
            self, tmp_path):
        """The unified historical scheduler's durable step: the marker edit
        succeeded and the code's second lifecycle check already ran; the awaited
        ``unstage_stale_mid`` is now in flight when the answer reservation lands.
        The re-check AFTER the unstage must RE-STAGE the stale copy so settlement
        turns its OPEN marker terminal."""
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)
        await reg.stage_stale_mid(rec.id, n, 500, kind="plain")
        await reg.update_question_mid(rec.id, n, 505)

        entered = asyncio.Event()
        release = asyncio.Event()
        real_unstage = reg.unstage_stale_mid

        async def blocked_unstage(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_unstage(*args, **kwargs)

        reg.unstage_stale_mid = blocked_unstage
        step = asyncio.create_task(drv._historical_durable_step(rec, n, 500))
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        # Marker edit succeeded and the code's second lifecycle check already ran;
        # the awaited unstage is now in flight. This is where Telegram can finish
        # advance_high_water and synchronously install the answer reservation.
        assert drv.reserve_answer(rec.id) is not None
        assert drv._settlement_begun(rec.id, n)
        release.set()
        await asyncio.wait_for(step, timeout=1.0)

        # Complete normal answer promotion/settlement. The old copy should have
        # remained staged so settlement could turn its OPEN marker terminal.
        await drv._promote_answer_on_enqueue(rec)
        old_texts = [text for _, mid, text in wire.edits if mid == 500]
        assert old_texts[-1] == f"⤵ MOVED Q{n} — resolved below"


# ===========================================================================
# wb4-3 (whole-branch gate wave 4, MAJOR): ``cancel()`` must DRAIN the cancelled
# re-anchor retry owner BEFORE dropping the D6 confirmed-pair map. A cancelled
# owner drains its child (by design), and the child re-creates ``_confirmed_pairs``
# — if teardown drops the map and returns first, the re-created record leaks
# unpumped + unlogged, violating the D6 terminal-teardown ownership contract.
# ===========================================================================


class TestCancelDrainsReanchorOwner:
    async def test_cancel_awaits_drained_child_then_logs_residual(
        self, tmp_path, caplog,
    ):
        import logging
        reg, rec = await _make_registry(tmp_path)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        eng_id = rec.id

        started = asyncio.Event()
        release = asyncio.Event()

        async def _owner():
            # Hold the ask-maintenance lock for the child's whole lifetime (as
            # ``_reanchor_pass`` does) and DRAIN the child on cancel — the exact
            # ownership shape of ``_reanchor_pass_locked``.
            async with drv.ask_maintenance_lock(eng_id):
                async def _child():
                    started.set()
                    await release.wait()
                    # The drained child re-creates a confirmed pair AFTER cancel()
                    # would otherwise have dropped the map.
                    drv._record_confirmed_pair(eng_id, 1, 1001)
                    return True

                child = asyncio.ensure_future(_child())
                try:
                    return await asyncio.shield(child)
                except asyncio.CancelledError:
                    while not child.done():
                        try:
                            await asyncio.shield(child)
                        except asyncio.CancelledError:
                            continue
                    raise

        owner = asyncio.create_task(_owner())
        drv._reanchor_retry_tasks[eng_id] = owner
        await asyncio.wait_for(started.wait(), timeout=1.0)

        with caplog.at_level(logging.INFO):
            cancel_task = asyncio.create_task(drv.cancel(rec))
            # Let cancel() run: WITHOUT the fix it cancels the owner, drops the
            # map and returns; WITH the fix it parks on the owner drain / lock
            # barrier.
            for _ in range(10):
                await asyncio.sleep(0)
            # Release the child so its drained completion re-creates the pair.
            release.set()
            await asyncio.wait_for(cancel_task, timeout=1.0)
        await asyncio.gather(owner, return_exceptions=True)

        # The re-created record was seen by teardown: dropped + logged, never
        # leaked back into ``_confirmed_pairs`` after cancel() returned.
        assert drv._confirmed_pairs.get(eng_id) is None
        assert any(
            "terminal teardown drops unconfirmed re-anchor pair" in r.getMessage()
            for r in caplog.records)


# ===========================================================================
# wb5-1 (whole-branch gate wave 5): a terminal engagement's RETAINED anchor
# (an unconfirmed settle edit keeps the ledger entry) must NOT be re-anchored
# by a boundary consumer that acquired the maintenance lock AFTER the sole
# terminal-settlement pass. The D6 re-anchor refuses to SEND once EITHER
# terminal condition holds (the record flipped terminal OR the sequencer
# latched), and ``post_discrete`` backstops that under the writer lock. Without
# the guard a full open question reposts + persists as current on a CLOSED
# engagement, and ``cancel()`` never re-runs settlement to clean it up.
# ===========================================================================


class TestTerminalReanchorRefusal:
    async def test_terminal_retained_anchor_not_reanchored_after_settle(
        self, tmp_path,
    ):
        # Sol's deterministic repro (inverted assertions): a terminal settle
        # with a FAILING (unconfirmed) edit retains the anchor; a later boundary
        # consumer must find the obligation already discharged and post NOTHING.
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500, text="Q1: DB name?")
        wire = _Wire(edit_ok=False)   # every settle edit stays unconfirmed
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600         # anchor 500 is NOT last ⇒ would re-anchor
        rec.status = "cancelled"

        await drv.settle_all_open_questions(rec, "cancelled")
        assert seq.is_terminal()
        # Unconfirmed edit ⇒ ledger entry RETAINED, still pointing at 500.
        entry = _entry(reg, rec, n)
        assert entry is not None
        assert entry["tg_message_id"] == 500
        assert wire.posts == []       # settle only edits, never posts

        # A relay ``result``/rollback boundary consumer runs AFTER settlement.
        await drv._consume_reanchor(rec)

        # No repost; ledger mid UNCHANGED — no open question resurrected on the
        # closed engagement (the buggy behaviour reposted the full body and
        # persisted the new mid as current).
        assert wire.posts == []
        assert _entry(reg, rec, n)["tg_message_id"] == 500

    async def test_boundary_consumer_queued_behind_terminal_settlement(
        self, tmp_path,
    ):
        # Ordering variant Sol names: the consumer is ALREADY queued (concurrent
        # with) the sole terminal-settlement pass on the ask-maintenance lock. In
        # EVERY interleaving the record is already terminal and the sequencer
        # latches at settlement entry, so the consumer must decline regardless of
        # which task wins the lock.
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500, text="Q1: DB name?")
        wire = _Wire(edit_ok=False)
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        rec.status = "cancelled"

        settle = asyncio.ensure_future(
            drv.settle_all_open_questions(rec, "cancelled"))
        consumer = asyncio.ensure_future(drv._consume_reanchor(rec))
        await asyncio.gather(settle, consumer)

        assert seq.is_terminal()
        assert wire.posts == []
        assert _entry(reg, rec, n)["tg_message_id"] == 500
