"""#1377: an ``operator_proposal`` deposit may carry ``pages`` — one to six plain
texts posted in order, each its own labelled message filed in the post map as it
lands, then the card, under one delivery, one key and one receipt. A page that
does not land sends no card; a card with pages is never placed in place
(INV-PROP-012).

The deposit and the post are driven like ``test_proposal_slot.py`` (the real
hooks and store, a recorder channel); the tap's fallback like
``test_tap_in_place_card.py``.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import delivery_keys
import result_broker as rb
import tools as tools_mod
import verdict_broker as vb
from channels import DeliveryOutcome
from test_proposal_slot import (LABEL, SLOT, _identity, _map, _open, _post,  # noqa: F401
                                _proposal, _Manager)
from test_desk_tap import LABEL as DESK_LABEL, OPERATOR, _tap, env  # noqa: F401
from test_tap_in_place_card import _respond, edit_env  # noqa: F401
from test_tap_next_card import _card, _live, card_env  # noqa: F401


class _PagesRecorder:
    """One ordered log of every message: ``("page", text)`` / ``("card", text)``."""

    def __init__(self):
        self.log: list[tuple[str, str]] = []
        self.posts: list = []
        self.fail_page: int | None = None      # 1-based page that does not land
        self.raise_page: int | None = None     # 1-based page whose send raises
        self.card_lands = True
        self.next_mid = 700

    async def deliver_operator_page(self, chat_id, text, *, post=None):
        n = sum(1 for k, _ in self.log if k == "page") + 1
        if n == self.raise_page:
            raise RuntimeError("TimedOut")
        if n == self.fail_page:
            return DeliveryOutcome.NOT_DELIVERED
        self.log.append(("page", text))
        self.next_mid += 1
        rb.POST_MAP.record(chat_id, self.next_mid, post)
        self.posts.append(post)
        return DeliveryOutcome.DELIVERED

    async def deliver_operator_proposal(self, chat_id, text, labels, rid, *, post=None):
        self.log.append(("card", text))
        if not self.card_lands:
            return None
        self.next_mid += 1
        rb.POST_MAP.record(chat_id, self.next_mid, post)
        return self.next_mid


@pytest.fixture
def penv(monkeypatch):
    rec = _PagesRecorder()
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(rec), raising=False)
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "finance": SimpleNamespace(character=SimpleNamespace(name="Finance"))})
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    monkeypatch.setattr(rb, "POSTS", rb.PostLedger())
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    store = rb.ReferenceStore(now=lambda: 1000.0)
    return SimpleNamespace(rec=rec, store=store)


PAGES = ["Payments without invoice (1/3)\n- Adobe 2 Sep €100", "(2/3)\n- Figma 3 Sep €12",
         "(3/3)\n- AWS 4 Sep €7"]


def _live_rids():
    return vb.BROKER.pending(namespace="proposal", scope="proposal:42")


# --- the deposit ---------------------------------------------------------------------

@pytest.mark.parametrize("pages", [["one"], ["p"] * 6, ["x" * 4000]], ids=["one", "six", "full"])
def test_the_deposit_keeps_one_to_six_pages(penv, pages):
    _open(penv.store)
    ref, err = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(pages=pages)))
    assert err is None
    assert penv.store._refs[ref].proposal["pages"] == pages


@pytest.mark.parametrize("pages", [None, "absent"], ids=["null", "absent"])
def test_no_pages_is_todays_card(penv, pages):
    value = _proposal() if pages == "absent" else _proposal(pages=None)
    _open(penv.store)
    ref, err = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(value))
    assert err is None and "pages" not in penv.store._refs[ref].proposal


@pytest.mark.parametrize("pages", [
    [], ["p"] * 7, "one page", [1], [""], ["  \n"], ["x" * 4001], ["a\x00b"], ["😀" * 2100]],
    ids=["empty", "seven", "not-a-list", "not-text", "empty-page", "blank-page", "long-page",
         "control-char", "two-messages"])
def test_the_deposit_refuses_bad_pages(penv, pages):
    _open(penv.store)
    value = json.dumps(_proposal(pages=pages))
    assert penv.store.deposit(client_id="c1", slot=SLOT, value=value) == (None, "bad_proposal")


# --- the post ------------------------------------------------------------------------

async def test_the_pages_land_in_order_then_the_card_under_one_receipt(penv):
    hook = rb.make_result_hook(_map(), client_id="c1", store=penv.store)
    _open(penv.store)
    ref, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(pages=PAGES)))
    registered_before_pages = []
    inner = penv.rec.deliver_operator_page

    async def checking(chat_id, text, *, post=None):
        registered_before_pages.append(len(_live_rids()))
        return await inner(chat_id, text, post=post)
    penv.rec.deliver_operator_page = checking
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert penv.rec.log == [("page", f"{LABEL}\n{p}") for p in PAGES] + [
        ("card", f"{LABEL}\nPair invoice 17 with the Adobe payment?")]
    assert registered_before_pages == [1, 1, 1]
    (rid,) = _live_rids()
    receipt = json.loads(out["hookSpecificOutput"]["updatedToolOutput"])
    assert receipt["casa_delivery"] == {"slot": SLOT, "status": "delivered", "to": "operator_chat",
                                        "proposal_id": rid, "buttons": 2, "pages": 3}
    # every page is filed under the card's own post record: a swipe-reply routes
    card_post = rb.POST_MAP.get(42, 704)
    assert [rb.POST_MAP.get(42, mid) for mid in (701, 702, 703)] == [card_post] * 3
    assert (card_post.role, card_post.kind) == ("finance", rb.OPERATOR_PROPOSAL)
    assert rb.echo_lines(rb.POSTS.drain("d-1")) == [
        LABEL + " posted 3 pages and a proposal to your chat (2 buttons)."]


async def test_a_page_that_does_not_land_sends_no_card_and_leaves_nothing_tappable(penv):
    hook = rb.make_result_hook(_map(), client_id="c1", store=penv.store)
    penv.rec.fail_page = 2
    _open(penv.store)
    ref, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(pages=PAGES)))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert [k for k, _ in penv.rec.log] == ["page"]          # page 1 only; no card
    assert _live_rids() == []
    told = out["hookSpecificOutput"]["updatedToolOutput"]
    assert "1 of 3 pages" in told and "delivered" not in json.loads(told).get("casa_delivery", {})
    assert rb.POSTS.drain("d-1") == []


async def test_a_page_send_that_raises_sends_no_card(penv):
    hook = rb.make_result_hook(_map(), client_id="c1", store=penv.store)
    penv.rec.raise_page = 1
    _open(penv.store)
    ref, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(pages=PAGES)))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert penv.rec.log == [] and _live_rids() == []
    assert "could not post the proposal" in out["hookSpecificOutput"]["updatedToolOutput"]


async def test_pages_that_outrun_the_bound_send_no_card(penv, monkeypatch):
    monkeypatch.setattr(rb, "DELIVERY_TIMEOUT_S", 0.05)
    hook = rb.make_result_hook(_map(), client_id="c1", store=penv.store)
    inner = penv.rec.deliver_operator_page

    async def slow(chat_id, text, *, post=None):
        await asyncio.sleep(0.03)
        return await inner(chat_id, text, post=post)
    penv.rec.deliver_operator_page = slow
    _open(penv.store)
    ref, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(pages=PAGES)))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert "card" not in [k for k, _ in penv.rec.log] and _live_rids() == []
    assert "of 3 pages" in out["hookSpecificOutput"]["updatedToolOutput"]


async def test_a_card_that_does_not_land_after_its_pages_is_withheld(penv):
    hook = rb.make_result_hook(_map(), client_id="c1", store=penv.store)
    penv.rec.card_lands = False
    _open(penv.store)
    ref, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(pages=PAGES)))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert [k for k, _ in penv.rec.log] == ["page"] * 3 + ["card"]
    assert _live_rids() == []
    assert "3 of 3 pages" in out["hookSpecificOutput"]["updatedToolOutput"]


async def test_a_card_without_pages_is_unchanged(penv):
    hook = rb.make_result_hook(_map(), client_id="c1", store=penv.store)
    _open(penv.store)
    ref, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal()))
    out = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    assert [k for k, _ in penv.rec.log] == ["card"]
    receipt = json.loads(out["hookSpecificOutput"]["updatedToolOutput"])
    assert "pages" not in receipt["casa_delivery"]
    assert rb.echo_lines(rb.POSTS.drain("d-1")) == [LABEL + " posted a proposal to your chat (2 buttons)."]


# --- the delivery key (#1312) ------------------------------------------------------------

async def test_a_keyed_repeat_while_the_card_is_live_sends_nothing(penv, monkeypatch, tmp_path):
    keys = delivery_keys.DeliveryKeys()
    keys.load(str(tmp_path / "keys.json"))
    monkeypatch.setattr(delivery_keys, "KEYS", keys)
    hook = rb.make_result_hook(_map(), client_id="c1", store=penv.store)
    value = json.dumps(_proposal(pages=PAGES))
    _open(penv.store, call="call-1")
    ref, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=value, key="list-q3")
    first = await hook(_post(json.dumps({SLOT: ref})), "call-1", {})
    _open(penv.store, call="call-2")
    ref2, _ = penv.store.deposit(client_id="c1", slot=SLOT, value=value, key="list-q3")
    second = await hook(_post(json.dumps({SLOT: ref2})), "call-2", {})
    assert [k for k, _ in penv.rec.log] == ["page"] * 3 + ["card"]
    a = json.loads(first["hookSpecificOutput"]["updatedToolOutput"])["casa_delivery"]
    b = json.loads(second["hookSpecificOutput"]["updatedToolOutput"])["casa_delivery"]
    assert b == {**a, "repeat": True} and a["pages"] == 3
    # the remembered entry survives a reload (its detail shape is accepted)
    reloaded = delivery_keys.DeliveryKeys()
    reloaded.load(str(tmp_path / "keys.json"))
    assert reloaded.lookup("probe", 42, "list-q3")["detail"]["pages"] == 3


# --- a tap's next card: never in place -----------------------------------------------------

async def test_a_next_card_with_pages_is_never_an_edit_and_falls_back_to_receipt_pages_card(edit_env):
    env = edit_env

    async def deliver_operator_page(chat_id, text, *, post=None):
        env.log.append(("page", text))
        return DeliveryOutcome.DELIVERED
    env.channel.deliver_operator_page = deliver_operator_page
    env.respond = _respond(nxt=_card(pages=["(1/2) Adobe", "(2/2) Figma"]))
    await _tap(env)
    assert env.edits == []
    assert [k for k, _ in env.log] == ["receipt", "page", "page", "card"]
    assert [t for k, t in env.log if k == "page"] == [f"{DESK_LABEL}\n(1/2) Adobe",
                                                      f"{DESK_LABEL}\n(2/2) Figma"]
    (card,) = env.proposals
    assert _live() == [card.rid]


# --- the channel: one page is one message ----------------------------------------------

def _channel_with_bot():
    from test_telegram_topic_stream import _mk_channel_with_fake_bot
    return _mk_channel_with_fake_bot()


async def test_a_page_is_one_rich_message_filed_under_its_post(monkeypatch):
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    ch, bot = _channel_with_bot()
    bot.send_message.return_value = SimpleNamespace(message_id=77)
    post = object()
    page = f"{LABEL}\n**Adobe** · [invoice](https://example.com/i/17)\n" + "\n".join(
        f"| row {i} | €{i} |" for i in range(20))
    assert await ch.deliver_operator_page(42, page, post=post) is DeliveryOutcome.DELIVERED
    assert bot.send_message.await_count == 1 and bot.send_message.await_args.kwargs["entities"]
    assert rb.POST_MAP.get(42, 77) is post


async def test_a_page_whose_entities_are_refused_is_resent_whole_as_one_plain_message(monkeypatch):
    from telegram.error import BadRequest
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    ch, bot = _channel_with_bot()
    landed = SimpleNamespace(message_id=78)

    async def send(**kw):
        if kw.get("entities"):
            raise BadRequest("Can't parse entities")
        return landed
    bot.send_message.side_effect = send
    post = object()
    page = f"{LABEL}\n**Adobe** · [invoice](https://example.com/i/17)\n" + "\n".join(
        f"| row {i} | €{i} |" for i in range(20))
    assert await ch.deliver_operator_page(42, page, post=post) is DeliveryOutcome.DELIVERED
    assert bot.send_message.await_count == 2                 # the rich try, then ONE plain message
    assert bot.send_message.await_args.kwargs["text"] == page
    assert rb.POST_MAP.get(42, 78) is post
