"""C1 — the in_casa delivery seam's two windows.

**#690 — the POST-admission window.** A message admitted for a job is written
into its session later, by the job's delivery worker. The registry's
``begin_turn_delivery`` decision runs again immediately before every
``client.query``, with no Casa suspension point between (INV-ENG-009), so a
terminal transition committed while the message waited in the job's queue
means it is never written.

**#663 — the PRE-admission window.** A broker-driven system continuation's
text exists only once ``deliver_system_turn`` admits it, so the job holds an
ingress reservation from the tap's synchronous commit until that admission; a
successful completion racing it is vetoed (INV-ENG-003).

Both are pinned here with REAL modules — a real ``EngagementRegistry``, a real
``InCasaDriver``, the real Telegram seams, the real ``VerdictBroker`` and the
real authorization-challenge coordinator. Event-driven only; never a patch of
``<module>.asyncio.sleep``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from tests.test_job_session_driver import until
    from tests.turn_scripted_session import TurnScriptedSession, attach_session
except ImportError:  # pragma: no cover — direct-path collection
    from test_job_session_driver import until
    from turn_scripted_session import TurnScriptedSession, attach_session

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Stream:
    async def emit(self, text):
        pass

    async def finalize(self, text):
        pass


class _ScriptedClient(TurnScriptedSession):
    """A live job session. Each write records its text AND, through
    ``on_query``, whatever the test wants to observe AT the hand-off instant;
    ``gate``, when set, holds each write inside the client call until it is
    set (``entered`` is set as a write reaches it)."""

    def __init__(self, on_query=None):
        self.query_prompts: list[str] = []
        self.close_calls = 0
        self.on_query = on_query
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def record_query(self, prompt):
        if self.on_query is not None:
            self.on_query(prompt)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        self.query_prompts.append(prompt)

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, ResultMessage, TextBlock,
        )
        yield AssistantMessage(content=[TextBlock(text="ok")], model="m")
        yield ResultMessage(subtype="success", duration_ms=1,
                            duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="sid")

    async def close(self):
        self.close_calls += 1


async def _mk_driver_rec(tmp_path, client):
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        "specialist", "configurator", "in_casa", "t",
        {"role": "assistant", "channel": "telegram", "user_id": 77},
        topic_id=555)
    drv = InCasaDriver(record_lookup=reg.get,
                       begin_turn_delivery=reg.begin_turn_delivery)
    attach_session(drv, rec, client)
    return reg, rec, drv


async def _hold_the_worker(drv, rec, client):
    """Put a Casa write in flight and hold it inside the client call, so the
    job's next message waits in its queue."""
    client.gate = asyncio.Event()
    drv.submit_casa(rec, "earlier")
    await asyncio.wait_for(client.entered.wait(), 5)


def _reservations(drv, engagement_id: str) -> int:
    return drv.inbound_reservations(engagement_id)


# ===========================================================================
# #690 — the write-time admission fence
# ===========================================================================


