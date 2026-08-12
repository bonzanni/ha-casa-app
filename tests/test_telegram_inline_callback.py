"""Tests for TelegramChannel inline-callback dispatch.

v0.75.0 (W5/Sol B3,B4): the callback ``_on_inline_callback`` was rewritten
onto ``verdict_broker.BROKER`` with a fail-closed contract — parse
``v1|ns|rid|idx`` (legacy ``perm:<verdict>:<rid>`` still routes), fail
closed on a missing/anonymous/wrong actor, reject a TERMINAL engagement
BEFORE claiming, claim/commit exactly once, and EXACTLY ONE
``await cq.answer(toast)`` per path. The callback itself never edits the
keyboard — that's the broker finish-hook's job (covered in
``tests/test_hooks_engagement_permission_relay.py``).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

import verdict_broker
from bus import MessageBus, MessageType
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    """Isolate every test on its own VerdictBroker — telegram.py resolves
    ``from verdict_broker import BROKER`` at call time inside
    ``_on_inline_callback``, so redirecting the module attribute here is
    picked up transparently."""
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


def _mk_callback_update(*, data, thread_id, chat_id, query_id="cq1",
                         user_id=999, answer_side_effect=None):
    cq = SimpleNamespace(
        id=query_id,
        data=data,
        message=SimpleNamespace(
            message_thread_id=thread_id,
            chat=SimpleNamespace(id=chat_id),
        ),
        answer=AsyncMock(return_value=None, side_effect=answer_side_effect),
        from_user=(SimpleNamespace(id=user_id) if user_id is not None else None),
    )
    return SimpleNamespace(callback_query=cq)


def _seed(broker, *, ns="permission", scope, rid, topic_id, operator_id,
          options=("allow", "deny"), timeout_s=5.0):
    req, created = broker.register(
        namespace=ns, scope=scope, request_id=rid, timeout_s=timeout_s,
    )
    assert created is True
    req.meta.update({
        "options": list(options), "topic_id": topic_id,
        "operator_id": operator_id,
    })
    return req


def _mk_channel(fake_telegram_bot, engagement_fixture):
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(
        bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
    )
    ch._engagement_registry = engagement_fixture.registry
    return ch


class TestV1AndLegacyRouting:
    async def test_v1_permission_allow_routes_and_answers_tick(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-001",
                   topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-001|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome == {"outcome": "answered", "option_index": 0,
                           "actor_id": 999}

    async def test_v1_permission_deny_routes_and_answers_tick(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-002",
                   topic_id=rec.topic_id, operator_id=42)

        update = _mk_callback_update(
            data="v1|permission|rid-002|1", thread_id=rec.topic_id,
            chat_id=-1001, user_id=42,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["option_index"] == 1

    async def test_legacy_perm_allow_still_routes(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-legacy",
                   topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="perm:allow:rid-legacy", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome == {"outcome": "answered", "option_index": 0,
                           "actor_id": 999}

    async def test_legacy_perm_deny_still_routes(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-legacy-d",
                   topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="perm:deny:rid-legacy-d", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["option_index"] == 1


class TestFailClosedActorBinding:
    async def test_wrong_user_refused_without_claiming(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """Pins INV-TG-002. Red case demonstrated: dropping the actor-mismatch half of _on_inline_callback's check lets the wrong user claim and fails this test."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-wu",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-wu|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=42,  # not the bound operator
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("not for you")
        # No claim: request still live/unresolved.
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-wu",
        ]

    async def test_missing_operator_id_fails_closed(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-noop",
              topic_id=rec.topic_id, operator_id=None)

        update = _mk_callback_update(
            data="v1|permission|rid-noop|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("not for you")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-noop",
        ]

    async def test_from_user_none_fails_closed(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-anon",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-anon|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=None,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("expired")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-anon",
        ]


class TestTerminalRecordRejection:
    async def test_terminal_record_rejected_before_claim(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-term",
              topic_id=rec.topic_id, operator_id=999)
        rec.status = "completed"  # simulate a terminal record

        update = _mk_callback_update(
            data="v1|permission|rid-term|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("expired")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-term",
        ]

    async def test_terminal_flip_barrier_rejects_mid_flight(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r7-B6: try_transition_terminal flips rec.status synchronously
        BEFORE the (awaited) tombstone write completes. A tap arriving in
        that window must be rejected — closes the race where
        _finalize_engagement hasn't reached cancel_scope yet."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-barrier",
              topic_id=rec.topic_id, operator_id=999)

        gate = asyncio.Event()
        orig_write_locked = registry._write_tombstone_locked

        async def _gated_write_locked():
            await gate.wait()
            await orig_write_locked()

        registry._write_tombstone_locked = _gated_write_locked

        flip_task = asyncio.create_task(
            registry.try_transition_terminal(rec.id, "completed", completed_at=1.0)
        )
        for _ in range(200):
            if rec.status == "completed":
                break
            await asyncio.sleep(0)
        assert rec.status == "completed"
        assert not flip_task.done()  # tombstone I/O still pending

        update = _mk_callback_update(
            data="v1|permission|rid-barrier|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-barrier",
        ]

        gate.set()
        await flip_task


