"""#1049: an approved protected call was lost when the engagement or delegation
that raised it ended before the tap.

Shape 1 — an interactive engagement completed with its own approval keyboard
still unanswered; the keyboard lived on, and a tap minted a grant bound to an
engagement nothing could resume. Shape 2 — a sync delegation from the DM
degraded to `pending`; the approval continuation re-delegated while the
specialist still held its slot (`busy`), and the delegation's completion turn
retried the call at an origin no grant can serve, then told the operator to
send it "fresh" while the approval was still live.

Real broker, coordinator, EngagementRegistry and SpecialistLimiter throughout;
only the Telegram channel is a recording fake.
"""
from __future__ import annotations

import asyncio

import pytest

import agent as agent_mod
import authz_grants
import tools as tools_mod
from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantKey, GrantStore,
    canonical_args_hash, make_resident_authz_hook,
    _DENY_DELIVERY_FAILED, _DENY_INACTIVE, _DENY_POSTED,
)
from engagement_registry import EngagementRegistry
from specialist_limits import SpecialistLimiter

TOOL = "mcp__plugin_p_p__purge"
ARTIFACT = "artifact-1"


class _Channel:
    def __init__(self):
        self.posts: list = []
        self.edits: list = []
        self.dispatches: list = []
        self.eng_dispatches: list = []
        self.post_gate: asyncio.Event | None = None
        self.post_raises = False
        self.eng_result = True

    def _user_id_is_operator(self, user_id) -> bool:
        return True

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append(request_id)
        if self.post_gate is not None:
            await self.post_gate.wait()
        if self.post_raises:
            raise RuntimeError("post boom")
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append(text)
        return True

    async def _dispatch_button_continuation(self, **kw):
        self.dispatches.append(kw)
        return True

    async def _dispatch_engagement_continuation(
            self, *, engagement_id, text, inbound_reservation=None):
        self.eng_dispatches.append(engagement_id)
        return self.eng_result


@pytest.fixture
def env(monkeypatch):
    import verdict_broker
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    monkeypatch.setattr(authz_grants, "_AWAITING_APPROVAL", {})
    return broker, ChallengeCoordinator(), _Channel()


async def _settle(n: int = 8):
    for _ in range(n):
        await asyncio.sleep(0)


def _challenge(coord, channel, *, engagement_id="", chat_id=100,
               enforcement_role="finance", target_role="assistant",
               tool_name="purge"):
    key = GrantKey(operator_id=7, chat_id=chat_id,
                   enforcement_role=enforcement_role, artifact_id=ARTIFACT,
                   tool_name=tool_name, args_hash=canonical_args_hash({"x": 1}),
                   engagement_id=engagement_id)
    handle = coord.get_or_create(
        key, chat_id=chat_id, operator_id=7, target_role=target_role,
        tool_name=tool_name, canonical_json='{"x":1}',
        enforcement_role=enforcement_role, channel=channel, grants=GrantStore(),
        engagement_id=engagement_id)
    return key, handle


def _approve(broker, coord, key):
    ch = coord._entries[key]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=7)
    assert not isinstance(claim, str), claim
    assert broker.commit(claim) is True
    ch.req.meta["on_commit_sync"](0)


# ---------------------------------------------------------------------------
# Shape 1 — an ended engagement retires its own unanswered challenges
# ---------------------------------------------------------------------------

WITHDRAWN = ("🚫 Withdrawn — purge was withdrawn when the engagement that "
             "asked for it ended")


