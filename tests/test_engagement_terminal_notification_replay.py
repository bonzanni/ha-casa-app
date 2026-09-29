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
        """This branch carries no answer, whichever producer reached it — so
        offering to "look it up" sends the resident after something that is not
        there.

        #688 changed which notices reach here without changing this test's
        subject. A replayed DELEGATION whose row retained its answer now takes
        the ordinary success branch instead; what still arrives here is the
        ENGAGEMENT producer, which never carries an answer, and a delegation row
        that retained none. The promise is refused for the same reason as
        before: this notice is all there is."""
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


# ---------------------------------------------------------------------------
# #926 regression guard: the live engagement finalization never marks its
# notice as a boot replay, and its prompt carries no replay statement. The
# engagement boot replay DOES mark its own notices (#1087), below.
# ---------------------------------------------------------------------------


def _synth_body(msg) -> str:
    from unittest.mock import Mock
    from agent import Agent
    return Agent._synthesize_delegation_turn(Mock(), msg).content


async def test_live_engagement_finalization_is_not_marked_as_a_boot_replay(
    tmp_path,
):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    channel = MagicMock()
    channel.send_to_topic = AsyncMock()
    channel.send_response_to_topic = AsyncMock()
    channel.close_topic = AsyncMock()
    channel.update_topic_state = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = channel
    bus = _RecordingBus()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    eng = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="plan Q2",
        origin={"role": "assistant", "channel": "telegram",
                "chat_id": "chat-B", "cid": "route-B",
                "user_text": "plan Q2 please"},
        topic_id=None)
    await _finalize_engagement(
        eng, outcome="completed", text="the plan", artifacts=[],
        next_steps=[], driver=_driver_double())

    assert len(bus.sent) == 1
    assert bus.sent[0].content.replayed_after_restart is False
    body = _synth_body(bus.sent[0])
    assert body.count("re-announcement") == 0
    assert body.count("Result text from finance:\nthe plan\n") == 1




# ---------------------------------------------------------------------------
# #1087 red cases — INV-ENG-018's replay clause: an engagement outcome replayed
# at boot says it is an earlier outcome reported after a restart, and when its
# record went terminal (or that the time is unknown).
#
# Specified by **astra** in the drive redcase round (MODE: SPECIFY) against
# ``17bd039afdf3710c3dd3ddee902420c9991e58f5``. They replace the pin that
# asserted the defect (``..._is_not_marked_as_a_delegation_replay``). The
# expected statement is written out literally here — never derived from the
# production constant or time helper — so a change to either is caught.
# ---------------------------------------------------------------------------

_OUTCOME_STATEMENT_1087 = (
    "This is a post-restart re-announcement. This outcome was reached before "
    "a Casa restart, and its announcement to the user was never confirmed as "
    "delivered. Tell the user it is an earlier result being reported after a "
    "restart — do NOT present it as something that just happened."
)
_UNKNOWN_TIME_1087 = (
    "Casa has no record of when this outcome was recorded, so its age is "
    "unknown."
)
_RECORDED_AT_1087 = 1790596800          # 2026-09-28T12:00:00+00:00
_CLOCK_1087 = 1790604000                # two hours later


async def _replay_with_completed_at(tmp_path, values):
    """The fixture's three owing rows, their ``completed_at`` rewritten on
    disk to ``values`` (A, B, C) and RELOADED, then replayed by the real owner.
    """
    import casa_core
    from engagement_registry import EngagementRegistry

    _reg, tombstone, a_id, b_id, c_id, told_id = await _owing_records(tmp_path)
    by_id = dict(zip((a_id, b_id, c_id), values))
    rows = _rows(tombstone)
    for r in rows:
        if r["id"] in by_id:
            r["completed_at"] = by_id[r["id"]]
    tombstone.write_text(json.dumps(rows))

    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reg.load()
    bus = _RecordingBus()
    await casa_core._notify_recovered_engagement_outcomes(
        reg, bus, assistant_role="assistant")
    return reg, tombstone, bus, (a_id, b_id, c_id), told_id, by_id


def _freeze_replay_clock(monkeypatch):
    from datetime import timezone

    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "_replay_clock", lambda: _CLOCK_1087)
    monkeypatch.setattr(agent_mod, "resolve_tz", lambda: timezone.utc)