class TestFollowUpAdmissionFence:
    """INV-ENG-009: a write is admitted by the registry immediately before it
    reaches the job's session, with no Casa suspension point between."""

    async def test_a_follow_up_to_a_terminal_record_never_reaches_the_client(
            self, tmp_path):
        """A message admitted and queued while an earlier write is in flight,
        a terminal transition committed while it waits, and the session STILL
        registered because ``driver.cancel`` runs many awaits later: the
        message is never written."""
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        await _hold_the_worker(drv, rec, client)

        drv.submit_operator(rec, "do not run")
        assert await reg.try_transition_terminal(
            rec.id, "cancelled", strict=True) is True
        assert drv.is_alive(rec) is True

        client.gate.set()
        await until(lambda: client.query_prompts == ["earlier"])
        await asyncio.sleep(0.01)

        facts = (client.query_prompts.count("do not run"),
                 reg.get(rec.id).status)
        assert facts == (0, "cancelled"), f"fence facts: {facts!r}"
        await drv.cancel(rec)

    async def test_an_idle_record_is_active_at_the_client_hand_off(
            self, tmp_path):
        """The fence's second half: a record found ``idle`` is ``active`` by
        the time the client sees the write, and the write does NOT re-stamp
        ``last_user_turn_ts`` (re-stamping postpones the idle reminder forever
        — ``begin_turn_delivery``'s own rule). Without it the bridge
        grant-gate, which binds only ``active``, would refuse the job's own
        tools."""
        seen: list[tuple[str, str]] = []
        client = _ScriptedClient()
        reg = rec = drv = None

        def _on_query(prompt):
            seen.append(("query", reg.get(rec.id).status))
        client.on_query = _on_query
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)

        reg._records[rec.id].status = "idle"
        stamp_before = getattr(reg.get(rec.id), "last_user_turn_ts", None)

        drv.submit_casa(rec, "follow-up")
        await until(lambda: client.query_prompts == ["follow-up"])

        facts = (seen, reg.get(rec.id).status,
                 getattr(reg.get(rec.id), "last_user_turn_ts", None)
                 == stamp_before)
        assert facts == ([("query", "active")], "active", True), (
            f"idle-reactivation facts: {facts!r}")
        await drv.cancel(rec)

    async def test_the_brief_is_fenced_like_every_write(self, tmp_path):
        """The job's brief is admitted at write time too: a job that ended
        between its session opening and the brief's write receives
        nothing."""
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        await _hold_the_worker(drv, rec, client)

        drv.enqueue_brief(rec, "launch")
        assert await reg.try_transition_terminal(
            rec.id, "completed", strict=True) is True
        client.gate.set()
        await until(lambda: client.query_prompts == ["earlier"])
        await asyncio.sleep(0.01)

        assert client.query_prompts == ["earlier"]
        await drv.cancel(rec)

    async def test_a_direct_caller_with_no_seam_ticket_is_fenced_too(
            self, tmp_path):
        """A write that never passed a channel seam — Casa's own
        ``submit_casa``, which takes no ticket — is fenced at the driver all
        the same. A fence installed channel-side would miss it entirely; this
        is the argument for the driver seam and the pin that keeps it
        there."""
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        assert await reg.try_transition_terminal(
            rec.id, "completed", strict=True) is True

        drv.submit_casa(rec, "direct")
        await asyncio.sleep(0.01)

        facts = (client.query_prompts.count("direct"),
                 drv.inbound_unread_depth(rec.id))
        assert facts == (0, 0), f"direct-caller fence facts: {facts!r}"
        await drv.cancel(rec)

    async def test_an_operator_message_to_a_terminal_record_is_refused_at_the_seam(
            self, tmp_path):
        """The seam's own admission: a terminal record refuses the operator's
        message synchronously, and nothing of it stays journaled or
        ledgered."""
        import job_inbox
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        assert await reg.try_transition_terminal(
            rec.id, "completed", strict=True) is True

        results = []
        try:
            drv.submit_operator(rec, "too late")
        except Exception as exc:  # noqa: BLE001 — the refusal under test
            results.append(type(exc).__name__)
        await asyncio.sleep(0.01)

        facts = (results, client.query_prompts, job_inbox.load(rec.id),
                 drv.inbound_unread_depth(rec.id))
        assert facts == (["EngagementTerminalError"], [], [], 0), (
            f"seam refusal facts: {facts!r}")
        await drv.cancel(rec)


# ===========================================================================
# #663 — the continuation's reservation, born at the synchronous tap-commit
# ===========================================================================


class _SpyLease:
    """Wraps the channel's real reservation lease so the production CALL
    ORDER is observable."""

    def __init__(self, inner, events):
        self._inner = inner
        self.events = events

    def take(self) -> bool:
        self.events.append("reserve")
        return self._inner.take()

    def release(self) -> None:
        self.events.append("release")
        self._inner.release()


def _authz_challenge(ch, engagement_id):
    """An engagement-origin authorization challenge built by the REAL
    ``ChallengeCoordinator.get_or_create`` against the REAL channel, with only
    the broker registration captured: it returns the coordinator holding the
    challenge's ``on_commit_sync`` (the synchronous tap-commit step, which
    takes the engagement's ingress reservation) and its ``finish_factory``.

    The authz grant is the continuation kind that still takes the reservation
    at the tap commit; the install-consent cards that used to (#663) now
    continue the configurator on its desk, outside any engagement."""
    from authz_grants import ChallengeCoordinator, GrantKey, GrantStore

    class _Capture(ChallengeCoordinator):
        def register_challenge(self, key, **kwargs):
            self.on_commit_sync = kwargs["on_commit_sync"]
            self.finish_factory = kwargs["finish_factory"]
            return SimpleNamespace(created=True)

    coord = _Capture()
    key = GrantKey(operator_id=701, chat_id=701, enforcement_role="finance",
                   artifact_id="art-1", tool_name="mcp__ledger__reset",
                   args_hash="h", engagement_id=engagement_id)
    coord.get_or_create(
        key, chat_id=701, operator_id=701, target_role="finance",
        tool_name="mcp__ledger__reset", canonical_json="{}",
        enforcement_role="finance", channel=ch, grants=GrantStore(),
        engagement_id=engagement_id)
    return coord