class TestMetaMismatchRejection:
    async def test_wrong_topic_rejected(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-wt",
              topic_id=999999, operator_id=999)  # meta says a DIFFERENT topic

        update = _mk_callback_update(
            data="v1|permission|rid-wt|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("expired")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-wt",
        ]

    async def test_out_of_range_option_index_rejected(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-oor",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-oor|5", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("invalid")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-oor",
        ]

    async def test_no_live_meta_is_expired(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        update = _mk_callback_update(
            data="v1|permission|no-such-rid|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")


class TestMalformedCallbackData:
    async def test_malformed_shapes_all_expire(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        for data in (
            "not-perm:x:y",
            "perm:nope:rid",
            "perm:allow",
            "v1|unknown_ns|rid|0",
            "v1|permission|rid|notanint",
            "v1|permission|rid",
            "v1|permission||0",
            "x" * 100,
        ):
            update = _mk_callback_update(
                data=data, thread_id=rec.topic_id, chat_id=-1001,
            )
            await ch._on_inline_callback(update, context=None)
            update.callback_query.answer.assert_awaited_once_with("expired")

    async def test_unknown_topic_id_is_expired(
        self, fake_telegram_bot, engagement_fixture,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        update = _mk_callback_update(
            data="v1|permission|rid-1|0", thread_id=99999, chat_id=-1001,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")


class TestClaimCommitAndDuplicates:
    async def test_duplicate_tap_after_winner_is_already_answered(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-dup",
              topic_id=rec.topic_id, operator_id=999)

        update1 = _mk_callback_update(
            data="v1|permission|rid-dup|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update1, context=None)
        update1.callback_query.answer.assert_awaited_once_with("✔")

        update2 = _mk_callback_update(
            data="v1|permission|rid-dup|1", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update2, context=None)
        update2.callback_query.answer.assert_awaited_once_with("already answered")

    async def test_stale_tap_after_timeout_is_expired(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-stale",
                   topic_id=rec.topic_id, operator_id=999, timeout_s=0.05)
        await asyncio.wait_for(_fresh_broker.await_result(req), 1.0)  # times out

        update = _mk_callback_update(
            data="v1|permission|rid-stale|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")

    async def test_answer_raising_does_not_crash_handler(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-boom",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-boom|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
            answer_side_effect=RuntimeError("telegram transport down"),
        )
        await ch._on_inline_callback(update, context=None)  # must not raise
        update.callback_query.answer.assert_awaited_once_with("✔")


class TestEngagementAskStateAdvance:
    """v0.75.0 (Task 2): the engagement_ask branch is wired generically —
    advance_interaction_state doesn't exist until Task 7, so a registry
    lacking it takes the no-op skip-to-commit path. These tests drive the
    branch that WILL activate once Task 7 lands the method."""

    async def test_state_write_failure_aborts_claim_and_stays_live(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        registry = engagement_fixture.registry
        registry.advance_interaction_state = AsyncMock(
            side_effect=RuntimeError("db down"),
        )
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-1",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-1|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with(
            "couldn't record — please tap again",
        )
        registry.advance_interaction_state.assert_awaited_once_with(
            rec.id, "operator_answered",
        )
        # NOT resolved: claim was aborted, request stays live for a re-tap.
        assert _fresh_broker.pending(namespace="engagement_ask", scope=rec.id) == [
            "ask-1",
        ]

        # A re-tap can still win (claim wasn't stranded).
        registry.advance_interaction_state = AsyncMock(return_value=None)
        update2 = _mk_callback_update(
            data="v1|engagement_ask|ask-1|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update2, context=None)
        update2.callback_query.answer.assert_awaited_once_with("✔")

    async def test_real_registry_persist_failure_aborts_claim_and_stays_live(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker, monkeypatch,
    ):
        """B3 (Sol r1): end-to-end with the REAL registry — a genuine
        tombstone-write failure (underlying file write raises) makes
        advance_interaction_state raise, so the callback aborts the claim,
        tells the operator to re-tap, and leaves the request live. The
        in-memory interaction_state is rolled back (never left advanced)."""
        import engagement_registry as er

        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        # A real interaction-required engagement whose ask is pending.
        rec.interaction_state = "first_contact_required"
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-b3",
              topic_id=rec.topic_id, operator_id=999)

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(er, "atomic_write_json", _boom)

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-b3|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with(
            "couldn't record — please tap again",
        )
        # Request still live (claim aborted); state rolled back, not advanced.
        assert _fresh_broker.pending(
            namespace="engagement_ask", scope=rec.id,
        ) == ["ask-b3"]
        assert rec.interaction_state == "first_contact_required"

    async def test_real_registry_noninteraction_required_still_commits(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """Task 7 activates the seam: the real EngagementRegistry now HAS
        advance_interaction_state. For a non-interaction-required
        engagement (interaction_state == "", the default), the pure
        transition is a no-op (returns None, no exception) — the tap
        still commits and the state stays untouched."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        assert hasattr(engagement_fixture.registry, "advance_interaction_state")
        assert rec.interaction_state == ""
        req = _seed(_fresh_broker, ns="engagement_ask", scope=rec.id,
                   rid="ask-2", topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-2|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["outcome"] == "answered"
        assert rec.interaction_state == ""

    async def test_cancelled_after_claim_releases_it(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r7-B1: any exit without a commit — including CancelledError,
        which `except Exception` does NOT catch — must abort_claim so the
        request isn't stranded with a cancelled timer."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-cxl",
              topic_id=rec.topic_id, operator_id=999)

        def _raise_cancelled(claim):
            raise asyncio.CancelledError()

        # Instance-attribute override shadows the bound class method for
        # this broker instance only; `del` below restores normal lookup.
        _fresh_broker.commit = _raise_cancelled
        try:
            update = _mk_callback_update(
                data="v1|permission|rid-cxl|0", thread_id=rec.topic_id,
                chat_id=-1001, user_id=999,
            )
            with pytest.raises(asyncio.CancelledError):
                await ch._on_inline_callback(update, context=None)
        finally:
            del _fresh_broker.commit  # restore the bound class method

        # The claim was released (abort_claim re-armed the timer) — a fresh
        # claim on the SAME request now succeeds instead of "duplicate".
        claim = _fresh_broker.claim(
            namespace="permission", scope=rec.id, request_id="rid-cxl",
            option_index=1, actor_id=999,
        )
        assert not isinstance(claim, str)


class TestInteractionStateActivation:
    """W2/Sol B9 (Task 7): the real EngagementRegistry now carries
    ``interaction_state`` + ``advance_interaction_state`` — these drive the
    callback against a record seeded ``awaiting_operator`` directly (per
    the brief: "your registry tests can set the field directly")."""

    async def test_winning_tap_authorizes_before_commit_resolves(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """B9/r2-B7: ordered event list — claim -> advance_interaction_state
        -> commit — so the state is ALREADY authorized at the moment commit
        resolves the awaiting handler."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rec.interaction_state = "awaiting_operator"
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-order",
              topic_id=rec.topic_id, operator_id=999)

        seen_state_at_commit: list[str] = []
        orig_commit = _fresh_broker.commit

        def _spy_commit(claim):
            seen_state_at_commit.append(rec.interaction_state)
            return orig_commit(claim)

        _fresh_broker.commit = _spy_commit
        try:
            update = _mk_callback_update(
                data="v1|engagement_ask|ask-order|0", thread_id=rec.topic_id,
                chat_id=-1001, user_id=999,
            )
            await ch._on_inline_callback(update, context=None)
        finally:
            del _fresh_broker.commit

        assert seen_state_at_commit == ["authorized"]
        assert rec.interaction_state == "authorized"
        update.callback_query.answer.assert_awaited_once_with("✔")

    async def test_fast_tap_wins_and_authorizes_from_first_contact_required(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r3-B4: a tap that beats the agent's first `reply` (state still
        ``first_contact_required``) still wins the claim and authorizes
        directly — never left stuck awaiting."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rec.interaction_state = "first_contact_required"
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-fast",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-fast|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("✔")
        assert rec.interaction_state == "authorized"

    async def test_late_tap_after_timeout_does_not_authorize(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r5-B1: register -> await no_answer (retired), then a valid tap:
        claim returns "stale", advance_interaction_state is never reached,
        state stays awaiting_operator."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rec.interaction_state = "awaiting_operator"
        req = _seed(_fresh_broker, ns="engagement_ask", scope=rec.id,
                   rid="ask-stale", topic_id=rec.topic_id, operator_id=999,
                   timeout_s=0.05)
        await asyncio.wait_for(_fresh_broker.await_result(req), 1.0)  # times out

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-stale|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")
        assert rec.interaction_state == "awaiting_operator"

    async def test_late_tap_after_cancel_does_not_authorize(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r5-B1: the cancel-flavoured sibling of the timeout case — a
        cancel_scope tombstone between register and tap also makes claim
        return "stale" without ever touching interaction_state."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rec.interaction_state = "awaiting_operator"
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-cxl2",
              topic_id=rec.topic_id, operator_id=999)
        _fresh_broker.cancel_scope(
            namespace="engagement_ask", scope=rec.id, reason="test_cancel",
        )

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-cxl2|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")
        assert rec.interaction_state == "awaiting_operator"

    async def test_cancel_during_blocked_persist_commits_authorized_ask(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """B4 (Sol diff r2): the callback task is cancelled while the tombstone
        write is BLOCKED (real registry, gated writer). advance shields its
        mutate+persist, so once the gate releases the durable write completes
        and the state is authorized. The callback must COMMIT the ask (the
        awaiting handler resolves ``answered``) rather than abort/re-arm it into
        ``no_answer``."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        registry = engagement_fixture.registry
        rec.interaction_state = "awaiting_operator"
        req = _seed(_fresh_broker, ns="engagement_ask", scope=rec.id,
                    rid="ask-cxl-persist", topic_id=rec.topic_id,
                    operator_id=999)

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_write = registry._write_tombstone_locked

        async def _gated_write(*, strict=False):
            entered.set()
            await gate.wait()
            await orig_write(strict=strict)

        registry._write_tombstone_locked = _gated_write

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-cxl-persist|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        task = asyncio.create_task(ch._on_inline_callback(update, context=None))
        # Wait until the shielded write is in flight, THEN cancel the callback.
        await asyncio.wait_for(entered.wait(), 1.0)
        task.cancel()
        await asyncio.sleep(0)      # deliver the cancel at the shield await
        gate.set()                  # release the blocked durable write
        with pytest.raises(asyncio.CancelledError):
            await task

        # Durable authorization landed despite the cancel...
        assert rec.interaction_state == "authorized"
        # ...and the ask was COMMITTED, not re-armed into no_answer.
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.5)
        assert outcome["outcome"] == "answered"
        assert outcome["option_index"] == 0
        assert _fresh_broker.pending(
            namespace="engagement_ask", scope=rec.id,
        ) == []

    async def test_no_answer_leaves_awaiting_operator(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """A timed-out ask (no_answer outcome) never touches
        interaction_state — no tap ever claimed it."""
        rec = engagement_fixture.active_record
        rec.interaction_state = "awaiting_operator"
        req = _seed(_fresh_broker, ns="engagement_ask", scope=rec.id,
                   rid="ask-noans", topic_id=rec.topic_id, operator_id=999,
                   timeout_s=0.05)
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 1.0)
        assert outcome["outcome"] == "no_answer"
        assert rec.interaction_state == "awaiting_operator"


class TestKeyboardOwnership:
    async def test_callback_never_edits_the_keyboard(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-noedit",
              topic_id=rec.topic_id, operator_id=999)

        fake_telegram_bot.edit_message_reply_markup = AsyncMock()
        fake_telegram_bot.edit_message_text = AsyncMock()

        update = _mk_callback_update(
            data="v1|permission|rid-noedit|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        fake_telegram_bot.edit_message_reply_markup.assert_not_awaited()
        fake_telegram_bot.edit_message_text.assert_not_awaited()

    async def test_composed_keyboard_callback_data_within_64_bytes(
        self, fake_telegram_bot, engagement_fixture,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rid = "f" * 32  # hooks._RID_MAX_LEN — the worst realistic case
        rec.topic_id = 1001

        captured: dict = {}

        async def _capture_send_to_topic(thread_id, text, **kwargs):
            captured["kwargs"] = kwargs
            return 1

        ch.send_to_topic = _capture_send_to_topic  # type: ignore[method-assign]

        mid = await ch.post_perm_keyboard(
            engagement_id=rec.id, request_id=rid, tool_name="Bash",
            tool_input={"command": "ls"},
        )
        assert mid == 1
        kbd = captured["kwargs"]["reply_markup"]
        for row in kbd.inline_keyboard:
            for btn in row:
                assert btn.callback_data.startswith("v1|permission|")
                assert len(btn.callback_data.encode("utf-8")) <= 64


# ===========================================================================
# v0.76.0 (W5b / resident_ask): DM-scoped single-owner tap contract, DM
# keyboard APIs, and the button-continuation dispatch helper [A:§2, r1-B2].
# ===========================================================================


def _mk_dm_update(*, data, chat_id, user_id=999, query_id="cq1",
                  answer_side_effect=None, has_message=True):
    """A DM (chat-addressed, NO topic thread) callback update. The resident
    branch reads ``cq.message.chat.id`` — no ``message_thread_id``."""
    message = (
        SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        if has_message else None
    )
    cq = SimpleNamespace(
        id=query_id,
        data=data,
        message=message,
        answer=AsyncMock(return_value=None, side_effect=answer_side_effect),
        from_user=(SimpleNamespace(id=user_id) if user_id is not None else None),
    )
    return SimpleNamespace(callback_query=cq)


def _mk_dm_channel(fake_telegram_bot, bus=None):
    from channels.telegram import TelegramChannel
    return TelegramChannel(bot=fake_telegram_bot, chat_id=100, bus=bus)


async def _settle(pred, tries=2000):
    """Yield the loop until *pred* holds (bounded). Used instead of
    ``drain_hooks`` for these unit tests because the finish-hook task often
    completes BEFORE we can drain — ``drain_hooks`` assumes hooks are still
    pending (the safe cancel_all -> drain shutdown ladder), so gathering an
    already-done hook would tight-spin. ``sleep(0)`` properly yields, letting
    both the hook AND its discard callback run."""
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never reached")


def _seed_resident(broker, *, kind="dm", chat_id, rid, operator_id=999,
                   options=("Yes", "No"), timeout_s=5.0, on_commit_sync=None,
                   meta_extra=None):
    scope = f"{kind}:{chat_id}"
    req, created = broker.register(
        namespace="resident_ask", scope=scope, request_id=rid,
        timeout_s=timeout_s,
    )
    assert created is True
    req.meta.update({
        "options": list(options),
        "chat_id": chat_id,
        "operator_id": operator_id,
        "_scope": scope,
    })
    if on_commit_sync is not None:
        req.meta["on_commit_sync"] = on_commit_sync
    if meta_extra:
        req.meta.update(meta_extra)
    return req


class TestResidentCommitContract:
    async def test_commit_then_sync_step_then_toast_order(
        self, fake_telegram_bot, _fresh_broker,
    ):
        """r3-B1: the callback event order is commit -> sync step -> toast.
        commit() is synchronous; the sync step runs with NO await between."""
        events: list[str] = []
        ch = _mk_dm_channel(fake_telegram_bot)

        orig_commit = _fresh_broker.commit

        def _spy_commit(claim):
            events.append("commit")
            return orig_commit(claim)

        _fresh_broker.commit = _spy_commit
        try:
            req = _seed_resident(
                _fresh_broker, chat_id=500, rid="rid-order",
                on_commit_sync=lambda i: events.append(f"step:{i}"),
            )
            update = _mk_dm_update(
                data="v1|resident_ask|rid-order|0", chat_id=500, user_id=999,
                answer_side_effect=lambda _t: events.append("toast"),
            )
            # Keyboard must NEVER be edited by the callback path itself.
            fake_telegram_bot.edit_message_text = AsyncMock()
            fake_telegram_bot.edit_message_reply_markup = AsyncMock()

            await ch._on_inline_callback(update, context=None)
        finally:
            del _fresh_broker.commit

        assert events == ["commit", "step:0", "toast"]
        update.callback_query.answer.assert_awaited_once_with("✔")
        fake_telegram_bot.edit_message_text.assert_not_awaited()
        fake_telegram_bot.edit_message_reply_markup.assert_not_awaited()
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome == {"outcome": "answered", "option_index": 0,
                           "actor_id": 999}

    async def test_authz_scope_also_resolves(
        self, fake_telegram_bot, _fresh_broker,
    ):
        """authz:<chat> is the disjoint sibling scope — a tap resolves it via
        the second get_meta lookup."""
        ch = _mk_dm_channel(fake_telegram_bot)
        req = _seed_resident(
            _fresh_broker, kind="authz", chat_id=700, rid="rid-authz",
            options=("Approve", "Deny"),
        )
        update = _mk_dm_update(
            data="v1|resident_ask|rid-authz|1", chat_id=700, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["option_index"] == 1

    async def test_sync_step_raising_is_logged_and_commit_still_wins(
        self, fake_telegram_bot, _fresh_broker, caplog,
    ):
        """r3-B1: on_commit_sync raising is logged and swallowed — commit has
        already succeeded (toast ✔, outcome answered) and `minted` stays absent
        so the finish hook can edit the internal-error text (pinned in the
        finish-hook-shape tests). The callback NEVER dispatches."""
        ch = _mk_dm_channel(fake_telegram_bot)
        ch._dispatch_button_continuation = AsyncMock()

        def _boom(_i):
            raise RuntimeError("mint failed")

        req = _seed_resident(
            _fresh_broker, kind="authz", chat_id=500, rid="rid-mintfail",
            options=("Approve", "Deny"), on_commit_sync=_boom,
        )
        update = _mk_dm_update(
            data="v1|resident_ask|rid-mintfail|0", chat_id=500, user_id=999,
        )
        with caplog.at_level(logging.ERROR):
            await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("✔")
        assert "on_commit_sync failed" in caplog.text
        assert "minted" not in req.meta
        ch._dispatch_button_continuation.assert_not_awaited()
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["outcome"] == "answered"


class TestResidentRejectionPaths:
    """Each path: exact toast, NO dispatch, exactly one cq.answer."""

    async def _assert_rejected(self, ch, update, expected_toast, broker, rid,
                               scope):
        ch._dispatch_button_continuation = AsyncMock()
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with(expected_toast)
        assert update.callback_query.answer.await_count == 1
        ch._dispatch_button_continuation.assert_not_awaited()

    async def test_absent_cq_message_expired(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        _seed_resident(_fresh_broker, chat_id=500, rid="rid-nomsg")
        update = _mk_dm_update(
            data="v1|resident_ask|rid-nomsg|0", chat_id=500, has_message=False,
        )
        await self._assert_rejected(
            ch, update, "expired", _fresh_broker, "rid-nomsg", "dm:500")

    async def test_no_live_meta_expired(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        update = _mk_dm_update(
            data="v1|resident_ask|no-such|0", chat_id=500, user_id=999,
        )
        await self._assert_rejected(
            ch, update, "expired", _fresh_broker, "no-such", "dm:500")

    async def test_wrong_chat_expired(
        self, fake_telegram_bot, _fresh_broker,
    ):
        """meta found under dm:<chat> but meta["chat_id"] mismatches the tap
        chat — the belt-and-suspenders chat check rejects it."""
        ch = _mk_dm_channel(fake_telegram_bot)
        _seed_resident(_fresh_broker, chat_id=500, rid="rid-wc",
                       meta_extra={"chat_id": 999})
        update = _mk_dm_update(
            data="v1|resident_ask|rid-wc|0", chat_id=500, user_id=999,
        )
        await self._assert_rejected(
            ch, update, "expired", _fresh_broker, "rid-wc", "dm:500")

    async def test_absent_from_user_not_for_you(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        _seed_resident(_fresh_broker, chat_id=500, rid="rid-anon")
        update = _mk_dm_update(
            data="v1|resident_ask|rid-anon|0", chat_id=500, user_id=None,
        )
        await self._assert_rejected(
            ch, update, "not for you", _fresh_broker, "rid-anon", "dm:500")

    async def test_wrong_user_not_for_you(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        _seed_resident(_fresh_broker, chat_id=500, rid="rid-wu",
                       operator_id=999)
        update = _mk_dm_update(
            data="v1|resident_ask|rid-wu|0", chat_id=500, user_id=42,
        )
        await self._assert_rejected(
            ch, update, "not for you", _fresh_broker, "rid-wu", "dm:500")

    async def test_out_of_range_option_invalid(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        _seed_resident(_fresh_broker, chat_id=500, rid="rid-oor")
        update = _mk_dm_update(
            data="v1|resident_ask|rid-oor|5", chat_id=500, user_id=999,
        )
        await self._assert_rejected(
            ch, update, "invalid", _fresh_broker, "rid-oor", "dm:500")

    async def test_stale_after_timeout_expired(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        req = _seed_resident(_fresh_broker, chat_id=500, rid="rid-stale",
                             timeout_s=0.05)
        await asyncio.wait_for(_fresh_broker.await_result(req), 1.0)  # no_answer
        update = _mk_dm_update(
            data="v1|resident_ask|rid-stale|0", chat_id=500, user_id=999,
        )
        await self._assert_rejected(
            ch, update, "expired", _fresh_broker, "rid-stale", "dm:500")

    async def test_duplicate_after_winner_already_answered(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        _seed_resident(_fresh_broker, chat_id=500, rid="rid-dup")
        u1 = _mk_dm_update(
            data="v1|resident_ask|rid-dup|0", chat_id=500, user_id=999,
        )
        await ch._on_inline_callback(u1, context=None)
        u1.callback_query.answer.assert_awaited_once_with("✔")
        u2 = _mk_dm_update(
            data="v1|resident_ask|rid-dup|1", chat_id=500, user_id=999,
        )
        await ch._on_inline_callback(u2, context=None)
        u2.callback_query.answer.assert_awaited_once_with("already answered")


class TestResidentFinishHookShape:
    """Pin the T4/T5 finish-hook continuation SHAPE now (edit-success FIRST ->
    dispatch SECOND -> overwrite with failure text ONLY on dispatch failure),
    using the real Task-2 code (edit_dm_message + _dispatch_button_continuation)
    against a seeded fake hook."""

    def _install_finish_hook(self, broker, req, hook):
        broker.set_finish_hook(req, hook)

    async def test_edit_first_then_dispatch_success_no_failure_edit(
        self, fake_telegram_bot, _fresh_broker,
    ):
        events: list[str] = []
        bus = MessageBus()
        bus.register("assistant")
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)
        fake_telegram_bot.edit_message_text = AsyncMock(
            side_effect=lambda **kw: events.append(f"edit:{kw['text']}"),
        )
        mid, chat = 42, 500
        req = _seed_resident(_fresh_broker, chat_id=chat, rid="rid-ok")

        async def _finish(outcome):
            if outcome["outcome"] != "answered":
                await ch.edit_dm_message(chat, mid, "expired")
                return
            await ch.edit_dm_message(chat, mid, "answered: Yes")
            ok = await ch._dispatch_button_continuation(
                chat_id=chat, user_id=999, target_role="assistant",
                request_id="rid-ok", text="Yes",
            )
            events.append(f"dispatch:{ok}")
            if not ok:
                await ch.edit_dm_message(chat, mid, "delivery failed")

        self._install_finish_hook(_fresh_broker, req, _finish)

        update = _mk_dm_update(
            data="v1|resident_ask|rid-ok|0", chat_id=chat, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        await _settle(lambda: events[-1:] == ["dispatch:True"])

        assert events == ["edit:answered: Yes", "dispatch:True"]
        # The dispatched synthetic turn reached the assistant queue.
        assert bus.queues["assistant"].qsize() == 1

    async def test_overwrite_with_failure_text_on_dispatch_failure(
        self, fake_telegram_bot, _fresh_broker,
    ):
        events: list[str] = []
        fake_sleep = AsyncMock()
        bus = MessageBus()  # no target registered -> dispatch fails
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)
        fake_telegram_bot.edit_message_text = AsyncMock(
            side_effect=lambda **kw: events.append(f"edit:{kw['text']}"),
        )
        mid, chat = 42, 500
        req = _seed_resident(_fresh_broker, chat_id=chat, rid="rid-fail")

        async def _finish(outcome):
            if outcome["outcome"] != "answered":
                await ch.edit_dm_message(chat, mid, "expired")
                return
            await ch.edit_dm_message(chat, mid, "answered: Yes")
            ok = await ch._dispatch_button_continuation(
                chat_id=chat, user_id=999, target_role="assistant",
                request_id="rid-fail", text="Yes", _sleep=fake_sleep,
            )
            events.append(f"dispatch:{ok}")
            if not ok:
                await ch.edit_dm_message(chat, mid, "delivery failed")

        self._install_finish_hook(_fresh_broker, req, _finish)

        update = _mk_dm_update(
            data="v1|resident_ask|rid-fail|0", chat_id=chat, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        await _settle(lambda: events[-1:] == ["edit:delivery failed"])

        assert events == [
            "edit:answered: Yes", "dispatch:False", "edit:delivery failed",
        ]
        assert fake_sleep.await_count == 2  # 3 attempts -> 2 backoffs

    async def test_non_answered_outcome_edits_expired_no_dispatch(
        self, fake_telegram_bot, _fresh_broker,
    ):
        events: list[str] = []
        bus = MessageBus()
        bus.register("assistant")
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)
        ch._dispatch_button_continuation = AsyncMock()
        fake_telegram_bot.edit_message_text = AsyncMock(
            side_effect=lambda **kw: events.append(f"edit:{kw['text']}"),
        )
        mid, chat = 42, 500
        req = _seed_resident(_fresh_broker, chat_id=chat, rid="rid-noans",
                             timeout_s=0.05)

        async def _finish(outcome):
            if outcome["outcome"] != "answered":
                await ch.edit_dm_message(chat, mid, "expired")
                return
            await ch.edit_dm_message(chat, mid, "answered")

        self._install_finish_hook(_fresh_broker, req, _finish)
        # Let it time out (no_answer) -> hook fires with a non-answered outcome.
        await asyncio.wait_for(_fresh_broker.await_result(req), 1.0)
        await _settle(lambda: events[-1:] == ["edit:expired"])

        assert events == ["edit:expired"]
        ch._dispatch_button_continuation.assert_not_awaited()

    async def test_authz_mint_absent_edits_internal_error_no_dispatch(
        self, fake_telegram_bot, _fresh_broker,
    ):
        """T5 authz shape: approve tap whose mint (on_commit_sync) failed —
        `minted` absent -> edit internal-error text and NEVER dispatch."""
        events: list[str] = []
        bus = MessageBus()
        bus.register("specialist:finance")
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)
        ch._dispatch_button_continuation = AsyncMock()
        fake_telegram_bot.edit_message_text = AsyncMock(
            side_effect=lambda **kw: events.append(f"edit:{kw['text']}"),
        )
        mid, chat = 42, 500

        def _boom(_i):
            raise RuntimeError("mint failed")

        req = _seed_resident(
            _fresh_broker, kind="authz", chat_id=chat, rid="rid-mint",
            options=("Approve", "Deny"), on_commit_sync=_boom,
        )

        async def _finish(outcome):
            if outcome["outcome"] != "answered":
                await ch.edit_dm_message(chat, mid, "expired")
                return
            # approve (idx 0) but mint absent -> internal error, NO dispatch.
            if not req.meta.get("minted"):
                await ch.edit_dm_message(
                    chat, mid,
                    "internal error recording the approval — call the tool again",
                )
                return
            await ch.edit_dm_message(chat, mid, "approved")
            await ch._dispatch_button_continuation(
                chat_id=chat, user_id=999, target_role="specialist:finance",
                request_id="rid-mint", text="approved",
            )

        self._install_finish_hook(_fresh_broker, req, _finish)
        update = _mk_dm_update(
            data="v1|resident_ask|rid-mint|0", chat_id=chat, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        await _settle(lambda: bool(events))

        assert events == [
            "edit:internal error recording the approval — call the tool again",
        ]
        ch._dispatch_button_continuation.assert_not_awaited()


class TestResidentCopiedMetaBoundary:
    """r3-B1 (REAL broker): register() shallow-copies the caller's meta dict,
    and get_meta returns the broker-owned dict BY REFERENCE for a live
    request. So a caller who mutates their ORIGINAL dict is invisible, while
    the get_meta-returned dict IS req.meta — this is why on_commit_sync must
    mutate the broker-owned meta, and the finish hook sees that mutation."""

    async def test_caller_mutation_invisible_broker_dict_seen_by_hook(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        caller = {
            "options": ["Yes", "No"], "chat_id": 500, "operator_id": 999,
            "_scope": "dm:500",
        }
        req, created = _fresh_broker.register(
            namespace="resident_ask", scope="dm:500", request_id="rid-cm",
            timeout_s=5.0, meta=caller,
        )
        assert created is True

        # Mutating the CALLER's original dict is invisible (shallow copy).
        caller["injected_by_caller"] = True
        gm = _fresh_broker.get_meta(
            namespace="resident_ask", scope="dm:500", request_id="rid-cm",
        )
        assert "injected_by_caller" not in gm
        assert gm is req.meta  # by reference for a live request

        # Mutating the get_meta-returned (broker-owned) dict IS req.meta and
        # the finish hook sees it.
        gm["injected_by_broker"] = "x"
        seen: dict = {}

        async def _finish(outcome):
            seen["v"] = req.meta.get("injected_by_broker")

        _fresh_broker.set_finish_hook(req, _finish)

        update = _mk_dm_update(
            data="v1|resident_ask|rid-cm|0", chat_id=500, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        await _settle(lambda: "v" in seen)

        assert seen["v"] == "x"


class TestResidentCancellationAtomicToast:
    async def test_cancel_after_commit_still_completes_exactly_one_answer(
        self, fake_telegram_bot, _fresh_broker,
    ):
        """r3-B2: gate cq.answer, cancel the callback task after commit+mint,
        then release the gate. The callback must not finish until exactly ONE
        answer completes — answer completion precedes CancelledError."""
        order: list[str] = []
        gate = asyncio.Event()
        ch = _mk_dm_channel(fake_telegram_bot)

        async def _gated_answer(_text):
            order.append("answer_start")
            await gate.wait()
            order.append("answer_done")

        _seed_resident(
            _fresh_broker, chat_id=500, rid="rid-cxl",
            on_commit_sync=lambda i: order.append("mint"),
        )
        update = _mk_dm_update(
            data="v1|resident_ask|rid-cxl|0", chat_id=500, user_id=999,
        )
        update.callback_query.answer = AsyncMock(side_effect=_gated_answer)

        task = asyncio.create_task(ch._on_inline_callback(update, context=None))
        # Wait until commit+mint ran and the (gated) answer task started.
        for _ in range(500):
            if "mint" in order and "answer_start" in order:
                break
            await asyncio.sleep(0)
        assert order[:2] == ["mint", "answer_start"]

        task.cancel()
        await asyncio.sleep(0)   # deliver the cancel at the shield await
        gate.set()               # release the gated answer

        with pytest.raises(asyncio.CancelledError):
            try:
                await task
            except asyncio.CancelledError:
                order.append("cancelled")
                raise

        assert order.index("answer_done") < order.index("cancelled")
        update.callback_query.answer.assert_awaited_once_with("✔")


class TestDispatchButtonContinuation:
    async def test_accepted_first_try_builds_synthetic_marker(
        self, fake_telegram_bot,
    ):
        bus = MessageBus()
        bus.register("assistant")
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)
        fake_sleep = AsyncMock()

        ok = await ch._dispatch_button_continuation(
            chat_id=500, user_id=999, target_role="assistant",
            request_id="rid-x", text="Yes", _sleep=fake_sleep,
        )
        assert ok is True
        fake_sleep.assert_not_awaited()

        _prio, _seq, msg = bus.queues["assistant"].get_nowait()
        assert msg.type == MessageType.CHANNEL_IN
        assert msg.target == "assistant"
        assert msg.content == "Yes"
        assert msg.channel == "telegram"
        assert msg.context["synthetic"] == "button"
        assert msg.context["button_answer"] == "rid-x"
        assert msg.context["chat_id"] == 500
        assert msg.context["user_id"] == 999
        assert msg.context["cid"]

    async def test_no_target_retries_thrice_then_false(
        self, fake_telegram_bot,
    ):
        bus = MessageBus()  # target never registered
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)
        fake_sleep = AsyncMock()

        ok = await ch._dispatch_button_continuation(
            chat_id=500, user_id=999, target_role="assistant",
            request_id="rid-x", text="Yes", _sleep=fake_sleep,
        )
        assert ok is False
        # 3 attempts -> 2 backoff sleeps (0.5s, 1.0s).
        assert fake_sleep.await_count == 2
        assert [c.args[0] for c in fake_sleep.await_args_list] == [0.5, 1.0]

    async def test_target_registered_after_retry_succeeds(
        self, fake_telegram_bot,
    ):
        bus = MessageBus()
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)

        async def _register_after_first(_delay):
            bus.register("assistant")  # target appears mid-retry

        fake_sleep = AsyncMock(side_effect=_register_after_first)
        ok = await ch._dispatch_button_continuation(
            chat_id=500, user_id=999, target_role="assistant",
            request_id="rid-x", text="Yes", _sleep=fake_sleep,
        )
        assert ok is True
        assert fake_sleep.await_count == 1

    async def test_send_checked_exception_retries_thrice_then_false(
        self, fake_telegram_bot,
    ):
        """Contract: a non-CancelledError raised by ``send_checked`` is caught +
        logged, counts as a FAILED attempt, and all 3 attempts are exhausted
        with the existing backoff → return False (an escaping exception would
        abort the finish hook before the delivery-failure overwrite edit)."""
        bus = MagicMock()
        bus.send_checked = AsyncMock(side_effect=RuntimeError("bus boom"))
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)
        fake_sleep = AsyncMock()

        ok = await ch._dispatch_button_continuation(
            chat_id=500, user_id=999, target_role="assistant",
            request_id="rid-x", text="Yes", _sleep=fake_sleep,
        )
        assert ok is False
        assert bus.send_checked.await_count == 3
        assert fake_sleep.await_count == 2
        assert [c.args[0] for c in fake_sleep.await_args_list] == [0.5, 1.0]

    async def test_send_checked_exception_then_success_returns_true(
        self, fake_telegram_bot,
    ):
        """A raising attempt followed by an accepted one still succeeds."""
        bus = MagicMock()
        bus.send_checked = AsyncMock(
            side_effect=[RuntimeError("transient"), "accepted"],
        )
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)
        fake_sleep = AsyncMock()

        ok = await ch._dispatch_button_continuation(
            chat_id=500, user_id=999, target_role="assistant",
            request_id="rid-x", text="Yes", _sleep=fake_sleep,
        )
        assert ok is True
        assert bus.send_checked.await_count == 2
        assert fake_sleep.await_count == 1

    async def test_send_checked_cancelled_error_propagates(
        self, fake_telegram_bot,
    ):
        """asyncio.CancelledError must re-raise immediately, never swallowed as
        a failed attempt."""
        bus = MagicMock()
        bus.send_checked = AsyncMock(side_effect=asyncio.CancelledError())
        ch = _mk_dm_channel(fake_telegram_bot, bus=bus)
        fake_sleep = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await ch._dispatch_button_continuation(
                chat_id=500, user_id=999, target_role="assistant",
                request_id="rid-x", text="Yes", _sleep=fake_sleep,
            )
        fake_sleep.assert_not_awaited()


class TestDmKeyboardApis:
    async def test_post_dm_keyboard_composes_callback_data_and_returns_mid(
        self, fake_telegram_bot,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        captured: dict = {}

        async def _capture(chat_id, text, **kw):
            captured["chat_id"] = chat_id
            captured["kw"] = kw
            return MagicMock(message_id=77)

        fake_telegram_bot.send_message = _capture
        mid = await ch.post_dm_keyboard(
            chat_id=500, request_id="f" * 32, text="Q?", options=["Yes", "No"],
        )
        assert mid == 77
        assert captured["chat_id"] == 500
        kbd = captured["kw"]["reply_markup"]
        labels = [btn.text for row in kbd.inline_keyboard for btn in row]
        assert labels == ["Yes", "No"]
        for i, row in enumerate(kbd.inline_keyboard):
            btn = row[0]
            assert btn.callback_data == f"v1|resident_ask|{'f' * 32}|{i}"
            assert len(btn.callback_data.encode("utf-8")) <= 64

    async def test_post_dm_keyboard_send_failure_returns_none(
        self, fake_telegram_bot,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        fake_telegram_bot.send_message = AsyncMock(
            side_effect=RuntimeError("telegram down"),
        )
        mid = await ch.post_dm_keyboard(
            chat_id=500, request_id="r", text="Q?", options=["Yes"],
        )
        assert mid is None

    async def test_edit_dm_message_success(self, fake_telegram_bot):
        ch = _mk_dm_channel(fake_telegram_bot)
        fake_telegram_bot.edit_message_text = AsyncMock()
        assert await ch.edit_dm_message(500, 42, "new") is True
        fake_telegram_bot.edit_message_text.assert_awaited_once_with(
            chat_id=500, message_id=42, text="new",
        )

    async def test_edit_dm_message_not_modified_is_success(
        self, fake_telegram_bot,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        fake_telegram_bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified"),
        )
        assert await ch.edit_dm_message(500, 42, "same") is True

    async def test_edit_dm_message_failure_returns_false(
        self, fake_telegram_bot,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        fake_telegram_bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("message to edit not found"),
        )
        assert await ch.edit_dm_message(500, 42, "x") is False


class TestUpdateTopicState:
    """E-12 (v0.37.0) Task 23: TelegramChannel.update_topic_state."""

    async def test_update_state_edits_topic_title(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel
        # The engagement_fixture creates only a registry record with
        # topic_id=555. Pre-create the forum topic on the fake bot so
        # update_topic_state's edit_forum_topic finds it.
        # v0.37.1 D-1: title format is now "<state> <task>" (role is on
        # the bubble, not in the title).
        await fake_telegram_bot.create_forum_topic(
            chat_id=-1001, name="🟢 t",
        )
        # The fake's auto-assigned thread id starts at 1001; manually align
        # the fixture's topic_id to that value.
        rec = engagement_fixture.active_record
        rec.topic_id = 1001

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting",
        )

        sg = fake_telegram_bot._supergroups[-1001]
        topic = sg.topics[1001]
        assert topic.name.startswith("🟡"), f"got {topic.name!r}"

    async def test_update_state_is_noop_when_state_unchanged(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        # Seed the registry with current_state_emoji=🟡 already.
        await engagement_fixture.registry.set_channel_state(
            rec.id, current_state_emoji="🟡",
        )

        # Sanity: edit_forum_topic shouldn't be called.
        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting",
        )
        ef.assert_not_awaited()

    async def test_live_repaint_refused_on_terminal_record(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        """#347 (Sol+Terra r4/r5): NO live paint (``active`` OR ``awaiting``)
        may land over a terminal record — the fresh status read happens under
        the topic-state lock, immediately before the wire edit. A terminal
        repaint (e.g. re-asserting ``completed``) stays allowed."""
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        rec.topic_id = 1001
        rec.status = "completed"

        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)
        await ch.update_topic_state(engagement_id=rec.id, new_state="active")
        await ch.update_topic_state(engagement_id=rec.id, new_state="awaiting")
        ef.assert_not_awaited()
        # Terminal paints still go through on a terminal record.
        await ch.update_topic_state(
            engagement_id=rec.id, new_state="completed")
        ef.assert_awaited_once()

    async def test_delayed_awaiting_after_terminal_edit_refused(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        """Terra r5 regression shape: terminal edit FIRST, then a delayed
        ``awaiting`` request — the yellow paint must be refused and the
        persisted emoji must stay terminal."""
        from channels.state_emoji import STATE_EMOJI
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        rec.topic_id = 1001

        names: list[str] = []

        async def record_edit(**kw):
            names.append(kw["name"])

        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", record_edit)
        rec.status = "completed"
        await ch.update_topic_state(
            engagement_id=rec.id, new_state="completed")
        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting")
        assert len(names) == 1
        assert names[0].startswith(STATE_EMOJI["completed"])
        assert rec.current_state_emoji == STATE_EMOJI["completed"]

    async def test_terminal_edit_queued_behind_inflight_edit_paints_last(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        """#347 (Sol+Terra r4): state edits for one engagement are totally
        ordered by the per-engagement lock — a terminal transition arriving
        while another state edit's wire call is in flight queues BEHIND it
        and paints last, so the topic can never end up green after close."""
        import asyncio
        from channels.state_emoji import STATE_EMOJI
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        rec.topic_id = 1001

        names: list[str] = []
        gate = asyncio.Event()

        async def slow_edit(**kw):
            names.append(kw["name"])
            if len(names) == 1:
                await gate.wait()

        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", slow_edit)

        t1 = asyncio.ensure_future(ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting"))
        async with asyncio.timeout(5.0):
            while len(names) < 1:
                await asyncio.sleep(0)
        # Terminalization lands while t1's wire edit is in flight.
        rec.status = "completed"
        t2 = asyncio.ensure_future(ch.update_topic_state(
            engagement_id=rec.id, new_state="completed"))
        for _ in range(8):
            await asyncio.sleep(0)
        assert len(names) == 1          # t2 is queued behind the lock
        gate.set()
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5.0)
        assert len(names) == 2
        assert names[-1].startswith(STATE_EMOJI["completed"])

    async def test_update_state_unknown_state_drops_silently(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record

        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="not-a-real-state",
        )
        ef.assert_not_awaited()

    async def test_update_state_unknown_engagement_drops_silently(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry

        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        await ch.update_topic_state(
            engagement_id="does-not-exist", new_state="awaiting",
        )
        ef.assert_not_awaited()

    async def test_update_state_persists_current_state_emoji(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel
        await fake_telegram_bot.create_forum_topic(
            chat_id=-1001, name="🟢·🤖 t",
        )
        rec = engagement_fixture.active_record
        rec.topic_id = 1001

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        assert rec.current_state_emoji is None  # baseline

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting",
        )
        assert rec.current_state_emoji == "🟡"


# ---------------------------------------------------------------------------
# v0.102.0 (#217): deliver_system_turn — the resume-if-suspended-then-deliver
# seam a background reconcile callback uses to proceed a paused configurator
# engagement after an operator taps Approve. Mirrors the resume step
# handle_update runs before a topic-routed delivery, then hands the turn to the
# SAME _deliver_turn_bg task the operator's manual message uses.
# ---------------------------------------------------------------------------


class _FakeResumeDriver:
    """Records resume/is_alive against a live-set; a resumed engagement joins
    the set so a subsequent is_alive is True (matches in_casa_driver's
    _clients membership contract). ``resume_error`` makes resume raise."""

    def __init__(self, alive: set | None = None, *, resume_error=None):
        self.alive = alive if alive is not None else set()
        self.resume_error = resume_error
        self.events: list = []

    def is_alive(self, rec):
        return rec.id in self.alive

    async def resume(self, rec, session_id):
        self.events.append(("resume", rec.id, session_id))
        if self.resume_error is not None:
            raise self.resume_error
        self.alive.add(rec.id)


def _mk_topic_update(*, thread_id, text="continue", user_id=77):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = -1001
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id)
    u.message.message_id = 999
    return u


class TestDeliverSystemTurn:
    """v0.102.0 (#217): deliver_system_turn + the shared _resume_and_ready core.
    Both the operator-message path (handle_update) and the system-turn path use
    ONE resume/lifecycle helper under ONE per-topic lock — no second resume
    implementation, no duplicate ClaudeSDKClient on a system-vs-manual race."""

    def _mk(self, fake_telegram_bot, engagement_fixture, *, alive=None,
            resume_error=None):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        drv = _FakeResumeDriver(alive, resume_error=resume_error)
        ch._engagement_driver = drv
        sent: list = []

        async def _send(rec, text, *, tg_message_id=None):
            sent.append((rec.id, text))
            return None

        ch._driver_send_user_turn = _send
        return ch, drv, sent

    async def test_resumes_a_suspended_engagement_before_delivering(
        self, fake_telegram_bot, engagement_fixture,
    ):
        rec = engagement_fixture.active_record  # driver="in_casa"
        rec.sdk_session_id = "sess-1"
        ch, drv, sent = self._mk(fake_telegram_bot, engagement_fixture)

        await ch.deliver_system_turn(rec, "proceed with the recipe now")
        await asyncio.gather(*list(ch._turn_tasks))  # drain the spawned turn

        assert drv.events == [("resume", rec.id, "sess-1")]  # resumed once
        assert sent == [(rec.id, "proceed with the recipe now")]  # then delivered

    async def test_delivers_without_resume_when_already_alive(
        self, fake_telegram_bot, engagement_fixture,
    ):
        rec = engagement_fixture.active_record
        rec.sdk_session_id = "sess-1"
        ch, drv, sent = self._mk(
            fake_telegram_bot, engagement_fixture, alive={rec.id})

        await ch.deliver_system_turn(rec, "go")
        await asyncio.gather(*list(ch._turn_tasks))

        assert drv.events == []  # already alive — resume never attempted
        assert sent == [(rec.id, "go")]

    async def test_terminal_record_skips_resume_and_delivery(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """A delayed Approve landing on a completed/cancelled/error engagement
        must NOT resume or deliver — _resume_and_ready gates it out."""
        import time
        rec = engagement_fixture.active_record
        rec.sdk_session_id = "sess-1"
        ch, drv, sent = self._mk(fake_telegram_bot, engagement_fixture)
        await engagement_fixture.registry.mark_completed(rec.id, time.time())

        await ch.deliver_system_turn(rec, "too late")
        await asyncio.gather(*list(ch._turn_tasks))

        assert drv.events == []          # never resumed a terminal record
        assert sent == []                # never delivered
        assert not ch._turn_tasks        # no doomed background turn spawned

    async def test_orphan_without_session_marks_error_and_skips(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """Dead record with NO sdk_session_id -> orphan branch (mark_error +
        cleanup), no doomed turn spawned."""
        rec = engagement_fixture.active_record
        rec.sdk_session_id = None
        ch, drv, sent = self._mk(fake_telegram_bot, engagement_fixture)  # not alive

        assert await ch._resume_and_ready(rec) is False
        assert drv.events == []                   # nothing to resume
        assert rec.status == "error"              # orphan_no_session marked
        assert sent == []

    async def test_resume_failure_two_strike_marks_error_and_skips(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """resume raises -> fail-count increments; the 2nd strike marks
        resume_failed and returns False (no delivery)."""
        rec = engagement_fixture.active_record
        rec.sdk_session_id = "sess-1"
        ch, drv, sent = self._mk(
            fake_telegram_bot, engagement_fixture,
            resume_error=RuntimeError("rotated"))

        assert await ch._resume_and_ready(rec) is False   # strike 1
        assert rec.origin["_resume_fail_count"] == 1
        assert rec.status != "error"                       # not yet terminal
        assert await ch._resume_and_ready(rec) is False   # strike 2
        assert rec.origin["_resume_fail_count"] == 2
        assert rec.status == "error"                       # resume_failed marked
        assert len(drv.events) == 2                         # attempted both times
        assert sent == []

    async def test_idle_engagement_stamps_user_turn_before_delivery(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """An idle (but deliverable) engagement -> update_user_turn advances it
        back to active before _resume_and_ready returns True."""
        rec = engagement_fixture.active_record
        rec.sdk_session_id = "sess-1"
        ch, drv, sent = self._mk(fake_telegram_bot, engagement_fixture)
        await engagement_fixture.registry.mark_idle(rec.id)
        assert rec.status == "idle"

        assert await ch._resume_and_ready(rec) is True
        assert rec.status == "active"      # update_user_turn flipped idle->active
        assert drv.events == [("resume", rec.id, "sess-1")]

    async def test_system_and_manual_turn_serialize_to_a_single_resume(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """The whole finding: a concurrent operator nudge (handle_update) and a
        system turn (deliver_system_turn) on the SAME suspended engagement must
        resume EXACTLY ONCE — the shared per-topic lock serializes them, so no
        duplicate ClaudeSDKClient / parallel recipe."""
        rec = engagement_fixture.active_record
        rec.sdk_session_id = "sess-1"
        ch, drv, sent = self._mk(fake_telegram_bot, engagement_fixture)

        u = _mk_topic_update(thread_id=rec.topic_id, text="operator nudge")
        await asyncio.gather(
            ch.handle_update(u),
            ch.deliver_system_turn(rec, "system resume"),
        )
        await asyncio.gather(*list(ch._turn_tasks))

        resumes = [e for e in drv.events if e[0] == "resume"]
        assert len(resumes) == 1                 # serialized — one resume only
        assert rec.id in drv.alive               # exactly one live client
        assert len(sent) == 2                    # both turns still delivered

    async def test_manual_topic_path_routes_through_shared_helper(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        """Extraction regression: handle_update now routes its resume/lifecycle
        through the SAME _resume_and_ready helper the system path uses."""
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        ch._driver_send_user_turn = AsyncMock()
        rec = engagement_fixture.active_record

        spy = AsyncMock(return_value=True)
        monkeypatch.setattr(ch, "_resume_and_ready", spy)

        u = _mk_topic_update(thread_id=rec.topic_id, text="please continue")
        await ch.handle_update(u)
        await asyncio.gather(*list(ch._turn_tasks))

        spy.assert_awaited_once()
        assert spy.await_args.args[0].id == rec.id
        ch._driver_send_user_turn.assert_awaited_once()


class TestUpdateTopicStateSentinel:
    """#529: a cancellation between the title wire edit and the emoji
    persistence used to leave wire=new / persisted=old, and the persisted-emoji
    no-op guard then swallowed the next paint requesting the persisted state —
    a PERMANENT visual divergence. The fix persists an UNCERTAIN sentinel
    (``""``) before the wire await and the real emoji only after it, so a
    cancellation anywhere in the window leaves a value no real paint can
    no-op against."""

    async def _mk(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel
        await fake_telegram_bot.create_forum_topic(chat_id=-1001, name="🟢 t")
        rec = engagement_fixture.active_record
        rec.topic_id = 1001
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        return ch, rec

    async def test_sentinel_is_persisted_before_the_wire_edit(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        ch, rec = await self._mk(fake_telegram_bot, engagement_fixture)
        await engagement_fixture.registry.set_channel_state(
            rec.id, current_state_emoji="🟢")
        seen_at_wire = []
        real_edit = fake_telegram_bot.edit_forum_topic

        async def spying_edit(**kw):
            seen_at_wire.append(rec.current_state_emoji)
            return await real_edit(**kw)

        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", spying_edit)
        await ch.update_topic_state(engagement_id=rec.id, new_state="awaiting")
        assert seen_at_wire == [""]          # uncertain across the wire await
        assert rec.current_state_emoji == "🟡"  # settled after success

    async def test_cancel_in_wire_window_self_heals_on_next_paint(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        """THE #529 repro: cancel lands in the wire await (title may have
        landed); persisted must be the sentinel, and the next paint — even one
        requesting the PRE-CANCEL persisted state — must go through."""
        ch, rec = await self._mk(fake_telegram_bot, engagement_fixture)
        await engagement_fixture.registry.set_channel_state(
            rec.id, current_state_emoji="🟢")
        real_edit = fake_telegram_bot.edit_forum_topic
        calls = []

        async def cancelled_edit(**kw):
            calls.append(kw)
            await real_edit(**kw)            # the wire ACCEPTED the title...
            raise asyncio.CancelledError     # ...then the task was cancelled

        monkeypatch.setattr(
            fake_telegram_bot, "edit_forum_topic", cancelled_edit)
        with pytest.raises(asyncio.CancelledError):
            await ch.update_topic_state(
                engagement_id=rec.id, new_state="awaiting")
        assert rec.current_state_emoji == ""     # uncertain, never stale-🟢

        # Next serialized paint requests 'active' — the state whose emoji was
        # persisted BEFORE the cancel. Pre-fix the guard saw 🟢==🟢 and
        # returned; the topic stayed amber forever.
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", real_edit)
        await ch.update_topic_state(engagement_id=rec.id, new_state="active")
        sg = fake_telegram_bot._supergroups[-1001]
        assert sg.topics[1001].name.startswith("🟢")
        assert rec.current_state_emoji == "🟢"

    async def test_not_modified_bad_request_converges_the_sentinel(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        """A landed-but-unpersisted edit leaves the wire ALREADY at the target
        title; Telegram then rejects the identical re-edit with 'message is
        not modified'. That must count as wire success so the real emoji
        persists — otherwise the sentinel loops forever."""
        ch, rec = await self._mk(fake_telegram_bot, engagement_fixture)
        await engagement_fixture.registry.set_channel_state(
            rec.id, current_state_emoji="")

        async def not_modified_edit(**kw):
            raise Exception("Bad Request: message is not modified")

        monkeypatch.setattr(
            fake_telegram_bot, "edit_forum_topic", not_modified_edit)
        await ch.update_topic_state(engagement_id=rec.id, new_state="awaiting")
        assert rec.current_state_emoji == "🟡"

    async def test_wire_failure_keeps_sentinel_for_retry(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        ch, rec = await self._mk(fake_telegram_bot, engagement_fixture)
        await engagement_fixture.registry.set_channel_state(
            rec.id, current_state_emoji="🟢")

        async def failing_edit(**kw):
            raise Exception("Bad Gateway")

        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", failing_edit)
        await ch.update_topic_state(engagement_id=rec.id, new_state="awaiting")
        # Wire failed AFTER the sentinel persisted: uncertain is the truth
        # (self-heals on the next paint); pre-fix this path kept 🟢, which was
        # also fine — the sentinel must simply never be the REAL old emoji.
        assert rec.current_state_emoji == ""

    async def test_sentinel_persist_failure_aborts_before_the_wire(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        ch, rec = await self._mk(fake_telegram_bot, engagement_fixture)
        reg = engagement_fixture.registry
        await reg.set_channel_state(rec.id, current_state_emoji="🟢")
        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        def boom(snapshot):
            raise OSError("disk full")

        monkeypatch.setattr(reg, "_write_tombstone", boom)
        await ch.update_topic_state(engagement_id=rec.id, new_state="awaiting")
        ef.assert_not_awaited()                  # never touch the wire
        assert rec.current_state_emoji == "🟢"   # rolled back, not ""
