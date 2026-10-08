"""#1339: a ``safe`` tap tool whose response carries ``"in_place": true`` beside
its ``receipt`` and its ``next`` card asks Casa to show that card IN PLACE of the
tapped one: the tapped message is edited to the new text and buttons, and the
receipt is not posted. The card is judged and registered exactly as a #1302
next card; a card that cannot replace the tapped one falls back to today's
sequence — the receipt, then the card as a new message.

The desk use is ``test_desk_tap.py``'s fixture, extended by
``test_tap_next_card.py``'s ``card_env`` (a real ``_post_proposal`` against a
real ``VerdictBroker``, the channel faked at its send) with the edit.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError

import pinned_run as pr
import result_broker as rb
import tools as tools_mod
import verdict_broker as vb
from channels import UnconfirmedDelivery
from test_desk_tap import LABEL, OPERATOR, _echo, _tap, env  # noqa: F401
from test_tap_next_card import _captured, _card, _live, _meta_of, card_env  # noqa: F401

TAPPED = 501                                   # the fixture's tapped message id


@pytest.fixture
def edit_env(card_env):
    """``card_env`` plus the in-place edit and the finish hooks' log."""
    env = card_env
    env.edits = []
    env.edit_result = "landed"                 # | "refused" | "unconfirmed"
    env.settled = []
    ch = env.channel

    async def replace_operator_proposal(chat_id, message_id, text, labels, rid, *, post=None):
        env.log.append(("edit", text))
        env.edits.append(SimpleNamespace(chat_id=chat_id, message_id=message_id, text=text,
                                         labels=list(labels), rid=rid, post=post))
        if env.edit_result == "unconfirmed":
            raise UnconfirmedDelivery("TimedOut")
        return message_id if env.edit_result == "landed" else None

    def proposal_finish_hook(*, rid, req):
        async def _finish(outcome):
            env.settled.append((rid, (req.meta or {}).get("message_id"), outcome))
        return _finish

    ch.replace_operator_proposal = replace_operator_proposal
    ch.proposal_finish_hook = proposal_finish_hook
    return env


def _respond(receipt="All quarters on.", nxt=None, in_place=True, rewritten=False):
    async def respond(call):
        call.owner.resolve(pr.Capture(
            "receipt", receipt, rewritten,
            next=json.dumps(nxt, ensure_ascii=False) if nxt is not None else "",
            in_place=in_place))
        return tools_mod.DelegatedOutput(text="")
    return respond


# --- the capture -------------------------------------------------------------------

def test_in_place_true_beside_a_receipt_and_a_next_object_is_captured():
    cap = _captured("safe", json.dumps({"receipt": "done", "next": _card(), "in_place": True}))
    assert (cap.kind, cap.text, cap.in_place) == ("receipt", "done", True)
    assert json.loads(cap.next) == _card()


@pytest.mark.parametrize("response", [
    {"receipt": "done", "next": _card()},
    {"receipt": "done", "next": _card(), "in_place": "true"},
    {"receipt": "done", "next": _card(), "in_place": 1},
    {"receipt": "done", "next": _card(), "in_place": False},
    {"receipt": "done", "in_place": True},
    {"receipt": "done", "next": None, "in_place": True},
    {"receipt": " ", "next": _card(), "in_place": True},
])
def test_in_place_is_read_only_as_json_true_beside_a_usable_receipt_and_a_next_object(response):
    assert _captured("safe", json.dumps(response)).in_place is False


def test_the_more_no_post_shape_never_carries_in_place():
    cap = _captured("more", json.dumps({"proposal": None, "receipt": "no more",
                                        "next": _card(), "in_place": True}))
    assert (cap.kind, cap.in_place) == ("no_post", False)


# --- the desk use: in place ---------------------------------------------------------

async def test_the_tapped_card_is_edited_in_place_and_nothing_is_sent(edit_env):
    env = edit_env
    registered_at_edit = []
    inner = env.channel.replace_operator_proposal

    async def checking(chat_id, message_id, text, labels, rid, *, post=None):
        # bound and filed BEFORE the edit goes out (d1, both reviewers)
        registered_at_edit.append((_live() == [rid], _meta_of(rid)["message_id"],
                                   rb.POST_MAP.get(OPERATOR, TAPPED) is post))
        return await inner(chat_id, message_id, text, labels, rid, post=post)
    env.channel.replace_operator_proposal = checking
    env.respond = _respond(nxt=_card())
    await _tap(env)
    assert [k for k, _ in env.log] == ["edit"]               # no receipt, no new card
    assert env.proposals == []
    assert registered_at_edit == [(True, TAPPED, True)]
    (edit,) = env.edits
    assert (edit.chat_id, edit.message_id) == (OPERATOR, TAPPED)
    assert edit.text.startswith(LABEL) and "INV-88" in edit.text
    assert edit.labels == ["Confirm", "Wrong"]
    (rid,) = _live()
    assert rid == edit.rid
    meta = _meta_of(rid)
    assert meta["message_id"] == TAPPED and meta["revision"] == "walk-1"
    assert (meta["chat_id"], meta["operator_id"], meta["role"]) == (OPERATOR, OPERATOR, "finance")
    post = rb.POST_MAP.get(OPERATOR, TAPPED)
    assert (post.role, post.kind) == ("finance", rb.OPERATOR_PROPOSAL)
    # the desk exchange and the echo are today's
    assert [(e.who, e.text) for e in env.desk.log] == [("operator", "[tapped: Yes]"),
                                                      ("specialist", "All quarters on.")]
    assert any("applied your tap (Yes)" in line for line in _echo())
    assert env.channel.notices == []