async def _mk_ctx(tmp_path, fake_telegram_bot):
    """A REAL TelegramChannel (real deliver_system_turn), a REAL registry +
    InCasaDriver with a live session, and ``tools`` initialised so
    ``emit_completion`` runs its production gate."""
    import agent as agent_mod
    from channels.telegram import TelegramChannel
    from tools import init_tools

    client = _ScriptedClient()
    reg, rec, drv = await _mk_driver_rec(tmp_path, client)

    tch = MagicMock()
    tch.send_to_topic = AsyncMock()
    tch.send_response_to_topic = AsyncMock()
    tch.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = tch
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    agent_mod.active_engagement_driver = drv

    ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                         engagement_supergroup_id=-1001)
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    ch._driver_advance_high_water = AsyncMock()
    ch._post_engagement_notice = AsyncMock()
    ch.edit_dm_message = AsyncMock()
    return ch, reg, rec, drv, client, tch


def _lease(ch, engagement_id, events):
    """The lease the production caller builds, wrapped to record its calls."""
    return _SpyLease(ch.engagement_inbound_reservation(engagement_id), events)


async def _emit_ok(rec):
    from tools import emit_completion, engagement_var
    token = engagement_var.set(rec)
    try:
        res = await emit_completion.handler({
            "text": "done", "next_steps": [],
            "status": "ok"})
    finally:
        engagement_var.reset(token)
    return json.loads(res["content"][0]["text"])


class TestACompletionRacingTheContinuationIsRefused:
    """INV-ENG-003, extended to in_casa: an admitted-but-undelivered system
    continuation exists from the tap-commit that decided it."""

    async def test_the_handler_pre_check_refuses_over_a_held_reservation(
            self, tmp_path, fake_telegram_bot):
        """A continuation the operator already approved holds a reservation
        from the tap's commit: ``emit_completion``'s handler pre-check refuses
        over it, before the terminal transition is entered."""
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        transitions: list = []
        real_transition = reg.try_transition_terminal

        async def _counting(*a, **kw):
            transitions.append(a)
            return await real_transition(*a, **kw)
        reg.try_transition_terminal = _counting
        try:
            coord = _authz_challenge(ch, rec.id)
            coord.on_commit_sync(0, {})

            payload = await _emit_ok(rec)
            facts = (payload.get("status"), payload.get("kind"),
                     len(transitions), reg.get(rec.id).status,
                     tch.close_topic.await_count)
            assert facts == ("error", "unread_inbound", 0, "active", 0), (
                f"pre-check veto facts: {facts!r}")
        finally:
            agent_mod.active_engagement_driver = None
            await drv.cancel(rec)

    async def test_a_reservation_born_during_finalize_is_vetoed_by_the_hook(
            self, tmp_path, fake_telegram_bot, monkeypatch):
        """The window itself: a tap committing after ``emit_completion``'s
        pre-check read zero and before ``try_transition_terminal`` runs its
        terminal hook is admitted past the pre-check. The refusal with the
        pre-check having read zero proves the veto fired inside the
        transition's critical section."""
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            coord = _authz_challenge(ch, rec.id)

            real_transition = reg.try_transition_terminal
            at_entry: list = []

            async def _transition(*a, **kw):
                # The operator's tap lands HERE — after the handler pre-check
                # read zero, before the terminal hook re-reads.
                at_entry.append((drv.inbound_unread_depth(rec.id),
                                 _reservations(drv, rec.id)))
                coord.on_commit_sync(0, {})
                return await real_transition(*a, **kw)
            monkeypatch.setattr(reg, "try_transition_terminal", _transition)

            payload = await _emit_ok(rec)
            facts = (payload.get("status"), payload.get("kind"), at_entry,
                     reg.get(rec.id).status, tch.close_topic.await_count)
            assert facts == ("error", "unread_inbound", [(0, 0)], "active", 0), (
                f"terminal-hook veto facts: {facts!r}")
        finally:
            agent_mod.active_engagement_driver = None
            await drv.cancel(rec)


