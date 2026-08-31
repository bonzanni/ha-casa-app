"""#766 red case — INV-ENG-018: a finalized outcome owes its engager a telling.

Specified by **sol** in the drive redcase round (MODE: SPECIFY) against
``f414c4c6a149aefa08c097b1fbf98ee771cc937e``, with a convergent specification
from terra. Accepted by **terra**.

The invariant this module pins (DECLARED, per D34 — there is no prior durable
announcement obligation on this registry to cite):

    An engagement outcome committed by the finalization funnel carries, in the
    same durable write as its terminal status, the obligation to tell the party
    that asked for the work. That obligation is cleared only by the delivery
    acknowledgement of the notice that carries it — the same head-sent boundary
    every durable announcement uses — never by the bus accepting the message,
    and never by another record's acknowledgement; a record still carrying it
    is exempt from terminal-retention expiry, so the obligation outlives the
    process. Casa's boot owner replays every record still owing one, addressed
    from that record's own persisted origin and reporting the FACT of the
    outcome, never a retained answer the record does not hold; startup awaits
    that owner unguarded, once the channels and the resident loops are running.
    A rolled-back transition owes nothing, a row written before the field
    existed owes nothing, and no other terminal writer arms it.

    NARROWED at acceptance round 3, on both reviewers' prescription (sol:
    ``VERDICT: CUT``). Two clauses claimed more than a test can pin:

      * "discharged only when the resident's own reply reaches the transport"
        was contradicted by this module's own sibling, which allowed one
        record's acknowledgement to clear another's. The assertion is fixed AND
        the text now says "never by another record's acknowledgement", which is
        the property actually pinned.
      * "a restart replays it" was an integration claim about BOOT, and sol
        defeated every pin for it — including argument-name identity, by
        rebinding the name to an empty registry around the call. Pinning it
        would mean booting the application. The text now claims exactly the two
        things that ARE pinned: what the boot owner does, and that startup
        awaits it unguarded after the channels and loops start.

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
    """Real registry on a real tombstone, with EVERY snapshot recorded.

    ``snapshots`` is what makes the "same durable write" clause testable: a
    two-write implementation (commit the terminal, then persist the obligation)
    leaves a snapshot in which the record is terminal and owes nothing, and a
    crash there loses the obligation permanently.
    """
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    tombstone = tmp_path / "engagements.json"
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)

    snapshots: list[list[dict]] = []
    _real_write = reg._write_tombstone

    def _recording_write(snapshot):
        snapshots.append([dict(r) for r in snapshot])
        return _real_write(snapshot)

    reg._write_tombstone = _recording_write
    reg.snapshots = snapshots
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


async def _ack_through_the_real_seam(msg, *, error_kind=None):
    """Drive ``Agent._ack_delivery`` — the production discharge seam.

    Called with the minimum ``self`` the method actually reads (its role, for
    one log line), so the code under test is the shipped method and not a
    re-implementation. ``error_kind is not None`` is the refusal arm: the text
    that reached the transport was the generic turn-failure reply, so the
    engager was told something went wrong, not what their engagement did.
    """
    from types import SimpleNamespace

    from agent import Agent

    stub = SimpleNamespace(config=SimpleNamespace(role="assistant"))
    await Agent._ack_delivery(stub, msg, error_kind)


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

        # ONE durable write carried both facts, and the whole persisted
        # HISTORY says so — not merely the final row.
        #
        # Three mutations this defeats, each of which leaves a crash window
        # that permanently loses the obligation, and each of which a
        # final-row assertion admits:
        #   * terminal first, obligation second  (terminal owing nothing);
        #   * obligation first, terminal second  (a LIVE record owing a
        #     terminal outcome that does not exist);
        #   * clear on acceptance and re-arm     (a window after the bus took
        #     the message in which nothing is owed).
        mine = [
            ([r for r in snap if r["id"] == rec.id] or [None])[0]
            for snap in reg.snapshots
        ]
        mine = [r for r in mine if r is not None]
        first_terminal = next(
            (i for i, r in enumerate(mine) if r["status"] == "completed"), None)
        assert first_terminal is not None, mine

        assert not any(
            r.get("terminal_notification_pending")
            for r in mine[:first_terminal]), mine[:first_terminal]
        assert all(
            r["terminal_notification_pending"] is True
            for r in mine[first_terminal:]), mine[first_terminal:]

        acked_from = len(reg.snapshots)

        # Only the resident reporting delivery discharges it — through the
        # REAL seam (agent.Agent._ack_delivery), not a hand-called callback.
        await _ack_through_the_real_seam(bus.sent[0], error_kind="turn_failed")
        assert _row(tombstone, rec.id)["terminal_notification_pending"] is True
        # Still owed across every write since the terminal.
        assert all(
            [r for r in snap if r["id"] == rec.id][0][
                "terminal_notification_pending"] is True
            for snap in reg.snapshots[first_terminal:acked_from]
            if any(r["id"] == rec.id for r in snap))

        await _ack_through_the_real_seam(bus.sent[0])
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

        The announcement obligation follows the WRITER, not the record, so
        EVERY other terminal writer in the registry is checked — not only
        ``_report_launch_death``. A predicate over the record (the shape #599's
        ``_owes_quiesce`` would suggest) cannot distinguish them: the funnel and
        the launch-death reporter call the same method, with the same outcome,
        on the same kind of record, and only the first announces.
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

        marked_error = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t3", origin=_origin(), topic_id=None)
        marked_cancelled = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t4", origin=_origin(), topic_id=None)
        marked_completed = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t5", origin=_origin(), topic_id=None)
        bare_transition = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t6", origin=_origin(), topic_id=None)

        await _finalize_engagement(
            funnel, outcome="error", text="it broke", artifacts=[],
            next_steps=[], driver=_driver_double())
        await _report_launch_death(
            None, reported, None, kind="launch_turn_incomplete",
            detail="the turn left nothing", driver=None)
        await reg.mark_error(marked_error.id, kind="no_driver", message="x")
        await reg.mark_cancelled(marked_cancelled.id)
        await reg.mark_completed(marked_completed.id, time.time())
        await reg.try_transition_terminal(
            bare_transition.id, "completed", strict=True)

        pending = {r["id"]: r.get("terminal_notification_pending", False)
                   for r in _rows(tombstone)}
        assert pending == {
            funnel.id: True,
            reported.id: False,
            marked_error.id: False,
            marked_cancelled.id: False,
            marked_completed.id: False,
            bare_transition.id: False,
        }, pending
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
