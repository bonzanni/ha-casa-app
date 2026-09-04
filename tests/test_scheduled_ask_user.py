"""Scheduled `ask_user` — a resident's own trigger turn asks the operator (#573).

Six things this covers, one per requirement the issue named:

1. ADMISSION — the scheduled arm is admitted by the #485 marker helper alone,
   and every non-schedule that superficially resembles one is refused.
2. LANE — a machine-timed question never displaces a live human one, across
   BOTH halves of the operator's attention lane (`dm:` and `authz:`).
3. DURABILITY — the record's state machine, and what each state means at boot.
4. TERMINAL OUTCOMES — every one of them reaches the scheduled session, and
   the shutdown cancel deliberately reaches nothing.
5. ATTRIBUTION — the continuation is machine-authored; the tap is content.
6. LIFECYCLE — a removed or rewritten trigger revokes its pending question,
   and a turn still in flight under the old trigger set cannot raise a new one.

Plus the one-writer-per-scheduled-session gate that all of it rests on.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import scheduled_asks
import verdict_broker
from broker_helpers import deliver, wait_until
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio

OPERATOR = 4242
LABEL = "cron-invoices"


# ---------------------------------------------------------------------------
# fixtures / doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduled_asks, "_ROLE_EPOCHS", {})
    monkeypatch.setattr(scheduled_asks, "_TRIGGER_EPOCHS", {})
    # Process-local boot-window state: a fresh process has neither, and the
    # module-level globals would otherwise leak between tests.
    monkeypatch.setattr(scheduled_asks, "_BOOT_REVOCATIONS", [])
    monkeypatch.setattr(scheduled_asks, "_BOOT_RECONCILED", False)
    store = scheduled_asks.ScheduledAskStore(str(tmp_path / "scheduled_asks.json"))
    monkeypatch.setattr(scheduled_asks, "STORE", store)
    return store


class _FakeChannel:
    """Telegram-channel double: records posts, edits and BOTH dispatchers."""

    def __init__(self, *, post_result=77, operator=OPERATOR):
        self.posts: list[tuple] = []
        self.edits: list[tuple] = []
        self.scheduled_dispatches: list[dict] = []
        self.button_dispatches: list[dict] = []
        self.calls: list[str] = []
        self._post_result = post_result
        self._operator = operator
        self.dispatch_result = True

    def operator_user_id(self):
        return self._operator

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        self.calls.append("post")
        return self._post_result

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        self.calls.append("edit")
        return True

    async def _dispatch_scheduled_continuation(self, *, session_scope,
                                               target_role, request_id, text,
                                               epoch=None):
        self.scheduled_dispatches.append({
            "session_scope": session_scope, "target_role": target_role,
            "request_id": request_id, "text": text, "epoch": epoch,
        })
        self.calls.append("dispatch")
        return self.dispatch_result

    async def _dispatch_button_continuation(self, *, chat_id, user_id,
                                            target_role, request_id, text):
        self.button_dispatches.append({"chat_id": chat_id, "text": text})
        return True


def _mk_cm(channel):
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)
    return cm


def _scheduled_origin(**overrides) -> dict:
    origin = {
        "role": "assistant",
        "execution_role": "assistant",
        "channel": "telegram",
        "chat_id": LABEL,          # a session LABEL, not a chat id
        "user_id": None,
        "message_type": "scheduled",
        "source": "scheduler",
        "_scheduled_delivery": True,
        "_scheduled_epoch": "0:0",
    }
    origin.update(overrides)
    return origin


def _payload(res):
    return json.loads(res["content"][0]["text"])


async def _ask(monkeypatch, *, channel=None, origin=None, args=None):
    import agent as agent_mod
    import tools as tools_mod

    channel = channel or _FakeChannel()
    tools_mod.init_tools(
        channel_manager=_mk_cm(channel), bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )
    tok = agent_mod.origin_var.set(origin or _scheduled_origin())
    try:
        res = await tools_mod.ask_user.handler(
            args or {"question": "Send the invoice?",
                     "options": ["Confirm", "Wrong", "Later"]},
        )
    finally:
        agent_mod.origin_var.reset(tok)
    return _payload(res), channel


def _record(store):
    recs = store.all()
    assert len(recs) == 1, recs
    return recs[0]


# ---------------------------------------------------------------------------
# 1. admission
# ---------------------------------------------------------------------------


class TestAdmission:
    async def test_scheduled_turn_gets_a_keyboard_in_the_operator_dm(
        self, monkeypatch, _fresh_broker,
    ):
        payload, channel = await _ask(monkeypatch)
        assert payload["status"] == "awaiting_user"
        assert payload["delivered_to"] == "operator_dm"
        # Delivered to the operator's DM, NOT to the session label.
        assert channel.posts[0][0] == OPERATOR
        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") == [
            payload["request_id"]]

    @pytest.mark.parametrize("origin_overrides,reason", [
        ({"_scheduled_delivery": None}, "no Casa-stamped marker"),
        ({"channel": "webhook"}, "not the telegram channel"),
        ({"execution_role": "researcher"}, "a delegated specialist turn"),
    ])
    async def test_refuses_everything_that_is_not_a_casa_schedule(
        self, monkeypatch, _fresh_broker, origin_overrides, reason,
    ):
        origin = _scheduled_origin(**origin_overrides)
        origin = {k: v for k, v in origin.items() if v is not None}
        payload, channel = await _ask(monkeypatch, origin=origin)
        assert payload["status"] == "error", reason
        assert payload["kind"] == "unsupported_origin"
        assert channel.posts == []

    async def test_refused_when_no_operator_is_configured(self, monkeypatch):
        # identity v0.136: with no operator configured, NOBODY is the operator.
        payload, channel = await _ask(
            monkeypatch, channel=_FakeChannel(operator=0))
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"
        assert channel.posts == []

    async def test_engagement_bound_turn_is_refused(self, monkeypatch):
        import tools as tools_mod

        tok = tools_mod.engagement_var.set(MagicMock(status="active"))
        try:
            payload, channel = await _ask(monkeypatch)
        finally:
            tools_mod.engagement_var.reset(tok)
        # Refused — an engagement-bound turn never reaches the scheduled arm
        # (`_scheduled_operator_target` fails closed on a bound engagement, and
        # the engagement guards ahead of it refuse it earlier still).
        assert payload["status"] == "error"
        assert channel.posts == []

    async def test_ordinary_dm_ask_is_untouched_by_the_scheduled_arm(
        self, monkeypatch, _fresh_store,
    ):
        payload, channel = await _ask(monkeypatch, origin={
            "role": "assistant", "execution_role": "assistant",
            "channel": "telegram", "chat_id": "500", "user_id": 500,
            "message_type": "channel_in", "source": "telegram",
        })
        assert payload["status"] == "awaiting_user"
        assert "delivered_to" not in payload
        # No durable record: only scheduled asks are recorded.
        assert _fresh_store.all() == []


# ---------------------------------------------------------------------------
# 2. lane admission
# ---------------------------------------------------------------------------


class TestLane:
    async def test_refuses_when_a_plain_dm_ask_is_live(
        self, monkeypatch, _fresh_broker,
    ):
        _fresh_broker.register(
            namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id="human", timeout_s=300, detached=True,
            meta={"operator_id": OPERATOR},
        )
        payload, channel = await _ask(monkeypatch)
        assert payload["kind"] == "operator_busy"
        assert channel.posts == []
        # The human question is STILL live — never superseded.
        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") == ["human"]

    async def test_refuses_when_an_authz_challenge_is_live(
        self, monkeypatch, _fresh_broker,
    ):
        _fresh_broker.register(
            namespace="resident_ask", scope=f"authz:{OPERATOR}",
            request_id="challenge", timeout_s=300, detached=True,
            meta={"operator_id": OPERATOR},
        )
        payload, channel = await _ask(monkeypatch)
        assert payload["kind"] == "operator_busy"
        assert channel.posts == []

    async def test_an_authz_challenge_cancels_a_live_scheduled_ask(
        self, monkeypatch, _fresh_broker, _fresh_store,
    ):
        payload, channel = await _ask(monkeypatch)
        rid = payload["request_id"]

        assert scheduled_asks.cancel_for_chat(OPERATOR, "operator_challenge") == 1
        await _fresh_broker.drain_hooks()
        await wait_until(lambda: channel.scheduled_dispatches)

        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") == []
        assert "operator_challenge" in channel.scheduled_dispatches[0]["text"]
        assert channel.scheduled_dispatches[0]["request_id"] == rid
        assert _fresh_store.all() == []

    async def test_cancel_for_chat_leaves_an_operators_own_ask_alone(
        self, _fresh_broker,
    ):
        _fresh_broker.register(
            namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id="human", timeout_s=300, detached=True,
            meta={"operator_id": OPERATOR},
        )
        assert scheduled_asks.cancel_for_chat(OPERATOR, "operator_challenge") == 0
        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") == ["human"]


# ---------------------------------------------------------------------------
# 3. the durable record
# ---------------------------------------------------------------------------


class TestDurableRecord:
    async def test_record_is_live_with_the_message_id_after_a_good_post(
        self, monkeypatch, _fresh_store,
    ):
        payload, _ch = await _ask(monkeypatch)
        rec = _record(_fresh_store)
        assert rec["state"] == scheduled_asks.STATE_LIVE
        assert rec["message_id"] == 77
        assert rec["rid"] == payload["request_id"]
        assert rec["session_scope"] == LABEL
        assert rec["chat_id"] == OPERATOR and rec["operator_id"] == OPERATOR
        # and it is on DISK, not only in memory
        on_disk = json.loads(open(_fresh_store._path).read())
        assert on_disk[rec["rid"]]["state"] == scheduled_asks.STATE_LIVE

    async def test_posting_record_is_written_before_the_keyboard(
        self, monkeypatch, _fresh_store,
    ):
        seen: list[str] = []

        class _WatchingChannel(_FakeChannel):
            async def post_dm_keyboard(self, **kw):
                seen.append(scheduled_asks.STORE.all()[0]["state"])
                return await super().post_dm_keyboard(**kw)

        await _ask(monkeypatch, channel=_WatchingChannel())
        assert seen == [scheduled_asks.STATE_POSTING]

    async def test_failed_post_leaves_no_record(self, monkeypatch, _fresh_store):
        payload, channel = await _ask(
            monkeypatch, channel=_FakeChannel(post_result=None))
        assert payload["kind"] == "delivery_failed"
        assert _fresh_store.all() == []

    async def test_state_transitions_are_compare_and_set(self, _fresh_store):
        await _fresh_store.put({"rid": "r", "state": scheduled_asks.STATE_POSTING})
        assert await _fresh_store.set_state("r", scheduled_asks.STATE_SETTLING)
        # A late `live` write from ask_user cannot resurrect a settled record.
        assert not await _fresh_store.set_state(
            "r", scheduled_asks.STATE_LIVE, message_id=5)
        assert _fresh_store.get("r")["state"] == scheduled_asks.STATE_SETTLING
        await _fresh_store.drop("r")
        assert not await _fresh_store.set_state("r", scheduled_asks.STATE_LIVE)

    async def test_corrupt_file_is_quarantined_and_the_store_starts_empty(
        self, tmp_path,
    ):
        path = tmp_path / "scheduled_asks.json"
        path.write_text("{not json")
        store = scheduled_asks.ScheduledAskStore(str(path))
        assert store.all() == []
        assert (tmp_path / "scheduled_asks.json.corrupt").exists()


# ---------------------------------------------------------------------------
# 4. terminal outcomes + 5. attribution
# ---------------------------------------------------------------------------


class TestTerminalOutcomes:
    async def test_answer_edits_the_keyboard_and_reaches_the_session(
        self, monkeypatch, _fresh_broker, _fresh_store,
    ):
        payload, channel = await _ask(monkeypatch)
        rid = payload["request_id"]

        assert deliver(
            _fresh_broker, namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id=rid, option_index=0, actor_id=OPERATOR,
        ) == "delivered"
        await _fresh_broker.drain_hooks()
        await wait_until(lambda: channel.scheduled_dispatches)

        assert "Answered: Confirm" in channel.edits[-1][2]
        dispatch = channel.scheduled_dispatches[0]
        assert dispatch["session_scope"] == LABEL   # the SCHEDULED session
        assert dispatch["target_role"] == "assistant"
        assert "Confirm" in dispatch["text"]
        assert _fresh_store.all() == []

    async def test_settling_is_persisted_before_the_edit(
        self, monkeypatch, _fresh_broker,
    ):
        states: list[str] = []

        class _WatchingChannel(_FakeChannel):
            async def edit_dm_message(self, chat_id, message_id, text):
                recs = scheduled_asks.STORE.all()
                states.append(recs[0]["state"] if recs else "gone")
                return await super().edit_dm_message(chat_id, message_id, text)

        payload, channel = await _ask(monkeypatch, channel=_WatchingChannel())
        _fresh_broker.cancel(
            namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id=payload["request_id"], reason="typed_answer",
        )
        await _fresh_broker.drain_hooks()
        await wait_until(lambda: channel.scheduled_dispatches)
        assert states == [scheduled_asks.STATE_SETTLING]

    @pytest.mark.parametrize("reason", [
        "superseded", "typed_answer", "new_session", "trigger_changed",
    ])
    async def test_every_cancellation_reaches_the_session(
        self, monkeypatch, _fresh_broker, _fresh_store, reason,
    ):
        payload, channel = await _ask(monkeypatch)
        _fresh_broker.cancel(
            namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id=payload["request_id"], reason=reason,
        )
        await _fresh_broker.drain_hooks()
        await wait_until(lambda: channel.scheduled_dispatches)
        assert reason in channel.scheduled_dispatches[0]["text"]
        assert "expired" in channel.edits[-1][2]
        assert _fresh_store.all() == []

    async def test_timeout_reaches_the_session(
        self, monkeypatch, _fresh_broker, _fresh_store,
    ):
        payload, channel = await _ask(
            monkeypatch,
            args={"question": "Send it?", "options": ["Yes", "No"],
                  "timeout_s": 30},
        )
        # Fire the broker's own timeout path rather than waiting 30s.
        _fresh_broker._on_timeout(
            ("resident_ask", f"dm:{OPERATOR}", payload["request_id"]))
        await _fresh_broker.drain_hooks()
        await wait_until(lambda: channel.scheduled_dispatches)
        assert "expired without an answer" in channel.scheduled_dispatches[0]["text"]
        assert _fresh_store.all() == []

    async def test_shutdown_settles_nothing_and_keeps_the_record(
        self, monkeypatch, _fresh_broker, _fresh_store,
    ):
        payload, channel = await _ask(monkeypatch)
        _fresh_broker.cancel_all(reason="casa_shutdown")
        await _fresh_broker.drain_hooks()

        assert channel.scheduled_dispatches == []
        assert channel.edits == []
        rec = _record(_fresh_store)
        assert rec["state"] == scheduled_asks.STATE_LIVE
        assert rec["rid"] == payload["request_id"]

    async def test_shutdown_during_live_state_save_returns_awaiting_and_restores(
        self, monkeypatch, _fresh_broker, _fresh_store, tmp_path,
    ):
        """#682's liveness sample must NOT be extended to this arm.

        GREEN mutation pin, not a red case — the behaviour already holds. The
        window: the durable `set_state(live)` mutates the record and then
        awaits its save while holding the store lock. `casa_shutdown` removes
        the request from the broker's live map in that window, and its finish
        hook deliberately edits and dispatches NOTHING so the boot reconcile
        can restore the question. So at return time the broker says "absent"
        while the question is genuinely alive — and `awaiting_user` is TRUE.

        `test_shutdown_settles_nothing_and_keeps_the_record` cannot see this:
        it cancels only after `_ask` has already returned, so a mutant that
        samples the broker before this arm's return stays green there.
        """
        release = asyncio.Event()
        started = asyncio.Event()

        class _BlockingSaveStore(scheduled_asks.ScheduledAskStore):
            def __init__(self, path):
                super().__init__(path)
                self.saves = 0

            async def _save_locked(self):
                self.saves += 1
                if self.saves == 2:      # the `posting` -> `live` save
                    started.set()
                    await release.wait()
                return await super()._save_locked()

        store = _BlockingSaveStore(str(tmp_path / "blocking.json"))
        monkeypatch.setattr(scheduled_asks, "STORE", store)

        channel = _FakeChannel()
        task = asyncio.create_task(_ask(monkeypatch, channel=channel))
        await asyncio.wait_for(started.wait(), 5.0)

        assert len(channel.posts) == 1
        live = _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}")
        assert len(live) == 1
        original_rid = live[0]
        assert _fresh_broker.cancel_all(reason="casa_shutdown") == 1
        await _fresh_broker.drain_hooks()
        assert len(channel.edits) == 0
        assert len(channel.scheduled_dispatches) == 0

        release.set()
        payload, _ch = await asyncio.wait_for(task, 5.0)

        # The broker says absent; the durable record says restorable.
        assert len(_fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}")) == 0
        assert sum(r.get("state") == scheduled_asks.STATE_LIVE
                   for r in store.all()) == 1
        assert sum(p.get("status") == "awaiting_user" for p in [payload]) == 1
        assert sum(p.get("status") == "settled" for p in [payload]) == 0
        assert sum(p.get("delivered_to") == "operator_dm"
                   for p in [payload]) == 1
        assert sum(p.get("request_id") == original_rid for p in [payload]) == 1

        # …and the next boot proves the question really was still alive.
        fresh = VerdictBroker()
        monkeypatch.setattr(verdict_broker, "BROKER", fresh)
        monkeypatch.setattr(scheduled_asks, "_BOOT_RECONCILED", False)
        boot_channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(boot_channel, now=0.0)
        assert counts["restored"] == 1
        restored = fresh.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}")
        assert len(restored) == 1
        assert sum(r == original_rid for r in restored) == 1

    async def test_a_failed_dispatch_still_drops_the_record(
        self, monkeypatch, _fresh_broker, _fresh_store,
    ):
        channel = _FakeChannel()
        channel.dispatch_result = False
        payload, _ch = await _ask(monkeypatch, channel=channel)
        _fresh_broker.cancel(
            namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id=payload["request_id"], reason="superseded",
        )
        await _fresh_broker.drain_hooks()
        await wait_until(lambda: channel.scheduled_dispatches)
        # A retained record would be re-registered at the next boot and could
        # dispatch the same outcome a second time.
        assert _fresh_store.all() == []


class TestAttribution:
    async def test_the_continuation_is_machine_authored(self):
        """The real dispatcher, not the double: a SCHEDULED turn into the
        label's session with NO trusted user origin — the operator's tap is
        reported in the content, never as the speaker."""
        import channels.telegram as tg

        sent = []

        class _Bus:
            async def send_checked(self, msg):
                sent.append(msg)
                return "accepted"

        channel = tg.TelegramChannel.__new__(tg.TelegramChannel)
        channel._bus = _Bus()
        ok = await tg.TelegramChannel._dispatch_scheduled_continuation(
            channel, session_scope=LABEL, target_role="assistant",
            request_id="rid1", text="[answer to rid1] the operator tapped: Confirm",
            epoch=3,
        )
        assert ok is True
        msg = sent[0]
        assert msg.type.value == "scheduled"
        assert msg.context["chat_id"] == LABEL
        assert msg.context["_scheduled_delivery"] is True
        assert msg.context["_scheduled_epoch"] == 3
        assert msg.context["button_answer"] == "rid1"
        assert msg.trusted_user_origin is None
        assert "_operator_turn" not in msg.context
        assert "_origin_clearance" not in msg.context


# ---------------------------------------------------------------------------
# 6. trigger lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_stale_epoch_refuses_the_question(self, monkeypatch):
        scheduled_asks.bump_role_epoch("assistant")
        payload, channel = await _ask(
            monkeypatch, origin=_scheduled_origin(_scheduled_epoch="0:0"))
        assert payload["kind"] == "trigger_changed"
        assert channel.posts == []

    async def test_cancelling_one_trigger_does_not_silence_its_siblings(
        self, monkeypatch,
    ):
        """`revoke_trigger` selects one trigger. An in-flight turn of a
        DIFFERENT trigger of the same role is not stale — refusing it was a
        role-wide epoch leaking into a per-trigger decision."""
        stamped = scheduled_asks.epoch_for("assistant", "cron-invoices")
        scheduled_asks.revoke_trigger(
            "assistant", "reminder-abcd", "trigger_cancelled")

        payload, _channel = await _ask(monkeypatch, origin=_scheduled_origin(
            chat_id="cron-invoices", _scheduled_epoch=stamped))
        assert payload["status"] == "awaiting_user"

    async def test_cancelling_a_trigger_does_refuse_its_own_in_flight_turn(
        self, monkeypatch,
    ):
        stamped = scheduled_asks.epoch_for("assistant", "date-reminder-abcd")
        scheduled_asks.revoke_trigger(
            "assistant", "reminder-abcd", "trigger_cancelled")

        payload, channel = await _ask(monkeypatch, origin=_scheduled_origin(
            chat_id="date-reminder-abcd", _scheduled_epoch=stamped))
        assert payload["kind"] == "trigger_changed"
        assert channel.posts == []

    async def test_absent_epoch_is_tolerated(self, monkeypatch):
        origin = _scheduled_origin()
        origin.pop("_scheduled_epoch")
        payload, _channel = await _ask(monkeypatch, origin=origin)
        assert payload["status"] == "awaiting_user"

    async def test_revoke_role_settles_the_pending_question(
        self, monkeypatch, _fresh_broker, _fresh_store,
    ):
        payload, channel = await _ask(monkeypatch)
        assert scheduled_asks.revoke_role("assistant", "trigger_reloaded") == 1
        await _fresh_broker.drain_hooks()
        await wait_until(lambda: channel.scheduled_dispatches)
        assert "trigger_reloaded" in channel.scheduled_dispatches[0]["text"]
        assert _fresh_store.all() == []
        # and the epoch moved, so a turn still running cannot ask again
        assert scheduled_asks.epoch_for("assistant", LABEL) == "1:0"
        payload2, channel2 = await _ask(
            monkeypatch, origin=_scheduled_origin(_scheduled_epoch="0:0"))
        assert payload2["kind"] == "trigger_changed"

    async def test_revoke_role_ignores_another_roles_question(
        self, monkeypatch, _fresh_broker,
    ):
        await _ask(monkeypatch)
        assert scheduled_asks.revoke_role("butler", "trigger_reloaded") == 0
        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") != []

    async def test_revoke_trigger_is_scoped_to_one_trigger_name(
        self, monkeypatch, _fresh_broker,
    ):
        await _ask(monkeypatch)                       # session label cron-invoices
        assert scheduled_asks.revoke_trigger(
            "assistant", "something-else", "trigger_cancelled") == 0
        assert scheduled_asks.revoke_trigger(
            "assistant", "invoices", "trigger_cancelled") == 1

    @pytest.mark.parametrize("trigger_type", ["date", "cron", "interval"])
    async def test_cancelling_by_name_finds_the_question_whatever_the_type(
        self, monkeypatch, _fresh_broker, trigger_type,
    ):
        """A cancellation site knows the reminder's NAME, not its type — and a
        REPEATING reminder is derived into a `cron` trigger, not a `date` one
        (`reminders.derive_recurrence`). Rebuilding `date-<name>` at the call
        site left a cancelled repeating reminder's keyboard answerable."""
        name = "reminder-abcd"
        payload, channel = await _ask(monkeypatch, origin=_scheduled_origin(
            chat_id=f"{trigger_type}-{name}"))
        assert scheduled_asks.revoke_trigger(
            "assistant", name, "trigger_cancelled") == 1
        await _fresh_broker.drain_hooks()
        await wait_until(lambda: channel.scheduled_dispatches)
        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") == []
        assert channel.scheduled_dispatches[0]["request_id"] == payload["request_id"]


