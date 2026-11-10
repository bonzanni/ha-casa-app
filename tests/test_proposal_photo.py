"""Slice 5b: a proposal with a photo, and a receipt written on the tapped card.

An ``operator_proposal`` deposit may carry ``photo`` — a path in the plugin's outbox — and is
then posted as ONE Telegram message: the photo, the composed text as its caption, the keyboard
(the browser plugin's question: the picture and its Yes in one message). The photo is claimed
through the ``photo`` media policy exactly as a delivered file; every later edit of the card is
a caption edit, and a caption edit Telegram refuses still removes the keyboard. A tap's
response may ask for its receipt as the card's last line (``on_card``) instead of a message.

The deposit and the post are driven like ``test_proposal_slot.py``; the channel like
``test_proposal_tap.py``; the tap like ``test_desk_tap.py``.
"""
from __future__ import annotations

import json
import types
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

import pinned_run as pr
import result_broker as rb
import specialist_desk as sd
import tools as tools_mod
import verdict_broker as vb
from text_util import utf16_len
from test_proposal_slot import (LABEL, SLOT, _Manager, _map, _open, _post,  # noqa: F401
                                _proposal)
from test_desk_tap import _tap as _desk_tap, env as desk_env  # noqa: F401
from test_proposal_tap import OPERATOR, RID, _channel, _meta
from two_endpoints import CASA_BOT as BOT

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
PHOTO = "/outbox/fido-0001.png"


class _Outbox:
    """The plugin outbox as the result broker uses it: claim, capture, remove the claim."""

    def __init__(self, data=PNG, refuse=False):
        self.data, self.refuse, self.calls = data, refuse, []

    def claim(self, path):
        self.calls.append(("claim", path))
        if self.refuse:
            from plugin_outbox import OutboxError
            raise OutboxError("missing")
        return f"claim:{path}"

    def capture(self, claim, kind):
        self.calls.append(("capture", kind))
        return self.data

    def remove_claim(self, claim):
        self.calls.append(("remove", claim))


class _PhotoRecorder:
    default_agent, bot_id, has_bot_id, on_identity = "assistant", BOT, True, None
    def __init__(self):
        self.cards: list[dict] = []

    async def deliver_operator_proposal(self, chat_id, text, labels, rid, *, post=None,
                                        photo=None):
        self.cards.append({"text": text, "labels": list(labels), "photo": photo})
        return 601

    async def deliver_desk_notice(self, chat_id, text):
        self.cards.append({"notice": text})
        return True


@pytest.fixture
def penv(monkeypatch):
    rec = _PhotoRecorder()
    outbox = _Outbox()
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(rec), raising=False)
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "finance": SimpleNamespace(character=SimpleNamespace(name="Finance"))})
    monkeypatch.setattr(tools_mod, "outbox_for_current_context", lambda: outbox)
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    monkeypatch.setattr(rb, "POSTS", rb.PostLedger())
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    return SimpleNamespace(rec=rec, outbox=outbox, store=rb.ReferenceStore(now=lambda: 1000.0))


def _live():
    return vb.BROKER.pending(namespace="proposal", scope=f"proposal:{BOT}:42")


# --- the deposit ---------------------------------------------------------------------

def test_a_photo_proposal_is_kept_with_its_path(penv):
    _open(penv.store)
    ref, err = penv.store.deposit(client_id="c1", slot=SLOT,
                                  value=json.dumps(_proposal(photo=PHOTO)))
    assert err is None and penv.store._refs[ref].proposal["photo"] == PHOTO


@pytest.mark.parametrize("over", [
    {"photo": ""}, {"photo": 7}, {"photo": "x" * 1025}, {"photo": "a\nb"},
    {"photo": PHOTO, "pages": ["one"]},
    {"photo": PHOTO, "text": "y" * 958},            # over the caption budget once labelled
], ids=["empty", "not-text", "long-path", "control-char", "with-pages", "caption-too-long"])
def test_the_deposit_refuses_a_bad_photo_proposal(penv, over):
    _open(penv.store)
    assert penv.store.deposit(client_id="c1", slot=SLOT,
                              value=json.dumps(_proposal(**over))) == (None, "bad_proposal")


def test_the_caption_budget_is_the_display_not_the_escapes(penv):
    """The caption Telegram counts is the rendered display: escapes do not count against it."""
    head = rb.post_label("finance")
    budget = rb.PROPOSAL_CAPTION_CHARS - rb.PROPOSAL_SETTLE_RESERVE - utf16_len(head) - 1
    text = "\\." * budget                                   # twice as long in the source
    assert rb.caption_fits(rb.compose_operator_message(text, head))
    assert not rb.caption_fits(rb.compose_operator_message(text + "x", head))


