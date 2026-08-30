"""#766 red case — INV-ENG-018: a finalized outcome owes its engager a telling.

Specified by **sol** in the drive redcase round (MODE: SPECIFY) against
``f414c4c6a149aefa08c097b1fbf98ee771cc937e``, with a convergent specification
from terra. Accepted by **terra**.

The invariant this module pins (DECLARED, per D34 — there is no prior durable
announcement obligation on this registry to cite):

    An engagement outcome committed by the finalization funnel carries, in the
    same durable write as its terminal status, the obligation to tell the party
    that asked for the work. That obligation is discharged only when the
    consuming resident's own reply reaches the transport — the same head-sent
    boundary every durable announcement uses — and never by the bus accepting
    the message; a record still carrying it is exempt from terminal-retention
    expiry, so a restart replays it, addressed from the record's own persisted
    origin, reporting the FACT of the outcome and never a retained answer the
    record does not hold. A rolled-back transition owes nothing, a row written
    before the field existed owes nothing, and no other terminal writer arms it.

Pre-fix terminus, read at ``f414c4c6``:

  - ``tools.py:9407-9425`` — the funnel's ``_bus.notify(BusMessage(...))``, with
    no ``on_delivery=`` keyword.
  - ``engagement_registry.py:716-747`` — the tombstone encoder's explicit
    literal dict, carrying no announcement field.
  - ``tools.py:3430-3444`` — ``_settle_then_announce``, one module away, which
    DOES pass ``on_delivery=_ack``: the shape being mirrored.
  - ``agent.py:1047-1085`` — ``Agent._ack_delivery``, which defines the
    head-sent discharge boundary this declaration is scoped to.

Every assertion reads the TOMBSTONE FILE, never an in-memory record, because
the whole claim is about what survives a restart.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio]


def _driver_double():
    """Shaped like the REAL engagement drivers — see test_finalize_engagement."""
    d = MagicMock()
    d.cancel = AsyncMock()
    for hook in ("finalize_completion_post", "finalize_summary",
                 "settle_all_open_questions", "drain_inbound_spool"):
        delattr(d, hook)
    return d


class _CapturingBus:
    """Accepts every message and NEVER invokes its delivery callback.

    That is the whole point: bus acceptance is not delivery, so nothing this
    class does may discharge an obligation.
    """

    def __init__(self) -> None:
        self.sent: list = []
        self.queues: dict = {}

    async def notify(self, msg) -> None:
        self.sent.append(msg)


def _rows(tombstone) -> list[dict]:
    return json.loads(tombstone.read_text())


def _row(tombstone, engagement_id: str) -> dict:
    hits = [r for r in _rows(tombstone) if r["id"] == engagement_id]
    assert len(hits) == 1, hits
    return hits[0]


def _origin(role="assistant", chat="12345", cid="route-1"):
    return {"role": role, "channel": "telegram", "chat_id": chat,
            "cid": cid, "user_text": "please do the thing"}


async def _build(tmp_path, *, bus=None):
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    tombstone = tmp_path / "engagements.json"
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    bus = bus if bus is not None else _CapturingBus()

    channel = MagicMock()
    channel.send_to_topic = AsyncMock()
    channel.send_response_to_topic = AsyncMock()
    channel.close_topic = AsyncMock()
    channel.update_topic_state = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = channel

    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    return reg, bus, tombstone


class TestTheObligationIsArmedWithTheTerminalAndClearedOnlyByDelivery:

    async def test_a_finalized_outcome_owes_a_telling_that_acceptance_does_not_clear(
        self, tmp_path,
    ):
        from tools import FinalizeResult, _finalize_engagement

        reg, bus, tombstone = await _build(tmp_path)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin=_origin(), topic_id=None,
        )

        result = await _finalize_engagement(
            rec, outcome="completed", text="the answer", artifacts=[],
            next_steps=[], driver=_driver_double(),
        )

        assert result is FinalizeResult.FINALIZED
        row = _row(tombstone, rec.id)
        # ONE durable write carries both facts.
        assert (row["status"], row["terminal_notification_pending"]) == (
            "completed", True), row
        assert [r.id for r in reg.records_owing_terminal_notification()] == [
            rec.id]

        # The bus accepted it. That is not a telling.
        assert len(bus.sent) == 1
        assert _row(tombstone, rec.id)["terminal_notification_pending"] is True

        # The record never retains the answer, so a replay cannot quote one.
        assert "text" not in row and "artifacts" not in row
        assert "next_steps" not in row

        # Only the resident reporting delivery discharges it.
        assert bus.sent[0].on_delivery is not None
        await bus.sent[0].on_delivery()
        assert _row(tombstone, rec.id)["terminal_notification_pending"] is False
        assert reg.records_owing_terminal_notification() == []

    async def test_a_rolled_back_finalize_owes_nothing(self, tmp_path):
        from tools import FinalizeResult, _finalize_engagement

        reg, bus, tombstone = await _build(tmp_path)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin=_origin(), topic_id=None,
        )

        real = reg._write_tombstone

        def _write(snapshot):
            if any(r.get("status") == "completed" for r in snapshot):
                raise OSError("tombstone volume is gone")
            return real(snapshot)

        reg._write_tombstone = _write

        result = await _finalize_engagement(
            rec, outcome="completed", text="the answer", artifacts=[],
            next_steps=[], driver=_driver_double(),
        )

        assert result is FinalizeResult.PERSIST_FAILED
        row = _row(tombstone, rec.id)
        assert (row["status"],
                row.get("terminal_notification_pending", False)) == (
                    "active", False), row
        assert reg.get(rec.id).status == "active"
        assert reg.get(rec.id).terminal_notification_pending is False
        assert reg.records_owing_terminal_notification() == []
        assert bus.sent == []

    async def test_a_legacy_row_without_the_field_owes_nothing(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement

        reg, bus, tombstone = await _build(tmp_path)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin=_origin(), topic_id=None,
        )
        await _finalize_engagement(
            rec, outcome="completed", text="x", artifacts=[], next_steps=[],
            driver=_driver_double())

        # Age the file back to what a pre-upgrade Casa wrote.
        rows = _rows(tombstone)
        for r in rows:
            r.pop("terminal_notification_pending", None)
        tombstone.write_text(json.dumps(rows))

        reloaded = EngagementRegistry(
            tombstone_path=str(tombstone), bus=None)
        await reloaded.load()

        assert reloaded.get(rec.id).terminal_notification_pending is False
        assert reloaded.records_owing_terminal_notification() == []

    async def test_only_the_finalization_funnel_arms_the_obligation(
        self, tmp_path,
    ):
        """The over-arming pin.

        A predicate that arms on any terminal ``error`` — the shape #599's
        record-derived ``_owes_quiesce`` would suggest — makes both of the
        other two records owe a telling they were explicitly designed not to
        owe. ``_report_launch_death`` writes a strict ``error`` terminal and is
        documented never to touch the bus.
        """
        import tools as tools_mod
        from tools import _finalize_engagement, _report_launch_death

        reg, bus, tombstone = await _build(tmp_path)

        funnel = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin=_origin(), topic_id=None)
        reported = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t2", origin=_origin(), topic_id=None)

        await _finalize_engagement(
            funnel, outcome="error", text="it broke", artifacts=[],
            next_steps=[], driver=_driver_double())
        await _report_launch_death(
            None, reported, None, kind="launch_turn_incomplete",
            detail="the turn left nothing", driver=None)

        pending = {r["id"]: r.get("terminal_notification_pending", False)
                   for r in _rows(tombstone)}
        assert pending == {funnel.id: True, reported.id: False}, pending
        assert [r.id for r in reg.records_owing_terminal_notification()] == [
            funnel.id]

    async def test_an_owing_terminal_is_exempt_from_retention_expiry(
        self, tmp_path,
    ):
        """A month-old obligation is still an obligation.

        Without the exemption the row ages out unannounced, and the party that
        asked for the work is permanently untold — the precise harm this
        invariant exists to close.
        """
        from tools import _finalize_engagement

        reg, bus, tombstone = await _build(tmp_path)
        owing = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="owed", origin=_origin(), topic_id=None)
        settled = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="settled", origin=_origin(), topic_id=None)

        for rec in (owing, settled):
            await _finalize_engagement(
                rec, outcome="completed", text="x", artifacts=[],
                next_steps=[], driver=_driver_double())
        await reg.ack_terminal_notification(settled.id)

        # Age both past the 30-day terminal retention, then force a rewrite.
        old = time.time() - 31 * 86400
        for rec in (owing, settled):
            reg.get(rec.id).completed_at = old
        # Any later write re-runs the expiry filter over the whole set.
        await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="a later engagement", origin=_origin(), topic_id=None)

        surviving_terminals = {
            r["id"] for r in _rows(tombstone)
            if r["status"] in ("completed", "cancelled", "error")}
        assert surviving_terminals == {owing.id}, _rows(tombstone)
        assert _row(tombstone, owing.id)["terminal_notification_pending"] is True