# ---------------------------------------------------------------------------
# boot reconciliation
# ---------------------------------------------------------------------------


class TestBootReconcile:
    def _rec(self, **overrides) -> dict:
        rec = {
            "rid": "rid-boot", "state": scheduled_asks.STATE_LIVE,
            "role": "assistant", "session_scope": LABEL,
            "scope": f"dm:{OPERATOR}", "chat_id": OPERATOR,
            "operator_id": OPERATOR, "message_id": 77,
            "options": ["Confirm", "Wrong"], "body": "Send the invoice?",
            "epoch": 0, "created_at": 0.0, "expires_at": 1_000.0,
        }
        rec.update(overrides)
        return rec

    async def test_live_record_is_restored_and_still_answerable(
        self, _fresh_broker, _fresh_store,
    ):
        await _fresh_store.put(self._rec())
        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts["restored"] == 1
        assert channel.edits == [] and channel.scheduled_dispatches == []

        # The keyboard on screen is honest again: a tap resolves normally.
        assert deliver(
            _fresh_broker, namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id="rid-boot", option_index=0, actor_id=OPERATOR,
        ) == "delivered"
        await _fresh_broker.drain_hooks()
        await wait_until(lambda: channel.scheduled_dispatches)
        assert "Confirm" in channel.scheduled_dispatches[0]["text"]
        assert channel.scheduled_dispatches[0]["epoch"] == 0
        assert _fresh_store.all() == []

    async def test_a_stranger_cannot_answer_a_restored_question(
        self, _fresh_broker, _fresh_store,
    ):
        await _fresh_store.put(self._rec())
        await scheduled_asks.reconcile_at_boot(_FakeChannel(), now=0.0)
        assert deliver(
            _fresh_broker, namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id="rid-boot", option_index=0, actor_id=OPERATOR + 1,
        ) == "forbidden"

    async def test_expired_record_is_settled(self, _fresh_store):
        await _fresh_store.put(self._rec(expires_at=10.0))
        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=99.0)
        assert counts["expired"] == 1
        assert "expired" in channel.edits[-1][2]
        assert "expired without an answer" in channel.scheduled_dispatches[0]["text"]
        assert _fresh_store.all() == []

    async def test_posting_record_tells_the_session_and_edits_nothing(
        self, _fresh_store,
    ):
        await _fresh_store.put(self._rec(
            state=scheduled_asks.STATE_POSTING, message_id=None))
        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts["unconfirmed"] == 1
        assert channel.edits == []
        assert "delivery_unconfirmed" in channel.scheduled_dispatches[0]["text"]
        assert _fresh_store.all() == []

    async def test_a_legacy_settling_record_makes_no_edit_and_no_dispatch(
        self, _fresh_store,
    ):
        """INV-JOB-013. A `settling` record written by a process that predates
        the persisted terminal text carries none, so there is nothing truthful
        to replay: it is dropped exactly as before, in silence.

        Inventing an "expired" body for it would overwrite a keyboard that may
        already read "Answered: Confirm" — telling the operator that a question
        they answered expired instead. The base's silence is at least not false.
        """
        await _fresh_store.put(self._rec(state=scheduled_asks.STATE_SETTLING))
        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts["settled_before_crash"] == 1
        assert channel.scheduled_dispatches == []   # at-most-once
        assert channel.edits == []                  # nothing truthful to say
        assert _fresh_store.all() == []

    async def test_a_settling_record_replays_its_persisted_edit_and_never_dispatches(
        self, _fresh_store,
    ):
        """INV-JOB-013, the fake-channel half of the crash-window replay.

        The real-channel half lives beside INV-TG-007 in
        `tests/test_telegram_dm_settle.py`; this one pins the reconciler's own
        counts.
        """
        await _fresh_store.put(self._rec(
            state=scheduled_asks.STATE_SETTLING,
            terminal_edit="Send the invoice?\n\nAnswered: Confirm"))
        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts["settled_before_crash"] == 1
        assert len(channel.edits) == 1
        assert sum(e[2] == "Send the invoice?\n\nAnswered: Confirm"
                   for e in channel.edits) == 1
        assert sum(e[1] == 77 for e in channel.edits) == 1
        assert channel.scheduled_dispatches == []   # at-most-once, still
        assert _fresh_store.all() == []

    async def test_a_settling_record_with_no_message_id_edits_nothing(
        self, _fresh_store,
    ):
        """The replay needs somewhere to land. A record whose keyboard id was
        never captured has nothing to edit, and must not become an error."""
        await _fresh_store.put(self._rec(
            state=scheduled_asks.STATE_SETTLING, message_id=None,
            terminal_edit="Send the invoice?\n\n(this question has expired)"))
        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts["settled_before_crash"] == 1
        assert channel.edits == []
        assert channel.scheduled_dispatches == []
        assert _fresh_store.all() == []

    async def test_a_replay_that_telegram_refuses_still_drops_the_record(
        self, _fresh_store,
    ):
        """The replay is BEST-EFFORT, and the record is dropped either way.

        `edit_dm_message` returns False for a transient rejection and for a
        message the operator deleted alike — it cannot tell them apart — so
        retaining the record to retry would retain it for every boot of a
        process whose message is gone for good, with no condition that ever
        retires it. The end state of a failed replay is exactly what the
        reconciler did for EVERY settling record before this rule: the keyboard
        is left as posted, nothing is dispatched, and the record goes. What the
        rule buys is the case where the edit lands, which is the common one.
        """
        class _RefusingChannel(_FakeChannel):
            async def edit_dm_message(self, chat_id, message_id, text):
                self.edits.append((chat_id, message_id, text))
                self.calls.append("edit")
                return False

        await _fresh_store.put(self._rec(
            state=scheduled_asks.STATE_SETTLING,
            terminal_edit="Send the invoice?\n\nAnswered: Confirm"))
        channel = _RefusingChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)

        assert counts["settled_before_crash"] == 1
        assert len(channel.edits) == 1          # attempted exactly once
        assert channel.scheduled_dispatches == []
        assert len(_fresh_store.all()) == 0     # not retained for a retry

    async def test_a_terminal_that_finds_no_settleable_record_says_so(
        self, _fresh_store, caplog,
    ):
        """The one place a terminal outcome used to disappear without a trace.

        `_settle` returns silently when the CAS fails — the record is gone, or
        another finisher owns it. That return is the tail of every boot race in
        this module, and an operator reading the log had nothing to find. It
        still edits nothing and dispatches nothing; it now says so.
        """
        import logging

        rec = self._rec(rid="rid-cas-miss",
                        state=scheduled_asks.STATE_SETTLING)
        await _fresh_store.put(rec)
        channel = _FakeChannel()
        with caplog.at_level(logging.WARNING, logger="scheduled_asks"):
            await scheduled_asks._settle(
                channel, rec, kind="cancelled", reason="trigger_removed",
                chosen=None, edit_text="whatever")

        assert sum(
            r.name == "scheduled_asks"
            and r.levelno == logging.WARNING
            and "rid-cas-miss" in r.getMessage()
            for r in caplog.records
        ) == 1
        assert len(channel.edits) == 0
        assert len(channel.scheduled_dispatches) == 0
        assert len(_fresh_store.all()) == 1

    async def test_a_revocation_during_the_boot_window_is_not_undone(
        self, _fresh_broker, _fresh_store,
    ):
        """The one state the broker cannot answer for the store: records exist
        on disk and `_live` is empty until this reconcile runs, so a revoke in
        that window cancels nothing. Restoring the question anyway would leave
        a cancelled trigger's keyboard answerable."""
        await _fresh_store.put(self._rec())
        # A reload / reminder-cancel lands before Telegram is ready.
        assert scheduled_asks.revoke_role("assistant", "trigger_reloaded") == 0
        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)

        assert counts.get("restored", 0) == 0
        assert counts["revoked_before_reconcile"] == 1
        assert "trigger_reloaded" in channel.scheduled_dispatches[0]["text"]
        assert _fresh_store.all() == []
        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") == []

    async def test_boot_revocation_uses_the_first_matching_callers_reason(
        self, _fresh_broker, _fresh_store,
    ):
        """INV-JOB-012. The marker carries the reason its caller passed, and
        when two markers match one record the FIRST one wins.

        First-match is not an implementation detail to be tidied later: had the
        record been live, the EARLIEST matching revocation would have cancelled
        it and every later one would have found nothing. Reproducing the live
        path's own answer is the whole point of the marker.
        """
        await _fresh_store.put(self._rec())
        # Both match this record; the chat marker is written first.
        results = [
            scheduled_asks.cancel_for_chat(OPERATOR, "new_session"),
            scheduled_asks.revoke_role("assistant", "trigger_reloaded"),
        ]
        assert sum(n == 0 for n in results) == 2
        assert len(scheduled_asks._BOOT_REVOCATIONS) == 2

        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)

        assert counts.get("revoked_before_reconcile", 0) == 1
        assert counts.get("restored", 0) == 0
        assert len(channel.edits) == 1
        assert len(channel.scheduled_dispatches) == 1
        assert sum("(new_session)" in d["text"]
                   for d in channel.scheduled_dispatches) == 1
        assert sum("trigger_reloaded" in d["text"]
                   for d in channel.scheduled_dispatches) == 0
        assert sum("trigger_changed" in d["text"]
                   for d in channel.scheduled_dispatches) == 0
        assert len(_fresh_store.all()) == 0
        assert len(_fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}")) == 0

    @pytest.mark.parametrize("revoke, reason", [
        (lambda: scheduled_asks.revoke_role("assistant", "trigger_reloaded"),
         "trigger_reloaded"),
        (lambda: scheduled_asks.revoke_role("assistant", "role_evicted"),
         "role_evicted"),
        (lambda: scheduled_asks.revoke_trigger(
            "assistant", "invoices", "trigger_removed"), "trigger_removed"),
        (lambda: scheduled_asks.revoke_trigger(
            "assistant", "invoices", "trigger_cancelled"), "trigger_cancelled"),
    ])
    async def test_every_live_reason_survives_the_boot_window(
        self, _fresh_broker, _fresh_store, revoke, reason,
    ):
        """INV-JOB-012, over all four reasons a shipped caller passes.

        `reload.py` revokes a reloaded role and an evicted one, the reminder
        sweep revokes a trigger whose entry is gone, and `cancel_reminder`
        revokes a cancelled one. Every one of them is discarded inside the boot
        window unless the marker carries it.
        """
        await _fresh_store.put(self._rec(session_scope="cron-invoices"))
        assert revoke() == 0

        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)

        assert counts.get("revoked_before_reconcile", 0) == 1
        assert counts.get("restored", 0) == 0
        assert len(channel.scheduled_dispatches) == 1
        assert sum(f"({reason})" in d["text"]
                   for d in channel.scheduled_dispatches) == 1
        assert sum("trigger_changed" in d["text"]
                   for d in channel.scheduled_dispatches) == 0

    async def test_an_untouched_role_is_still_restored(
        self, _fresh_broker, _fresh_store,
    ):
        """The guard above must not swallow the normal case: another role's
        revocation says nothing about this record."""
        await _fresh_store.put(self._rec())
        scheduled_asks.revoke_role("butler", "trigger_reloaded")
        counts = await scheduled_asks.reconcile_at_boot(_FakeChannel(), now=0.0)
        assert counts["restored"] == 1

    async def test_cancelling_one_trigger_leaves_the_roles_others_alone(
        self, _fresh_broker, _fresh_store,
    ):
        """`revoke_trigger` selects ONE trigger's questions; the boot-window
        guard has to select the same ones. Keying it on the role (whose epoch
        that call also bumps) discarded every other question the role was
        waiting on."""
        await _fresh_store.put(self._rec(
            rid="mine", session_scope="date-reminder-abcd"))
        await _fresh_store.put(self._rec(
            rid="sibling", session_scope="cron-invoices"))
        scheduled_asks.revoke_trigger(
            "assistant", "reminder-abcd", "trigger_cancelled")

        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts["revoked_before_reconcile"] == 1
        assert counts["restored"] == 1
        assert [d["request_id"] for d in channel.scheduled_dispatches] == ["mine"]
        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") == ["sibling"]

    async def test_a_pre_reconcile_challenge_cancellation_is_honoured(
        self, _fresh_broker, _fresh_store,
    ):
        """An authorization challenge raised before Telegram is ready cancels
        nothing (the broker is empty) — and if that challenge is itself gone by
        reconcile time, `require_idle` has nothing to refuse against either, so
        the machine question would be restored into a lane it had already
        yielded (INV-JOB-008)."""
        await _fresh_store.put(self._rec())
        assert scheduled_asks.cancel_for_chat(OPERATOR, "operator_challenge") == 0

        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts["revoked_before_reconcile"] == 1
        assert counts.get("restored", 0) == 0
        assert _fresh_store.all() == []

    async def test_markers_do_not_outlive_the_reconcile(
        self, _fresh_broker, _fresh_store,
    ):
        """After the pass every surviving record is in the broker, so the
        markers describe nothing invisible any more — a later boot-window
        marker must not settle a question asked afterwards."""
        scheduled_asks.revoke_role("assistant", "trigger_reloaded")
        await scheduled_asks.reconcile_at_boot(_FakeChannel(), now=0.0)

        # A revocation after the window records NOTHING — the caller's own
        # broker scan is authoritative from here on, and a marker that kept
        # accumulating would both leak and mis-settle a later record.
        scheduled_asks.revoke_role("assistant", "trigger_reloaded")
        scheduled_asks.cancel_for_chat(OPERATOR, "operator_challenge")
        assert scheduled_asks._BOOT_REVOCATIONS == []

        await _fresh_store.put(self._rec(rid="later"))
        counts = await scheduled_asks.reconcile_at_boot(_FakeChannel(), now=0.0)
        assert counts["restored"] == 1

    def test_the_store_exposes_no_query_surface(self):
        """A by-role/by-chat reader would be the invitation to answer a LIVE
        decision from the lagging store; the broker is the authority for that."""
        assert not hasattr(scheduled_asks.ScheduledAskStore, "for_role")
        assert not hasattr(scheduled_asks.ScheduledAskStore, "for_chat")

    async def test_boot_markers_are_bounded(self, _fresh_broker):
        """A deploy with no Telegram channel never runs the reconciler, so the
        retirement flag never flips and the cap is the only bound."""
        for i in range(scheduled_asks._BOOT_REVOCATIONS_MAX + 10):
            scheduled_asks.revoke_role(f"role-{i}", "trigger_reloaded")
        assert (len(scheduled_asks._BOOT_REVOCATIONS)
                == scheduled_asks._BOOT_REVOCATIONS_MAX)

    async def test_a_changed_operator_is_not_restored(self, _fresh_store):
        await _fresh_store.put(self._rec())
        channel = _FakeChannel(operator=OPERATOR + 1)
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts["operator_changed"] == 1
        assert "operator_changed" in channel.scheduled_dispatches[0]["text"]
        assert _fresh_store.all() == []

    async def test_no_operator_configured_is_not_restored(self, _fresh_store):
        await _fresh_store.put(self._rec())
        counts = await scheduled_asks.reconcile_at_boot(
            _FakeChannel(operator=None), now=0.0)
        assert counts["operator_changed"] == 1

    async def test_a_scheduled_ask_still_posting_keeps_the_lane(
        self, _fresh_broker, _fresh_store,
    ):
        """INV-JOB-014's boundary: the relaxation is for HUMAN-raised questions.

        Yielding the lane to an occupant whose keyboard has not landed is only
        safe because that occupant retires the restored question itself once it
        does — `authz_grants._drive` and `tools.ask_user` both displace on
        delivery. A machine-timed ask never displaces anything: the rule is
        one-way by design. Restoring over one would leave TWO machine questions
        live in the operator's single lane, each answerable, with nothing to
        retire either — which is a state neither the base nor this change
        allows. So it holds the lane from registration, as it always did.
        """
        await _fresh_store.put(self._rec())
        # Exactly what `_ask_user_scheduled` registers: the `scheduled` marker,
        # and no message id until its own post returns one.
        req, _created = _fresh_broker.register(
            namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id="fresh-scheduled", timeout_s=300, detached=True,
            meta={"operator_id": OPERATOR, "scheduled": True},
        )
        assert req is not None
        assert _fresh_broker.get_meta(
            namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id="fresh-scheduled").get("message_id") is None

        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)

        assert counts["operator_busy"] == 1
        assert counts.get("restored", 0) == 0
        assert sum("operator_busy" in d["text"]
                   for d in channel.scheduled_dispatches) == 1
        assert len(_fresh_store.all()) == 0
        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") == [
            "fresh-scheduled"]

    async def test_a_live_human_question_keeps_the_lane(
        self, _fresh_broker, _fresh_store,
    ):
        """INV-JOB-008. A human question the operator can SEE holds the lane,
        and the machine one yields to it — the direction is one-way by design.

        The fixture records a `message_id` because that is what makes this a
        human question the operator has: the reconciler judges the lane by what
        reached the screen, not by what registered. Its sibling below is the
        same fixture without one.
        """
        await _fresh_store.put(self._rec())
        _fresh_broker.register(
            namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id="human", timeout_s=300, detached=True,
            meta={"operator_id": OPERATOR, "message_id": 4242},
        )
        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts["operator_busy"] == 1
        assert "operator_busy" in channel.scheduled_dispatches[0]["text"]
        assert _fresh_store.all() == []
        assert _fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}") == ["human"]

    async def test_a_human_question_still_posting_does_not_keep_the_lane(
        self, _fresh_broker, _fresh_store,
    ):
        """#762 · INV-JOB-008, the same fixture minus the delivered keyboard.

        Registered is not delivered. Refusing the restore here is irreversible
        and the question is destroyed for a keyboard that may never arrive; the
        restore is not, because the human question's own path displaces it at
        delivery. Only the BOOT pass reads the lane this way — the live path's
        `require_idle` is unchanged, which `TestLane` pins.
        """
        await _fresh_store.put(self._rec())
        _fresh_broker.register(
            namespace="resident_ask", scope=f"dm:{OPERATOR}",
            request_id="human", timeout_s=300, detached=True,
            meta={"operator_id": OPERATOR},
        )
        channel = _FakeChannel()
        counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
        assert counts.get("operator_busy", 0) == 0
        assert counts["restored"] == 1
        assert channel.scheduled_dispatches == []
        assert channel.edits == []
        assert len(_fresh_store.all()) == 1
        assert sorted(_fresh_broker.pending(
            namespace="resident_ask", scope=f"dm:{OPERATOR}")) == [
            "human", "rid-boot"]