# --- the post ------------------------------------------------------------------------

async def test_the_photo_card_is_one_message_its_photo_claimed_once(penv):
    hook = rb.make_result_hook(_map(), client_id="c1", store=penv.store)
    _open(penv.store)
    ref, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(photo=PHOTO)))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    (card,) = penv.rec.cards
    assert card["photo"] == (PNG, "fido-0001.png")
    assert card["text"] == f"{LABEL}\nPair invoice 17 with the Adobe payment?"
    assert penv.outbox.calls == [("claim", PHOTO), ("capture", "photo"), ("remove", f"claim:{PHOTO}")]
    (rid,) = _live()
    assert vb.BROKER.get_meta(namespace="proposal", scope=f"proposal:{BOT}:42", request_id=rid)["photo"]
    receipt = json.loads(out["hookSpecificOutput"]["updatedToolOutput"])
    assert receipt["casa_delivery"]["status"] == "delivered"


async def test_a_photo_that_cannot_be_claimed_sends_no_card(penv):
    penv.outbox.refuse = True
    hook = rb.make_result_hook(_map(), client_id="c1", store=penv.store)
    _open(penv.store)
    ref, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(photo=PHOTO)))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert penv.rec.cards == [] and _live() == []          # nothing on screen, nothing registered
    assert "casa_result_withheld" in out["hookSpecificOutput"]["updatedToolOutput"]


async def test_a_tell_joins_a_photo_caption_only_within_its_budget(penv):
    """d1 Astra A1: Casa's rewritten-call tell must not push a caption past Telegram's limit."""
    head = rb.post_label("finance")
    room = rb.PROPOSAL_CAPTION_CHARS - rb.PROPOSAL_SETTLE_RESERVE - utf16_len(head) - 1
    call = SimpleNamespace(tool_use_id="call-1")
    post = rb.PostRecord(role="finance", operator_id=42, plugin="probe", slot=SLOT,
                         tool_use_id="call-1", owner="d-1", posted_at=1.0, kind=rb.OPERATOR_PROPOSAL)
    identity = __import__("test_proposal_slot")._identity()
    parsed, _ = rb.proposal_ok(json.dumps(_proposal(photo=PHOTO, text="z" * room)),
                               SimpleNamespace(identity=identity, entry=_map().tools[
                                   "mcp__plugin_probe_api__offer"], contract_map=_map(),
                                   protected={}))
    assert parsed is not None
    ok, *_ = await rb._post_proposal(identity, "probe", SLOT, call, parsed, head, post,
                                     warning="⚠ the call's arguments were changed")
    assert ok
    card, notice = penv.rec.cards
    assert "⚠" not in card["text"] and rb.caption_fits(card["text"])
    assert notice["notice"].endswith("⚠ the call's arguments were changed")


async def test_a_repeated_photo_proposal_consumes_its_staged_photo(penv):
    await rb._settle_repeat("probe", None, rb.OPERATOR_PROPOSAL,
                            json.dumps(_proposal(photo=PHOTO)), None)
    assert penv.outbox.calls == [("claim", PHOTO), ("remove", f"claim:{PHOTO}")]


# --- the channel ---------------------------------------------------------------------

class _PhotoBot:
    def __init__(self):
        self.photos, self.captions, self.texts, self.markups = [], [], [], []
        self.refuse_entities = False
        self.refuse_caption = False

    async def send_photo(self, chat_id, photo, **kw):
        if self.refuse_entities and kw.get("caption_entities"):
            raise BadRequest("can't parse entities")
        self.photos.append(kw)
        return types.SimpleNamespace(message_id=601)

    async def edit_message_caption(self, **kw):
        if self.refuse_caption:
            raise BadRequest("message caption is too long")
        self.captions.append(kw)
        return True

    async def edit_message_text(self, **kw):
        self.texts.append(kw)
        raise BadRequest("there is no text in the message to edit")

    async def edit_message_reply_markup(self, **kw):
        self.markups.append(kw)
        return True


@pytest.fixture
def tg(monkeypatch):
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    ch = _channel()
    ch._app = types.SimpleNamespace(bot=_PhotoBot())
    return SimpleNamespace(ch=ch, bot=ch._app.bot)


async def test_the_channel_sends_the_photo_with_its_caption_and_keyboard(tg):
    mid = await tg.ch.deliver_operator_proposal(OPERATOR, "📊 Finance\n**Pay** €84\\.20?",
                                                ["Yes, pay", "Not now"], RID,
                                                photo=(PNG, "q.png"))
    assert mid == 601
    (sent,) = tg.bot.photos
    assert sent["caption"] == "📊 Finance\nPay €84.20?" and sent["caption_entities"]
    assert [b.text for row in sent["reply_markup"].inline_keyboard for b in row] == [
        "Yes, pay", "Not now"]


