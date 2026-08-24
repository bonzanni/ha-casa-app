"""#649 — in_casa admission-ticket ledger + Telegram seam admission.

INV-ENG-003 extended to the in_casa driver: an admitted inbound turn vetoes a
successful completion until the client takes it, is disclosed if the
engagement dies first, and every failure-path discharge is preceded by one
bounded visible-outcome attempt (the visibility law). The red case lives in
``test_answer_reservation.py::TestInCasaCompletionInboundGate`` (frozen);
these are the seam-review mutation pins around it.

REAL ``InCasaDriver`` + REAL registry + the REAL Telegram handler seams;
event-driven coordination only — no sleeps, never ``<module>.asyncio.sleep``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _mk_assistant(text: str):
    from claude_agent_sdk import AssistantMessage, TextBlock
    try:
        return AssistantMessage(content=[TextBlock(text=text)], model="m")
    except TypeError:
        m = AssistantMessage.__new__(AssistantMessage)
        m.content = [TextBlock(text=text)]
        return m


def _mk_fault_assistant():
    """A main-loop API-fault AssistantMessage, built so the REAL
    ``api_error_kind`` classifies it (not a mock of the classifier)."""
    from error_kinds import api_error_kind
    m = _mk_assistant("API Error: 500 · request_id")
    m.parent_tool_use_id = None
    # The CLI stamps the envelope with error:"<value>" (error_kinds.py:81 —
    # the gate is the truthiness of ``error``).
    m.error = "api_error"
    assert api_error_kind(m) is not None, "fault fixture must classify"
    return m


class _ScriptedClient:
    """query() records prompts; receive_response() yields the next scripted
    frame list. ``on_query`` lets a test observe ledger state AT the
    hand-off instant (the pre-query-move mutation pin)."""

    def __init__(self, scripts=None, on_query=None):
        self.query_prompts: list[str] = []
        self.close_calls = 0
        self.closed = False
        self.scripts = list(scripts or [])
        self.on_query = on_query

    async def query(self, prompt):
        if self.closed:
            raise RuntimeError("client closed")
        if self.on_query is not None:
            self.on_query(prompt)
        self.query_prompts.append(prompt)

    def receive_response(self):
        frames = self.scripts.pop(0) if self.scripts else []

        async def _gen():
            for f in frames:
                yield f
        return _gen()

    async def close(self):
        self.close_calls += 1
        self.closed = True


class _Stream:
    async def emit(self, text):
        pass

    async def finalize(self, text):
        pass


async def _mk_driver_rec(tmp_path, client):
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "in_casa", "t",
        {"role": "assistant", "channel": "telegram", "user_id": 77},
        topic_id=555)
    drv = InCasaDriver(topic_stream_factory=lambda tid: _Stream())
    drv._clients[rec.id] = client
    drv._ctx_stack[rec.id] = client
    drv._locks[rec.id] = asyncio.Lock()
    return reg, rec, drv


# ===========================================================================
# 1. ledger unit behavior
# ===========================================================================


class TestLedger:
    async def test_exact_token_discharge_with_duplicate_texts(self, tmp_path):
        reg, rec, drv = await _mk_driver_rec(tmp_path, _ScriptedClient())
        t1 = drv.admit_inbound(rec.id, "same text")
        t2 = drv.admit_inbound(rec.id, "same text")
        assert drv.inbound_unread_depth(rec.id) == 2
        drv.discharge_inbound(rec.id, t1)
        assert drv.inbound_unread_depth(rec.id) == 1
        assert drv.inbound_unread_texts(rec.id) == ["same text"]
        drv.discharge_inbound(rec.id, t1)          # idempotent no-op
        assert drv.inbound_unread_depth(rec.id) == 1
        drv.discharge_inbound(rec.id, t2)
        assert drv.inbound_unread_depth(rec.id) == 0

    async def test_strict_return_types(self, tmp_path):
        reg, rec, drv = await _mk_driver_rec(tmp_path, _ScriptedClient())
        assert type(drv.inbound_unread_depth(rec.id)) is int
        assert type(drv.inbound_unread_texts(rec.id)) is list
        assert type(drv.inbound_in_flight_texts(rec.id)) is list
        assert not hasattr(drv, "inbound_in_flight_blocking")
        assert not hasattr(drv, "inbound_reservations")
        assert not hasattr(drv, "force_completion_turn_boundary")


# ===========================================================================
# 2. turn lifecycle — move point, evidence, failure ownership
# ===========================================================================


class TestTurnLifecycle:
    async def test_unread_until_lock_accepted_at_query(self, tmp_path):
        """At the query() hand-off the ticket is ALREADY accepted (pre-query
        move): unread 0 / in_flight 1 at that instant. Kills the
        move-after-query mutation (the SDK can dispatch emit_completion
        during the transport write — an unread ticket there self-vetoes)."""
        seen = []

        def on_query(prompt):
            seen.append((drv.inbound_unread_depth(rec.id),
                         drv.inbound_in_flight_texts(rec.id)))
        client = _ScriptedClient(scripts=[[_mk_assistant("ok")]],
                                 on_query=on_query)
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        drv2 = drv  # bind name for the closure above
        drv = drv2
        await drv.send_user_turn(rec, "hello")
        assert seen == [(0, ["hello"])]
        # Evidence frame discharged it; clean end leaves nothing.
        assert drv.inbound_in_flight_texts(rec.id) == []
        assert drv.inbound_unread_depth(rec.id) == 0

    async def test_fault_frames_are_not_evidence(self, tmp_path):
        """A main-loop fault Assistant then a clean-looking Result must NOT
        discharge (cumulative latch): the turn ends in ApiErrorTurn with the
        ticket retained for its failure owner."""
        from error_kinds import ApiErrorTurn
        client = _ScriptedClient(scripts=[[_mk_fault_assistant()]])
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        token = drv.admit_inbound(rec.id, "doomed")
        with pytest.raises(ApiErrorTurn):
            await drv.send_user_turn(rec, "doomed", inbound_token=token)
        # SEAM-owned ticket retained on the failure exit (owner discharges
        # after its telling) — still in the accepted population.
        assert drv.inbound_in_flight_texts(rec.id) == ["doomed"]
        drv.discharge_inbound(rec.id, token)

    async def test_zero_evidence_inbound_turn_raises_empty_turn(
            self, tmp_path):
        from drivers.in_casa_driver import EmptyTurnError
        client = _ScriptedClient(scripts=[[]])
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        with pytest.raises(EmptyTurnError):
            await drv.send_user_turn(rec, "vanishes")
        # Self-admitted (no seam token) → the driver discharged on failure.
        assert drv.inbound_unread_depth(rec.id) == 0
        assert drv.inbound_in_flight_texts(rec.id) == []

    async def test_launch_prompt_keeps_warn_only_empty_turn(self, tmp_path):
        """No admission token (start()'s path) — zero evidence stays the
        historical warn-only outcome, no raise."""
        client = _ScriptedClient(scripts=[[]])
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        await drv._deliver_turn(rec, "launch prompt")   # must not raise

    async def test_not_alive_discharges_self_admitted(self, tmp_path):
        from drivers.in_casa_driver import DriverNotAliveError
        client = _ScriptedClient()
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        drv._clients.pop(rec.id)
        with pytest.raises(DriverNotAliveError):
            await drv.send_user_turn(rec, "x")
        assert drv.inbound_unread_depth(rec.id) == 0

    async def test_seam_token_survives_query_failure(self, tmp_path):
        client = _ScriptedClient()
        client.closed = True
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        token = drv.admit_inbound(rec.id, "lost")
        with pytest.raises(RuntimeError):
            await drv.send_user_turn(rec, "lost", inbound_token=token)
        assert drv.inbound_token_held(rec.id, token) is True
        drv.discharge_inbound(rec.id, token)
        assert drv.inbound_token_held(rec.id, token) is False


# ===========================================================================
# 3. Telegram seams — admission placement + visibility law
# ===========================================================================


def _mk_update(*, chat_id, text, thread_id=None, user_id=77):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = chat_id
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id)
    u.message.message_id = 999
    return u


async def _mk_channel(tmp_path, fake_telegram_bot, client):
    from channels.telegram import TelegramChannel

    reg, rec, drv = await _mk_driver_rec(tmp_path, client)
    ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                         engagement_supergroup_id=-1001)
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    ch._observer = MagicMock()
    ch._driver_advance_high_water = AsyncMock()
    ch._post_engagement_notice = AsyncMock()
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
    return ch, reg, rec, drv


async def _drain(ch):
    """Drain the channel's tracked task sets until BOTH are empty.

    #692: one pass over each set is not enough, and the shortfall is not
    hypothetical. ``_inbound_cleanup_tasks`` is populated from the DELIVERY
    task's done-callback, which the loop runs one iteration AFTER the gather
    that awaited that task resumes — so a single pass snapshots the cleanup
    set before its task exists, returns, and leaves a pending task behind. The
    old single-pass version happened to be sufficient only while every
    delivery path was short enough for both callbacks to land inside the first
    gather's window; adding a bounded notice attempt to the success path made
    it insufficient, and a frozen red case caught it.

    Bounded rather than ``while``: a set that never empties is a defect to
    surface, not to hang the suite on — and surfacing it means RAISING with
    the leftovers named, not falling off the end of the loop and letting the
    caller assert on whatever else it was checking. Reviewer finding, first
    diff round.
    """
    for _ in range(20):
        tasks = [t for s in ("_turn_tasks", "_inbound_cleanup_tasks")
                 for t in list(getattr(ch, s, ()) or ())]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        # bpo-46672: gather() over already-done tasks takes the done_futs fast
        # path and does NOT yield, so the tasks' own done-callbacks — the ones
        # that both discard from these sets and CREATE the cleanup task — are
        # still queued. One bare yield flushes what is already queued; nothing
        # waits for a duration and no asyncio.sleep is patched.
        await asyncio.sleep(0)
    raise AssertionError(
        "tracked tasks did not drain in 20 passes — leftover work: "
        "_turn_tasks=%r _inbound_cleanup_tasks=%r" % (
            list(getattr(ch, "_turn_tasks", ()) or ()),
            list(getattr(ch, "_inbound_cleanup_tasks", ()) or ()),
        ))


class TestManualSeamAdmission:
    async def test_admitted_before_clearance_await(
            self, tmp_path, fake_telegram_bot):
        """Sol seam r1/r6 mutation pin: while lower_origin_clearance is
        BLOCKED, the ticket already exists — admission moved back to the
        historical post-clearance seat must fail this."""
        client = _ScriptedClient(scripts=[[_mk_assistant("ok")]])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        entered = asyncio.Event()
        gate = asyncio.Event()
        real_lower = reg.lower_origin_clearance

        async def blocking_lower(eng_id, clearance):
            entered.set()
            await gate.wait()
            return await real_lower(eng_id, clearance)
        reg.lower_origin_clearance = blocking_lower

        u = _mk_update(chat_id=-1001, text="please adjust",
                       thread_id=555, user_id=77)
        task = asyncio.create_task(ch.handle_update(u))
        await asyncio.wait_for(entered.wait(), 5)
        assert drv.inbound_unread_depth(rec.id) == 1
        assert drv.inbound_unread_texts(rec.id) == ["please adjust"]
        gate.set()
        await task
        await _drain(ch)
        assert client.query_prompts == ["please adjust"]
        assert drv.inbound_unread_depth(rec.id) == 0

    async def test_recognized_commands_are_never_ticketed(
            self, tmp_path, fake_telegram_bot):
        """The command cut: /cancel is consumed by the handler, not
        delivered — no ticket at any point (its own ungated finalize must
        not disclose it as unread)."""
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._finalize_cancel = AsyncMock(return_value=True)
        seen = []
        real_admit = drv.admit_inbound
        drv.admit_inbound = lambda eid, text: (
            seen.append(text), real_admit(eid, text))[1]

        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=555,
                       user_id=77)
        await ch.handle_update(u)
        await _drain(ch)
        assert seen == []
        ch._finalize_cancel.assert_awaited_once()

    async def test_unknown_and_other_bot_slash_texts_are_admitted(
            self, tmp_path, fake_telegram_bot):
        """Sol seam r4/r5 pin: /foo and /cancel@otherbot are DELIVERABLE and
        get early accounting like plain text."""
        for text in ("/foo now", "/cancel@otherbot"):
            client = _ScriptedClient(scripts=[[_mk_assistant("ok")]])
            ch, reg, rec, drv = await _mk_channel(
                tmp_path, fake_telegram_bot, client)
            ch._bot_username = "casabot"
            entered = asyncio.Event()
            gate = asyncio.Event()
            real_lower = reg.lower_origin_clearance

            async def blocking_lower(eng_id, clearance):
                entered.set()
                await gate.wait()
                return await real_lower(eng_id, clearance)
            reg.lower_origin_clearance = blocking_lower

            u = _mk_update(chat_id=-1001, text=text, thread_id=555,
                           user_id=77)
            task = asyncio.create_task(ch.handle_update(u))
            await asyncio.wait_for(entered.wait(), 5)
            assert drv.inbound_unread_depth(rec.id) == 1, text
            gate.set()
            await task
            await _drain(ch)
            assert client.query_prompts == [text]

    async def test_command_persist_failure_posts_retry_notice(
            self, tmp_path, fake_telegram_bot):
        """Sol seam r2 pin: a falsy finalize with the record STILL LIVE
        posts the retry notice; an already-terminal record stays silent."""
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._finalize_cancel = AsyncMock(return_value=False)   # falsy, live
        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=555,
                       user_id=77)
        await ch.handle_update(u)
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be finalized" in n for n in notices) == 1


class TestSystemSeamAdmission:
    async def test_admission_precedes_resume_and_ready(
            self, tmp_path, fake_telegram_bot):
        """The consent-continuation window: while _resume_and_ready is
        blocked, the ticket already exists — a completion racing those
        awaits sees depth 1."""
        client = _ScriptedClient(scripts=[[_mk_assistant("ok")]])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def blocking_ready(r):
            entered.set()
            await gate.wait()
            return True
        ch._resume_and_ready = blocking_ready

        task = asyncio.create_task(
            ch.deliver_system_turn(rec, "[consent granted] continue"))
        await asyncio.wait_for(entered.wait(), 5)
        assert drv.inbound_unread_depth(rec.id) == 1
        gate.set()
        await task
        await _drain(ch)
        assert client.query_prompts == ["[consent granted] continue"]
        assert drv.inbound_unread_depth(rec.id) == 0

    async def test_not_deliverable_discharges_without_leak(
            self, tmp_path, fake_telegram_bot):
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._resume_and_ready = AsyncMock(return_value=False)
        await ch.deliver_system_turn(rec, "resume")
        assert drv.inbound_unread_depth(rec.id) == 0
        assert client.query_prompts == []

    async def test_resume_raise_applies_visibility_law(
            self, tmp_path, fake_telegram_bot):
        """Sol seam r2 pin: a RAISE inside _resume_and_ready leaves no task
        owner — one notice attempt for the live record, then discharge
        (omit the ownership wrapper and depth stays 1 forever)."""
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._resume_and_ready = AsyncMock(side_effect=RuntimeError("persist"))
        with pytest.raises(RuntimeError):
            await ch.deliver_system_turn(rec, "resume")
        assert drv.inbound_unread_depth(rec.id) == 0
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be delivered" in n for n in notices) == 1

    async def test_cancelled_before_first_step_settles_via_callback(
            self, tmp_path, fake_telegram_bot):
        """Terra seam r5/sol r6 pin: a delivery task cancelled before its
        coroutine ever ran has no active finally — the done_callback's
        tracked finalizer posts the notice and discharges."""
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._resume_and_ready = AsyncMock(return_value=True)
        await ch.deliver_system_turn(rec, "resume")
        tasks = list(ch._turn_tasks)
        assert len(tasks) == 1
        tasks[0].cancel()                      # before its first step
        await _drain(ch)
        assert drv.inbound_unread_depth(rec.id) == 0
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be delivered" in n for n in notices) == 1

    async def test_stop_gate_refuses_new_delivery(
            self, tmp_path, fake_telegram_bot):
        """Sol seam r8/r9 pin: once _stopping is set, hand-off refuses to
        create a task and the ticket is settled, not leaked."""
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._resume_and_ready = AsyncMock(return_value=True)
        ch._stopping = True
        await ch.deliver_system_turn(rec, "resume")
        assert list(ch._turn_tasks) == []
        assert drv.inbound_unread_depth(rec.id) == 0


class TestFailureOwnership:
    async def test_bg_failure_on_live_record_tells_then_discharges(
            self, tmp_path, fake_telegram_bot):
        """A delivery raising against a LIVE record: 'Turn failed' is the
        one visible outcome, discharge follows it (never precedes it)."""
        client = _ScriptedClient()
        client.closed = True                    # query raises
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        token = drv.admit_inbound(rec.id, "lost turn")
        await ch._deliver_turn_bg(rec, "lost turn", inbound_token=token)
        assert drv.inbound_unread_depth(rec.id) == 0
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("Turn failed" in n for n in notices) == 1

    async def test_bg_terminal_drop_still_attempts_the_telling(
            self, tmp_path, fake_telegram_bot):
        """Never infer disclosure from a status read (seam r6/r9): the
        terminal-drop branch makes its bounded attempt too — harmless for a
        genuine terminal (closed topic), load-bearing for a transient-fake
        one (persist rolled back live)."""
        client = _ScriptedClient()
        client.closed = True
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        await reg.mark_error(rec.id, kind="x", message="dead")
        token = drv.admit_inbound(rec.id, "raced")
        await ch._deliver_turn_bg(rec, "raced", inbound_token=token)
        assert drv.inbound_unread_depth(rec.id) == 0
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be delivered" in n for n in notices) == 1


# ===========================================================================
# 4. ungated terminal discloses a pending in_casa text
# ===========================================================================


class TestUngatedDisclosure:
    async def test_cancel_finalize_discloses_the_queued_text(self, tmp_path):
        """A non-gated finalize (cancel) with a queued in_casa ticket posts
        the dying text exactly once — silent loss became disclosed loss."""
        import agent as agent_mod
        from tools import _finalize_engagement, init_tools

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
        try:
            drv.admit_inbound(rec.id, "approval resume that died")
            fin = await _finalize_engagement(
                rec, outcome="cancelled", text="", artifacts=[],
                next_steps=[], driver=drv)
            assert bool(fin) is True
            posted = "".join(
                str(c.args) + str(c.kwargs)
                for c in (list(tch.send_to_topic.call_args_list)
                          + list(tch.send_response_to_topic.call_args_list)))
            assert posted.count("approval resume that died") == 1
            assert "1 inbound message(s)" in posted
            # Non-durable ledger: no spool-file recoverability claim.
            assert "spool file" not in posted
        finally:
            agent_mod.active_engagement_driver = None

    async def test_overflow_suffix_claims_spool_only_for_spooled_drivers(
            self, tmp_path):
        """Fix 7/terra seam r2 pin: with more texts than the 2,800-byte
        excerpt budget renders, the '…and N more' suffix claims spool-file
        recoverability ONLY when the driver actually has a durable spool."""
        import agent as agent_mod
        from tools import _finalize_engagement, init_tools

        for spooled in (False, True):
            client = _ScriptedClient()
            base = tmp_path / str(spooled)
            base.mkdir()
            reg, rec, drv = await _mk_driver_rec(base, client)
            if spooled:
                drv.drain_inbound_spool = AsyncMock()
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
            try:
                for i in range(9):
                    drv.admit_inbound(rec.id, f"msg-{i} " + "x" * 395)
                fin = await _finalize_engagement(
                    rec, outcome="cancelled", text="", artifacts=[],
                    next_steps=[], driver=drv)
                assert bool(fin) is True
                posted = "".join(
                    str(c.args) + str(c.kwargs)
                    for c in (list(tch.send_to_topic.call_args_list)
                              + list(tch.send_response_to_topic
                                     .call_args_list)))
                assert "9 inbound message(s)" in posted
                assert "…and 3 more" in posted
                assert ("spool file" in posted) is spooled
            finally:
                agent_mod.active_engagement_driver = None
