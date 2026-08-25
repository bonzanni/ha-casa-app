"""v0.81.0 (W-R3, Sol r1-5) — readable ask buttons; v0.84.0 (round 4, spec D2)
— the button-label half moved to whole-set floor/verbatim semantics.

The original bug: ``post_options_keyboard`` used each FULL option string as the
button label, which Telegram truncates to a few words → most choices were
unpickable, and the full option texts appeared NOWHERE. The fix (still
current):

  1. ONE canonical ``render_ask_body(number, question, options)`` renders the
     full options VERBATIM + numbered in the MESSAGE body. This single helper
     feeds all four ask consumers (initial post, finish-hook settle base,
     persisted ``open_questions[].text``, boot reconciliation) so they can
     never disagree.
  2. Buttons carry a resolved caption; ``callback_data`` identity stays the
     option INDEX.

v0.83.0's per-option ELISION LADDER (``_short_option_label``, deriving a
summary from the full option text) is GONE (D2: "Drop the
``_short_option_label`` elision ladder entirely"). Button labels are now
MODEL-generated: the agent supplies an optional ``short`` per option, resolved
with WHOLE-SET semantics by ``resolve_button_labels`` (see
``tests/test_ask_button_labels.py`` for its exhaustive coverage) — no agent
shorts anywhere (as in the plain-``str``-option calls below) always floors to
the numbered placeholder.

These tests assert: the four consumers render a byte-identical body; every full
option appears verbatim + numbered; a floored keyboard maps to the correct
index; settle appends the BOUNDED positional copy (v0.84.0 D1 bullet 3, never
the full chosen option) below the canonical body; free-text anchors render
without an option list; and the SEPARATE authz-challenge renderer is
untouched.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from broker_helpers import deliver, wait_until
from aiohttp import web
from unittest.mock import AsyncMock, MagicMock

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker
from channels.channel_handlers import (
    _ask_settle_text,
    render_ask_body,
)

# ``asyncio_mode = auto`` (pytest.ini) auto-detects the async tests here; no
# module-level asyncio mark (this file mixes sync helper tests + async ones).


# ---------------------------------------------------------------------------
# render_ask_body — the single canonical body
# ---------------------------------------------------------------------------


class TestRenderAskBody:
    def test_numbered_verbatim_options_below_question(self) -> None:
        body = render_ask_body(
            3, "Which account?", ["Personal Gmail", "Work Outlook"])
        assert body == (
            "Q3: Which account?\n\n1. Personal Gmail\n2. Work Outlook")

    def test_every_full_option_appears_verbatim(self) -> None:
        options = [
            "Set up a brand new dedicated work account",
            "Reuse the existing personal address for now",
            "Ask the finance team which alias to use",
        ]
        body = render_ask_body(2, "How should I proceed?", options)
        # Header carries the canonical durable number.
        assert body.startswith("Q2: How should I proceed?\n\n")
        # EVERY option is present verbatim, 1-based numbered, in order.
        for i, opt in enumerate(options):
            assert f"{i + 1}. {opt}" in body
        assert body.splitlines()[2:] == [
            "1. " + options[0], "2. " + options[1], "3. " + options[2]]

    def test_prepends_qnumber_verbatim(self) -> None:
        # v0.85.0 (round 4, D4): the agent-authored "Q7:" prefix is preserved
        # VERBATIM; Casa only PREPENDS the allocated durable number (parity
        # with _canonical_question).
        body = render_ask_body(1, "Q7: Which DB?", ["A", "B"])
        assert body == "Q1: Q7: Which DB?\n\n1. A\n2. B"

    def test_anchor_no_option_list(self) -> None:
        # Free-text anchor (options == []) renders the numbered question ALONE.
        assert render_ask_body(4, "What's the DB name?", []) == (
            "Q4: What's the DB name?")

    def test_no_number_uses_bare_question(self) -> None:
        assert render_ask_body(None, "Proceed?", ["A", "B"]) == (
            "Proceed?\n\n1. A\n2. B")

    def test_body_stays_well_under_telegram_limit_at_caps(self) -> None:
        # Worst case at _validate_ask_args caps (question 1024, 8 options × 48)
        # is far below Telegram's 4096 — no truncation path needed (Sol r1-5).
        body = render_ask_body(999, "q" * 1024, ["o" * 48 for _ in range(8)])
        assert len(body) < 4096


# ---------------------------------------------------------------------------
# short_option_labels / resolve_button_labels — resolved button labels
# ---------------------------------------------------------------------------


class TestShortOptionLabel:
    """v0.84.0 (round 4, D2): the per-option elision ladder is gone; these pin
    ``short_option_labels``'s whole-set floor/verbatim behaviour for the
    exact shapes this file used to probe with the deleted
    ``_short_option_label``. See ``tests/test_ask_button_labels.py`` for the
    resolver's own exhaustive coverage."""

    def test_short_option_verbatim_when_short_given(self) -> None:
        from channels.telegram import short_option_labels

        assert short_option_labels(["Personal Gmail"], ["Gmail"]) == [
            "1 · Gmail"]

    def test_no_short_floors_to_numbered_placeholder(self) -> None:
        # v0.84.0 (D2): no agent-supplied short → the WHOLE set floors; it
        # never elides the full option text into a garbled summary.
        from channels.telegram import short_option_labels

        label = short_option_labels(
            ["Configure the enterprise SSO integration"])[0]
        assert label == "Option 1"

    def test_number_matches_body_line(self) -> None:
        # The floor's number is the 1-based option position, matching the
        # numbered line in render_ask_body so the operator can
        # cross-reference even without an agent-supplied short.
        from channels.telegram import short_option_labels

        options = ["Personal Gmail", "Work Outlook"]
        body = render_ask_body(1, "Which?", options)
        labels = short_option_labels(options)
        for i, opt in enumerate(options):
            assert labels[i] == f"Option {i + 1}"
            assert f"{i + 1}. {opt}" in body

    def test_caption_within_64_char_cap_when_short_given(self) -> None:
        from channels.telegram import _ASK_BUTTON_CAPTION_CAP, short_option_labels

        label = short_option_labels(["Full option text"], ["SSO"])[0]
        assert label == "1 · SSO"
        assert len(label) <= _ASK_BUTTON_CAPTION_CAP