def _assert_one_block_after_header(msg, block):
    """The replayed prompt is the same notice synthesized unflagged, with
    exactly ``block`` inserted right after the header."""
    import copy
    import dataclasses

    body = _synth_body(msg)
    unflagged = copy.copy(msg)
    unflagged.content = dataclasses.replace(
        msg.content, replayed_after_restart=False)
    plain = _synth_body(unflagged)
    head, sep, rest = plain.partition("]\n\n")
    assert sep, plain
    assert body == head + sep + block + rest, body
    assert body.count(_OUTCOME_STATEMENT_1087) == 1, body
    return body


async def test_replayed_engagement_outcomes_carry_one_timed_replay_block(
    tmp_path, monkeypatch,
):
    _freeze_replay_clock(monkeypatch)
    reg, tombstone, bus, ids, told_id, _ = await _replay_with_completed_at(
        tmp_path, [_RECORDED_AT_1087] * 3)
    a_id, b_id, c_id = ids

    assert len(bus.sent) == 3
    by_id = {m.content.delegation_id: m for m in bus.sent}
    assert set(by_id) == {a_id, b_id, c_id}
    assert told_id not in by_id

    expected = {
        a_id: ("ok", ""), b_id: ("error", "cancelled"), c_id: ("error", "error"),
    }
    block = (
        _OUTCOME_STATEMENT_1087 + " Casa recorded this outcome at "
        "2026-09-28T12:00:00+00:00 (about 2 hours ago).\n\n"
    )
    for eid, msg in by_id.items():
        c = msg.content
        assert c.replayed_after_restart is True
        assert c.terminal_at == _RECORDED_AT_1087
        assert (c.status, c.kind) == expected[eid]
        assert c.result_available is False
        body = _assert_one_block_after_header(msg, block)
        # #766: the answerless wording stays timing- and storage-neutral.
        assert "during a Casa restart" not in body
        assert "the answer itself is gone" not in body

    # Enqueueing is not a telling: all three are still owed, in memory and
    # on disk.
    assert {r.id for r in reg.records_owing_terminal_notification()} == {
        a_id, b_id, c_id}
    for eid in ids:
        assert _row(tombstone, eid)["terminal_notification_pending"] is True


@pytest.mark.parametrize("values", [
    [None, "yesterday", True],
    [10**400, None, True],
], ids=["none-string-bool", "oversized-int"])
async def test_replayed_engagement_outcomes_with_unusable_times_still_become_prompts(
    tmp_path, monkeypatch, values,
):
    _freeze_replay_clock(monkeypatch)
    _reg, _tombstone, bus, ids, _told, by_id = await _replay_with_completed_at(
        tmp_path, values)

    assert len(bus.sent) == 3
    assert {m.content.delegation_id for m in bus.sent} == set(ids)
    block = _OUTCOME_STATEMENT_1087 + " " + _UNKNOWN_TIME_1087 + "\n\n"
    prompts = 0
    for msg in bus.sent:
        c = msg.content
        persisted = by_id[c.delegation_id]
        assert c.replayed_after_restart is True
        assert type(c.terminal_at) is type(persisted)
        assert c.terminal_at == persisted
        body = _assert_one_block_after_header(msg, block)
        assert body.count(_UNKNOWN_TIME_1087) == 1
        assert body.count("Casa recorded") == 0
        prompts += 1
    assert prompts == 3


# ---------------------------------------------------------------------------
# #1093 + #1094 red cases — INV-ENG-018. Specified by **astra** in the drive
# redcase round (MODE: SPECIFY) against
# ``59ed58d8cff57ac9c0961fc8ee15166d6b2186e3``.
#
# #1093: the boot replay tells only what the previous process left owed. A
# record finalized live in THIS process before the replay runs is told once,
# live, and never as a pre-restart outcome; left undelivered, it is the NEXT
# boot's to replay.
#
# #1094: a terminal row whose persisted ``completed_at`` is not a usable number
# never makes a registry write raise — the owed-notice ack persists, and load()'s
# reconcile write does not fail — and the value is kept exactly as persisted.
# ---------------------------------------------------------------------------


async def _one_owed_previous_process_row(tmp_path):
    """The fixture's completed ``a_id`` row alone: a real funnel-finalized
    outcome from a previous process, still owing its telling."""
    _reg, tombstone, a_id, _b, _c, _told = await _owing_records(tmp_path)
    tombstone.write_text(json.dumps([_row(tombstone, a_id)]))
    return tombstone, a_id