class TestTheHandOffOutcomeAndTheTransfer:
    """``deliver_system_turn`` reports whether the job ACCEPTED the
    continuation — the synchronous admission at the seam — not whether it was
    later written, which the job's delivery worker decides afterwards."""

    async def test_the_seam_admits_the_ticket_then_releases_the_reservation(
            self, tmp_path, fake_telegram_bot):
        """No await separates the admission from the release, so the gate
        population is never zero across the transfer."""
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            events: list[str] = []
            observed: list[tuple[str, int, int]] = []
            lease = _lease(ch, rec.id, events)
            lease.take()
            client.gate = asyncio.Event()       # nothing is taken up meanwhile

            inner_submit = drv.submit_operator

            def _submit(*a, **kw):
                events.append("admit")
                ticket = inner_submit(*a, **kw)
                observed.append(("admit", drv.inbound_unread_depth(rec.id),
                                 _reservations(drv, rec.id)))
                return ticket
            drv.submit_operator = _submit

            inner_release = lease.release

            def _release():
                inner_release()
                observed.append(("release", drv.inbound_unread_depth(rec.id),
                                 _reservations(drv, rec.id)))
            lease.release = _release

            assert await ch.deliver_system_turn(
                rec, "resume", inbound_reservation=lease) is True

            facts = (events, observed)
            assert facts == (
                ["reserve", "admit", "release"],
                [("admit", 1, 1), ("release", 1, 0)]), (
                f"transfer facts: {facts!r}")
            client.gate.set()
        finally:
            agent_mod.active_engagement_driver = None
            await drv.cancel(rec)

    async def test_the_seam_reports_whether_it_handed_off(
            self, tmp_path, fake_telegram_bot):
        """True when the job accepted the continuation, False when it was
        refused — so the reconcile callbacks report what the seam decided
        rather than sampling the record's status before it ran."""
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            results = [await ch.deliver_system_turn(rec, "resume-ok")]
            await until(lambda: client.query_prompts == ["resume-ok"])
            assert await reg.try_transition_terminal(
                rec.id, "cancelled", strict=True) is True
            results.append(await ch.deliver_system_turn(rec, "resume-refused"))

            assert results == [True, False], f"hand-off report: {results!r}"
        finally:
            agent_mod.active_engagement_driver = None
            await drv.cancel(rec)

    async def test_a_later_fence_refusal_does_not_rewrite_the_hand_off_report(
            self, tmp_path, fake_telegram_bot):
        """The bool is the acceptance and stays ``True`` even when the
        write-time fence later refuses the write — and that message is told
        ONCE, by the journal's terminal branch, never twice."""
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            await _hold_the_worker(drv, rec, client)
            handed = await ch.deliver_system_turn(rec, "continue")
            # Terminal writer wins after the hand-off, before the write.
            assert await reg.try_transition_terminal(
                rec.id, "cancelled", strict=True) is True
            client.gate.set()
            await until(lambda: client.query_prompts == ["earlier"])
            await asyncio.sleep(0.01)
            # What the finalize funnel's tail runs after the job's client
            # closed.
            await drv.cancel(rec)
            await drv.settle_journal_terminal(rec, ch.tell_journal_lost)

            notices = [c.args[1] for c in
                       ch._post_engagement_notice.await_args_list]
            facts = (handed, client.query_prompts.count("continue"),
                     sum("could not be delivered" in n for n in notices),
                     drv.inbound_unread_depth(rec.id))
            assert facts == (True, 0, 1, 0), f"late-refusal facts: {facts!r}"
        finally:
            agent_mod.active_engagement_driver = None
            await drv.cancel(rec)

    async def test_a_finish_hook_that_never_reaches_the_seam_still_releases(
            self, tmp_path, fake_telegram_bot):
        """Release-guarantee: an arm that never enters
        ``deliver_system_turn`` — here an approval whose mint the commit step
        did not record, which must never dispatch — leaves the whole-body
        ``finally`` as the only thing that can dispose of the reservation. An
        unreleased one makes a successful completion permanently impossible
        under INV-ENG-003.

        """
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            counts: list[int] = []
            dispatched: list[bool] = []
            real_dispatch = ch._dispatch_engagement_continuation

            async def _spy_dispatch(**kw):
                dispatched.append(True)
                return await real_dispatch(**kw)
            ch._dispatch_engagement_continuation = _spy_dispatch
            coord = _authz_challenge(ch, rec.id)
            # the commit step ran (the reservation is taken), but its mint
            # record did not reach the request the finish hook reads
            coord.on_commit_sync(0, {})
            req = SimpleNamespace(meta={}, request_id="rid-1")
            counts.append(_reservations(drv, rec.id))
            await coord.finish_factory(88, req)(
                {"outcome": "answered", "option_index": 0})
            counts.append(_reservations(drv, rec.id))

            assert (counts, dispatched) == ([1, 0], []), (
                f"release-guarantee counts: {counts!r}, dispatched: {dispatched!r}")
        finally:
            agent_mod.active_engagement_driver = None
            await drv.cancel(rec)


