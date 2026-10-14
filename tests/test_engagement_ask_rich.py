"""R2b+c (v0.89.0) — ask/anchor bodies + lifecycle edits render rich.

The engagement ``ask`` (tappable option buttons) and free-text ``anchor``
bodies, plus their lifecycle edits (settle/answer/expire/withdraw), used to be
posted/edited as PLAIN text, so markdown (``**bold**``, `` `code` ``) leaked
literal markers. This routes them through the rich renderer.

The BINDING invariant (round-4 single-post cancellation gate): the ask poster
(``post_options_keyboard``) and the free-text-anchor poster
(``_post_anchor``) must make EXACTLY ONE physical Bot API send attempt. So the
POST path uses ``post_ask_body_rich`` — a SINGLE-ATTEMPT rich send that FAILS
CLOSED on an entity ``BadRequest`` (no plain retry, no second send) — NOT
``send_to_topic_rich``/``send_response_to_topic`` (whose rich→plain fallback is
a second send). Lifecycle EDITS are not cancellation-gated, so they route
through ``edit_topic_message_rich`` (render → plain fallback is fine for edits).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from broker_helpers import deliver
from telegram import InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker

from test_telegram_topic_stream import _mk_channel_with_fake_bot
from test_engagement_ask_lifecycle import _FakeDriver, _FakeRequest, _body

pytestmark = pytest.mark.asyncio


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    """Poll *predicate* until it holds, bounded by WALL-CLOCK time (not a
    fixed real-time sleep), so a slow/loaded runner gets real slack while a
    fast run resolves in a handful of scheduler turns. Cheap when fast,
    tolerant when slow, and no weaker than the fixed sleep it replaces — it
    still raises (``TimeoutError``) if the condition never fires."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


# ===========================================================================
# Part (b) · primitive: post_ask_body_rich — SINGLE-ATTEMPT rich send
# ===========================================================================

async def test_post_ask_body_rich_renders_entities():
    ch, bot = _mk_channel_with_fake_bot()
    mid = await ch.post_ask_body_rich(42, "**Deploy** now?")
    assert bot.send_message.await_count == 1  # exactly ONE physical send
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "Deploy now?"
    assert kw["message_thread_id"] == 42
    assert kw["entities"][0].type == MessageEntity.BOLD
    assert mid == 12345


async def test_post_ask_body_rich_plain_sends_raw_no_entities():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.post_ask_body_rich(42, "just a question?")
    assert bot.send_message.await_count == 1
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "just a question?"
    assert "entities" not in kw


async def test_post_ask_body_rich_fails_closed_no_second_send():
    """The load-bearing invariant: an entity BadRequest FAILS CLOSED — it
    propagates and there is NO plain retry (that second send is exactly what
    the round-4 cancellation gate forecloses)."""
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message.side_effect = BadRequest("bad entity")
    with pytest.raises(BadRequest):
        await ch.post_ask_body_rich(42, "**Deploy** now?")
    assert bot.send_message.await_count == 1  # ONE attempt, no fallback


async def test_post_ask_body_rich_101_spans_sends_raw_body_once():
    """>100 spans ⇒ render() strips to entities=None ⇒ the ORIGINAL RAW body is
    sent plain in exactly ONE attempt (never the marker-stripped display)."""
    ch, bot = _mk_channel_with_fake_bot()
    body = " ".join(f"**b{i}**" for i in range(101))  # 101 bold spans
    await ch.post_ask_body_rich(42, body)
    assert bot.send_message.await_count == 1
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == body  # RAW body, markers intact
    assert "entities" not in kw


async def test_post_ask_body_rich_forwards_reply_markup_on_both_paths():
    kbd = InlineKeyboardMarkup([])
    # entities path
    ch, bot = _mk_channel_with_fake_bot()
    await ch.post_ask_body_rich(42, "**bold**", reply_markup=kbd)
    assert bot.send_message.await_args.kwargs["reply_markup"] is kbd
    assert "entities" in bot.send_message.await_args.kwargs
    # plain path
    ch2, bot2 = _mk_channel_with_fake_bot()
    await ch2.post_ask_body_rich(42, "plain", reply_markup=kbd)
    assert bot2.send_message.await_args.kwargs["reply_markup"] is kbd
    assert "entities" not in bot2.send_message.await_args.kwargs