# ---------------------------------------------------------------------------
# post_options_keyboard — body verbatim, resolved labels, index identity
# ---------------------------------------------------------------------------


class TestPostOptionsKeyboardReadable:
    def _channel(self):
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(return_value=MagicMock(topic_id=42))
        ch.send_to_topic = AsyncMock(return_value=101)
        return ch

    async def test_body_posted_verbatim_floors_without_shorts_index_identity(
        self,
    ) -> None:
        ch = self._channel()
        options = ["Personal Gmail", "Configure the enterprise SSO integration"]
        body = render_ask_body(1, "Which account?", options)

        await ch.post_options_keyboard(
            engagement_id="x" * 32, request_id="rid",
            question=body, options=options)

        # The MESSAGE body is posted verbatim (full options live here).
        args, _ = ch.send_to_topic.call_args
        assert args[1] == body
        assert "Personal Gmail" in args[1]
        assert "Configure the enterprise SSO integration" in args[1]

        rows = ch.send_to_topic.call_args.kwargs["reply_markup"].inline_keyboard
        # v0.84.0 (D2): no agent-supplied ``short`` per option → the WHOLE set
        # floors to the numbered placeholder (never a garbled elision).
        assert [r[0].text for r in rows] == ["Option 1", "Option 2"]
        # callback_data identity is the option INDEX (unchanged schema).
        assert [r[0].callback_data for r in rows] == [
            "v1|engagement_ask|rid|0", "v1|engagement_ask|rid|1"]

    async def test_agent_shorts_render_verbatim_when_whole_set_usable(
        self,
    ) -> None:
        ch = self._channel()
        options = ["Personal Gmail", "Configure the enterprise SSO integration"]
        shorts = ["Gmail", "SSO"]
        body = render_ask_body(1, "Which account?", options)

        await ch.post_options_keyboard(
            engagement_id="x" * 32, request_id="rid",
            question=body, options=options, shorts=shorts)

        rows = ch.send_to_topic.call_args.kwargs["reply_markup"].inline_keyboard
        assert [r[0].text for r in rows] == ["1 · Gmail", "2 · SSO"]


