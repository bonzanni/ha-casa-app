"""v0.83.0 (A4 · F-BTN) / v0.84.0 (round 4, spec D2) — button labels.

Two halves:

  1. PURE-FUNCTION tests of ``short_option_labels`` (the ``labels``/``shorts``
     convenience wrapper over ``resolve_button_labels`` — see
     ``tests/test_ask_button_labels.py`` for the resolver's own exhaustive
     whole-set/floor/telemetry coverage). v0.83.0's per-option ELISION LADDER
     (``_elide_one``/``_resolve_label_collisions``/``_short_option_label``) is
     GONE (D2): labels are now MODEL-generated with WHOLE-SET floor/verbatim
     semantics — either every option has a usable short and the whole set
     renders ``n · <short>`` verbatim, or the whole set floors to
     ``Option 1``, ``Option 2``, … . These tests pin that behaviour for
     ``short_option_labels`` specifically (name/signature PINNED — resident
     ``ask_user`` depends on it via ``post_dm_keyboard(short_labels=True)``).

  2. HANDLER-LEVEL dict-options tests (REAL ``VerdictBroker`` + REAL
     ``EngagementRegistry`` + REAL ``_make_channel_handlers``, injected clocks)
     asserting the body carries FULL labels verbatim, the buttons carry
     ``n · <short>``, a tap resolves to the FULL label in the response AND the
     settle ✅ line, and malformed dict SHAPES (wrong type, missing/blank
     label, duplicate full labels) still refuse ``invalid_args``. These
     exercise ``channel_handlers._validate_ask_args``, which Task A2 (round 4,
     spec D1) changed: the invented length caps are gone and ``short`` is now
     advisory-only — a missing/blank/duplicate/over-budget/non-string
     ``short`` is ACCEPTED and flows through to the D2 resolver (which floors
     the whole button set) instead of rejecting the ask. A fake channel only
     RECORDS the ``options``/``shorts`` it's called with, so these don't
     exercise ``resolve_button_labels`` itself and are unaffected by the
     round-4 resolver change.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from broker_helpers import deliver
from aiohttp import web

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker
from channels.telegram import _ASK_BUTTON_CAPTION_CAP, short_option_labels


# ===========================================================================
# Pure-function: short_option_labels — whole-set floor/verbatim
# ===========================================================================


class TestAllShortsUsable:
    def test_verbatim_captions_when_every_short_usable(self) -> None:
        labels = ["Personal Gmail", "Work Outlook", "Yahoo Mail"]
        shorts = ["Gmail", "Outlook", "Yahoo"]
        assert short_option_labels(labels, shorts) == [
            "1 · Gmail", "2 · Outlook", "3 · Yahoo"]

    def test_single_option_with_short(self) -> None:
        assert short_option_labels(["Personal Gmail"], ["Gmail"]) == [
            "1 · Gmail"]


class TestWholeSetFloors:
    # #1386: an option without a usable short offers its own label; the
    # whole set floors only when the words do not fit (or collide / are blank).
    _WIDE = ["Personal Gmail, the one you use every day",
             "Work Outlook, through the company tenant"]

    def test_no_shorts_short_labels_render_own_words(self) -> None:
        labels = ["Personal Gmail", "Work Outlook"]
        assert short_option_labels(labels) == ["Personal Gmail", "Work Outlook"]

    def test_no_shorts_wide_labels_floor_whole_set(self) -> None:
        assert short_option_labels(self._WIDE) == ["Option 1", "Option 2"]

    def test_partial_shorts_borrow_own_label_numbered(self) -> None:
        labels = ["Personal Gmail", "Work Outlook"]
        shorts = ["Gmail", None]
        assert short_option_labels(labels, shorts) == [
            "1 · Gmail", "2 · Work Outlook"]

    def test_partial_shorts_wide_label_floors_whole_set(self) -> None:
        assert short_option_labels(self._WIDE, ["Gmail", None]) == [
            "Option 1", "Option 2"]

    def test_blank_short_floors_whole_set(self) -> None:
        assert short_option_labels(self._WIDE, ["Gmail", "   "]) == [
            "Option 1", "Option 2"]

    def test_duplicate_shorts_floors_whole_set(self) -> None:
        assert short_option_labels(self._WIDE, ["Same", "Same"]) == [
            "Option 1", "Option 2"]

    def test_duplicate_shorts_fall_back_to_fitting_labels(self) -> None:
        labels = ["Personal Gmail", "Work Outlook"]
        assert short_option_labels(labels, ["Same", "Same"]) == labels

    def test_non_string_short_uses_own_label(self) -> None:
        labels = ["Personal Gmail", "Work Outlook"]
        assert short_option_labels(labels, ["Gmail", 7]) == [
            "1 · Gmail", "2 · Work Outlook"]

    def test_shorts_shorter_than_labels_borrow_own_labels(self) -> None:
        # ``shorts`` has fewer entries than ``labels`` — the missing tail
        # entries show their own labels.
        labels = ["Personal Gmail", "Work Outlook", "Yahoo Mail"]
        assert short_option_labels(labels, ["Gmail"]) == [
            "1 · Gmail", "2 · Work Outlook", "3 · Yahoo Mail"]


class TestCaptionCapBoundary:
    def test_exact_64_char_decorated_caption_passes_verbatim(self) -> None:
        short = "s" * 60  # "1 · " (4) + 60 == 64
        label = short_option_labels(["full label text"], [short])[0]
        assert label == f"1 · {short}"
        assert len(label) == _ASK_BUTTON_CAPTION_CAP == 64

    def test_65_char_decorated_caption_floors(self) -> None:
        short = "s" * 61  # 65 > 64
        assert short_option_labels(
            ["full label text, far too wide for one button"], [short]) == [
                "Option 1"]

    def test_sol_live_case_no_shorts_never_elides(self) -> None:
        # The exact live F-BTN regression — no agent shorts supplied, so the
        # whole set floors; it never garbles into "1 · A —…MCP…MCPB".
        labels = [
            "Option A — Python MCP server, MCPB packaged",
            "Option B — Python MCP server via venv",
        ]
        result = short_option_labels(labels)
        assert result == ["Option 1", "Option 2"]
        for lab in result:
            assert "MCP" not in lab
            assert "…" not in lab


# ===========================================================================
# Handler-level: dict options end-to-end
# ===========================================================================


class _FakeChannel:
    """Records keyboard posts (incl. the ``shorts`` kwarg) and settle edits."""

    def __init__(self) -> None:
        self.keyboards: list[dict] = []
        self.edits: list[dict] = []
        self._next = 8000

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options, shorts=None,
    ) -> int:
        self.keyboards.append(
            {"question": question, "options": list(options),
             "shorts": list(shorts) if shorts is not None else None})
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


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


@pytest.fixture
def fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture
async def wired(tmp_path, fresh_broker, monkeypatch):
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    # driver=None ⇒ eager fallback (no relay), the simplest deterministic post.
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", None)
    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    ch = _FakeChannel()
    handlers = _make_channel_handlers(
        telegram_channel=ch, engagement_registry=reg)
    return {
        "reg": reg, "rec": rec, "ch": ch, "broker": fresh_broker,
        "ask": handlers["/internal/channel/ask"],
    }


def _body(resp: web.Response) -> dict:
    return json.loads(resp.text)


async def _await_registered(broker, key, *, timeout_s: float = 5.0) -> None:
    """Yield the event loop until the ask task has REGISTERED its waiter.

    Replaces a fixed ``asyncio.sleep(0.02)``. The tap below can only be
    ``delivered`` once the ask task has registered with the broker; taps
    arriving before that resolve ``stale``. How many event-loop turns the ask
    needs is machine-dependent, so on a slow CI runner the fixed sleep expired
    early and the assertion failed with ``'stale' == 'delivered'``. Waiting on
    the actual precondition is correct at any speed. (Same flake class as the
    v0.124.1 engagement ask-gate deflake.)
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if key in broker._live:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"ask never registered {key!r} with the broker")