def _fake_registry_with_topic(topic_id: int = 42):
    reg = MagicMock()
    reg.get = MagicMock(return_value=MagicMock(topic_id=topic_id))
    return reg


async def test_post_options_keyboard_renders_entities():
    ch, bot = _mk_channel_with_fake_bot()
    ch._engagement_registry = _fake_registry_with_topic()
    fresh = VerdictBroker()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(verdict_broker, "BROKER", fresh)
        mid = await ch.post_options_keyboard(
            engagement_id="e1", request_id="r1",
            question="Q1: **Deploy** now?\n\n1. Yes\n2. No",
            options=["Yes", "No"],
        )
    assert bot.send_message.await_count == 1  # single physical send
    kw = bot.send_message.await_args.kwargs
    assert "**" not in kw["text"]  # markers rendered, not literal
    assert any(e.type == MessageEntity.BOLD for e in kw["entities"])
    assert isinstance(kw["reply_markup"], InlineKeyboardMarkup)
    assert mid == 12345


async def test_post_options_keyboard_forwards_reply_to():
    """#332: a deferred ask that is the turn's first output threads to the
    inbound operator message — the poster passes the consumed turn reply
    target through as reply_to."""
    ch, bot = _mk_channel_with_fake_bot()
    ch._engagement_registry = _fake_registry_with_topic()
    fresh = VerdictBroker()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(verdict_broker, "BROKER", fresh)
        await ch.post_options_keyboard(
            engagement_id="e1", request_id="r1",
            question="Q1: Deploy now?\n\n1. Yes\n2. No",
            options=["Yes", "No"], reply_to=555,
        )
    kw = bot.send_message.await_args.kwargs
    assert kw["reply_parameters"].message_id == 555
    # #1434: a deleted inbound message must not fail the ask.
    assert kw["reply_parameters"].allow_sending_without_reply is True
    # and omitting it adds nothing
    ch2, bot2 = _mk_channel_with_fake_bot()
    ch2._engagement_registry = _fake_registry_with_topic()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(verdict_broker, "BROKER", VerdictBroker())
        await ch2.post_options_keyboard(
            engagement_id="e1", request_id="r1",
            question="Q", options=["Yes", "No"],
        )
    assert "reply_parameters" not in bot2.send_message.await_args.kwargs


async def test_post_options_keyboard_plain_body_sends_raw():
    ch, bot = _mk_channel_with_fake_bot()
    ch._engagement_registry = _fake_registry_with_topic()
    fresh = VerdictBroker()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(verdict_broker, "BROKER", fresh)
        await ch.post_options_keyboard(
            engagement_id="e1", request_id="r1",
            question="Q1: Deploy now?\n\n1. Yes\n2. No",
            options=["Yes", "No"],
        )
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "Q1: Deploy now?\n\n1. Yes\n2. No"
    assert "entities" not in kw
    assert isinstance(kw["reply_markup"], InlineKeyboardMarkup)


# ===========================================================================
# Handler-level env: REAL cancellation gate + REAL registry, real channel
# over a fake bot (counts PHYSICAL Bot API send attempts).
# ===========================================================================

@pytest.fixture
async def real_env(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers
    from channels.telegram import TelegramChannel

    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=7777))
    fake_bot.edit_message_text = AsyncMock(return_value=True)
    fake_app = MagicMock()
    fake_app.bot = fake_bot
    ch = TelegramChannel(
        bot_token="x:y", chat_id=100, default_agent="assistant",
        engagement_supergroup_id=-1001)
    ch._app = fake_app
    ch._engagement_registry = reg

    driver = _FakeDriver()
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)
    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    return {
        "reg": reg, "rec": rec, "ch": ch, "bot": fake_bot, "driver": driver,
        "broker": fresh, "ask": handlers["/internal/channel/ask"],
    }


def _payload(**over):
    base = {
        "engagement_id": "PLACEHOLDER", "request_id": "rid-1",
        "question": "Proceed?", "options": ["A", "B"], "timeout_s": 60,
        "projection_hash": "hash-abc",
    }
    base.update(over)
    return base


# --- Part (b) · anchor single physical send -------------------------------