class TestEngagementEndRetiresItsChallenges:
    async def test_cancel_matching_by_engagement_touches_only_that_one(self, env):
        broker, coord, channel = env
        k1, h1 = _challenge(coord, channel, engagement_id="E1", tool_name="purge")
        k2, h2 = _challenge(coord, channel, engagement_id="E2", tool_name="restore")
        k3, h3 = _challenge(coord, channel, engagement_id="", tool_name="wipe")
        for h in (h1, h2, h3):
            assert await h.settled_post() == "posted"
        assert coord.cancel_matching(
            engagement="E1", reason="engagement_ended") == 1
        # The DM path's "" is never an engagement filter.
        assert coord.cancel_matching(engagement="") == 0
        await _settle()
        assert channel.edits == [WITHDRAWN]
        assert k1 not in coord._entries
        assert k2 in coord._entries and k3 in coord._entries

    @pytest.mark.parametrize("end", ["completed", "cancelled", "error"])
    async def test_terminal_transition_withdraws_the_keyboard(
            self, env, tmp_path, monkeypatch, end):
        broker, coord, channel = env
        monkeypatch.setattr(tools_mod, "CHALLENGES", coord)
        monkeypatch.setattr(tools_mod, "_PLUGIN_INSTALLERS", {})
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        reg.set_terminal_observer(tools_mod._on_engagement_terminal)
        rec = await reg.create(kind="specialist", role_or_type="finance",
                               driver="in_casa", task="purge", origin={},
                               topic_id=None)
        other = await reg.create(kind="specialist", role_or_type="finance",
                                 driver="in_casa", task="other", origin={},
                                 topic_id=None)
        key, handle = _challenge(coord, channel, engagement_id=rec.id,
                                 target_role="finance")
        okey, ohandle = _challenge(coord, channel, engagement_id=other.id,
                                   target_role="finance", tool_name="restore")
        assert await handle.settled_post() == "posted"
        assert await ohandle.settled_post() == "posted"

        if end == "completed":
            await reg.mark_completed(rec.id, 1.0)
        elif end == "cancelled":
            await reg.mark_cancelled(rec.id)
        else:
            await reg.mark_error(rec.id, kind="x", message="boom")
        await _settle()

        assert channel.edits == [WITHDRAWN]
        assert key not in coord._entries
        assert okey in coord._entries          # the live engagement keeps its own
        assert channel.eng_dispatches == []     # nothing was ever resumed

    async def test_a_tap_right_after_the_transition_is_refused(
            self, env, tmp_path, monkeypatch):
        """The retirement runs in the terminal step itself, so no tap can
        commit between the transition returning and the keyboard retiring."""
        broker, coord, channel = env
        monkeypatch.setattr(tools_mod, "CHALLENGES", coord)
        monkeypatch.setattr(tools_mod, "_PLUGIN_INSTALLERS", {})
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        reg.set_terminal_observer(tools_mod._on_engagement_terminal)
        rec = await reg.create(kind="specialist", role_or_type="finance",
                               driver="in_casa", task="purge", origin={},
                               topic_id=None)
        key, handle = _challenge(coord, channel, engagement_id=rec.id,
                                 target_role="finance")
        assert await handle.settled_post() == "posted"
        ch = coord._entries[key]
        await reg.mark_cancelled(rec.id)
        claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                             request_id=ch.rid, option_index=0, actor_id=7)
        assert isinstance(claim, str), claim     # refused: nothing live

    async def test_an_engagement_delivery_failure_does_not_say_retry_in_chat(
            self, env):
        """A tap that committed before the end (so the cancel found nothing
        live) reaches the delivery seam, which refuses a terminal record."""
        broker, coord, channel = env
        channel.eng_result = False
        key, handle = _challenge(coord, channel, engagement_id="E1",
                                 target_role="finance")
        assert await handle.settled_post() == "posted"
        _approve(broker, coord, key)
        await _settle()
        assert channel.edits[-1] == (
            "⚠️ Approved, but engagement E1 could not be resumed to use it — "
            "ask for it again")


# ---------------------------------------------------------------------------
# Shape 2a — the approval continuation waits for the specialist's slot
# ---------------------------------------------------------------------------


class TestLimiterWaitUntilFree:
    async def test_free_scope_returns_at_once(self):
        lim = SpecialistLimiter(max_global=2)
        assert await lim.wait_until_free("100:finance", 0.01) is True

    async def test_wakes_on_release_and_leaves_nothing_behind(self):
        lim = SpecialistLimiter(max_global=2)
        permit = lim.try_acquire("100:finance")
        waiter = asyncio.ensure_future(lim.wait_until_free("100:finance", 5))
        await _settle()
        assert not waiter.done()
        permit.release()
        assert await waiter is True
        assert lim._free_events == {}

    async def test_times_out_and_the_release_clears_up(self):
        lim = SpecialistLimiter(max_global=2)
        permit = lim.try_acquire("100:finance")
        assert await lim.wait_until_free("100:finance", 0.01) is False
        permit.release()
        assert lim._free_events == {}

    async def test_one_waiter_timing_out_does_not_strand_another(self):
        lim = SpecialistLimiter(max_global=2)
        permit = lim.try_acquire("100:finance")
        second = asyncio.ensure_future(lim.wait_until_free("100:finance", 5))
        assert await lim.wait_until_free("100:finance", 0.01) is False
        await _settle()
        assert not second.done()
        permit.release()
        assert await asyncio.wait_for(second, 1.0) is True


