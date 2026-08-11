"""A5 · F-MULTI — multi-select ask (toggle-many + submit).

Spec: ``docs/superpowers/specs/2026-07-15-engagement-ux-round3-design.md``
§"### A5 · F-MULTI". A multi ask renders checkbox toggle rows + a ``✅ Submit``
row; a tap TOGGLES ``meta["selected"]`` on a LIVE+UNCLAIMED broker request and
redraws the keyboard through the sequencer's ``edit_discrete`` (revalidated
live-and-unclaimed under the serialized CM); submit claims/commits carrying the
full ``option_indices``; the finish hook stays the sole TERMINAL writer.

REAL ``VerdictBroker`` + REAL ``OutputSequencer`` + REAL handler/registry
harnesses (fakes only at the wire boundary), injected clocks. RED-first per the
spec list.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker

# asyncio_mode = auto (pytest.ini) runs the async tests; sync unit tests here
# must NOT carry an asyncio mark, so there is no module-level pytestmark.


# ===========================================================================
# 1. Broker: toggle_selection / is_live_unclaimed / claim+commit indices
# ===========================================================================


def _reg_multi(broker, *, scope="e", rid="r", timeout_s=60.0):
    req, created = broker.register(
        namespace="engagement_ask", scope=scope, request_id=rid,
        timeout_s=timeout_s,
        meta={"options": ["A", "B", "C"], "operator_id": 7},
    )
    assert created
    return req


class TestBrokerToggle:
    async def test_toggle_flips_and_returns_sorted_selection(self):
        b = VerdictBroker()
        _reg_multi(b)
        assert b.toggle_selection(
            namespace="engagement_ask", scope="e", request_id="r", idx=2) == [2]
        assert b.toggle_selection(
            namespace="engagement_ask", scope="e", request_id="r", idx=0) == [0, 2]
        # flipping 2 again removes it
        assert b.toggle_selection(
            namespace="engagement_ask", scope="e", request_id="r", idx=2) == [0]
        assert b.get_meta(
            namespace="engagement_ask", scope="e", request_id="r")["selected"] == [0]

    async def test_toggle_on_absent_returns_none(self):
        b = VerdictBroker()
        assert b.toggle_selection(
            namespace="engagement_ask", scope="e", request_id="nope", idx=0) is None

    async def test_toggle_on_claimed_returns_none_no_mutation(self):
        b = VerdictBroker()
        req = _reg_multi(b)
        claim = b.claim(namespace="engagement_ask", scope="e", request_id="r",
                        option_index=0, actor_id=7)
        assert not isinstance(claim, str)
        assert b.toggle_selection(
            namespace="engagement_ask", scope="e", request_id="r", idx=1) is None
        assert "selected" not in req.meta

    async def test_toggle_on_retired_returns_none(self):
        b = VerdictBroker()
        _reg_multi(b)
        b.cancel(namespace="engagement_ask", scope="e", request_id="r",
                 reason="superseded_by_text")
        assert b.toggle_selection(
            namespace="engagement_ask", scope="e", request_id="r", idx=0) is None

    async def test_is_live_unclaimed(self):
        b = VerdictBroker()
        _reg_multi(b)
        assert b.is_live_unclaimed(
            namespace="engagement_ask", scope="e", request_id="r") is True
        b.claim(namespace="engagement_ask", scope="e", request_id="r",
                option_index=0, actor_id=7)
        assert b.is_live_unclaimed(
            namespace="engagement_ask", scope="e", request_id="r") is False

    async def test_claim_commit_carries_option_indices(self):
        b = VerdictBroker()
        req = _reg_multi(b)
        claim = b.claim(namespace="engagement_ask", scope="e", request_id="r",
                        option_index=0, actor_id=7, option_indices=[0, 2])
        assert not isinstance(claim, str)
        assert b.commit(claim) is True
        outcome = await b.await_result(req)
        assert outcome == {
            "outcome": "answered", "option_index": 0, "actor_id": 7,
            "option_indices": [0, 2],
        }

    async def test_single_select_commit_unchanged(self):
        b = VerdictBroker()
        req = _reg_multi(b)
        claim = b.claim(namespace="engagement_ask", scope="e", request_id="r",
                        option_index=1, actor_id=7)
        assert b.commit(claim) is True
        outcome = await b.await_result(req)
        assert "option_indices" not in outcome
        assert outcome["option_index"] == 1


# ===========================================================================
# 2. callback grammar parse fuzz
# ===========================================================================


class TestParseCallback:
    def test_toggle_shape(self):
        from channels.telegram import _parse_callback_data
        assert _parse_callback_data("v1|ask_multi|rid|2") == (
            "engagement_ask", "rid", 2, "toggle")

    def test_submit_shape(self):
        from channels.telegram import _parse_callback_data
        assert _parse_callback_data("v1|ask_multi|rid|s") == (
            "engagement_ask", "rid", None, "submit")

    def test_single_select_kind_none(self):
        from channels.telegram import _parse_callback_data
        assert _parse_callback_data("v1|engagement_ask|rid|1") == (
            "engagement_ask", "rid", 1, None)

    def test_legacy_perm_kind_none(self):
        from channels.telegram import _parse_callback_data
        assert _parse_callback_data("perm:allow:rid") == (
            "permission", "rid", 0, None)

    def test_bad_idx_fuzz(self):
        from channels.telegram import _parse_callback_data
        for bad in ("v1|ask_multi|rid|x", "v1|ask_multi|rid|", "v1|ask_multi||2",
                    "v1|ask_multi|rid|1.5", "v1|ask_multi|rid|submit"):
            assert _parse_callback_data(bad) == (None, None, None, None)

    def test_overlong_rejected(self):
        from channels.telegram import _parse_callback_data
        assert _parse_callback_data("v1|ask_multi|" + "r" * 200 + "|s") == (
            None, None, None, None)


# ===========================================================================
# 3. hash-identity: client _ask_projection_hash == relay projection_hash
# ===========================================================================


class TestHashIdentity:
    def test_multi_hash_client_equals_relay(self):
        import channels.casa_engagement_channel as ch
        from channels.output_sequencer import projection_hash, ASK_TOOL

        raw = {"question": "Q?", "options": ["A", "B"], "multi": True}
        assert ch._ask_projection_hash("Q?", ["A", "B"], None, True) == \
            projection_hash(ASK_TOOL, raw)

    def test_non_multi_default_still_matches(self):
        import channels.casa_engagement_channel as ch
        from channels.output_sequencer import projection_hash, ASK_TOOL

        assert ch._ask_projection_hash("Q?", ["A", "B"], 100) == \
            projection_hash(
                ASK_TOOL,
                {"question": "Q?", "options": ["A", "B"], "timeout_s": 100})


# ===========================================================================
# 4. validation: multi requires >=2 options; multi+anchor refused
# ===========================================================================


class TestValidation:
    def _v(self, **body):
        from channels.channel_handlers import _validate_ask_args
        return _validate_ask_args(body)

    def test_multi_anchor_refused(self):
        assert self._v(question="q?", options=[], multi=True) is None

    def test_multi_ok_two_options(self):
        out = self._v(question="q?", options=["A", "B"], multi=True, timeout_s=60)
        assert out is not None

    def test_non_multi_anchor_still_accepted(self):
        out = self._v(question="q?", options=[], timeout_s=60)
        assert out is not None

    def test_multi_false_two_options_ok(self):
        assert self._v(question="q?", options=["A", "B"], timeout_s=60) is not None


# ===========================================================================
# 5. settle copy + response mapping with option_indices
# ===========================================================================


class TestSettleAndResponse:
    def test_settle_text_joins_labels(self):
        from channels.channel_handlers import _ask_settle_text
        text = _ask_settle_text(
            "Q1: pick", {"outcome": "answered", "option_indices": [0, 2]},
            ["Alpha", "Beta", "Gamma"])
        # v0.84.0 (round 4, D1 bullet 3): BOUNDED positional copy, 1-based
        # ascending — never the full labels.
        assert text == "Q1: pick\n✅ Options 1, 3"

    def test_response_lists_labels_and_indices_plus_first_compat(self):
        from channels.channel_handlers import _ask_outcome_response
        resp = _ask_outcome_response(
            {"outcome": "answered", "option_index": 0, "option_indices": [0, 2]},
            ["Alpha", "Beta", "Gamma"])
        body = json.loads(resp.text)
        assert body == {
            "ok": True, "outcome": "answered",
            "options": ["Alpha", "Gamma"], "option_indices": [0, 2],
            "option": "Alpha", "option_index": 0,
        }

    def test_single_select_response_unchanged(self):
        from channels.channel_handlers import _ask_outcome_response
        resp = _ask_outcome_response(
            {"outcome": "answered", "option_index": 1}, ["Alpha", "Beta"])
        body = json.loads(resp.text)
        assert body == {
            "ok": True, "outcome": "answered", "option": "Beta", "option_index": 1}

    async def test_no_answer_multi_takes_a2_pause_path(self):
        # An un-submitted multi ask that expires reuses the A2 pause path
        # unchanged (no_answer carries no indices, so the single/multi split
        # never reaches it).
        from channels.channel_handlers import _ask_final_response

        calls = []

        class _Drv:
            def note_operator_away(self, eid, gen):
                calls.append((eid, gen))
                return True

        req = SimpleNamespace(meta={"inbound_gen": 0})
        resp = await _ask_final_response(
            {"outcome": "no_answer"}, ["A", "B"], _Drv(), "eng", "r", req)
        body = json.loads(resp.text)
        assert body["outcome"] == "no_answer"
        assert body["engagement_paused"] is True
        assert calls == [("eng", 0)]


# ===========================================================================
# Dispatch harness — REAL TelegramChannel._on_inline_callback + REAL sequencer
# ===========================================================================


class _MarkupWire:
    def __init__(self) -> None:
        self.sends: list = []
        self.edits: list = []

    async def send(self, topic, text, markup, reply_to=None):
        self.sends.append((topic, text, markup, reply_to))
        return 6000

    async def edit(self, topic, mid, text, markup) -> bool:
        self.edits.append((topic, mid, text, markup))
        return True


class _SeqDriver:
    """Fake ``claude_code`` driver exposing a REAL OutputSequencer behind the
    A5 seam methods (delegating exactly like the production driver)."""

    def __init__(self, eid: str, topic_id: int = 555) -> None:
        from channels.output_sequencer import OutputSequencer

        self.wire = _MarkupWire()

        async def _ns(t, x, r=None):
            return None

        async def _ne(t, m, x):
            return True

        self.seq = OutputSequencer(
            engagement_id=eid, topic_id=topic_id,
            send_message=_ns, edit_message=_ne,
            send_message_markup=self.wire.send, edit_message_markup=self.wire.edit)
        self._sequencers = {eid: self.seq}

    async def edit_ask_keyboard(self, eid, mid, markup, *, revalidate=None) -> bool:
        seq = self._sequencers.get(eid)
        if seq is None:
            return False
        return await seq.edit_discrete(mid, markup=markup, revalidate=revalidate)

    async def settle_ask_keyboard(self, eid, mid, text) -> bool:
        seq = self._sequencers.get(eid)
        if seq is None:
            return False
        from channels.output_sequencer import MARKUP_EMPTY
        return await seq.edit_discrete(mid, text=text, markup=MARKUP_EMPTY)


def _mk_channel(fake_telegram_bot, registry):
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(
        bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001)
    ch._engagement_registry = registry
    return ch


def _mk_update(*, data, thread_id, chat_id=-1001, user_id=999):
    cq = SimpleNamespace(
        id="cq1", data=data,
        message=SimpleNamespace(
            message_thread_id=thread_id, chat=SimpleNamespace(id=chat_id)),
        answer=AsyncMock(return_value=None),
        from_user=SimpleNamespace(id=user_id))
    return SimpleNamespace(callback_query=cq)


def _seed_multi(broker, *, scope, rid, topic_id, operator_id, options,
                message_id=7000, selected=None, shorts=None, button_labels=None):
    meta = {
        "options": list(options), "topic_id": topic_id,
        "operator_id": operator_id, "multi": True,
        "shorts": shorts, "message_id": message_id,
    }
    if button_labels is not None:
        meta["button_labels"] = list(button_labels)
    req, created = broker.register(
        namespace="engagement_ask", scope=scope, request_id=rid, timeout_s=60.0,
        meta=meta)
    assert created
    if selected is not None:
        req.meta["selected"] = list(selected)
    return req


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


class TestToggleDispatch:
    async def test_toggle_mutates_meta_and_redraws_without_resolving(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker, monkeypatch,
    ):
        rec = engagement_fixture.active_record
        drv = _SeqDriver(rec.id, topic_id=rec.topic_id)
        monkeypatch.setattr(agent_mod, "active_claude_code_driver", drv)
        ch = _mk_channel(fake_telegram_bot, engagement_fixture.registry)
        req = _seed_multi(
            _fresh_broker, scope=rec.id, rid="m1", topic_id=rec.topic_id,
            operator_id=999, options=["Alpha", "Beta", "Gamma"])

        update = _mk_update(data="v1|ask_multi|m1|1", thread_id=rec.topic_id)
        await ch._on_inline_callback(update, context=None)

        # meta mutated, request NOT resolved (no commit).
        assert req.meta["selected"] == [1]
        assert _fresh_broker.is_live_unclaimed(
            namespace="engagement_ask", scope=rec.id, request_id="m1") is True
        assert not req._future.done()
        # keyboard redrawn through the REAL sequencer's edit_discrete (markup
        # -only edit → text None).
        assert len(drv.wire.edits) == 1
        _topic, mid, text, _markup = drv.wire.edits[0]
        assert mid == 7000
        assert text is None
        from telegram import InlineKeyboardMarkup
        assert isinstance(_markup, InlineKeyboardMarkup)
        # toast reflects the new toggle state.
        update.callback_query.answer.assert_awaited_once_with("added")

        # A second tap on the same option clears it (removed).
        update2 = _mk_update(data="v1|ask_multi|m1|1", thread_id=rec.topic_id)
        await ch._on_inline_callback(update2, context=None)
        assert req.meta["selected"] == []
        update2.callback_query.answer.assert_awaited_once_with("removed")

    async def test_redraw_consumes_persisted_button_labels_byte_identical(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker, monkeypatch,
    ):
        """D2 item 3 (Task A5): the multi redraw prepends only the mutable ☐/☑
        glyph to the PERSISTED ``button_labels`` — it NEVER re-resolves from
        ``shorts``. Seed captions that DIFFER from what re-deriving ``shorts``
        (here: ``None`` ⇒ the numbered floor) would produce, so a re-deriving
        redraw would render ``☑ Option 2`` while the correct persisted-caption
        redraw renders ``☑ 2 · Beta`` byte-identically."""
        rec = engagement_fixture.active_record
        drv = _SeqDriver(rec.id, topic_id=rec.topic_id)
        monkeypatch.setattr(agent_mod, "active_claude_code_driver", drv)
        ch = _mk_channel(fake_telegram_bot, engagement_fixture.registry)
        persisted = ["1 · Alpha", "2 · Beta", "3 · Gamma"]
        _seed_multi(
            _fresh_broker, scope=rec.id, rid="bl1", topic_id=rec.topic_id,
            operator_id=999, options=["Alpha", "Beta", "Gamma"],
            shorts=None, button_labels=persisted)

        update = _mk_update(data="v1|ask_multi|bl1|1", thread_id=rec.topic_id)
        await ch._on_inline_callback(update, context=None)

        assert len(drv.wire.edits) == 1
        _topic, _mid, _text, markup = drv.wire.edits[0]
        captions = [row[0].text for row in markup.inline_keyboard]
        assert captions == [
            "☐ 1 · Alpha", "☑ 2 · Beta", "☐ 3 · Gamma", "✅ Submit",
        ]

    async def test_toggle_on_claimed_returns_expired_no_edit(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker, monkeypatch,
    ):
        rec = engagement_fixture.active_record
        drv = _SeqDriver(rec.id, topic_id=rec.topic_id)
        monkeypatch.setattr(agent_mod, "active_claude_code_driver", drv)
        ch = _mk_channel(fake_telegram_bot, engagement_fixture.registry)
        _seed_multi(_fresh_broker, scope=rec.id, rid="m2", topic_id=rec.topic_id,
                    operator_id=999, options=["A", "B"])
        # claim it (a submit in flight) → toggle must decline.
        _fresh_broker.claim(namespace="engagement_ask", scope=rec.id,
                            request_id="m2", option_index=0, actor_id=999)

        update = _mk_update(data="v1|ask_multi|m2|1", thread_id=rec.topic_id)
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")
        assert drv.wire.edits == []

    async def test_toggle_on_retired_returns_expired_no_edit(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker, monkeypatch,
    ):
        rec = engagement_fixture.active_record
        drv = _SeqDriver(rec.id, topic_id=rec.topic_id)
        monkeypatch.setattr(agent_mod, "active_claude_code_driver", drv)
        ch = _mk_channel(fake_telegram_bot, engagement_fixture.registry)
        _seed_multi(_fresh_broker, scope=rec.id, rid="m3", topic_id=rec.topic_id,
                    operator_id=999, options=["A", "B"])
        _fresh_broker.cancel(namespace="engagement_ask", scope=rec.id,
                             request_id="m3", reason="superseded_by_text")

        update = _mk_update(data="v1|ask_multi|m3|0", thread_id=rec.topic_id)
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")
        assert drv.wire.edits == []


class TestSubmitDispatch:
    async def test_submit_empty_refused_no_claim(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker, monkeypatch,
    ):
        rec = engagement_fixture.active_record
        drv = _SeqDriver(rec.id, topic_id=rec.topic_id)
        monkeypatch.setattr(agent_mod, "active_claude_code_driver", drv)
        ch = _mk_channel(fake_telegram_bot, engagement_fixture.registry)
        req = _seed_multi(_fresh_broker, scope=rec.id, rid="s0",
                          topic_id=rec.topic_id, operator_id=999,
                          options=["A", "B"])  # no selection

        update = _mk_update(data="v1|ask_multi|s0|s", thread_id=rec.topic_id)
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("pick at least one")
        # not claimed, not resolved.
        assert _fresh_broker.is_live_unclaimed(
            namespace="engagement_ask", scope=rec.id, request_id="s0") is True
        assert not req._future.done()

    async def test_submit_resolves_with_option_indices(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker, monkeypatch,
    ):
        rec = engagement_fixture.active_record
        drv = _SeqDriver(rec.id, topic_id=rec.topic_id)
        monkeypatch.setattr(agent_mod, "active_claude_code_driver", drv)
        ch = _mk_channel(fake_telegram_bot, engagement_fixture.registry)
        req = _seed_multi(_fresh_broker, scope=rec.id, rid="s1",
                          topic_id=rec.topic_id, operator_id=999,
                          options=["A", "B", "C"], selected=[0, 2])

        update = _mk_update(data="v1|ask_multi|s1|s", thread_id=rec.topic_id)
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.2)
        assert outcome["outcome"] == "answered"
        assert outcome["option_indices"] == [0, 2]
        assert outcome["option_index"] == 0  # first selection, compat
        assert outcome["actor_id"] == 999


# ===========================================================================
# 6. toggle redraw racing a terminal settle — revalidation skips the edit
# ===========================================================================


class TestRevalidationRace:
    async def test_redraw_racing_terminal_settle_is_skipped(self):
        from channels.output_sequencer import OutputSequencer
        from telegram import InlineKeyboardMarkup

        broker = VerdictBroker()
        req, _ = broker.register(
            namespace="engagement_ask", scope="e", request_id="r", timeout_s=60.0,
            meta={"options": ["A", "B"]})
        wire = _MarkupWire()

        async def _ns(t, x, r=None):
            return None

        async def _ne(t, m, x):
            return True

        seq = OutputSequencer(
            engagement_id="e", topic_id=42, send_message=_ns, edit_message=_ne,
            send_message_markup=wire.send, edit_message_markup=wire.edit)

        gate = asyncio.Event()

        async def _revalidate() -> bool:
            await gate.wait()
            return broker.is_live_unclaimed(
                namespace="engagement_ask", scope="e", request_id="r")

        task = asyncio.ensure_future(seq.edit_discrete(
            7000, markup=InlineKeyboardMarkup([]), revalidate=_revalidate))
        await asyncio.sleep(0)  # task acquires the lock, blocks on the gate

        # A terminal settle resolves the broker in the race window.
        broker.cancel(namespace="engagement_ask", scope="e", request_id="r",
                      reason="superseded_by_text")
        gate.set()

        ok = await asyncio.wait_for(task, 0.2)
        assert ok is False          # revalidation declined
        assert wire.edits == []     # no wire edit ever fired
        assert req is not None


# ===========================================================================
# 7. handler end-to-end (eager) — multi keyboard post + submit settle + super
# ===========================================================================


class _MultiFakeChannel:
    def __init__(self) -> None:
        self.keyboards: list[dict] = []
        self.edits: list[dict] = []
        self._next = 8000

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options,
        shorts=None, multi=False,
    ) -> int:
        self.keyboards.append({
            "question": question, "options": list(options),
            "shorts": list(shorts) if shorts is not None else None,
            "multi": multi, "request_id": request_id})
        mid = self._next
        self._next += 1
        return mid

    async def edit_topic_message(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        self.edits.append({"text": text, "clear_keyboard": clear_keyboard})
        return True

    async def edit_topic_message_rich(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        return await self.edit_topic_message(
            topic_id, message_id, text, clear_keyboard=clear_keyboard)


class _HandlerRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


@pytest.fixture
async def wired(tmp_path, _fresh_broker, monkeypatch):
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    monkeypatch.setattr(agent_mod, "active_claude_code_driver", None)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    ch = _MultiFakeChannel()
    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    return SimpleNamespace(
        reg=reg, rec=rec, ch=ch, broker=_fresh_broker,
        ask=handlers["/internal/channel/ask"])


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    """Poll *predicate* until it holds, bounded by WALL-CLOCK time (not a
    fixed real-time sleep), so a slow/loaded runner gets real slack while a
    fast run resolves in a handful of scheduler turns. Cheap when fast,
    tolerant when slow, and no weaker than the fixed sleep it replaces — it
    still raises (``TimeoutError``) if the condition never fires."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