def _live_tools(reg):
    from tools import init_tools

    channel = MagicMock()
    channel.send_to_topic = AsyncMock()
    channel.send_response_to_topic = AsyncMock()
    channel.close_topic = AsyncMock()
    channel.update_topic_state = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = channel
    bus = _RecordingBus()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    return bus


def _notice_counts(messages):
    from collections import Counter
    return Counter(
        (m.content.delegation_id, m.content.replayed_after_restart)
        for m in messages
    )


async def test_boot_replays_loaded_owed_outcome_but_defers_live_outcome(
    tmp_path,
):
    from collections import Counter

    import casa_core
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement

    tombstone, old_id = await _one_owed_previous_process_row(tmp_path)
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reg.load()
    bus = _live_tools(reg)

    # After load() returns and before the replay runs: an engagement ends in
    # THIS process, through the live funnel.
    live = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="live task", topic_id=None,
        origin={"role": "assistant", "channel": "telegram",
                "chat_id": "live-chat", "cid": "live-route",
                "user_text": "live task"})
    await _finalize_engagement(
        live, outcome="completed", text="live result", artifacts=[],
        next_steps=[], driver=_driver_double())
    assert _notice_counts(bus.sent) == Counter({(live.id, False): 1})

    await casa_core._notify_recovered_engagement_outcomes(
        reg, bus, assistant_role="assistant")
    first = _notice_counts(bus.sent)

    # No delivery callback and no acknowledgement ran: both are still owed.
    assert len(_rows(tombstone)) == 2
    assert sum(
        r["terminal_notification_pending"] is True
        for r in _rows(tombstone)
    ) == 2

    restarted = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await restarted.load()
    second = _RecordingBus()
    await casa_core._notify_recovered_engagement_outcomes(
        restarted, second, assistant_role="assistant")

    assert first == Counter({
        (old_id, True): 1,
        (live.id, False): 1,
    })
    assert _notice_counts(second.sent) == Counter({
        (old_id, True): 1,
        (live.id, True): 1,
    })


_UNUSABLE_COMPLETED_AT = pytest.mark.parametrize(
    "value",
    ["yesterday", ["yesterday"], {"when": "yesterday"}],
    ids=["string", "list", "dict"],
)


@_UNUSABLE_COMPLETED_AT
async def test_ack_persists_with_unusable_completed_at_unchanged(
    tmp_path, value,
):
    from engagement_registry import EngagementRegistry

    tombstone, a_id = await _one_owed_previous_process_row(tmp_path)
    rows = _rows(tombstone)
    rows[0]["completed_at"] = value
    tombstone.write_text(json.dumps(rows))

    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reg.load()
    assert len(reg.records_owing_terminal_notification()) == 1

    await reg.ack_terminal_notification(a_id)    # must return without raising

    assert len(_rows(tombstone)) == 1
    row = _row(tombstone, a_id)
    assert row["terminal_notification_pending"] is False
    assert type(row["completed_at"]) is type(value)
    assert row["completed_at"] == value

    restarted = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await restarted.load()
    assert len(restarted.terminal_records()) == 1
    assert len(restarted.records_owing_terminal_notification()) == 0


@_UNUSABLE_COMPLETED_AT
async def test_load_persists_reconcile_and_retains_unusable_terminal_time(
    tmp_path, value,
):
    from collections import Counter

    from engagement_registry import EngagementRegistry

    tombstone, a_id = await _one_owed_previous_process_row(tmp_path)
    seeded = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await seeded.load()
    active = await seeded.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="unfinished", origin={}, topic_id=None)

    # The cleared bit is deliberate: retention of an unusable time must hold
    # independently of the owed-telling exemption.
    rows = _rows(tombstone)
    for r in rows:
        if r["id"] == a_id:
            r["completed_at"] = value
            r["terminal_notification_pending"] = False
    assert sum(r["status"] == "active" for r in rows) == 1
    tombstone.write_text(json.dumps(rows))

    restarted = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await restarted.load()

    disk = _rows(tombstone)
    assert len(disk) == 2
    assert Counter(r["id"] for r in disk) == Counter({
        a_id: 1, active.id: 1,
    })
    assert sum(r["status"] == "active" for r in disk) == 0
    assert sum(r["status"] == "idle" for r in disk) == 1
    assert _row(tombstone, active.id)["status"] == "idle"

    row = _row(tombstone, a_id)
    assert type(row["completed_at"]) is type(value)
    assert row["completed_at"] == value
    assert len(restarted.terminal_records()) == 1
    assert len(restarted.active_and_idle()) == 1