class TestApprovalWaitsForTheSlot:
    @pytest.fixture
    def limiter(self, monkeypatch):
        lim = SpecialistLimiter(max_global=2)
        monkeypatch.setattr(tools_mod, "_specialist_limiter", lim)
        return lim

    async def test_delegated_approval_is_handed_back_once_the_run_ends(
            self, env, limiter):
        broker, coord, channel = env
        # The degraded sync delegation still running: the exact scope the
        # continuation's re-delegation will ask for.
        permit = limiter.try_acquire(
            tools_mod._delegation_scope({"chat_id": 100}, "finance", "sync"))
        key, handle = _challenge(coord, channel)
        assert await handle.settled_post() == "posted"
        _approve(broker, coord, key)
        await _settle()
        assert channel.edits == [
            "✅ Approved — finance (finance) may run purge once with exactly "
            "these arguments in the next 5 minutes"]
        assert channel.dispatches == []
        permit.release()
        await _settle()
        assert [d["target_role"] for d in channel.dispatches] == ["assistant"]
        # The re-delegation would now be admitted.
        assert limiter.try_acquire("100:finance") is not None

    async def test_the_waiting_decision_never_holds_the_broker_hook_drain(
            self, env, limiter):
        """Engagement finalization and shutdown both await the broker's global
        hook drain; a decision waiting for the specialist must not be inside
        it."""
        broker, coord, channel = env
        permit = limiter.try_acquire("100:finance")
        key, handle = _challenge(coord, channel)
        assert await handle.settled_post() == "posted"
        _approve(broker, coord, key)
        await asyncio.wait_for(broker.drain_hooks(), 1.0)
        assert channel.dispatches == [] and len(coord._continuations) == 1
        permit.release()
        await asyncio.wait_for(
            asyncio.gather(*coord._continuations), 1.0)
        assert len(channel.dispatches) == 1

    async def test_shutdown_drain_delivers_at_once(self, env, limiter):
        broker, coord, channel = env
        limiter.try_acquire("100:finance")          # never released
        key, handle = _challenge(coord, channel)
        assert await handle.settled_post() == "posted"
        _approve(broker, coord, key)
        await _settle()
        assert channel.dispatches == []
        await asyncio.wait_for(coord.drain(), 1.0)
        assert len(channel.dispatches) == 1
        assert coord._continuations == set() and coord._slot_waits == set()
        # A decision after the drain began is delivered inline, never parked.
        key2, handle2 = _challenge(coord, channel, tool_name="restore")
        assert await handle2.settled_post() == "posted"
        _approve(broker, coord, key2)
        await asyncio.wait_for(broker.drain_hooks(), 1.0)
        assert len(channel.dispatches) == 2

    async def test_a_continuation_queued_before_the_drain_never_waits(
            self, env, limiter):
        """The drain sweeps the waits it can see; a continuation task created
        before it but first run after it must not start one of its own."""
        broker, coord, channel = env
        limiter.try_acquire("100:finance")          # never released
        key, handle = _challenge(coord, channel)
        assert await handle.settled_post() == "posted"
        _approve(broker, coord, key)
        while not coord._continuations:
            await asyncio.sleep(0)
        assert coord._slot_waits == set()           # queued, not yet waiting
        await asyncio.wait_for(coord.drain(), 1.0)
        assert len(channel.dispatches) == 1

    async def test_a_direct_call_does_not_wait(self, env, limiter):
        broker, coord, channel = env
        limiter.try_acquire("100:finance")
        key, handle = _challenge(coord, channel, target_role="finance")
        assert await handle.settled_post() == "posted"
        _approve(broker, coord, key)
        await _settle()
        assert len(channel.dispatches) == 1

    async def test_the_wait_is_bounded(self, env, limiter, monkeypatch):
        broker, coord, channel = env
        monkeypatch.setattr(authz_grants, "_APPROVAL_SLOT_WAIT_S", 0.01)
        limiter.try_acquire("100:finance")
        key, handle = _challenge(coord, channel)
        assert await handle.settled_post() == "posted"
        _approve(broker, coord, key)
        await asyncio.sleep(0.05)
        await _settle()
        assert len(channel.dispatches) == 1


