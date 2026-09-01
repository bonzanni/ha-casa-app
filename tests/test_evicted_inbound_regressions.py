"""#780 regressions — the evicted-pending population's boundaries, its exclusion
join, its detached-file tier, its no-veto property and its failure isolation.

These are the post-fix regressions the design round specified around the
frozen red case in ``test_evicted_inbound_disclosure.py`` (whose helpers they
reuse). Each test names the mutation it kills. They bind the widened
INV-ENG-016 (visibility to every terminal disclosure, disclose-never-veto) and
INV-ENG-017 (the printable-envelope exclusion set) clauses.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import pytest  # parametrize only; asyncio_mode=auto needs no module-level mark

from test_evicted_inbound_disclosure import (
    PARAGRAPH,
    _InboundOnly,
    _NarrowChannel,
    _evict_with_a_failing_notice,
    _record,
    _registry,
)


def _row(text, *, state="queued", notice="pending", seq=0, mid=None, **over):
    row = {"text": text, "tg_message_id": mid, "priority": False,
           "receipt": "not_required", "notice": notice, "notice_text": None,
           "enqueued_at": 1.0, "delivery_epoch": None, "state": state,
           "seq": seq, "is_initial": False, "answer_anchor_mid": None}
    row.update(over)
    return row


def _plant(eid, rows):
    """A durable spool file at the production path, with NO in-memory spool —
    the detached tier ``_inbound_view`` answers from."""
    from drivers.workspace import control_dir, inbound_spool_path

    os.makedirs(control_dir(eid), exist_ok=True)
    path = inbound_spool_path(eid)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def _driver(tmp_path, eid):
    import drivers.claude_code_driver as ccd

    return ccd.ClaudeCodeDriver(
        engagements_root=str(tmp_path / "eng" / eid),
        send_to_topic=AsyncMock(), casa_framework_mcp_url="http://x")


class _Raising(_InboundOnly):
    """The forwarding double, with ONE named accessor made to raise."""

    def __init__(self, real, broken: str):
        super().__init__(real)
        self._broken = broken

    def __getattr__(self, name):
        if name == self._broken:
            def _boom(_eid):
                raise RuntimeError(f"{name} is broken")
            return _boom
        return super().__getattr__(name)


class TestThePopulationsBoundaries:
    """Exactly `queued` ∧ `notice == "pending"` ∧ ¬initial, and nothing else."""

    def _view(self, tmp_path, eid, rows):
        _plant(eid, rows)
        d = _driver(tmp_path, eid)
        return d, d._inbound_view(eid)

    def test_a_queued_row_whose_notice_already_sent_is_not_evicted_pending(
            self, tmp_path):
        """Kills widening the predicate to `notice != "none"`: a loaded
        `queued`/`sent` row (the loader validates no enum) would then be
        quoted although its operator was already told."""
        d, view = self._view(tmp_path, "e-780-sent",
                             [_row("m9", notice="sent", mid=109)])
        assert view.evicted_pending_texts() == []
        assert d.inbound_evicted_pending_texts("e-780-sent") == []
        assert d._disclosed_spool_message_ids("e-780-sent") == frozenset()

    def test_a_capacity_drop_notice_row_contributes_nothing(self, tmp_path):
        """Kills dropping the `state == "queued"` clause: the notice-only row
        `_record_drop_notice` plants is `consumed`/`pending`/`text=""` and
        would print as an empty bullet."""
        d, view = self._view(tmp_path, "e-780-drop",
                             [_row("", state="consumed", mid=112)])
        assert view.evicted_pending_texts() == []
        assert d.inbound_evicted_pending_texts("e-780-drop") == []

    def test_an_initial_envelope_contributes_nothing(self, tmp_path):
        """Kills dropping `not is_initial`: the launch prompt is not an
        operator message."""
        d, view = self._view(tmp_path, "e-780-init",
                             [_row("the task", mid=100, is_initial=True)])
        assert view.evicted_pending_texts() == []

    def test_queued_and_delivered_rows_stay_in_their_own_populations(
            self, tmp_path):
        """Kills any predicate that admits `notice == "none"` rows: those are
        the unread and in-flight populations, quoted already."""
        d, view = self._view(tmp_path, "e-780-none", [
            _row("q", notice="none", seq=0, mid=1),
            _row("d", notice="none", state="delivered", seq=1, mid=2),
        ])
        assert view.unread_texts() == ["q"]
        assert view.in_flight_texts() == ["d"]
        assert view.evicted_pending_texts() == []
        assert view.disclosed_message_ids() == frozenset({1, 2})

    async def test_a_notice_that_finally_sends_retires_the_row(self, tmp_path):
        """The at-least-once notice lane keeps working: once the eviction
        notice is delivered the row is pruned and the population is empty.
        Kills retaining or re-quoting a settled envelope."""
        eid = "e-780-settles"
        real, spool, send_notice = await _evict_with_a_failing_notice(tmp_path, eid)
        assert real.inbound_evicted_pending_texts(eid) == ["m9"]
        send_notice.return_value = True
        await spool.drain()
        assert real.inbound_evicted_pending_texts(eid) == []
        assert spool.has_pending() is False
        assert spool._envelopes == []


class TestTheExclusionJoin:
    """INV-ENG-017: the evicted envelope is PRINTED, so it suppresses the
    aliasing held reservation — printed and excluded together."""

    async def test_a_held_reservation_for_the_evicted_message_is_quoted_once(
            self, tmp_path):
        """Kills omitting the population from `disclosed_message_ids`: the
        text would print twice (spool bullet + reservation bullet). Kills
        adding it to the ids WITHOUT printing it: nothing would print at all.
        The count keeps the pre-existing hedge — one text plus one anonymous
        reservation is "up to 2"."""
        from tools import FinalizeResult, _finalize_engagement

        reg, tch = await _registry(tmp_path)
        rec = await _record(reg, 45)
        real, _spool, _n = await _evict_with_a_failing_notice(
            tmp_path, rec.id, hold_victim=True)
        assert real.inbound_message_reservations(rec.id) == 1
        assert real.inbound_reservation_texts(rec.id) == []
        assert real.inbound_evicted_pending_texts(rec.id) == ["m9"]
        assert real._disclosed_spool_message_ids(rec.id) == frozenset({109})

        result = await _finalize_engagement(
            rec, outcome="cancelled", text="", artifacts=[], next_steps=[],
            driver=_InboundOnly(real))

        assert result is FinalizeResult.FINALIZED
        (_t, payload), _kw = tch.send_response_to_topic.await_args
        assert payload.count("• m9") == 1, payload
        assert payload.count("up to 2 inbound message(s)") == 1, payload
        assert payload.count("• \n") == 0 and not payload.endswith("• "), payload


class TestTheDetachedFileTier:
    """INV-ENG-016: where no incarnation ever attached, the population is read
    from the durable file — and the file tier discloses, never vetoes."""

    async def test_a_file_only_evicted_row_discloses_and_vetoes_nothing(
            self, tmp_path):
        """Kills reading the population from `_inbound` instead of
        `_inbound_view`; kills giving it a veto counterpart."""
        from tools import FinalizeResult, _finalize_engagement

        reg, tch = await _registry(tmp_path)
        rec = await _record(reg, 46)
        _plant(rec.id, [_row("m9", mid=109, seq=9)])
        real = _driver(tmp_path, rec.id)
        assert rec.id not in real._inbound

        assert real.inbound_evicted_pending_texts(rec.id) == ["m9"]
        assert real._disclosed_spool_message_ids(rec.id) == frozenset({109})
        assert real.inbound_unread_depth(rec.id) == 0
        assert real.inbound_in_flight_blocking(rec.id) == 0
        assert real.inbound_reservations(rec.id) == 0

        result = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=_InboundOnly(real), inbound_gate=True)
        assert result is FinalizeResult.FINALIZED
        (_t, payload), _kw = tch.send_response_to_topic.await_args
        assert payload == "Engagement completed. Summary:\ndone" + PARAGRAPH, payload

    def test_no_file_and_no_ledger_answers_empty(self, tmp_path):
        real = _driver(tmp_path, "e-780-absent")
        assert real.inbound_evicted_pending_texts("e-780-absent") == []


class TestItNeverVetoes:
    """The population feeds no veto: the completion tool's precheck, the
    terminal hook's gate and the accessors both ask gates read
    (`inbound_unread_depth`, at `channel_handlers._make_ask`) all see zero.
    Kills folding the population into `unread_texts`/`unread_depth`."""

    async def test_the_completion_tool_precheck_and_hook_admit_the_completion(
            self, tmp_path):
        import agent as agent_mod
        from test_emit_completion_tool import _FakeInboundDriver
        from tools import emit_completion, engagement_var

        reg, tch = await _registry(tmp_path)
        rec = await _record(reg, 47)
        real, _spool, _n = await _evict_with_a_failing_notice(tmp_path, rec.id)
        assert real.inbound_unread_depth(rec.id) == 0
        assert real.inbound_in_flight_blocking(rec.id) == 0
        assert real.inbound_reservations(rec.id) == 0

        drv = _FakeInboundDriver(real=real, real_eid=rec.id)
        agent_mod.active_claude_code_driver = drv
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "artifacts": [], "next_steps": [],
                "status": "ok"})
        finally:
            engagement_var.reset(token)
            agent_mod.active_claude_code_driver = None
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "acknowledged", payload
        assert drv.refusals == []
        assert drv.forced_boundaries == []
        assert reg.get(rec.id).status == "completed"


class TestAccessorFailureIsolation:
    """Each read has its own guard: a failure costs only its own coverage."""

    async def test_the_new_accessor_raising_costs_only_the_evicted_bullet(
            self, tmp_path):
        """Both renderers: with a queued unread message and a held reservation
        present, the new accessor raising leaves the older populations
        printed. Kills sharing the older reads' guard."""
        from tools import _finalize_engagement, _report_launch_death

        reg, tch = await _registry(tmp_path)
        rec = await _record(reg, 48)
        real, spool, _n = await _evict_with_a_failing_notice(tmp_path, rec.id)
        real.reserve_inbound(rec.id, text="held", message_id=500)
        real.reserve_inbound(rec.id, text="q1", message_id=501)
        assert await spool.enqueue("q1", tg_message_id=501) == "queued"
        real.release_inbound_reservation(rec.id, message_id=501)

        await _finalize_engagement(
            rec, outcome="cancelled", text="", artifacts=[], next_steps=[],
            driver=_Raising(real, "inbound_evicted_pending_texts"))
        (_t, payload), _kw = tch.send_response_to_topic.await_args
        assert payload.count("• q1") == 1, payload
        assert payload.count("• held") == 1, payload
        assert payload.count("• m9") == 0, payload
        assert payload.count("up to 2 inbound message(s)") == 1, payload

        rec2 = await _record(reg, 49)
        real2, spool2, _n2 = await _evict_with_a_failing_notice(tmp_path, rec2.id)
        real2.reserve_inbound(rec2.id, text="held", message_id=500)
        channel = _NarrowChannel()
        await _report_launch_death(
            channel, rec2, 49, kind="cutoff", detail="cutoff",
            driver=_Raising(real2, "inbound_evicted_pending_texts"))
        (_t, notice), _kw = channel.send_to_topic.await_args
        assert notice.count("• held") == 1, notice
        assert notice.count("• m9") == 0, notice

    @pytest.mark.parametrize("broken", [
        "inbound_in_flight_texts", "inbound_message_reservations",
        "inbound_reservation_texts", "inbound_reservations",
        "inbound_unread_depth",
    ])
    async def test_an_older_accessor_raising_keeps_the_evicted_bullet_in_the_funnel(
            self, tmp_path, broken):
        """The funnel's own per-accessor guards: the evicted bullet survives
        every older read failing — except the unread-text read, whose
        pre-existing early return skips the whole hook and is not this
        change's to move."""
        from tools import FinalizeResult, _finalize_engagement

        reg, tch = await _registry(tmp_path)
        rec = await _record(reg, 50)
        real, _spool, _n = await _evict_with_a_failing_notice(tmp_path, rec.id)

        result = await _finalize_engagement(
            rec, outcome="cancelled", text="", artifacts=[], next_steps=[],
            driver=_Raising(real, broken))
        assert result is FinalizeResult.FINALIZED
        (_t, payload), _kw = tch.send_response_to_topic.await_args
        assert payload == "Engagement cancelled." + PARAGRAPH, payload

    @pytest.mark.parametrize("broken", [
        "inbound_unread_texts", "inbound_in_flight_texts",
        "inbound_message_reservations", "inbound_reservation_texts",
    ])
    async def test_an_older_accessor_raising_keeps_the_evicted_bullet_in_the_launch_death_notice(
            self, tmp_path, broken):
        from tools import LaunchDeathResult, _report_launch_death

        reg, _tch = await _registry(tmp_path)
        rec = await _record(reg, 51)
        real, _spool, _n = await _evict_with_a_failing_notice(tmp_path, rec.id)
        channel = _NarrowChannel()

        result = await _report_launch_death(
            channel, rec, 51, kind="cutoff", detail="cutoff",
            driver=_Raising(real, broken))
        assert result is LaunchDeathResult.REPORTED
        (_t, notice), _kw = channel.send_to_topic.await_args
        assert notice.count("• m9") == 1, notice
        assert notice.count("1 inbound message(s)") == 1, notice