# ---------------------------------------------------------------------------
# _ask_settle_text — BOUNDED positional copy appended below the canonical body
# ---------------------------------------------------------------------------


class TestSettleAppendsFullOption:
    def test_answered_appends_positional_copy_below_body(self) -> None:
        options = ["Personal Gmail", "Configure the enterprise SSO integration"]
        body = render_ask_body(1, "Which account?", options)
        settled = _ask_settle_text(
            body, {"outcome": "answered", "option_index": 1}, options)
        # Base body preserved byte-for-byte; the BOUNDED POSITION (not the
        # full option label) appended below with a ✅ (v0.84.0 D1 bullet 3).
        assert settled == body + "\n✅ Option 2"
        assert settled.startswith(body)


# ---------------------------------------------------------------------------
# Byte-identity across the four consumers (handler-driven)
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self) -> None:
        self.options_keyboards: list[dict] = []
        self.edits: list[dict] = []
        self._next = 7000

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options,
    ) -> int:
        self.options_keyboards.append(
            {"question": question, "options": list(options)})
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


async def test_body_identical_across_post_persist_and_settle(
    tmp_path, monkeypatch,
) -> None:
    """The initial post body, the persisted ``open_questions[].text`` and the
    finish-hook settle BASE are the SAME render_ask_body output — one source,
    three consumers agree byte-for-byte; the fourth (reconcile) reads the
    persisted text and is covered below."""
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    # driver=None ⇒ eager fallback (no relay), simplest deterministic post path.
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", None)

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    eid = rec.id
    ch = _FakeChannel()
    handlers = _make_channel_handlers(
        telegram_channel=ch, engagement_registry=reg)
    ask = handlers["/internal/channel/ask"]

    options = ["Personal Gmail", "Work Outlook"]
    payload = {
        "engagement_id": eid, "request_id": "a1",
        "engagement_token": rec.auth_token,
        "question": "Which account?", "options": options, "timeout_s": 60,
    }
    task = asyncio.ensure_future(ask(_FakeRequest(payload)))
    # #731: wait on the condition delivery actually needs — the broker
    # registration of ("engagement_ask", eid, "a1") — not a fixed sleep (a
    # loaded worker delivered before registration and got 'stale') and not
    # the keyboard-post append (the post races the registration). The handler
    # reaches ensure_posted with no await after BROKER.register, so once the
    # key is live the setup task exists and set_finish_hook fires
    # retroactively on an already-resolved future — the post and settle
    # asserts below hold even for this earliest possible delivery.
    await wait_until(
        lambda: "a1" in fresh.pending(namespace="engagement_ask", scope=eid))
    assert deliver(fresh,
        namespace="engagement_ask", scope=eid, request_id="a1",
        option_index=0, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    await fresh.drain_hooks()
    assert json.loads(resp.text)["ok"] is True

    expected = render_ask_body(1, "Which account?", options)
    # (1) initial post body.
    assert ch.options_keyboards[-1]["question"] == expected
    # (2) persisted open_questions[].text.
    persisted = reg.get(eid).open_questions
    # (settled entries are removed on close, so read the text before close via
    # the settle edit; the persisted text was the same source — assert the
    # settle base derived from it).
    # (3) settle base == body + BOUNDED positional copy (v0.84.0 D1 bullet 3;
    # option_index=0 is position 1, never the chosen label).
    assert ch.edits[-1]["text"] == expected + "\n✅ Option 1"
    assert ch.edits[-1]["clear_keyboard"] is True
    # The persisted ledger is now empty (question settled), proving the close
    # path ran over the SAME entry; the persisted text identity is asserted in
    # the reconcile test below where the entry survives.
    assert list(persisted) == []


async def test_delivery_survives_slow_registration(tmp_path, monkeypatch) -> None:
    """Red case for #731: delivery must synchronize on the broker registration
    of ``("engagement_ask", eid, "a1")`` — the condition delivery actually
    needs — not on a fixed sleep. The ask handler awaits
    ``allocate_question_number`` BEFORE it reaches ``BROKER.register``
    (channel_handlers._maybe_allocate_number), so delaying the allocator by
    50 ms deterministically postpones registration past any 20 ms bet: with a
    fixed-sleep synchronization the delivery claims an unregistered key and
    the broker answers ``"stale"`` (assert "stale" == "delivered"). A
    condition wait makes the same drive pass regardless of allocator latency.

    The delay is injected at the CLASS attribute because the registry instance
    is created inside the driven test; the patch is monkeypatch-scoped, so no
    other test sees it. It wraps the real allocator — never
    ``<module>.asyncio.sleep`` (memory-cage rule)."""
    from engagement_registry import EngagementRegistry

    calls = 0
    real_allocate = EngagementRegistry.allocate_question_number

    async def delayed_allocate(self, engagement_id):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return await real_allocate(self, engagement_id)

    monkeypatch.setattr(
        EngagementRegistry, "allocate_question_number", delayed_allocate)

    await test_body_identical_across_post_persist_and_settle(
        tmp_path, monkeypatch)
    assert calls == 1


async def test_reconcile_settles_over_persisted_render_ask_body(
    tmp_path,
) -> None:
    """The FOURTH consumer: boot reconciliation edits the expired copy OVER the
    persisted ``open_questions[].text`` — which is exactly render_ask_body's
    output — so the option list survives a restart-time settle."""
    from drivers.claude_code_driver import ClaudeCodeDriver
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t", {}, topic_id=999)

    options = ["Personal Gmail", "Work Outlook"]
    body = render_ask_body(1, "Which account?", options)
    n1 = await reg.allocate_question_number(rec.id)
    # Persist EXACTLY what the ask handler persists (render_ask_body output).
    await reg.add_open_question(rec.id, n1, 7001, text=body, kind="button")

    edits: list = []

    async def _edit(topic_id, message_id, text, *, clear_keyboard=False):
        edits.append(text)
        return True

    drv = ClaudeCodeDriver(
        engagements_root=str(tmp_path / "engagements"),
        send_to_topic=AsyncMock(),
        casa_framework_mcp_url="http://x",
        edit_topic_message=_edit,
        registry=reg,
    )
    await drv.reconcile_open_questions(rec)

    # The reconcile settle copy is the persisted body (full options) + suffix —
    # the option list is NOT dropped.
    assert edits == [body + "\n⌛ expired — answer by text below"]
    assert "1. Personal Gmail" in edits[0]
    assert "2. Work Outlook" in edits[0]


# ---------------------------------------------------------------------------
# The SEPARATE authz-challenge renderer is UNTOUCHED
# ---------------------------------------------------------------------------


def test_authz_challenge_renderer_untouched() -> None:
    """authz_grants renders its own challenge body (render_challenge_message)
    and posts via post_dm_keyboard — a SEPARATE path that must not share the
    ask body/label code changed here."""
    import authz_grants

    # The authz challenge renderer exists and is independent.
    assert hasattr(authz_grants, "render_challenge_message")
    # It does NOT import or depend on the ask-body helper.
    assert not hasattr(authz_grants, "render_ask_body")
    assert not hasattr(authz_grants, "_short_option_label")
    src = authz_grants.render_challenge_message.__module__
    assert src == "authz_grants"