async def _drive_answer(wired, payload, option_index):
    """Post an ask, tap ``option_index``, return the tool response dict."""
    task = asyncio.ensure_future(wired["ask"](_FakeRequest(payload)))
    await _await_registered(
        wired["broker"],
        ("engagement_ask", wired["rec"].id, payload["request_id"]),
    )
    assert deliver(wired["broker"], 
        namespace="engagement_ask", scope=wired["rec"].id,
        request_id=payload["request_id"], option_index=option_index,
        actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    await wired["broker"].drain_hooks()
    return _body(resp)


class TestDictOptionsEndToEnd:
    async def test_body_full_buttons_short_tap_resolves_full(self, wired):
        options = [
            {"label": "Single account with aliases", "short": "Aliased"},
            {"label": "Separate Google accounts", "short": "Separate"},
        ]
        payload = {
            "engagement_id": wired["rec"].id, "request_id": "d1",
            "engagement_token": wired["rec"].auth_token,
            "question": "Which account?", "options": options, "timeout_s": 60,
        }
        body = await _drive_answer(wired, payload, option_index=0)

        # Tap resolves to the FULL label (not the short) in the response.
        assert body["ok"] is True
        assert body["outcome"] == "answered"
        assert body["option"] == "Single account with aliases"
        assert body["option_index"] == 0

        kb = wired["ch"].keyboards[-1]
        # The MESSAGE body carries the FULL labels VERBATIM + numbered.
        assert "1. Single account with aliases" in kb["question"]
        assert "2. Separate Google accounts" in kb["question"]
        # The keyboard is driven by full labels + a parallel shorts list.
        assert kb["options"] == [
            "Single account with aliases", "Separate Google accounts"]
        assert kb["shorts"] == ["Aliased", "Separate"]

        # Settle ✅ line is the BOUNDED positional copy (v0.84.0 D1 bullet 3),
        # never the full label or short.
        settle = wired["ch"].edits[-1]["text"]
        assert "✅ Option 1" in settle
        assert wired["ch"].edits[-1]["clear_keyboard"] is True

    async def test_mixed_str_and_dict_allowed(self, wired):
        options = [
            "Personal Gmail",
            {"label": "Configure the enterprise SSO integration",
             "short": "SSO"},
        ]
        payload = {
            "engagement_id": wired["rec"].id, "request_id": "d2",
            "engagement_token": wired["rec"].auth_token,
            "question": "Which?", "options": options, "timeout_s": 60,
        }
        body = await _drive_answer(wired, payload, option_index=1)
        assert body["option"] == "Configure the enterprise SSO integration"
        kb = wired["ch"].keyboards[-1]
        assert kb["options"] == [
            "Personal Gmail", "Configure the enterprise SSO integration"]
        # str option → None short (heuristic); dict option → its short.
        assert kb["shorts"] == [None, "SSO"]

    async def test_str_only_ask_omits_shorts_kwarg(self, wired):
        # No agent short anywhere → the keyboard is posted WITHOUT ``shorts``
        # (backward-compatible call shape); the fake records None.
        payload = {
            "engagement_id": wired["rec"].id, "request_id": "d3",
            "engagement_token": wired["rec"].auth_token,
            "question": "Which?", "options": ["A", "B"], "timeout_s": 60,
        }
        await _drive_answer(wired, payload, option_index=0)
        assert wired["ch"].keyboards[-1]["shorts"] is None


class TestInvalidDictShapes:
    """Malformed dict SHAPES still refuse ``invalid_args`` at validation (no
    post); ``short`` length/blank/duplicate is no longer one of them (D1,
    round 4) — see ``TestShortIsAdvisoryNeverRejects`` below."""

    def _invalid(self, options):
        from channels.channel_handlers import _validate_ask_args
        return _validate_ask_args(
            {"question": "q?", "options": options, "timeout_s": 60}) is None

    def test_missing_label_key(self):
        assert self._invalid([{"short": "x"}, "B"])

    def test_label_whitespace_only(self):
        assert self._invalid([{"label": "   ", "short": "ok"}, "B"])

    def test_duplicate_full_labels_refused(self):
        assert self._invalid([
            {"label": "Same", "short": "a"},
            {"label": "Same", "short": "b"},
        ])

    def test_option_wrong_type_refused(self):
        assert self._invalid([123, "B"])

    def test_valid_dict_short_at_25_accepted(self):
        from channels.channel_handlers import _validate_ask_args
        out = _validate_ask_args({
            "question": "q?", "timeout_s": 60,
            "options": [
                {"label": "First full label", "short": "y" * 25},
                {"label": "Second full label", "short": "z"},
            ]})
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["First full label", "Second full label"]
        assert shorts == ["y" * 25, "z"]

    def test_valid_str_only_shorts_all_none(self):
        from channels.channel_handlers import _validate_ask_args
        out = _validate_ask_args({
            "question": "q?", "options": ["A", "B"], "timeout_s": 60})
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["A", "B"]
        assert shorts == [None, None]


class TestShortIsAdvisoryNeverRejects:
    """D1 (round 4): a missing/blank/duplicate/over-budget/non-string
    ``short`` is NEVER a rejection cause — it flows through to the D2
    resolver (which floors the whole button set) instead. Full LABEL
    structural checks (type, non-blank, uniqueness) are unaffected."""

    def _validated(self, options):
        from channels.channel_handlers import _validate_ask_args
        return _validate_ask_args(
            {"question": "q?", "options": options, "timeout_s": 60})

    def test_missing_short_key_accepted_as_absent(self):
        out = self._validated([{"label": "Full label"}, "B"])
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["Full label", "B"]
        assert shorts == [None, None]

    def test_short_over_25_chars_accepted_advisory(self):
        out = self._validated([{"label": "Full", "short": "x" * 26}, "B"])
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["Full", "B"]
        assert shorts == ["x" * 26, None]

    def test_short_whitespace_only_accepted_advisory(self):
        # v0.85.0 (round 4, D4): shorts flow through VERBATIM — no
        # enumerator-strip's incidental ``.strip()`` collapsing this to "".
        out = self._validated([{"label": "Full", "short": "   "}, "B"])
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["Full", "B"]
        assert shorts == ["   ", None]

    def test_short_non_string_treated_as_absent(self):
        out = self._validated([{"label": "Full", "short": 7}, "B"])
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["Full", "B"]
        assert shorts == [None, None]

    def test_duplicate_shorts_accepted_advisory(self):
        out = self._validated([
            {"label": "First full label", "short": "dup"},
            {"label": "Second full label", "short": "dup"},
        ])
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["First full label", "Second full label"]
        assert shorts == ["dup", "dup"]

    def test_label_over_48_chars_accepted(self):
        # D1: the invented FULL-label length cap is gone too (the live Q2
        # failure — a 139-char label was refused pre-round-4).
        out = self._validated([{"label": "x" * 49, "short": "ok"}, "B"])
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["x" * 49, "B"]
        assert shorts == ["ok", None]