# ---------------------------------------------------------------------------
# one writer per scheduled session
# ---------------------------------------------------------------------------


class TestSessionWriteGate:
    # #573 classified WHICH turns take the gate (scheduled ones, plus the
    # delegation-completion REQUEST that keying on message type alone missed).
    # v0.208.0 (#579) deleted that classifier: the gate is unconditional,
    # because the pool entry lock it deferred to for ordinary turns serializes
    # only within ONE pool and a reload builds a second one over the same key.
    # The replacement binding — that every turn takes it, scheduled or not —
    # lives in tests/test_session_key_authority.py; what remains here is the
    # scheduled turns' own stake in the shared gate.

    async def test_gate_serialises_and_releases(self):
        import session_gate

        order: list[str] = []

        async def _hold(name, delay):
            async with session_gate.session_write_gate("k"):
                order.append(f"{name}-in")
                await asyncio.sleep(delay)
                order.append(f"{name}-out")

        await asyncio.gather(_hold("a", 0.02), _hold("b", 0))
        assert order in (["a-in", "a-out", "b-in", "b-out"],
                         ["b-in", "b-out", "a-in", "a-out"])
        # refcounted: nothing is retained once released
        assert "k" not in session_gate._SESSION_GATES

    async def test_two_scheduled_turns_never_run_concurrently(self, tmp_path):
        """The outcome, not the arrangement: two SCHEDULED turns dispatched at
        once for ONE session label must not overlap inside the SDK, and the
        second must see the first's published session id.

        SCHEDULED turns bypass the warm client pool, so before #573 nothing
        serialized them and both resumed the same sid.
        """
        from unittest.mock import patch

        from bus import BusMessage, MessageType
        from test_agent_process import _make_agent, _mk_assistant, _mk_result

        live = 0
        overlapped = False
        resumed: list[str | None] = []
        turn = 0

        class _SlowClient:
            def __init__(self, options):
                nonlocal turn
                turn += 1
                self._sid = f"sdk-sid-{turn}"
                resumed.append(getattr(options, "resume", None))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def query(self, text):
                return None

            async def receive_response(self):
                nonlocal live, overlapped
                live += 1
                if live > 1:
                    overlapped = True
                await asyncio.sleep(0.02)
                live -= 1
                yield _mk_assistant("ok")
                yield _mk_result(self._sid)

        agent = _make_agent(tmp_path)

        # Widen the publish window so "the gate spans the publish" is a real
        # assertion rather than a scheduling accident: released at the end of
        # the attempt, the second turn would read the registry while the first
        # is still inside register() and resume nothing.
        _register = agent._session_registry.register

        async def _slow_register(**kw):
            await asyncio.sleep(0.02)
            await _register(**kw)

        agent._session_registry.register = _slow_register

        def _msg():
            return BusMessage(
                type=MessageType.SCHEDULED, source="scheduler",
                target="assistant", content="run", channel="telegram",
                context={"chat_id": LABEL, "_scheduled_delivery": True},
            )

        with patch("sdk_client_pool._default_make_client", _SlowClient):
            await asyncio.gather(agent._process(_msg()), agent._process(_msg()))

        assert not overlapped
        # The gate spans the registry publish too: the second turn resumed the
        # session the first one registered, rather than reading past it.
        assert resumed[1] == "sdk-sid-1"

    async def test_different_sessions_do_not_block_each_other(self):
        import agent as agent_mod

        started = asyncio.Event()

        async def _first():
            async with agent_mod.session_write_gate("one"):
                started.set()
                await asyncio.sleep(0.05)

        async def _second():
            await started.wait()
            async with agent_mod.session_write_gate("two"):
                return "ran"

        _t = asyncio.create_task(_first())
        assert await asyncio.wait_for(_second(), timeout=1.0) == "ran"
        await _t