# ---------------------------------------------------------------------------
# #1093 + #1094 regression tests (the converged design's matrix; these are not
# red cases). The boot replay's membership is fixed at load; retention ages only
# a usable time.
# ---------------------------------------------------------------------------


async def _corrupt_owed_non_terminal(tmp_path):
    """The fixture's owed ``a_id`` row, hand-edited to a live status while still
    owing — a shape no writer produces — and reloaded (active -> idle)."""
    from engagement_registry import EngagementRegistry

    tombstone, a_id = await _one_owed_previous_process_row(tmp_path)
    rows = _rows(tombstone)
    rows[0]["status"] = "active"
    rows[0]["completed_at"] = None
    tombstone.write_text(json.dumps(rows))
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reg.load()
    assert reg.get(a_id).status == "idle"
    assert reg.get(a_id).terminal_notification_pending is True
    return reg, tombstone, a_id


async def test_an_unchanged_corrupt_owed_row_still_reaches_the_owner_and_is_retained(
    tmp_path, caplog,
):
    import casa_core

    reg, tombstone, a_id = await _corrupt_owed_non_terminal(tmp_path)
    bus = _RecordingBus()
    with caplog.at_level("ERROR"):
        await casa_core._notify_recovered_engagement_outcomes(
            reg, bus, assistant_role="assistant")

    assert bus.sent == []
    assert sum("is not terminal" in r.getMessage()
               for r in caplog.records) == 1
    assert _row(tombstone, a_id)["terminal_notification_pending"] is True


async def test_a_corrupt_owed_row_finalized_live_is_told_once_live_then_next_boot(
    tmp_path,
):
    from collections import Counter

    import casa_core
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement

    reg, tombstone, a_id = await _corrupt_owed_non_terminal(tmp_path)
    bus = _live_tools(reg)
    await _finalize_engagement(
        reg.get(a_id), outcome="completed", text="done now", artifacts=[],
        next_steps=[], driver=_driver_double())

    await casa_core._notify_recovered_engagement_outcomes(
        reg, bus, assistant_role="assistant")
    assert _notice_counts(bus.sent) == Counter({(a_id, False): 1})

    restarted = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await restarted.load()
    second = _RecordingBus()
    await casa_core._notify_recovered_engagement_outcomes(
        restarted, second, assistant_role="assistant")
    assert _notice_counts(second.sent) == Counter({(a_id, True): 1})


async def test_a_corrupt_owed_row_terminalized_at_boot_without_arming_is_deferred_not_lost(
    tmp_path,
):
    """The seam-round sequence: boot's own refusal terminalizes the corrupt row
    without a telling. This boot's replay leaves it (its status changed since
    load); the persisted bit makes it the next boot's."""
    from collections import Counter

    import casa_core
    from engagement_registry import EngagementRegistry

    reg, tombstone, a_id = await _corrupt_owed_non_terminal(tmp_path)
    assert await reg.try_transition_terminal(
        a_id, "error", strict=True, error_kind="k", error_message="m") is True

    bus = _RecordingBus()
    await casa_core._notify_recovered_engagement_outcomes(
        reg, bus, assistant_role="assistant")
    assert bus.sent == []
    assert _row(tombstone, a_id)["terminal_notification_pending"] is True

    restarted = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await restarted.load()
    second = _RecordingBus()
    await casa_core._notify_recovered_engagement_outcomes(
        restarted, second, assistant_role="assistant")
    assert _notice_counts(second.sent) == Counter({(a_id, True): 1})


async def test_an_ack_landing_mid_walk_is_seen_before_its_record_is_reached(
    tmp_path,
):
    """Membership is fixed at load, but whether a member still owes is read as
    the walk reaches it — not from a list taken when the walk began."""
    import casa_core

    reg, tombstone, a_id, b_id, c_id, _told = await _owing_records(tmp_path)
    order = [r.id for r in reg.records_owed_at_load()]
    first, later = order[0], order[-1]

    class _AckingBus(_RecordingBus):
        async def notify(self, msg) -> None:
            await super().notify(msg)
            if msg.content.delegation_id == first:
                await reg.ack_terminal_notification(later)

    bus = _AckingBus()
    await casa_core._notify_recovered_engagement_outcomes(
        reg, bus, assistant_role="assistant")

    sent = [m.content.delegation_id for m in bus.sent]
    assert len(sent) == 2
    assert sent.count(later) == 0
    assert sorted(sent) == sorted(set(order) - {later})