class TestHandlerEndToEnd:
    async def test_multi_keyboard_posted_and_submit_settles(self, wired):
        payload = {
            "engagement_id": wired.rec.id, "request_id": "h1",
            "engagement_token": wired.rec.auth_token,
            "question": "Which apply?", "options": ["Alpha", "Beta", "Gamma"],
            "timeout_s": 60, "multi": True,
        }
        task = asyncio.ensure_future(wired.ask(_HandlerRequest(payload)))
        # Await the observable itself (CI flake on a loaded runner: the fixed
        # 0.02 s sleep lost to the handler's real awaits and keyboards[-1]
        # raised IndexError before the post landed).
        await _wait_until(lambda: wired.ch.keyboards)
        # keyboard posted with multi=True.
        assert wired.ch.keyboards[-1]["multi"] is True

        # Simulate the operator's submit tap: claim+commit carrying indices.
        claim = wired.broker.claim(
            namespace="engagement_ask", scope=wired.rec.id, request_id="h1",
            option_index=0, actor_id=555, option_indices=[0, 2])
        assert not isinstance(claim, str)
        assert wired.broker.commit(claim) is True

        resp = await asyncio.wait_for(task, timeout=1.0)
        await wired.broker.drain_hooks()

        body = json.loads(resp.text)
        assert body["outcome"] == "answered"
        assert body["options"] == ["Alpha", "Gamma"]
        assert body["option_indices"] == [0, 2]
        assert body["option"] == "Alpha"
        # settle copy: BOUNDED positional (driver None ⇒ edit_topic_message
        # fallback) — v0.84.0 D1 bullet 3, never the full labels.
        settle = wired.ch.edits[-1]
        assert "✅ Options 1, 3" in settle["text"]
        assert settle["clear_keyboard"] is True

    async def test_multi_supersession_settles(self, wired):
        payload = {
            "engagement_id": wired.rec.id, "request_id": "h2",
            "engagement_token": wired.rec.auth_token,
            "question": "Which apply?", "options": ["Alpha", "Beta"],
            "timeout_s": 60, "multi": True,
        }
        task = asyncio.ensure_future(wired.ask(_HandlerRequest(payload)))
        # Cancel only once the request is durably live in the broker — a
        # cancel racing registration would no-op and the task would hang to
        # the real 60 s deadline (same boundary as test_multi_timeout_no_answer).
        await _wait_until(lambda: wired.broker.is_live_unclaimed(
            namespace="engagement_ask", scope=wired.rec.id, request_id="h2"))
        wired.broker.cancel(
            namespace="engagement_ask", scope=wired.rec.id, request_id="h2",
            reason="superseded_by_text")
        resp = await asyncio.wait_for(task, timeout=1.0)
        await wired.broker.drain_hooks()

        body = json.loads(resp.text)
        assert body["ok"] is False
        assert body["error"] == "superseded"
        assert "superseded" in wired.ch.edits[-1]["text"]

    async def test_multi_timeout_no_answer(self, wired):
        payload = {
            "engagement_id": wired.rec.id, "request_id": "h3",
            "engagement_token": wired.rec.auth_token,
            "question": "Which apply?", "options": ["Alpha", "Beta"],
            "timeout_s": 60, "multi": True,
        }
        task = asyncio.ensure_future(wired.ask(_HandlerRequest(payload)))
        # Await the actual durable-registration boundary (the request becomes
        # live+unclaimed in the broker) instead of a fixed real-time sleep:
        # the handler does one real await (registry number allocation, real
        # tmp-file I/O) before ``BROKER.register`` lands the request, and on a
        # slow/loaded runner that can outlast a tight fixed sleep — firing the
        # synthetic timeout on a not-yet-live key would silently no-op and
        # the task would only resolve at the real 60s broker deadline, well
        # past any bounded ``wait_for``.
        await _wait_until(lambda: wired.broker.is_live_unclaimed(
            namespace="engagement_ask", scope=wired.rec.id, request_id="h3"))
        wired.broker._on_timeout(("engagement_ask", wired.rec.id, "h3"))
        resp = await asyncio.wait_for(task, timeout=10.0)
        await wired.broker.drain_hooks()

        body = json.loads(resp.text)
        assert body["outcome"] == "no_answer"

    async def test_multi_anchor_refused_invalid_args(self, wired):
        payload = {
            "engagement_id": wired.rec.id, "request_id": "h4",
            "engagement_token": wired.rec.auth_token,
            "question": "Which apply?", "options": [], "multi": True,
        }
        resp = await asyncio.wait_for(
            wired.ask(_HandlerRequest(payload)), timeout=1.0)
        body = json.loads(resp.text)
        assert body["ok"] is False
        assert body["error"] == "invalid_args"
