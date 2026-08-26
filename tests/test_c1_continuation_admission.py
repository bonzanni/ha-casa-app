"""C1 red cases — the in_casa delivery seam's two open windows.

**#690 — the POST-admission window.** ``InCasaDriver.send_user_turn`` admits a
turn on client-map liveness alone (``is_alive`` is ``engagement.id in
self._clients``), with no registry status fence, so a turn dispatched to a
background delivery task reaches ``await client.query(prompt)`` on an
engagement a racing terminal writer already finalized. INV-ENG-009's fence
exists only for ``claude_code`` (``claude_code_driver.py``'s
``begin_turn_delivery`` call is its single caller tree-wide).

**#663 — the PRE-admission window.** A broker-driven system continuation's
admission ticket is born only inside ``deliver_system_turn``, and the in_casa
driver takes no ingress reservation at all, so a successful completion
committing between the tap's ``BROKER.commit`` and that admission is un-vetoed
and the resume turn is dropped.

Both are pinned here with REAL modules — a real ``EngagementRegistry``, a real
``InCasaDriver``, the real Telegram seams, the real ``VerdictBroker`` and the
real consent modules. Event-driven only; no sleeps, and never a patch of
``<module>.asyncio.sleep``.

Two deliberate constructions keep the PRE-FIX failure the intended one rather
than an incidental import or signature error (Sol, red-case specification):

* the fence's exception is matched by ``type(exc).__name__``, never imported —
  a pre-fix ``ImportError`` would otherwise become the red;
* every not-yet-existing keyword is passed through
  :func:`_supported`, and every not-yet-existing accessor is read through
  ``getattr(..., default)``, so the pre-fix red is the wrong OUTCOME (a turn
  delivered to a dead engagement, a completion acknowledged past an
  un-vetoed continuation) and never an ``AttributeError`` or ``TypeError``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _supported(fn, /, *args, **kwargs):
    """Call ``fn`` passing only the keywords its signature accepts.

    The pre-fix tree has neither ``inbound_reservation`` nor the reservation
    accessors. Filtering here means the pre-fix run exercises TODAY's code
    exactly and fails on the wrong outcome, not on ``TypeError``.
    """
    params = inspect.signature(fn).parameters
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


class _ProbeLock(asyncio.Lock):
    """Per-engagement driver lock that flags when a SECOND turn queues."""

    def __init__(self):
        super().__init__()
        self.second_waiting = asyncio.Event()

    async def __aenter__(self):
        if self.locked():
            self.second_waiting.set()
        await self.acquire()
        return None


class _Stream:
    async def emit(self, text):
        pass

    async def finalize(self, text):
        pass


class _ScriptedClient:
    """query() records the prompt AND, through ``on_query``, whatever the test
    wants to observe AT the hand-off instant."""

    def __init__(self, on_query=None):
        self.query_prompts: list[str] = []
        self.close_calls = 0
        self.closed = False
        self.on_query = on_query

    async def query(self, prompt):
        if self.closed:
            raise RuntimeError("client closed")
        if self.on_query is not None:
            self.on_query(prompt)
        self.query_prompts.append(prompt)

    def receive_response(self):
        async def _gen():
            from claude_agent_sdk import AssistantMessage, TextBlock
            try:
                msg = AssistantMessage(content=[TextBlock(text="ok")],
                                       model="m")
            except TypeError:  # pragma: no cover - SDK signature drift
                msg = AssistantMessage.__new__(AssistantMessage)
                msg.content = [TextBlock(text="ok")]
            yield msg
        return _gen()

    async def close(self):
        self.close_calls += 1
        self.closed = True


async def _mk_driver_rec(tmp_path, client, *, lock=None, fence=None):
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "specialist", "configurator", "in_casa", "t",
        {"role": "assistant", "channel": "telegram", "user_id": 77},
        topic_id=555)
    drv = InCasaDriver(topic_stream_factory=lambda tid: _Stream())
    drv._clients[rec.id] = client
    drv._ctx_stack[rec.id] = client
    drv._locks[rec.id] = lock if lock is not None else asyncio.Lock()
    # Installed on the INSTANCE under the production attribute name, never as
    # a constructor keyword: the pre-fix driver has no such parameter, and the
    # red must not be a TypeError. Post-fix __init__ sets the same attribute.
    drv._begin_turn_delivery = (
        reg.begin_turn_delivery if fence is None else fence)
    return reg, rec, drv


def _reservations(drv, engagement_id: str) -> int:
    """Read the in_casa ingress-reservation count WITHOUT requiring it to
    exist. Pre-fix this is 0 for every engagement — which is the defect, and
    the assertion that names it, rather than an AttributeError."""
    accessor = getattr(drv, "inbound_reservations", None)
    return accessor(engagement_id) if accessor is not None else 0


# ===========================================================================
# #690 — the follow-up admission fence
# ===========================================================================


class TestFollowUpAdmissionFence:
    """INV-ENG-009, widened: a turn is admitted by the registry immediately
    before it reaches the engagement, with no Casa suspension point between."""

    async def test_a_follow_up_to_a_terminal_record_never_reaches_the_client(
            self, tmp_path):
        """The dossier's reproduced interleaving: a turn already dispatched to
        its delivery task, a terminal transition committed while that task
        waits on the per-engagement lock, and the client STILL registered
        because ``driver.cancel`` runs many awaits later.

        Pre-fix: ``client.query`` is called once with ``"do not run"`` and no
        refusal is raised — the terminal record is handed a turn.
        """
        lock = _ProbeLock()
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client, lock=lock)

        held = asyncio.Event()
        release = asyncio.Event()

        async def _hold():
            async with lock:
                held.set()
                await release.wait()
        holder = asyncio.create_task(_hold())
        await asyncio.wait_for(held.wait(), 5)

        ticket = drv.admit_inbound(rec.id, "do not run")
        second = asyncio.create_task(
            drv.send_user_turn(rec, "do not run", inbound_token=ticket))
        await asyncio.wait_for(lock.second_waiting.wait(), 5)

        # The terminal writer wins WHILE the delivery task is queued on the
        # lock. is_alive is still True: the client is only popped by cancel().
        assert await reg.try_transition_terminal(
            rec.id, "cancelled", strict=True) is True
        assert drv.is_alive(rec) is True

        release.set()
        await asyncio.wait_for(holder, 5)
        results = await asyncio.gather(second, return_exceptions=True)

        facts = (
            client.query_prompts.count("do not run"),
            sum(type(x).__name__ == "EngagementTerminalError" for x in results),
            reg.get(rec.id).status,
        )
        assert facts == (0, 1, "cancelled"), f"fence facts: {facts!r}"

    async def test_an_idle_record_is_active_at_the_client_hand_off(
            self, tmp_path):
        """The fence's second half: a record found ``idle`` is ``active`` by
        the time the client sees the prompt, and the redelivery does NOT
        re-stamp ``last_user_turn_ts`` (re-stamping postpones the idle
        reminder forever — ``begin_turn_delivery``'s own rule).

        Pre-fix: the client is handed the prompt while the record is still
        ``idle``, so the bridge grant-gate — which binds only ``active`` —
        would refuse the engagement's own tools.
        """
        seen: list[tuple[str, str]] = []
        client = _ScriptedClient()
        reg = rec = drv = None

        def _on_query(prompt):
            seen.append(("query", reg.get(rec.id).status))
        client.on_query = _on_query
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)

        reg._records[rec.id].status = "idle"
        stamp_before = getattr(reg.get(rec.id), "last_user_turn_ts", None)

        await drv.send_user_turn(rec, "follow-up")

        facts = (seen, reg.get(rec.id).status,
                 getattr(reg.get(rec.id), "last_user_turn_ts", None)
                 == stamp_before)
        assert facts == ([("query", "active")], "active", True), (
            f"idle-reactivation facts: {facts!r}")

    async def test_the_launch_prompt_is_not_fenced(self, tmp_path):
        """Boundary pin, not a red case: the LAUNCH turn keeps INV-ENG-011's
        death-reporting owner. Fencing it would raise into
        ``_engage_executor_impl``'s ``mark_error`` sites and
        ``_report_launch_death``, telling the operator a launch died for an
        engagement a racing writer (or the operator's own ``/cancel``) just
        ended — a telling INV-ENG-013 forbids. Passes on both trees; it exists
        so a later widening is a visible test change."""
        calls: list[str] = []
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(
            tmp_path, client, fence=lambda eid: calls.append(eid) or False)

        assert await reg.try_transition_terminal(
            rec.id, "completed", strict=True) is True
        await drv._deliver_turn(rec, "launch")

        facts = (client.query_prompts, calls)
        assert facts == (["launch"], []), f"launch-boundary facts: {facts!r}"

    async def test_a_direct_caller_with_no_seam_ticket_is_fenced_too(
            self, tmp_path):
        """Entry point 5 — a direct ``driver.send_user_turn(rec, text)`` with
        no ``inbound_token`` takes the self-admitted branch. A fence installed
        channel-side would miss it entirely; this is the argument for the
        driver seam and the pin that keeps it there.

        Pre-fix: the query lands on a completed record.
        """
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        assert await reg.try_transition_terminal(
            rec.id, "completed", strict=True) is True

        results = await asyncio.gather(
            drv.send_user_turn(rec, "direct"), return_exceptions=True)

        facts = (
            client.query_prompts.count("direct"),
            sum(type(x).__name__ == "EngagementTerminalError" for x in results),
            # A SELF-ADMITTED ticket has no outer owner and is discharged by
            # send_user_turn's own except branch.
            drv.inbound_unread_depth(rec.id),
        )
        assert facts == (0, 1, 0), f"direct-caller fence facts: {facts!r}"


# ===========================================================================
# #663 — the continuation's reservation, born at the synchronous tap-commit
# ===========================================================================


class _Acks:
    """Ack store that RECORDS its call into a shared ordered event log, so a
    reservation taken before the ack (which would reorder around #543's
    revocation generations) is visible as an ordering failure."""

    def __init__(self, events, acked=True):
        self.events = events
        self.acked = acked
        self.records: list[dict] = []

    def record(self, **kwargs):
        self.events.append("acks.record")
        self.records.append(kwargs)
        return self.acked

    def revocation_generations(self, **kwargs):
        return {}


class _SpyLease:
    """Wraps the channel's real reservation lease so the production CALL
    ORDER is observable. Only constructed when the production factory exists;
    on the pre-fix tree there is no lease and no event."""

    def __init__(self, inner, events):
        self._inner = inner
        self.events = events

    def take(self) -> bool:
        self.events.append("reserve")
        return self._inner.take()

    def release(self) -> None:
        self.events.append("release")
        self._inner.release()


class _FakeCoordinator:
    def register_challenge(self, key, **kwargs):
        self.on_commit_sync = kwargs["on_commit_sync"]
        self.finish_factory = kwargs["finish_factory"]
        return SimpleNamespace(created=True)


def _inspection():
    from specialist_install import DependencyResolution, InspectionResult
    return InspectionResult(
        component_id="casa-test/mtg", version="0.1.0", slug="mtg",
        component_checksum="sha256:" + "1" * 64,
        root_digest="sha256:" + "4" * 64,
        mission="Answer test questions.",
        default_persona_ref="casa/judge@0.1.0",
        default_persona_checksum="sha256:" + "2" * 64,
        required_config_names=(), required_secret_names=(),
        dependencies=(DependencyResolution(
            kind="persona", identifier="casa/judge@0.1.0",
            digest="sha256:" + "2" * 64, available=True, detail=""),),
        staged_dir=Path("/config/specialists/.staging/x"),
    )


def _persona_inspection():
    return SimpleNamespace(
        persona_id="casa/judge", version="0.1.0",
        checksum="sha256:" + "3" * 64, display_name="Judge")


async def _mk_ctx(tmp_path, fake_telegram_bot, *, lock=None):
    """A REAL TelegramChannel (real deliver_system_turn / _resume_and_ready),
    a REAL registry + InCasaDriver, and ``tools`` initialised so
    ``emit_completion`` runs its production gate."""
    import agent as agent_mod
    from channels.telegram import TelegramChannel
    from tools import init_tools

    client = _ScriptedClient()
    reg, rec, drv = await _mk_driver_rec(tmp_path, client, lock=lock)

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
    ch._observer = MagicMock()
    ch._driver_advance_high_water = AsyncMock()
    ch._post_engagement_notice = AsyncMock()
    ch.edit_dm_message = AsyncMock()
    ch._driver_admit_inbound = lambda r, text: drv.admit_inbound(r.id, text)
    ch._driver_discharge_inbound = (
        lambda r, tok: drv.discharge_inbound(r.id, tok))
    ch._driver_inbound_held = (
        lambda r, tok: drv.inbound_token_held(r.id, tok))

    async def _send_user_turn(r, text, *, tg_message_id=None,
                              inbound_token=None):
        await drv.send_user_turn(r, text, inbound_token=inbound_token)
        return None
    ch._driver_send_user_turn = _send_user_turn
    return ch, reg, rec, drv, client, tch


def _lease(ch, engagement_id, events):
    """The lease the production caller will build, or ``None`` on the pre-fix
    tree — where the channel exposes no such factory and the consent module
    accepts no such keyword."""
    factory = getattr(ch, "engagement_inbound_reservation", None)
    return None if factory is None else _SpyLease(factory(engagement_id),
                                                  events)


async def _emit_ok(rec):
    from tools import emit_completion, engagement_var
    token = engagement_var.set(rec)
    try:
        res = await emit_completion.handler({
            "text": "done", "artifacts": [], "next_steps": [],
            "status": "ok"})
    finally:
        engagement_var.reset(token)
    return json.loads(res["content"][0]["text"])


class TestReservationIsBornAtTheTapCommit:
    """The window is [``BROKER.commit()`` returns, the finish-hook task's
    first step]. ``_on_commit_sync`` is the only synchronous instant inside
    it, so the reservation is born there or not at all."""

    async def test_specialist_approve_reserves_after_the_ack(
            self, tmp_path, fake_telegram_bot):
        """Pre-fix: the ack is recorded and NOTHING counts as inbound, so a
        successful completion racing the hook has nothing to be vetoed on."""
        import agent as agent_mod
        from specialist_install_consent import (
            prompt_specialist_install_consent)
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            events: list[str] = []
            acks = _Acks(events)
            _supported(
                prompt_specialist_install_consent,
                coordinator=(coord := _FakeCoordinator()), channel=ch,
                chat_id=701, operator_id=701, inspection=_inspection(),
                acks=acks, reconcile_cb=None,
                inbound_reservation=_lease(ch, rec.id, events))
            coord.on_commit_sync(0, {})

            facts = (events, _reservations(drv, rec.id))
            assert facts == (["acks.record", "reserve"], 1), (
                f"specialist tap-commit facts: {facts!r}")
        finally:
            agent_mod.active_engagement_driver = None

    async def test_persona_approve_reserves_after_the_ack(
            self, tmp_path, fake_telegram_bot):
        """The persona sibling. ``acks.record`` RETURNS the acked bool into
        ``meta["acked"]`` (#543 revocation generations), so the reservation
        must not reorder around it."""
        import agent as agent_mod
        from persona_install_consent import prompt_persona_install_consent
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            events: list[str] = []
            acks = _Acks(events)
            meta: dict = {}
            _supported(
                prompt_persona_install_consent,
                coordinator=(coord := _FakeCoordinator()), channel=ch,
                chat_id=701, operator_id=701,
                inspection=_persona_inspection(), acks=acks, reconcile_cb=None,
                inbound_reservation=_lease(ch, rec.id, events))
            coord.on_commit_sync(0, meta)

            facts = (events, _reservations(drv, rec.id), meta.get("acked"))
            assert facts == (["acks.record", "reserve"], 1, True), (
                f"persona tap-commit facts: {facts!r}")
        finally:
            agent_mod.active_engagement_driver = None

    async def test_a_deny_tap_on_an_install_arm_reserves_nothing(
            self, tmp_path, fake_telegram_bot):
        """Boundary: neither install arm dispatches a continuation on Deny, so
        neither may hold a reservation there. Passes on both trees; it exists
        so a reservation taken unconditionally is a visible failure."""
        import agent as agent_mod
        from specialist_install_consent import (
            prompt_specialist_install_consent)
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            events: list[str] = []
            _supported(
                prompt_specialist_install_consent,
                coordinator=(coord := _FakeCoordinator()), channel=ch,
                chat_id=701, operator_id=701, inspection=_inspection(),
                acks=_Acks(events), reconcile_cb=None,
                inbound_reservation=_lease(ch, rec.id, events))
            coord.on_commit_sync(1, {})

            facts = (events, _reservations(drv, rec.id))
            assert facts == ([], 0), f"deny facts: {facts!r}"
        finally:
            agent_mod.active_engagement_driver = None


class TestACompletionRacingTheContinuationIsRefused:
    """INV-ENG-003, extended to in_casa: an admitted-but-undelivered system
    continuation exists from the tap-commit that decided it."""

    async def test_the_handler_pre_check_refuses_over_a_held_reservation(
            self, tmp_path, fake_telegram_bot):
        """Pre-fix: ``hasattr(driver, "inbound_reservations")`` is False for
        in_casa, so ``emit_completion``'s G4 D1 read is ``(0, 0, 0)`` and the
        completion is ACKNOWLEDGED past a continuation the operator has
        already approved."""
        import agent as agent_mod
        from specialist_install_consent import (
            prompt_specialist_install_consent)
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            events: list[str] = []
            _supported(
                prompt_specialist_install_consent,
                coordinator=(coord := _FakeCoordinator()), channel=ch,
                chat_id=701, operator_id=701, inspection=_inspection(),
                acks=_Acks(events), reconcile_cb=None,
                inbound_reservation=_lease(ch, rec.id, events))
            coord.on_commit_sync(0, {})

            payload = await _emit_ok(rec)
            facts = (payload.get("status"), payload.get("kind"),
                     "waiting unread" in payload.get("message", ""),
                     reg.get(rec.id).status, tch.close_topic.await_count)
            assert facts == ("error", "unread_inbound", True, "active", 0), (
                f"pre-check veto facts: {facts!r}")
        finally:
            agent_mod.active_engagement_driver = None

    async def test_a_reservation_born_during_finalize_is_vetoed_by_the_hook(
            self, tmp_path, fake_telegram_bot):
        """The window itself. ``_finalize_engagement`` drains the inbound
        spool BEFORE ``try_transition_terminal`` runs its terminal hook, so a
        tap committing there is admitted AFTER ``emit_completion``'s pre-check
        read zero — the exact interleaving the dossier reproduced. The refusal
        must carry the TERMINAL HOOK's copy, not the pre-check's, proving the
        veto fired inside the transition's critical section.

        Pre-fix: the pre-check reads zero, the hook reads zero, the completion
        commits, and the approved continuation is dropped.
        """
        import agent as agent_mod
        from specialist_install_consent import (
            prompt_specialist_install_consent)
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            events: list[str] = []
            _supported(
                prompt_specialist_install_consent,
                coordinator=(coord := _FakeCoordinator()), channel=ch,
                chat_id=701, operator_id=701, inspection=_inspection(),
                acks=_Acks(events), reconcile_cb=None,
                inbound_reservation=_lease(ch, rec.id, events))

            async def _drain(engagement):
                # The operator's tap lands HERE — after the handler pre-check
                # read (0, 0, 0), before the terminal hook re-reads.
                coord.on_commit_sync(0, {})
            drv.drain_inbound_spool = _drain

            payload = await _emit_ok(rec)
            facts = (payload.get("status"), payload.get("kind"),
                     "arrived while completing" in payload.get("message", ""),
                     reg.get(rec.id).status, tch.close_topic.await_count)
            assert facts == ("error", "unread_inbound", True, "active", 0), (
                f"terminal-hook veto facts: {facts!r}")
        finally:
            agent_mod.active_engagement_driver = None


async def _drain_tasks(ch):
    """Drain the channel's tracked task sets until BOTH are empty. Bounded:
    a set that never empties is a defect to surface, not to hang on."""
    for _ in range(20):
        tasks = [t for s in ("_turn_tasks", "_inbound_cleanup_tasks")
                 for t in list(getattr(ch, s, ()) or ())]
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks,
                                              return_exceptions=True), 5)
        # bpo-46672: gather() over already-done tasks takes the done_futs fast
        # path and does NOT yield, so the tasks' own done-callbacks — which
        # both discard from these sets and CREATE the cleanup task — are still
        # queued. One bare yield flushes what is already queued; nothing waits
        # for a duration and no asyncio.sleep is patched.
        await asyncio.sleep(0)
    raise AssertionError(
        "tracked tasks did not drain in 20 passes: _turn_tasks=%r "
        "_inbound_cleanup_tasks=%r" % (
            list(getattr(ch, "_turn_tasks", ()) or ()),
            list(getattr(ch, "_inbound_cleanup_tasks", ()) or ())))


class TestTheHandOffOutcomeAndTheTransfer:
    """``deliver_system_turn`` reports the SYNCHRONOUS hand-off decision — not
    the driver fence's result, which is taken inside the per-engagement lock
    long after the seam returned."""

    async def test_the_seam_admits_the_ticket_then_releases_the_reservation(
            self, tmp_path, fake_telegram_bot):
        """No await separates the admission from the release, so the gate
        population is never zero across the transfer.

        Pre-fix: no reservation exists and nothing is released.
        """
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            events: list[str] = []
            observed: list[tuple[int, int]] = []
            lease = _lease(ch, rec.id, events)
            if lease is not None:
                lease.take()

            inner_admit = ch._driver_admit_inbound

            def _admit(r, text):
                events.append("admit")
                return inner_admit(r, text)
            ch._driver_admit_inbound = _admit

            async def _ready(r):
                events.append("ready_enter")
                observed.append((drv.inbound_unread_depth(r.id),
                                 _reservations(drv, r.id)))
                return True
            ch._resume_and_ready = _ready

            await _supported(ch.deliver_system_turn, rec, "resume",
                             inbound_reservation=lease)
            await _drain_tasks(ch)

            facts = (events, observed)
            assert facts == (
                ["reserve", "admit", "release", "ready_enter"], [(1, 0)]), (
                f"transfer facts: {facts!r}")
        finally:
            agent_mod.active_engagement_driver = None

    async def test_the_seam_reports_whether_it_handed_off(
            self, tmp_path, fake_telegram_bot):
        """Pre-fix the seam returns ``None`` on BOTH paths, which is why the
        reconcile callbacks sample the record's status before the seam runs
        instead of reporting what the seam decided."""
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            results = []
            ch._resume_and_ready = AsyncMock(return_value=True)
            results.append(await _supported(
                ch.deliver_system_turn, rec, "resume-ok"))
            await _drain_tasks(ch)
            ch._resume_and_ready = AsyncMock(return_value=False)
            results.append(await _supported(
                ch.deliver_system_turn, rec, "resume-refused"))
            await _drain_tasks(ch)

            assert results == [True, False], f"hand-off report: {results!r}"
        finally:
            agent_mod.active_engagement_driver = None

    async def test_a_later_fence_refusal_does_not_rewrite_the_hand_off_report(
            self, tmp_path, fake_telegram_bot):
        """The bool is the hand-off decision and stays ``True`` even when the
        driver fence later refuses — and that refusal is settled ONCE by the
        delivery task's existing failure owner, never told twice.

        Pre-fix: the seam returns ``None`` and the turn is delivered to a
        terminal record instead of being refused.
        """
        import agent as agent_mod
        lock = _ProbeLock()
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot, lock=lock)
        try:
            ch._resume_and_ready = AsyncMock(return_value=True)
            handed = await _supported(ch.deliver_system_turn, rec, "continue")
            # Terminal writer wins after the hand-off, before the fence.
            assert await reg.try_transition_terminal(
                rec.id, "cancelled", strict=True) is True
            await _drain_tasks(ch)

            notices = [c.args[1] for c in
                       ch._post_engagement_notice.await_args_list]
            facts = (handed, client.query_prompts.count("continue"),
                     sum("could not be delivered" in n for n in notices),
                     drv.inbound_unread_depth(rec.id))
            assert facts == (True, 0, 1, 0), f"late-refusal facts: {facts!r}"
        finally:
            agent_mod.active_engagement_driver = None

    async def test_a_finish_hook_that_never_reaches_the_seam_still_releases(
            self, tmp_path, fake_telegram_bot):
        """Release-guarantee: an arm whose reconcile callback is absent never
        enters ``deliver_system_turn``, so the whole-body ``finally`` is the
        only thing that can dispose of the reservation. An unreleased one
        makes a successful completion permanently impossible under
        INV-ENG-003.

        Pre-fix: nothing is reserved, so the first count is 0.
        """
        import agent as agent_mod
        from specialist_install_consent import (
            prompt_specialist_install_consent)
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            events: list[str] = []
            counts: list[int] = []
            _supported(
                prompt_specialist_install_consent,
                coordinator=(coord := _FakeCoordinator()), channel=ch,
                chat_id=701, operator_id=701, inspection=_inspection(),
                acks=_Acks(events), reconcile_cb=None,
                inbound_reservation=_lease(ch, rec.id, events))
            req = SimpleNamespace(meta={})
            coord.on_commit_sync(0, req.meta)
            counts.append(_reservations(drv, rec.id))
            await coord.finish_factory(88, req)(
                {"outcome": "answered", "option_index": 0})
            counts.append(_reservations(drv, rec.id))

            assert counts == [1, 0], f"release-guarantee counts: {counts!r}"
        finally:
            agent_mod.active_engagement_driver = None


class TestTheReservationCounterHasNoAttachStep:
    """#740's shape is an accessor whose absent key means *state was torn
    down* rather than *nothing is held*. These counters have no attach and no
    detach step, and this is where that stays true."""

    async def test_cancel_does_not_zero_a_held_reservation(self, tmp_path):
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        assert hasattr(drv, "reserve_inbound"), (
            "in_casa exposes no ingress reservation accessor (#663) — the "
            "continuation window has nothing to be vetoed on")

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
        assert hasattr(drv, "inbound_reservations"), (
            "in_casa exposes no inbound_reservations accessor (#663)")

        facts = (drv.inbound_reservations("never-seen"),
                 drv.inbound_message_reservations("never-seen"),
                 type(drv.inbound_reservations(rec.id)) is int)
        assert facts == (0, 0, True), f"positive-zero facts: {facts!r}"

    async def test_an_operator_message_holds_a_ticket_and_no_reservation(
            self, tmp_path, fake_telegram_bot):
        """Boundary pin for the wiring decision: the operator-message path
        already admits a text-bearing ticket, so turning the EXISTING
        ``_driver_reserve_inbound`` seam on for in_casa would make one message
        hold both — double-counting it in the veto and inflating the #664
        lost-inbound disclosure. Passes on both trees."""
        import agent as agent_mod
        ch, reg, rec, drv, client, tch = await _mk_ctx(
            tmp_path, fake_telegram_bot)
        try:
            token = ch._driver_admit_inbound(rec, "operator message")
            facts = (drv.inbound_unread_depth(rec.id),
                     _reservations(drv, rec.id))
            drv.discharge_inbound(rec.id, token)
            assert facts == (1, 0), f"operator-path facts: {facts!r}"
        finally:
            agent_mod.active_engagement_driver = None