async def test_anchor_rich_body_single_physical_send(real_env):
    """A free-text anchor with markdown posts in EXACTLY ONE physical send,
    carrying entities (markers rendered, not literal)."""
    eid = real_env["rec"].id
    resp = await real_env["ask"](_FakeRequest(_payload(
        engagement_id=eid, engagement_token=real_env["rec"].auth_token,
        request_id="ft1",
        question="Pick the **prod** DB?", options=[])))
    assert _body(resp)["ok"] is True
    await asyncio.sleep(0.01)  # drive the simulated relay-deferred post
    bot = real_env["bot"]
    assert bot.send_message.await_count == 1  # ONE physical Bot API send
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "Q1: Pick the prod DB?"  # markers rendered away
    assert any(e.type == MessageEntity.BOLD for e in kw["entities"])


async def test_anchor_rich_badrequest_fails_closed_single_send(real_env):
    """The BINDING single-post invariant: an entity BadRequest on the anchor
    post FAILS CLOSED — exactly ONE physical send attempt (no rich→plain
    double-send), and the handler reports failure rather than posting twice."""
    real_env["bot"].send_message.side_effect = BadRequest("bad entity")
    eid = real_env["rec"].id
    resp = await real_env["ask"](_FakeRequest(_payload(
        engagement_id=eid, engagement_token=real_env["rec"].auth_token,
        request_id="ft2",
        question="Pick the **prod** DB?", options=[])))
    await asyncio.sleep(0.01)
    assert real_env["bot"].send_message.await_count == 1  # NO second send
    assert _body(resp)["ok"] is False


# --- Part (c) · single-select settle edit renders rich (the :1340 path) ----

async def test_single_select_settle_edit_renders_rich(real_env):
    """A settled single-select ask's finish-hook edit (channel_handlers:1340)
    routes through the RICH edit primitive — no literal ``**`` after answer."""
    eid = real_env["rec"].id
    task = asyncio.ensure_future(real_env["ask"](_FakeRequest(_payload(
        engagement_id=eid, engagement_token=real_env["rec"].auth_token,
        request_id="s1",
        question="Deploy **prod**?", options=["Yes", "No"]))))
    # Await the actual posted-and-live boundary (keyboard physically sent AND
    # the request live+unclaimed in the broker) instead of a fixed real-time
    # sleep — on a slow/loaded runner the handler's pre-register awaits can
    # outlast a tight sleep and the deliver below would report "stale" for a
    # not-yet-live key; delivering before the keyboard post would settle an
    # ask with no message to edit.
    await _wait_until(lambda: real_env["bot"].send_message.await_count >= 1
                      and real_env["broker"].is_live_unclaimed(
                          namespace="engagement_ask", scope=eid,
                          request_id="s1"))
    assert deliver(real_env["broker"], 
        namespace="engagement_ask", scope=eid, request_id="s1",
        option_index=0, actor_id=555) == "delivered"
    await asyncio.wait_for(task, timeout=1.0)
    await real_env["broker"].drain_hooks()

    bot = real_env["bot"]
    assert bot.edit_message_text.await_count >= 1
    kw = bot.edit_message_text.await_args.kwargs
    assert "**" not in kw["text"]  # rendered, not literal
    assert any(e.type == MessageEntity.BOLD for e in kw["entities"])
    # The settle STILL clears the keyboard (S1 fix retained through the rich path).
    assert isinstance(kw["reply_markup"], InlineKeyboardMarkup)
    assert list(kw["reply_markup"].inline_keyboard) == []


# ===========================================================================
# Part (c) · MULTI settle: edit_topic_message_markup renders the body rich
# (settle_ask_keyboard → edit_discrete → edit_topic_message_markup)
# ===========================================================================

async def test_edit_markup_renders_entities_markup_touched():
    """A multi settle (text + explicit-empty keyboard) renders markdown as
    entities — the keyboard is still cleared."""
    from channels.output_sequencer import MARKUP_EMPTY
    ch, bot = _mk_channel_with_fake_bot()
    ok = await ch.edit_topic_message_markup(
        42, 999, "Deploy **prod**?\n✅ Options 1, 2", MARKUP_EMPTY)
    assert ok is True
    kw = bot.edit_message_text.await_args.kwargs
    assert "**" not in kw["text"]
    assert any(e.type == MessageEntity.BOLD for e in kw["entities"])
    assert isinstance(kw["reply_markup"], InlineKeyboardMarkup)
    assert list(kw["reply_markup"].inline_keyboard) == []