# ---------------------------------------------------------------------------
# Shape 2b — the delegation's completion turn does not retry the call
# ---------------------------------------------------------------------------


def _delegated_origin(**extra) -> dict:
    return {"role": "assistant", "execution_role": "finance",
            "channel": "telegram", "chat_id": "100", "user_id": 7,
            "cid": "c", "message_type": "channel_in", "source": "telegram",
            "_delegation_id": "deleg-1", **extra}


async def _hook_call(deps, origin):
    hook = make_resident_authz_hook(
        "finance", {TOOL: {"artifact_id": ARTIFACT, "summary": None}},
        lambda: deps)
    otok = agent_mod.origin_var.set(origin)
    etok = tools_mod.engagement_var.set(None)
    try:
        out = await hook({"tool_name": TOOL, "tool_input": {"x": 1}}, None, {})
    finally:
        agent_mod.origin_var.reset(otok)
        tools_mod.engagement_var.reset(etok)
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def _synth(delegation_id: str) -> str:
    from agent import Agent
    from bus import BusMessage, MessageType
    from specialist_registry import DelegationComplete
    complete = DelegationComplete(
        delegation_id=delegation_id, agent="finance", status="ok",
        text="Purge needs the operator's tap.", origin={"user_text": "purge"})
    msg = BusMessage(type=MessageType.NOTIFICATION, source="finance",
                     target="assistant", content=complete, channel="telegram",
                     context={"cid": "x", "chat_id": "100"})
    return Agent._synthesize_delegation_turn(object.__new__(Agent), msg).content


NOTE = "On THIS turn do NOT retry that action"


class TestCompletionTurnLeavesTheCallToTheApproval:
    async def test_a_posted_challenge_marks_the_delegation_once(self, env):
        broker, coord, channel = env
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        assert await _hook_call(deps, _delegated_origin()) == _DENY_POSTED
        body = _synth("deleg-1")
        assert NOTE in body
        assert "finance stopped at an action that needs the operator's approval" in body
        assert NOTE not in _synth("deleg-1")          # taken exactly once
        assert NOTE not in _synth("deleg-other")

    async def test_the_mark_precedes_the_post_settling(self, env):
        """An Approve that commits while the post is still settling reads back
        as `inactive` — the delegation must already be marked by then."""
        broker, coord, channel = env
        channel.post_gate = asyncio.Event()
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        task = asyncio.ensure_future(_hook_call(deps, _delegated_origin()))
        await _settle()
        assert channel.posts and not task.done()
        assert "deleg-1" in authz_grants._AWAITING_APPROVAL
        channel.post_gate.set()
        await task

    async def test_an_answer_that_races_the_post_keeps_the_mark(self, env):
        broker, coord, channel = env
        channel.post_gate = asyncio.Event()
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        task = asyncio.ensure_future(_hook_call(deps, _delegated_origin()))
        await _settle()
        (key,) = coord._entries
        _approve(broker, coord, key)
        channel.post_gate.set()
        assert await task == _DENY_INACTIVE
        assert NOTE in _synth("deleg-1")

    async def test_a_challenge_retired_during_the_post_leaves_no_mark(self, env):
        broker, coord, channel = env
        channel.post_gate = asyncio.Event()
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        task = asyncio.ensure_future(_hook_call(deps, _delegated_origin()))
        await _settle()
        broker.cancel_scope(namespace="resident_ask", scope="authz:100",
                            reason="new_session")
        channel.post_gate.set()
        assert await task == _DENY_INACTIVE
        assert NOTE not in _synth("deleg-1")

    async def test_a_failed_post_leaves_no_mark(self, env):
        broker, coord, channel = env
        channel.post_raises = True
        deps = AuthzDeps(channel=channel, grants=GrantStore(), challenges=coord)
        assert await _hook_call(deps, _delegated_origin()) == _DENY_DELIVERY_FAILED
        assert NOTE not in _synth("deleg-1")

    async def test_the_record_is_bounded(self, monkeypatch):
        monkeypatch.setattr(authz_grants, "_AWAITING_APPROVAL", {})
        for i in range(authz_grants._AWAITING_APPROVAL_CAP + 10):
            authz_grants.note_delegation_awaiting_approval(f"d{i}")
        assert len(authz_grants._AWAITING_APPROVAL) == \
            authz_grants._AWAITING_APPROVAL_CAP
        assert authz_grants.take_delegation_awaiting_approval("d0") is False