class TestTheReservationCounterHasNoAttachStep:
    """#740's shape is an accessor whose absent key means *state was torn
    down* rather than *nothing is held*. These counters have no attach and no
    detach step, and this is where that stays true."""

    async def test_cancel_does_not_zero_a_held_reservation(self, tmp_path):
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)

        drv.reserve_inbound(rec.id)
        before = _reservations(drv, rec.id)
        await drv.cancel(rec)
        after = _reservations(drv, rec.id)
        drv.release_inbound_reservation(rec.id)
        released = _reservations(drv, rec.id)

        facts = (before, after, released)
        assert facts == (1, 1, 0), f"no-attach-step facts: {facts!r}"

    async def test_an_absent_key_is_a_positive_zero(self, tmp_path):
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)

        facts = (drv.inbound_reservations("never-seen"),
                 drv.inbound_message_reservations("never-seen"),
                 type(drv.inbound_reservations(rec.id)) is int)
        assert facts == (0, 0, True), f"positive-zero facts: {facts!r}"
        await drv.cancel(rec)

    async def test_an_operator_message_holds_a_ticket_and_no_reservation(
            self, tmp_path, fake_telegram_bot):
        """Boundary pin for the wiring decision: the operator-message path
        admits a text-bearing ticket, so a reservation beside it would make
        one message hold both — double-counting it in the veto and inflating
        the #664 lost-inbound disclosure."""
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            client.gate = asyncio.Event()
            assert await ch._deliver_to_job(rec, "operator message") is True
            facts = (drv.inbound_unread_depth(rec.id),
                     _reservations(drv, rec.id))
            assert facts == (1, 0), f"operator-path facts: {facts!r}"
            client.gate.set()
        finally:
            agent_mod.active_engagement_driver = None
            await drv.cancel(rec)


class TestTheFenceCoversTheDecisionNotTheDelivery:
    """The in_casa hand-off is an AWAITED client call, so a terminal
    transition can commit while it is in progress and the message still
    arrives.

    That limit is not closable here and must not be closed by widening the
    fence: excluding terminal transitions until the SDK write completed would
    hold a termination out across an unbounded client call, which is exactly
    what the terminal-hook family must never do. ``begin_turn_delivery``'s own
    docstring states the same limit: it fences the decision to deliver only,
    and stopping an in-flight turn is ``driver.cancel``'s job. Pinned here so
    the documented boundary is a tested statement rather than prose, and so a
    future widening is a visible test change rather than a silent one.
    """

    async def test_a_terminal_landing_inside_the_hand_off_is_not_revoked(
            self, tmp_path):
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        client.gate = asyncio.Event()
        drv.submit_operator(rec, "continue")
        # The SDK-internal suspension: everything Casa can observe happens
        # before this, and the write happens after it.
        await asyncio.wait_for(client.entered.wait(), 5)

        # The terminal writer commits WHILE the hand-off is in progress.
        assert await reg.try_transition_terminal(
            rec.id, "cancelled", strict=True) is True
        client.gate.set()
        await until(lambda: client.query_prompts)

        facts = (client.query_prompts, reg.get(rec.id).status)
        assert facts == (["continue"], "cancelled"), (
            f"hand-off boundary facts: {facts!r}")
        await drv.cancel(rec)

    async def test_the_same_terminal_one_step_earlier_is_refused(
            self, tmp_path):
        """The boundary's other side, so the pin above cannot be read as "the
        fence does nothing": a terminal committing one step earlier — while the
        message still waits in the job's queue — IS refused, and that is the
        reachable window, because it spans every write ahead of it rather than
        a single client call."""
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        await _hold_the_worker(drv, rec, client)

        drv.submit_operator(rec, "continue")
        assert await reg.try_transition_terminal(
            rec.id, "cancelled", strict=True) is True
        client.gate.set()
        await until(lambda: client.query_prompts == ["earlier"])
        await asyncio.sleep(0.01)

        assert client.query_prompts == ["earlier"]
        await drv.cancel(rec)