async def test_edit_markup_renders_entities_text_only():
    """A text-only edit (markup ABSENT — keyboard untouched) also renders rich,
    and does NOT pass a reply_markup (PTB leaves the keyboard in place)."""
    from channels.output_sequencer import _ABSENT
    ch, bot = _mk_channel_with_fake_bot()
    ok = await ch.edit_topic_message_markup(42, 999, "now `code`", _ABSENT)
    assert ok is True
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "now code"
    assert kw["entities"][0].type == MessageEntity.CODE
    assert "reply_markup" not in kw


async def test_edit_markup_marker_literal_sends_raw_no_entities():
    """Regression: the re-anchor MOVED marker literal has no markdown metachars
    ⇒ render() returns entities=None ⇒ the RAW text is edited verbatim
    (identical output to the pre-R2c plain edit)."""
    from channels.output_sequencer import MARKUP_EMPTY
    ch, bot = _mk_channel_with_fake_bot()
    marker = "⤵ MOVED Q3 — answer the current copy below"
    await ch.edit_topic_message_markup(42, 999, marker, MARKUP_EMPTY)
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == marker
    assert "entities" not in kw


async def test_edit_markup_markup_only_render_not_invoked():
    """Regression: text=None ⇒ markup-only edit via edit_message_reply_markup;
    render() is never invoked and edit_message_text is never called."""
    from channels.output_sequencer import MARKUP_EMPTY
    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    ok = await ch.edit_topic_message_markup(42, 999, None, MARKUP_EMPTY)
    assert ok is True
    assert bot.edit_message_reply_markup.await_count == 1
    assert bot.edit_message_text.await_count == 0


async def test_edit_markup_badrequest_falls_back_to_plain_once():
    """An EDIT is not the cancellation-gated POST, so an entity BadRequest may
    retry ONCE plain with the ORIGINAL raw text."""
    from channels.output_sequencer import MARKUP_EMPTY
    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_text.side_effect = [BadRequest("bad entity"), None]
    ok = await ch.edit_topic_message_markup(42, 999, "Deploy **prod**?", MARKUP_EMPTY)
    assert ok is True
    assert bot.edit_message_text.await_count == 2
    last = bot.edit_message_text.await_args_list[-1].kwargs
    assert last["text"] == "Deploy **prod**?"  # ORIGINAL, verbatim
    assert "entities" not in last


async def test_edit_markup_not_modified_is_success():
    from channels.output_sequencer import MARKUP_EMPTY
    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_text.side_effect = BadRequest("Bad Request: message is not modified")
    ok = await ch.edit_topic_message_markup(42, 999, "Deploy **prod**?", MARKUP_EMPTY)
    assert ok is True
    assert bot.edit_message_text.await_count == 1  # not-modified ⇒ no plain retry


async def test_multi_settle_via_edit_discrete_renders_rich():
    """The REAL settle path: OutputSequencer.edit_discrete → the channel's
    edit_topic_message_markup wire (mirroring casa_core's closure) renders the
    settled body's markdown as entities — no literal ** on settle."""
    from channels.output_sequencer import OutputSequencer, MARKUP_EMPTY
    ch, bot = _mk_channel_with_fake_bot()

    async def _send_markup(topic, text, markup, reply_to=None):
        return await ch.send_topic_message_markup(topic, text, markup, reply_to=reply_to)

    async def _edit_markup(topic, mid, text, markup):
        return await ch.edit_topic_message_markup(topic, mid, text, markup)

    seq = OutputSequencer(
        engagement_id="e1", topic_id=42,
        send_message=None, edit_message=None,
        send_message_markup=_send_markup, edit_message_markup=_edit_markup)
    ok = await seq.edit_discrete(
        999, text="Deploy **prod**?\n✅ Options 1, 2", markup=MARKUP_EMPTY)
    assert ok is True
    kw = bot.edit_message_text.await_args.kwargs
    assert "**" not in kw["text"]
    assert any(e.type == MessageEntity.BOLD for e in kw["entities"])
