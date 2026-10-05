"""#1302: a ``safe`` tap tool's response may carry, beside its ``receipt``, the
``next`` card — a proposal deposit Casa posts right after the receipt, in the
same desk use, through the same deposit predicate and the same register-then-post
path as any proposal (INV-PROP-006). A receipt-only response is today's.

The desk use is ``test_desk_tap.py``'s fixture (a faked runner resolving the
owner's watch); the proposal post is the real ``_post_proposal`` against a real
``VerdictBroker``, with the channel faked at its send.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import pinned_run as pr
import result_broker as rb
import tools as tools_mod
import verdict_broker as vb
from test_desk_tap import APPLY, ART, LABEL, OPERATOR, SEG, _echo, _map, _tap, _tool, env  # noqa: F401


def _card(**over):
    base = {"text": "Adobe · 2 Sep · €100.00 ↔ invoice INV-88?",
            "buttons": [{"label": "Confirm", "call": {"tool": "apply", "arguments": {"choice": "yes", "item": 2}}},
                        {"label": "Wrong", "call": {"tool": "apply", "arguments": {"choice": "no", "item": 2}}}],
            "revision": "walk-1"}
    base.update(over)
    return base


@pytest.fixture
def card_env(env, monkeypatch):
    """The fixture's channel, extended with the proposal send and one ordered
    log of what reached the operator."""
    env.log = []
    env.proposals = []
    env.land = True
    ch = env.channel
    send_response = ch.send_response

    async def logged_send(message, context):
        env.log.append(("receipt", str(message)))
        return await send_response(message, context)

    async def deliver_operator_proposal(chat_id, text, labels, rid, *, post=None):
        env.log.append(("card", text))
        env.proposals.append(SimpleNamespace(chat_id=chat_id, text=text, labels=list(labels),
                                             rid=rid, post=post))
        return 900 + len(env.proposals) if env.land else None

    ch.send_response = logged_send
    ch.deliver_operator_proposal = deliver_operator_proposal
    monkeypatch.setattr(rb, "_telegram_channel", lambda: ch)
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    env.broker = vb.BROKER
    return env


def _respond_with(receipt="Confirmed: Adobe ↔ INV-88.", nxt=None):
    async def respond(call):
        # encoded exactly as the result hook's capture encodes it
        call.owner.resolve(pr.Capture("receipt", receipt, next=json.dumps(
            nxt, ensure_ascii=False) if nxt is not None else ""))
        return tools_mod.DelegatedOutput(text="")
    return respond


def _live():
    """The live proposals' request ids, in registration order."""
    return vb.BROKER.pending(namespace="proposal", scope=f"proposal:{OPERATOR}")


def _meta_of(rid):
    return vb.BROKER.get_meta(namespace="proposal", scope=f"proposal:{OPERATOR}", request_id=rid)


# --- the capture -------------------------------------------------------------------

def _captured(kind, response):
    contract_map = _map([_tool("apply"), _tool("more", "capability", provides=("proposal",),
                                               delivers={"proposal": "operator_proposal"})])
    tool = APPLY if kind == "safe" else "mcp__plugin_probe_probe__more"
    data = {"tool_name": tool, "tool_response": response}
    return rb._capture_of(contract_map, tool, data, {}, False)


def test_a_safe_receipt_carries_its_next_object_encoded():
    nxt = _card()
    cap = _captured("safe", json.dumps({"receipt": "done", "next": nxt}))
    assert (cap.kind, cap.text) == ("receipt", "done")
    assert json.loads(cap.next) == nxt


@pytest.mark.parametrize("response", [
    json.dumps({"receipt": "done"}),
    json.dumps({"receipt": "done", "next": None}),
    json.dumps({"receipt": "done", "next": "a string"}),
    "plain text",
])
def test_a_receipt_without_a_next_object_is_todays_capture(response):
    cap = _captured("safe", response)
    assert cap.kind == "receipt" and cap.next == ""


def test_a_next_without_a_usable_receipt_is_ignored_and_the_text_is_verbatim():
    response = json.dumps({"receipt": "  ", "next": _card()})
    cap = _captured("safe", response)
    assert (cap.text, cap.next) == (response, "")


def test_the_more_no_post_shape_never_carries_a_next_card():
    cap = _captured("more", json.dumps({"proposal": None, "receipt": "no more", "next": _card()}))
    assert (cap.kind, cap.text, cap.next) == ("no_post", "no more", "")


# --- the desk use ------------------------------------------------------------------

async def test_the_next_card_is_posted_after_the_receipt_and_registered_before_its_send(card_env):
    env = card_env
    registered_at_send = []
    inner = env.channel.deliver_operator_proposal

    async def checking(chat_id, text, labels, rid, *, post=None):
        registered_at_send.append(_live() == [rid])
        return await inner(chat_id, text, labels, rid, post=post)
    env.channel.deliver_operator_proposal = checking
    env.respond = _respond_with(nxt=_card())
    await _tap(env)
    assert [k for k, _ in env.log] == ["receipt", "card"]
    assert registered_at_send == [True]
    (card,) = env.proposals
    assert card.labels == ["Confirm", "Wrong"]
    assert card.text.startswith(LABEL) and "INV-88" in card.text
    (rid,) = _live()
    meta = _meta_of(rid)
    assert rid == card.rid
    # the buttons are bound to the tapped plugin's own tool, resolved like any deposit
    assert [c["runtime_name"] for c in meta["calls"]] == [APPLY, APPLY]
    assert [c["canonical"] for c in meta["calls"]] == ['{"choice":"yes","item":2}',
                                                       '{"choice":"no","item":2}']
    assert (meta["role"], meta["artifact_id"], meta["plugin_seg"], meta["operator_id"],
            meta["message_id"], meta["revision"]) == ("finance", ART, SEG, OPERATOR, 901, "walk-1")
    assert (card.post.kind, card.post.role, card.post.plugin) == ("operator_proposal", "finance", SEG)
    assert env.channel.notices == []
    assert _echo() == [f"{LABEL} applied your tap (Yes)."]


