"""#766 red case — INV-ENG-018's restart half: the boot replay of an owed telling.

Specified by **sol** in the drive redcase round (MODE: SPECIFY) against
``f414c4c6a149aefa08c097b1fbf98ee771cc937e``; the two-record identity
assertions are sol's, added because a raw loop closure over the owing records
reproduced ``A delivery → B ack`` in the seam round. Accepted by **terra**.

Deliberately a NEW module: ``tests/test_boot_replay.py`` is being rewritten in
parallel and must not be edited.

At the base there is NO engagement boot replay of an outcome at all — the only
boot walk over ``terminal_records()`` is ``casa_core.reconcile_terminal_spools``
(``casa_core.py:1786-1808``), which drains an inbound spool into the Telegram
topic and enqueues nothing on the bus. So the owner under test does not exist
and these cases are red by construction.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio]


class _RecordingBus:
    def __init__(self, roles=("assistant", "concierge")) -> None:
        self.sent: list = []
        self.queues = {r: MagicMock() for r in roles}

    async def notify(self, msg) -> None:
        self.sent.append(msg)


def _driver_double():
    d = MagicMock()
    d.cancel = AsyncMock()
    for hook in ("finalize_completion_post", "finalize_summary",
                 "settle_all_open_questions", "drain_inbound_spool"):
        delattr(d, hook)
    return d


def _rows(tombstone) -> list[dict]:
    return json.loads(tombstone.read_text())


def _row(tombstone, engagement_id: str) -> dict:
    hits = [r for r in _rows(tombstone) if r["id"] == engagement_id]
    assert len(hits) == 1, hits
    return hits[0]


async def _owing_records(tmp_path):
    """Persist two outcomes that were never told, then RELOAD from disk.

    The reload is the point: everything the replay may say has to have
    survived the tombstone, so the test reads it back rather than reusing the
    live objects.
    """
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    tombstone = tmp_path / "engagements.json"
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)

    channel = MagicMock()
    channel.send_to_topic = AsyncMock()
    channel.send_response_to_topic = AsyncMock()
    channel.close_topic = AsyncMock()
    channel.update_topic_state = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = channel

    init_tools(
        channel_manager=cm, bus=_RecordingBus(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    a = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="tidy the plugins",
        origin={"role": "concierge", "channel": "telegram",
                "chat_id": "chat-A", "cid": "route-A",
                "user_text": "tidy the plugins please"},
        topic_id=None)
    b = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="plan Q2",
        origin={"role": "assistant", "channel": "telegram",
                "chat_id": "chat-B", "cid": "route-B",
                "user_text": "plan Q2 please"},
        topic_id=None)

    await _finalize_engagement(
        a, outcome="completed", text="all tidy", artifacts=[], next_steps=[],
        driver=_driver_double())
    await _finalize_engagement(
        b, outcome="cancelled", text="", artifacts=[], next_steps=[],
        driver=_driver_double())

    c = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="reconcile the ledger",
        origin={"role": "assistant", "channel": "telegram",
                "chat_id": "chat-D", "cid": "route-D",
                "user_text": "reconcile the ledger please"},
        topic_id=None)
    await _finalize_engagement(
        c, outcome="error", text="", artifacts=[], next_steps=[],
        driver=_driver_double())

    # A third row that was already told, and so owes nothing. Written into
    # the file directly rather than through the ack, so this fixture does not
    # depend on the very machinery the replay owner is being tested against —
    # a pre-fix failure must land in the OWNER, not in the setup.
    told = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="already told",
        origin={"role": "assistant", "channel": "telegram",
                "chat_id": "chat-C", "cid": "route-C", "user_text": "c"},
        topic_id=None)
    await _finalize_engagement(
        told, outcome="completed", text="done", artifacts=[], next_steps=[],
        driver=_driver_double())

    rows = _rows(tombstone)
    for r in rows:
        if r["id"] == told.id:
            r["terminal_notification_pending"] = False
    tombstone.write_text(json.dumps(rows))

    reloaded = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reloaded.load()
    return reloaded, tombstone, a.id, b.id, c.id, told.id


class TestBootReplaysOnlyWhatIsStillOwed:

    async def test_two_owing_outcomes_replay_to_their_persisted_origins(
        self, tmp_path,
    ):
        import casa_core

        reg, tombstone, a_id, b_id, c_id, told_id = await _owing_records(tmp_path)
        bus = _RecordingBus()

        await casa_core._notify_recovered_engagement_outcomes(
            reg, bus, assistant_role="assistant")

        by_id = {m.content.delegation_id: m for m in bus.sent}
        assert set(by_id) == {a_id, b_id, c_id}, sorted(by_id)
        assert told_id not in by_id

        a_msg, b_msg, c_msg = by_id[a_id], by_id[b_id], by_id[c_id]

        # Addressed from the record's own persisted origin.
        assert (a_msg.target, a_msg.channel,
                a_msg.context["chat_id"], a_msg.context["cid"]) == (
                    "concierge", "telegram", "chat-A", "route-A")
        assert (b_msg.target, b_msg.channel,
                b_msg.context["chat_id"], b_msg.context["cid"]) == (
                    "assistant", "telegram", "chat-B", "route-B")
        # The error arm is addressed from ITS OWN origin: a replay routed to
        # another record's chat tells the wrong person about the wrong work.
        assert (c_msg.target, c_msg.channel,
                c_msg.context["chat_id"], c_msg.context["cid"]) == (
                    "assistant", "telegram", "chat-D", "route-D")
        assert c_msg.content.origin["user_text"] == "reconcile the ledger please"

        # The FACT of the outcome, never a retained answer.
        assert a_msg.content.status == "ok"
        assert a_msg.content.result_available is False
        assert a_msg.content.text == ""
        assert a_msg.content.kind == ""

        # The EXACT outcome, not merely "something went wrong". A replay
        # reporting a cancellation as `restart_orphan`, or an error as a
        # cancellation, misstates what happened to the engager's work — and a
        # status-only assertion admits both.
        assert b_msg.content.status == "error"
        assert b_msg.content.kind == "cancelled"
        assert b_msg.content.result_available is False
        assert b_msg.content.text == ""
        assert c_msg.content.status == "error"
        assert c_msg.content.kind == "error"
        assert c_msg.content.result_available is False
        assert c_msg.content.text == ""
        # Both non-ok arms render `message`; an empty one tells the engager
        # nothing at all (`Delegation failed (cancelled): `).
        assert b_msg.content.message.strip() != ""
        assert c_msg.content.message.strip() != ""

    async def test_each_callback_acknowledges_the_id_it_owns(self, tmp_path):
        """The late-binding pin.

        A raw loop closure over the owing records binds the LAST record, so
        acknowledging A's message clears B. Counting callbacks cannot see that;
        only asserting which id was cleared can.
        """
        import casa_core

        reg, tombstone, a_id, b_id, c_id, _told = await _owing_records(tmp_path)
        bus = _RecordingBus()

        await casa_core._notify_recovered_engagement_outcomes(
            reg, bus, assistant_role="assistant")
        by_id = {m.content.delegation_id: m for m in bus.sent}

        await by_id[a_id].on_delivery()

        assert _row(tombstone, a_id)["terminal_notification_pending"] is False
        assert _row(tombstone, b_id)["terminal_notification_pending"] is True
        assert {r.id for r in reg.records_owing_terminal_notification()} == {
            b_id, c_id}

        await by_id[b_id].on_delivery()
        assert _row(tombstone, b_id)["terminal_notification_pending"] is False
        # C is still owed. Asserting an EMPTY set here would admit an
        # implementation in which B's callback also clears C — an outcome that
        # never reached the transport, discarded.
        assert _row(tombstone, c_id)["terminal_notification_pending"] is True
        assert {r.id for r in reg.records_owing_terminal_notification()} == {
            c_id}

        await by_id[c_id].on_delivery()
        assert _row(tombstone, c_id)["terminal_notification_pending"] is False
        assert reg.records_owing_terminal_notification() == []

    async def test_an_unroutable_role_retains_the_obligation_for_the_next_boot(
        self, tmp_path,
    ):
        """Enqueue is not delivery, and a missing consumer is not a discharge."""
        import casa_core

        reg, tombstone, a_id, b_id, c_id, _told = await _owing_records(tmp_path)
        bus = _RecordingBus(roles=("assistant",))     # no `concierge` queue

        await casa_core._notify_recovered_engagement_outcomes(
            reg, bus, assistant_role="assistant")

        assert sorted(m.content.delegation_id for m in bus.sent) == sorted(
            [b_id, c_id])
        assert _row(tombstone, a_id)["terminal_notification_pending"] is True
        assert a_id in [r.id for r in reg.records_owing_terminal_notification()]

    async def test_acceptance_without_a_consumer_replays_at_the_next_boot(
        self, tmp_path,
    ):
        """The #701 lesson, restated for this funnel: a queued message that no
        resident ever consumed leaves the obligation exactly where it was."""
        import casa_core
        from engagement_registry import EngagementRegistry

        reg, tombstone, a_id, b_id, c_id, _told = await _owing_records(tmp_path)
        first = _RecordingBus()
        await casa_core._notify_recovered_engagement_outcomes(
            reg, first, assistant_role="assistant")
        assert len(first.sent) == 3

        # The process dies. Nothing was delivered; nothing was acknowledged.
        restarted = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await restarted.load()
        assert {r.id for r in
                restarted.records_owing_terminal_notification()} == {
                    a_id, b_id, c_id}

        second = _RecordingBus()
        await casa_core._notify_recovered_engagement_outcomes(
            restarted, second, assistant_role="assistant")
        assert {m.content.delegation_id for m in second.sent} == {
            a_id, b_id, c_id}


class TestBootActuallyInvokesTheReplayOwner:
    """A replay owner boot never calls is not a replay.

    An earlier version of this walked the whole AST, and sol defeated it with a
    compilable mutation — wrapping the call in ``if False:`` left every
    assertion green while no owed row was ever replayed. So the call must be a
    TOP-LEVEL awaited statement in ``main``'s own body, unguarded, carrying the
    production registry and bus, and positioned after the top-level statements
    that start the channels and the agent loops. A replay enqueued before a
    consumer can run is not a replay, and one behind a guard is not a call.
    """

    async def test_main_replays_owed_outcomes_after_the_agent_loops_start(self):
        import ast
        import inspect
        import textwrap

        import casa_core

        tree = ast.parse(textwrap.dedent(inspect.getsource(casa_core.main)))
        body = tree.body[0].body

        def _first_top_level(name: str):
            """Index of the first TOP-LEVEL statement mentioning `name`."""
            for i, stmt in enumerate(body):
                for node in ast.walk(stmt):
                    fn = getattr(node, "func", None)
                    if fn is None:
                        continue
                    got = (fn.id if isinstance(fn, ast.Name)
                           else fn.attr if isinstance(fn, ast.Attribute) else "")
                    if got == name:
                        return i
            return None

        replay = None
        for i, stmt in enumerate(body):
            # Expr(Await(Call)) at the TOP level of main — not nested inside an
            # `if`, a `try`, a `with` or a loop.
            if not isinstance(stmt, ast.Expr):
                continue
            if not isinstance(stmt.value, ast.Await):
                continue
            call = stmt.value.value
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            got = (fn.id if isinstance(fn, ast.Name)
                   else fn.attr if isinstance(fn, ast.Attribute) else "")
            if got == "_notify_recovered_engagement_outcomes":
                replay = (i, call)
                break

        assert replay is not None, (
            "main must AWAIT _notify_recovered_engagement_outcomes as an "
            "unguarded top-level statement")
        index, call = replay

        # The production collaborators, not stand-ins.
        passed = {a.id for a in call.args if isinstance(a, ast.Name)}
        passed |= {kw.value.id for kw in call.keywords
                   if isinstance(kw.value, ast.Name)}
        assert "engagement_registry" in passed, sorted(passed)
        assert "bus" in passed, sorted(passed)

        started_channels = _first_top_level("start_all")
        started_loops = _first_top_level("start_agent_loop")
        assert started_channels is not None and started_loops is not None
        assert index > started_channels, (index, started_channels)
        assert index > started_loops, (index, started_loops)


class TestTheReplayOwnerCannotStopBootOrLie:
    """Diff-review round 1 (sol S2, terra S2 x2). This owner is awaited
    UNGUARDED from `main`, and every value it reads came off disk as JSON."""

    async def test_an_unhashable_persisted_role_does_not_raise_out_of_boot(
        self, tmp_path,
    ):
        """`origin["role"]` is a string only by convention.

        A list reaches the `in bus.queues` membership test and raises
        `TypeError: unhashable type`, which — from an unguarded `await` in
        `main` — stops boot for every other record too.
        """
        import casa_core

        reg, tombstone, a_id, b_id, c_id, _told = await _owing_records(tmp_path)
        reg.get(a_id).origin["role"] = ["assistant"]     # a corrupt row
        bus = _RecordingBus()

        await casa_core._notify_recovered_engagement_outcomes(
            reg, bus, assistant_role="assistant")

        # Boot survived and no record was lost. The corrupt role falls back to
        # the assistant — the same shape the delegation replay uses for a row
        # with no creating role — rather than raising or being dropped.
        assert {m.content.delegation_id for m in bus.sent} == {
            a_id, b_id, c_id}
        assert [m.target for m in bus.sent
                if m.content.delegation_id == a_id] == ["assistant"]
        # Still owed until its notice is actually delivered.
        assert _row(tombstone, a_id)["terminal_notification_pending"] is True

    async def test_an_owed_row_that_is_not_terminal_is_retained_not_announced(
        self, tmp_path,
    ):
        """The obligation is only ever armed WITH a terminal status, so this
        shape means a corrupt tombstone. Announcing it would tell the engager
        that a still-resumable engagement ended in an error — and the delivery
        would then discharge the obligation for good."""
        import casa_core

        reg, tombstone, a_id, b_id, c_id, _told = await _owing_records(tmp_path)
        reg.get(a_id).status = "idle"
        bus = _RecordingBus()

        await casa_core._notify_recovered_engagement_outcomes(
            reg, bus, assistant_role="assistant")

        assert {m.content.delegation_id for m in bus.sent} == {b_id, c_id}
        assert a_id in [
            r.id for r in reg.records_owing_terminal_notification()]

    async def test_a_recovered_success_is_not_narrated_as_a_restart_casualty(
        self, tmp_path,
    ):
        """The shared recovery prose must be true of BOTH producers.

        A delegation row really did finish during the restart. An ENGAGEMENT
        outcome may have finished long before it, and its summary may well have
        been retained elsewhere — so "completed during a Casa restart" and "the
        answer itself is gone" told the operator something false about durable
        state, and invited a needless re-run. What is true of both is only that
        THIS NOTICE does not carry the answer.
        """
        from types import SimpleNamespace

        import casa_core
        from agent import Agent

        reg, tombstone, a_id, _b, _c, _told = await _owing_records(tmp_path)
        bus = _RecordingBus()
        await casa_core._notify_recovered_engagement_outcomes(
            reg, bus, assistant_role="assistant")
        msg = [m for m in bus.sent if m.content.delegation_id == a_id][0]

        stub = SimpleNamespace(config=SimpleNamespace(role="concierge"))
        body = Agent._synthesize_delegation_turn(stub, msg).content

        assert "during a Casa restart" not in body, body
        assert "the answer itself is gone" not in body, body
        assert "this recovery notice does not carry its answer" in body, body
        # Still tells the engager the work finished and quotes the request.
        assert "finished" in body
        assert "tidy the plugins please" in body


    async def test_a_malformed_persisted_id_cannot_escape_the_guard(
        self, tmp_path,
    ):
        """The guard must not raise while reporting a failure.

        `rec.id` is a string by convention only. A hand-edited or corrupt row
        carrying an integer made `rec.id[:8]` raise — and the per-record
        handler sliced the same id again, so the TypeError escaped the
        unguarded await in `main` and stopped boot for every record after it.
        """
        import casa_core

        reg, tombstone, a_id, b_id, c_id, _told = await _owing_records(tmp_path)
        broken = reg.get(a_id)
        broken.id = 12345                      # a corrupt row, as loaded
        bus = _RecordingBus()

        await casa_core._notify_recovered_engagement_outcomes(
            reg, bus, assistant_role="assistant")

        # Boot survived; the malformed row was retained, not announced; and the
        # records after it in the walk were still told.
        assert {m.content.delegation_id for m in bus.sent} == {b_id, c_id}
        assert broken.terminal_notification_pending is True

    async def test_the_recovery_narration_promises_no_lookup(self, tmp_path):
        """The shared branch also serves the DELEGATION replay, where #688
        decided nothing is retained — so offering to "look it up" sends the
        resident after something that provably does not exist."""
        from types import SimpleNamespace

        import casa_core
        from agent import Agent

        reg, tombstone, a_id, _b, _c, _told = await _owing_records(tmp_path)
        bus = _RecordingBus()
        await casa_core._notify_recovered_engagement_outcomes(
            reg, bus, assistant_role="assistant")
        msg = [m for m in bus.sent if m.content.delegation_id == a_id][0]

        stub = SimpleNamespace(config=SimpleNamespace(role="concierge"))
        body = Agent._synthesize_delegation_turn(stub, msg).content

        assert "look it up" not in body.split("do NOT promise")[0], body
        assert "offer to run it again" in body, body