async def test_an_empty_load_then_a_live_outcome_replays_nothing(tmp_path):
    from collections import Counter

    import casa_core
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    await reg.load()
    bus = _live_tools(reg)
    live = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", topic_id=None,
        origin={"role": "assistant", "channel": "telegram",
                "chat_id": "c", "cid": "r", "user_text": "t"})
    await _finalize_engagement(
        live, outcome="cancelled", text="", artifacts=[], next_steps=[],
        driver=_driver_double())

    await casa_core._notify_recovered_engagement_outcomes(
        reg, bus, assistant_role="assistant")
    assert _notice_counts(bus.sent) == Counter({(live.id, False): 1})


async def test_main_loads_the_engagement_registry_before_anything_can_arm_an_obligation():
    """The boot replay's owed set is captured inside ``load()``; that is only
    what the previous process left if ``load()`` completes before the internal
    socket runner, the channels and the resident loops can finalize anything."""
    import ast
    import inspect
    import textwrap

    import casa_core

    body = ast.parse(
        textwrap.dedent(inspect.getsource(casa_core.main))).body[0].body

    def _first(name, owner=None):
        for i, stmt in enumerate(body):
            for node in ast.walk(stmt):
                fn = getattr(node, "func", None)
                if not isinstance(fn, (ast.Name, ast.Attribute)):
                    continue
                got = fn.id if isinstance(fn, ast.Name) else fn.attr
                if got != name:
                    continue
                if owner is not None and not (
                        isinstance(fn, ast.Attribute)
                        and isinstance(fn.value, ast.Name)
                        and fn.value.id == owner):
                    continue
                return i
        return None

    load = None
    for i, stmt in enumerate(body):
        if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await)
                and isinstance(stmt.value.value, ast.Call)):
            fn = stmt.value.value.func
            if (isinstance(fn, ast.Attribute) and fn.attr == "load"
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "engagement_registry"):
                load = i
                break
    assert load is not None, "main must await engagement_registry.load() unguarded"
    for name in ("start_internal_unix_runner", "start_all", "start_agent_loop",
                 "_notify_recovered_engagement_outcomes"):
        at = _first(name)
        assert at is not None, name
        assert load < at, (name, load, at)


def _retention_row(**over):
    base = {"id": "x", "kind": "specialist", "role_or_type": "finance",
            "driver": "in_casa", "status": "completed", "topic_id": None,
            "started_at": 1.0, "last_user_turn_ts": 1.0,
            "completed_at": None, "origin": {}, "task": "t",
            "auth_token": "tok-" + "a" * 40}
    base.update(over)
    return base


_RETENTION_CASES = [
    # id, completed_at (callable of the old time), extra fields, kept
    ("usable-old", lambda old: old, {}, False),
    ("usable-old-int", lambda old: int(old), {}, False),
    ("usable-fresh", lambda old: old + 60 * 86400, {}, True),
    ("none", lambda old: None, {}, True),
    ("string", lambda old: "yesterday", {}, True),
    ("list", lambda old: [old], {}, True),
    ("dict", lambda old: {"t": old}, {}, True),
    ("bool", lambda old: True, {}, True),
    ("nan", lambda old: float("nan"), {}, True),
    ("inf", lambda old: float("inf"), {}, True),
    ("neg-inf", lambda old: float("-inf"), {}, True),
    ("oversized-int", lambda old: 10**400, {}, True),
    ("old-owes-quiesce", lambda old: old, {"quiesce_pending": True}, True),
    ("old-owes-telling", lambda old: old,
     {"terminal_notification_pending": True}, True),
    ("string-owes-telling", lambda old: "yesterday",
     {"terminal_notification_pending": True}, True),
    ("old-but-live", lambda old: old, {"status": "idle"}, True),
]


@pytest.mark.parametrize("value,extra,kept", [c[1:] for c in _RETENTION_CASES],
                         ids=[c[0] for c in _RETENTION_CASES])