async def test_a_receipt_only_tap_posts_nothing_more(card_env):
    env = card_env
    env.respond = _respond_with()
    await _tap(env)
    assert [k for k, _ in env.log] == ["receipt"]
    assert env.proposals == [] and _live() == [] and env.channel.notices == []


@pytest.mark.parametrize("bad", [
    _card(buttons=[{"label": "Elsewhere", "call": {"tool": "other_plugin_tool", "arguments": {}}}]),
    _card(buttons=[{"label": "Raw", "call": {"tool": "mcp__plugin_x_x__apply", "arguments": {}}}]),
    _card(text=""),
    _card(buttons=[]),
])
async def test_a_card_the_deposit_predicate_refuses_is_one_notice_and_the_receipt_stands(card_env, bad):
    env = card_env
    env.respond = _respond_with(nxt=bad)
    await _tap(env)
    assert [k for k, _ in env.log] == ["receipt"]
    assert env.proposals == [] and _live() == []
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not show the next card (invalid).")]


async def test_a_card_whose_button_is_a_setup_tool_is_refused(card_env):
    env = card_env
    from test_desk_tap import _build
    env.build = _build(tools=[_tool("apply"), _tool("connect")])
    env.build.contract_map.plugins[SEG] = env.build.contract_map.plugins[SEG].__class__(
        ART, True, frozenset({"mcp__plugin_probe_probe__connect"}), name="probe")
    env.respond = _respond_with(nxt=_card(buttons=[
        {"label": "Connect", "call": {"tool": "connect", "arguments": {}}}]))
    await _tap(env)
    assert env.proposals == []
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not show the next card (invalid).")]


async def test_the_33rd_live_card_is_too_many_open(card_env):
    env = card_env
    for i in range(rb.PROPOSAL_MAX_LIVE):
        vb.BROKER.register(namespace="proposal", scope=f"proposal:{OPERATOR}", request_id=f"r{i}",
                           timeout_s=3600, detached=True, supersede=False, meta={})
    env.respond = _respond_with(nxt=_card())
    await _tap(env)
    assert env.proposals == []
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not show the next card (too many open).")]


async def test_a_card_that_did_not_land_is_unregistered_and_told(card_env):
    env = card_env
    env.land = False
    env.respond = _respond_with(nxt=_card())
    await _tap(env)
    assert [k for k, _ in env.log] == ["receipt", "card"]
    assert _live() == []
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not show the next card (not delivered).")]


async def test_a_card_with_the_same_revision_replaces_the_live_one(card_env):
    env = card_env
    env.respond = _respond_with(nxt=_card())
    await _tap(env)
    (first,) = _live()
    env.respond = _respond_with(nxt=_card(text="Twilio · 9 Jul · €20.00?"))
    await _tap(env)
    (second,) = _live()
    assert second != first
    assert len(env.proposals) == 2


async def test_a_desk_faulted_by_the_run_posts_the_receipt_but_no_card(card_env, monkeypatch):
    env = card_env
    monkeypatch.setattr(tools_mod, "_DELEGATION_CEILING_S", 0.05)
    env.confirm = False

    async def respond(call):
        call.owner.resolve(pr.Capture("receipt", "applied", next=json.dumps(_card())))
        await asyncio.sleep(30)
    env.respond = respond
    await asyncio.wait_for(_tap(env), 5)
    assert env.desk.faulted
    assert [k for k, _ in env.log] == ["receipt"]
    assert env.proposals == [] and _live() == []


async def test_a_card_the_encoder_refuses_is_invalid_not_an_escaping_error(card_env):
    """Diff r1 (Astra): a lone surrogate in the card's text raised out of the
    deposit predicate — no card and no notice. It is an invalid card."""
    env = card_env
    response = json.dumps({"receipt": "done", "next": _card(text="bad\ud800")})
    cap = rb._capture_of(_map([_tool("apply")]), APPLY,
                         {"tool_name": APPLY, "tool_response": response}, {}, False)
    assert cap.next                                      # the real capture keeps it

    async def respond(call):
        call.owner.resolve(cap)
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _tap(env)
    assert [k for k, _ in env.log] == ["receipt"]
    assert env.proposals == [] and _live() == []
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not show the next card (invalid).")]


async def test_a_receipt_that_did_not_go_out_posts_no_card(card_env):
    """Diff r1 (Terra): the card follows a receipt that landed, never one that
    did not."""
    env = card_env
    env.channel.fail_send = True
    env.respond = _respond_with(nxt=_card())
    await _tap(env)
    assert [k for k, _ in env.log] == ["receipt"]
    assert env.proposals == [] and _live() == []
    assert env.channel.notices == [(OPERATOR, f"{LABEL} applied your tap; the receipt did not go out.")]