async def test_a_refused_caption_entity_resends_the_display_without_entities(tg):
    tg.bot.refuse_entities = True
    assert await tg.ch.deliver_operator_proposal(OPERATOR, "📊 Finance\n**Pay**?", ["Yes"], RID,
                                                 photo=(PNG, "q.png")) == 601
    (sent,) = tg.bot.photos
    assert sent["caption"] == "📊 Finance\nPay?" and sent.get("caption_entities") is None


@pytest.fixture
def tg_bot(tg):
    assert tg.ch.bot is tg.bot                                # the edits use the channel's bot
    return tg


async def test_a_photo_cards_settle_line_is_a_caption_edit(tg_bot):
    meta = _meta(photo=True)
    assert await tg_bot.ch.mark_proposal(meta, "☑ Yes") is True
    (edit,) = tg_bot.bot.captions
    assert edit["caption"].endswith("☑ Yes") and edit["reply_markup"].inline_keyboard in ((), [])
    assert tg_bot.bot.texts == []                             # never a text edit on a photo


async def test_a_refused_caption_edit_still_removes_the_keyboard(tg_bot):
    """d1 Terra: a settled photo card never keeps live buttons."""
    tg_bot.bot.refuse_caption = True
    assert await tg_bot.ch.mark_proposal(_meta(photo=True), "⌛ expired") is False
    (markup,) = tg_bot.bot.markups
    assert markup["message_id"] == 501 and markup["reply_markup"].inline_keyboard in ((), [])


# --- in place, and the receipt on the card -------------------------------------------

async def test_a_photo_card_is_never_replaced_in_place(monkeypatch):
    monkeypatch.setattr(rb, "proposal_ok", lambda value, call: (_proposal(), None))
    build = SimpleNamespace(contract_map=_map(), protected={})
    why = await sd._post_next_card("{}", meta=_meta(photo=True), build=build,
                                   runtime="mcp__plugin_probe_api__apply", seg="probe",
                                   run_id="r1", edit_message_id=501)
    assert why == "photo"


async def test_an_on_card_receipt_is_the_cards_last_line_and_no_message(desk_env):
    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "Done: pay €84.20.", on_card=True))
        return tools_mod.DelegatedOutput(text="")
    desk_env.respond = respond
    await _desk_tap(desk_env)
    assert desk_env.channel.marks == ["☑ Done: pay €84.20."]
    assert desk_env.channel.replies == []                    # no receipt message
    assert desk_env.desk.log[-1].text == "Done: pay €84.20."


async def test_an_on_card_receipt_whose_line_did_not_land_is_posted(desk_env):
    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "Done: pay €84.20.", on_card=True))
        return tools_mod.DelegatedOutput(text="")
    desk_env.respond = respond
    marks = []

    async def failing(meta, line):
        marks.append(line)
        return False
    desk_env.channel.mark_proposal = failing
    await _desk_tap(desk_env)
    assert marks[0] == "☑ Done: pay €84.20."
    (message, _), = desk_env.channel.replies
    assert str(message).endswith("Done: pay €84.20.")


async def test_a_tap_that_did_not_do_its_button_is_never_marked_a_success(desk_env):
    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "Nothing was pressed.", not_done=True))
        return tools_mod.DelegatedOutput(text="")
    desk_env.respond = respond
    await _desk_tap(desk_env)
    assert len(desk_env.channel.marks) == 1
    assert desk_env.channel.marks[0].startswith("✖ ")
    (message, _), = desk_env.channel.replies                 # the receipt still says why
    assert str(message).endswith("Nothing was pressed.")


def test_not_done_is_read_only_as_json_true_and_only_on_a_safe_receipt():
    assert rb._not_done_of(json.dumps({"receipt": "Nothing.", "not_done": True})) is True
    assert rb._not_done_of(json.dumps({"receipt": "Nothing.", "not_done": "yes"})) is False
    assert rb._not_done_of("plain text") is False


def test_on_card_is_read_only_beside_a_receipt_and_never_beside_a_next_card():
    assert rb._receipt_of(json.dumps({"receipt": "Done.", "on_card": True})) == (
        "Done.", "", False, True)
    assert rb._receipt_of(json.dumps({"receipt": "Done.", "on_card": "yes"}))[3] is False
    assert rb._receipt_of(json.dumps({"receipt": "Done.", "on_card": True,
                                      "next": {"text": "t"}}))[3] is False