async def test_retention_ages_only_a_usable_time(tmp_path, value, extra, kept):
    import time

    import engagement_registry as er
    from engagement_registry import EngagementRegistry

    old = time.time() - (er._TERMINAL_RETENTION_DAYS + 1) * 86400
    completed_at = value(old)
    tombstone = tmp_path / "engagements.json"
    rows = [_retention_row(id="subject", completed_at=completed_at, **extra),
            _retention_row(id="anchor", status="idle")]
    tombstone.write_text(json.dumps(rows))
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reg.load()

    # A best-effort write through a real mutator: it must not raise.
    await reg.set_procedural_epoch("anchor", "epoch-1")

    ids = [r["id"] for r in _rows(tombstone)]
    assert ids.count("anchor") == 1
    assert ids.count("subject") == (1 if kept else 0)
    if kept:
        held = _row(tombstone, "subject")["completed_at"]
        assert type(held) is type(completed_at)
        if completed_at == completed_at:            # NaN is not equal to itself
            assert held == completed_at


async def test_a_strict_writer_still_rolls_back_beside_an_unusable_time(tmp_path):
    """The guard must not mask a REAL write failure: with an unusable-time row
    in the file, an injected failure still reaches the strict caller, which
    still restores its record."""
    import unittest.mock as _mock

    from engagement_registry import EngagementRegistry

    tombstone = tmp_path / "engagements.json"
    tombstone.write_text(json.dumps([
        _retention_row(id="subject", completed_at="yesterday")]))
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reg.load()
    rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)

    with _mock.patch.object(
        reg, "_write_tombstone", side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError):
            await reg.try_transition_terminal(
                rec.id, "error", error_kind="k", error_message="m",
                strict=True)
    assert rec.status == "active"
    assert rec.completed_at is None
    assert _row(tombstone, "subject")["completed_at"] == "yesterday"


_USABLE_TIME_AGREEMENT = [
    "yesterday", "1790596800", ["x"], {"t": 1}, None, True, False,
    float("nan"), float("inf"), float("-inf"), 10**400,
    0, 0.0, 1790596800, 1790596800.5, -1.0,
]


@pytest.mark.parametrize("value", _USABLE_TIME_AGREEMENT,
                         ids=[repr(v)[:20] for v in _USABLE_TIME_AGREEMENT])
async def test_retention_and_the_replay_renderer_agree_on_a_usable_time(
    monkeypatch, value,
):
    """#1094 / C3-3: retention mirrors the renderer's test rather than importing
    it (``agent`` imports ``engagement_registry``). Inside the platform's
    timestamp range the two must agree; a change to either breaks this."""
    from datetime import timezone

    import agent as agent_mod
    import engagement_registry as er
    from specialist_registry import DelegationComplete

    monkeypatch.setattr(agent_mod, "resolve_tz", lambda: timezone.utc)
    monkeypatch.setattr(agent_mod, "_replay_clock", lambda: 1790604000)
    complete = DelegationComplete(
        delegation_id="d", agent="finance", status="ok", kind="",
        message="", result_available=False, origin={}, elapsed_s=0.0,
        replayed_after_restart=True, terminal_at=value)
    rendered = agent_mod._replay_time_sentence(complete, orphan=False)
    renderer_usable = rendered != agent_mod._REPLAY_TIME_UNKNOWN
    assert er._usable_time(value) is renderer_usable


@pytest.mark.parametrize("value", [1e20, -1e20], ids=["huge", "huge-negative"])
async def test_the_stated_divergence_outside_the_timestamp_range(monkeypatch, value):
    """The one declared difference: a finite number ``fromtimestamp`` rejects is
    unknown to the renderer, but retention orders it as the number it is."""
    from datetime import timezone

    import agent as agent_mod
    import engagement_registry as er
    from specialist_registry import DelegationComplete

    monkeypatch.setattr(agent_mod, "resolve_tz", lambda: timezone.utc)
    complete = DelegationComplete(
        delegation_id="d", agent="finance", status="ok", kind="",
        message="", result_available=False, origin={}, elapsed_s=0.0,
        replayed_after_restart=True, terminal_at=value)
    assert (agent_mod._replay_time_sentence(complete, orphan=False)
            == agent_mod._REPLAY_TIME_UNKNOWN)
    assert er._usable_time(value) is True
