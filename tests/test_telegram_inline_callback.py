"""Tests for TelegramChannel inline-callback dispatch.

v0.75.0 (W5/Sol B3,B4): the callback ``_on_inline_callback`` dispatches onto
``verdict_broker.BROKER`` with a fail-closed contract — parse
``v1|ns|rid|idx``, route each DM-scoped namespace (``resident_ask``,
``proposal``, ``run_stop``) to its single-owner branch, fail closed on a
missing/anonymous/wrong actor, claim/commit exactly once, and EXACTLY ONE
``await cq.answer(toast)`` per path. The callback itself never edits the
keyboard — that's the broker finish-hook's job.
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
from output_boundary_testing import admitted

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


class TestFailClosedActorBinding:
    async def test_wrong_user_refused_without_claiming(
        self, fake_telegram_bot, _fresh_broker,
    ):
        """Pins INV-TG-002. The actor is checked twice — in _on_resident_callback
        and in the broker's claim (pinned on its own by
        tests/test_verdict_broker.py::test_claim_forbidden_on_actor_mismatch).
        Red case demonstrated: dropping both checks lets the wrong user claim and
        fails this test; dropping either one alone does not."""
        ch = _mk_dm_channel(fake_telegram_bot)
        _seed_resident(_fresh_broker, chat_id=500, rid="rid-wu",
                       operator_id=999)

        update = _mk_dm_update(
            data="v1|resident_ask|rid-wu|0", chat_id=500,
            user_id=42,  # not the bound operator
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("not for you")
        # No claim: request still live/unresolved.
        assert _fresh_broker.pending(namespace="resident_ask", scope="dm:500") == [
            "rid-wu",
        ]

    async def test_missing_operator_id_fails_closed(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        _seed_resident(_fresh_broker, chat_id=500, rid="rid-noop",
                       operator_id=None)

        update = _mk_dm_update(
            data="v1|resident_ask|rid-noop|0", chat_id=500, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("not for you")
        assert _fresh_broker.pending(namespace="resident_ask", scope="dm:500") == [
            "rid-noop",
        ]

    async def test_from_user_none_fails_closed(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        _seed_resident(_fresh_broker, chat_id=500, rid="rid-anon",
                       operator_id=999)

        update = _mk_dm_update(
            data="v1|resident_ask|rid-anon|0", chat_id=500, user_id=None,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("not for you")
        assert _fresh_broker.pending(namespace="resident_ask", scope="dm:500") == [
            "rid-anon",
        ]


class TestMalformedCallbackData:
    async def test_malformed_shapes_all_expire(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        for data in (
            "not-perm:x:y",
            "perm:allow:rid",
            "v1|unknown_ns|rid|0",
            "v1|permission|rid|0",
            "v1|engagement_ask|rid|0",
            "v1|resident_ask|rid|notanint",
            "v1|resident_ask|rid",
            "v1|resident_ask||0",
            "x" * 100,
        ):
            update = _mk_dm_update(data=data, chat_id=500)
            await ch._on_inline_callback(update, context=None)
            update.callback_query.answer.assert_awaited_once_with("expired")


class TestClaimCommitAndDuplicates:
    async def test_answer_raising_does_not_crash_handler(
        self, fake_telegram_bot, _fresh_broker,
    ):
        ch = _mk_dm_channel(fake_telegram_bot)
        req = _seed_resident(_fresh_broker, chat_id=500, rid="rid-boom")

        update = _mk_dm_update(
            data="v1|resident_ask|rid-boom|0", chat_id=500, user_id=999,
            answer_side_effect=RuntimeError("telegram transport down"),
        )
        await ch._on_inline_callback(update, context=None)  # must not raise
        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["outcome"] == "answered"


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

    async def test_plugin_question_scope_also_resolves(
        self, fake_telegram_bot, _fresh_broker,
    ):
        """Browser executor §5.3/§5.5: ask:<chat> — a plugin's operator
        question (``operator_calls.ask_operator``) — resolves through the
        same single-owner handler, operator-only."""
        ch = _mk_dm_channel(fake_telegram_bot)
        req = _seed_resident(
            _fresh_broker, kind="ask", chat_id=710, rid="rid-ask",
            options=("Allow", "Not now"),
        )
        stranger = _mk_dm_update(
            data="v1|resident_ask|rid-ask|0", chat_id=710, user_id=12345,
        )
        await ch._on_inline_callback(stranger, context=None)
        stranger.callback_query.answer.assert_awaited_once_with("not for you")
        update = _mk_dm_update(
            data="v1|resident_ask|rid-ask|0", chat_id=710, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["option_index"] == 0

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
            chat_id=500, request_id="f" * 32, text=admitted("Q?"), options=["Yes", "No"],
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
            chat_id=500, request_id="r", text=admitted("Q?"), options=["Yes"],
        )
        assert mid is None

    async def test_edit_dm_message_success(self, fake_telegram_bot):
        ch = _mk_dm_channel(fake_telegram_bot)
        fake_telegram_bot.edit_message_text = AsyncMock()
        assert await ch.edit_dm_message(500, 42, "new") is True
        # #569: the settle edit carries an EXPLICIT EMPTY markup so the
        # question's buttons go with its text (INV-TG-007, pinned in
        # tests/test_telegram_dm_settle.py).
        _, kwargs = fake_telegram_bot.edit_message_text.call_args
        assert kwargs["chat_id"] == 500
        assert kwargs["message_id"] == 42
        assert kwargs["text"] == "new"
        assert list(kwargs["reply_markup"].inline_keyboard) == []
        assert fake_telegram_bot.edit_message_text.await_count == 1

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


# ---------------------------------------------------------------------------
# v0.102.0 (#217): deliver_system_turn — the seam a background callback uses
# to continue a job after the operator taps (an Approve, an answer). It hands
# the message to the job exactly as the operator's own reply is
# handed: journaled, admitted and queued through the driver, which resumes a
# suspended session itself — one resume owner, never the channel.
# ---------------------------------------------------------------------------


class TestDeliverSystemTurn:
    """#217: deliver_system_turn and the operator's reply reach a job
    through the same driver entry; a suspended job is resumed once by the
    driver, however its inputs race."""

    def _mk(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel
        from drivers.in_casa_driver import InCasaDriver
        from claude_agent_sdk import ResultMessage
        try:
            from tests.turn_scripted_session import (
                TurnScriptedSession, attach_session)
        except ImportError:  # pragma: no cover — direct-path collection
            from turn_scripted_session import TurnScriptedSession, attach_session

        reg = engagement_fixture.registry
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = reg
        drv = InCasaDriver(
            record_lookup=reg.get, begin_turn_delivery=reg.begin_turn_delivery,
            lifecycle_lock=ch.engagement_lifecycle_lock)
        ch._engagement_driver = drv
        written: list = []
        resumes: list = []

        class _Client(TurnScriptedSession):
            async def record_query(self, text):
                written.append(text)

            async def receive_response(self):
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="sess-1")

        async def resume(rec, session_id):
            resumes.append((rec.id, session_id))
            await asyncio.sleep(0.01)        # a resume takes a while
            attach_session(drv, rec, _Client())

        drv.resume = resume
        return ch, drv, written, resumes

    async def _settled(self, drv, rec):
        try:
            from tests.test_job_session_driver import until
        except ImportError:  # pragma: no cover — direct-path collection
            from test_job_session_driver import until
        await until(lambda: drv.inbound_unread_depth(rec.id) == 0
                    and not drv.turn_open(rec.id))

    async def test_resumes_a_suspended_engagement_before_delivering(
        self, fake_telegram_bot, engagement_fixture,
    ):
        rec = engagement_fixture.active_record  # driver="in_casa"
        rec.sdk_session_id = "sess-1"
        ch, drv, written, resumes = self._mk(fake_telegram_bot, engagement_fixture)

        assert await ch.deliver_system_turn(rec, "proceed with the recipe now")
        await self._settled(drv, rec)

        assert resumes == [(rec.id, "sess-1")]               # resumed once
        assert written == ["proceed with the recipe now"]    # then delivered
        await drv.cancel(rec)

    async def test_terminal_record_skips_resume_and_delivery(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """A delayed Approve landing on a completed/cancelled/error engagement
        must NOT resume or deliver: the driver's admission refuses it."""
        import time
        rec = engagement_fixture.active_record
        rec.sdk_session_id = "sess-1"
        ch, drv, written, resumes = self._mk(fake_telegram_bot, engagement_fixture)
        await engagement_fixture.registry.mark_completed(rec.id, time.time())

        assert await ch.deliver_system_turn(rec, "too late") is False
        await asyncio.sleep(0.02)

        assert resumes == []             # never resumed a terminal record
        assert written == []             # never delivered
        assert drv.inbound_unread_depth(rec.id) == 0

    async def test_system_and_manual_turn_serialize_to_a_single_resume(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """A concurrent operator reply (deliver_operator_reply) and a system
        turn (deliver_system_turn) on the SAME suspended engagement resume it
        EXACTLY ONCE — the job's delivery worker is its one resume owner — and
        both messages are written into that one session."""
        rec = engagement_fixture.active_record
        rec.sdk_session_id = "sess-1"
        ch, drv, written, resumes = self._mk(fake_telegram_bot, engagement_fixture)

        await asyncio.gather(
            ch.deliver_operator_reply(rec, "operator nudge", chat_id=100,
                                      message_id=31),
            ch.deliver_system_turn(rec, "system resume"),
        )
        await self._settled(drv, rec)

        assert len(resumes) == 1                 # serialized — one resume only
        assert drv.is_alive(rec)                 # exactly one live session
        assert sorted(written) == ["operator nudge", "system resume"]
        await drv.cancel(rec)