async def test_a_rewritten_call_carries_its_tell_on_the_edited_card(edit_env):
    env = edit_env
    env.respond = _respond(nxt=_card(), rewritten=True)
    await _tap(env)
    (edit,) = env.edits
    assert "installed hook" in edit.text
    assert env.channel.replies == []


@pytest.mark.parametrize("respond", [
    _respond(nxt=_card(), in_place=False),
    _respond(nxt=None, in_place=True),
])
async def test_without_in_place_and_a_next_card_the_tap_is_todays(edit_env, respond):
    env = edit_env
    env.respond = respond
    await _tap(env)
    assert env.edits == []
    assert [k for k, _ in env.log][:1] == ["receipt"]


# --- the fallback: today's sequence, visibly ----------------------------------------

async def test_a_refused_edit_falls_back_to_the_receipt_and_a_new_card(edit_env):
    env = edit_env
    env.edit_result = "refused"
    env.respond = _respond(nxt=_card())
    await _tap(env)
    assert [k for k, _ in env.log] == ["edit", "receipt", "card"]
    (edit,) = env.edits
    (card,) = env.proposals
    assert _live() == [card.rid]                              # the edit's record is gone
    assert edit.rid != card.rid
    assert env.settled == []                                  # unregistered: no hook fired


async def test_an_unconfirmed_edit_keeps_its_bound_record_and_the_fallback_supersedes_it(edit_env):
    env = edit_env
    env.edit_result = "unconfirmed"
    env.respond = _respond(nxt=_card())                      # revision "walk-1"
    await _tap(env)
    assert [k for k, _ in env.log] == ["edit", "receipt", "card"]
    (edit,) = env.edits
    (card,) = env.proposals
    assert _live() == [card.rid]
    # the in-place record was superseded WITH its message id bound, so its finish
    # hook can mark the message (d1 S1, both reviewers)
    import asyncio
    await asyncio.sleep(0)
    assert [(rid, mid) for rid, mid, _ in env.settled] == [(edit.rid, TAPPED)]


async def test_an_unconfirmed_edit_without_a_revision_leaves_both_cards_working(edit_env):
    env = edit_env
    env.edit_result = "unconfirmed"
    env.respond = _respond(nxt=_card(revision=None))
    await _tap(env)
    (edit,) = env.edits
    (card,) = env.proposals
    assert sorted(_live()) == sorted([edit.rid, card.rid])
    assert _meta_of(edit.rid)["message_id"] == TAPPED
    assert rb.POST_MAP.get(OPERATOR, TAPPED) is not None      # swipe-replies still route


async def test_an_invalid_card_falls_back_to_the_receipt_and_todays_notice(edit_env):
    env = edit_env
    env.respond = _respond(nxt=_card(buttons=[]))
    await _tap(env)
    assert env.edits == []
    assert [k for k, _ in env.log] == ["receipt"]
    assert any("could not show the next card (invalid)" in t for _, t in env.channel.notices)


async def test_a_tapped_card_without_a_message_id_falls_back(edit_env):
    env = edit_env
    env.respond = _respond(nxt=_card())
    await _tap(env, meta={"message_id": None})
    assert env.edits == []
    assert [k for k, _ in env.log] == ["receipt", "card"]


# --- the channel's edit -------------------------------------------------------------

def _channel_with_bot():
    from test_telegram_topic_stream import _mk_channel_with_fake_bot
    return _mk_channel_with_fake_bot()


async def test_the_channel_edits_the_message_with_the_new_keyboard():
    ch, bot = _channel_with_bot()
    mid = await ch.replace_operator_proposal(OPERATOR, TAPPED, "📊 Finance\nPair **17**?",
                                             ["Yes", "No"], "rid9")
    assert mid == TAPPED
    kw = bot.edit_message_text.await_args.kwargs
    assert (kw["chat_id"], kw["message_id"]) == (OPERATOR, TAPPED)
    assert kw["text"] == "📊 Finance\nPair 17?" and kw["entities"]
    kbd = kw["reply_markup"]
    assert isinstance(kbd, InlineKeyboardMarkup)
    assert [r[0].callback_data for r in kbd.inline_keyboard] == ["v1|proposal|rid9|0",
                                                                 "v1|proposal|rid9|1"]
    assert bot.send_message.await_count == 0


async def test_the_channel_reports_a_refused_edit_as_nothing_landed():
    ch, bot = _channel_with_bot()
    bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    assert await ch.replace_operator_proposal(OPERATOR, TAPPED, "plain", ["Ok"], "r") is None


async def test_the_channel_reports_a_lost_link_as_unconfirmed():
    ch, bot = _channel_with_bot()
    bot.edit_message_text.side_effect = NetworkError("connection reset")
    with pytest.raises(UnconfirmedDelivery):
        await ch.replace_operator_proposal(OPERATOR, TAPPED, "plain", ["Ok"], "r")
