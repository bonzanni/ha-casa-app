"""The own-chat lifecycle: an agent's bot from offer to release.

Driven through a fake Telegram world (``own_voice_world``) that survives
simulated restarts: a "restart" abandons the lifecycle and its host and
builds new ones over the same disk. The crash sweep kills the process after
every durable write and every external effect of each transition and checks
that boot completes or rolls it back."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import own_voice_lifecycle as ovl
from channels.tg_address import TgAddress
from own_voice_world import (
    ALEX_BOT, FIDO_BOT, OPERATOR, OP_ENV, BadRequest, Crash, FakeOp, Forbidden,
    InvalidToken, NetworkError, World, created, make_lifecycle, specialist, update,
)

ALEX_TOKEN = f"{ALEX_BOT}:alex-token-aaaaaaaaaaaaaaaa"
ALEX_TOKEN_2 = f"{ALEX_BOT}:alex-token-rotated-cccccccc"
FIDO_TOKEN = f"{FIDO_BOT}:fido-token-bbbbbbbbbbbbbbbb"


@pytest.fixture
def world() -> World:
    w = World()
    w.configs = {"finance": specialist("Alex"), "browser": specialist("Fido")}
    w.queues = {"finance", "browser"}
    w.tokens = {ALEX_BOT: ALEX_TOKEN, FIDO_BOT: FIDO_TOKEN}
    return w


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "data"


async def _bind_alex(lc, host) -> None:
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


def _disk(root: Path) -> dict:
    return json.loads((root / "agent-voices.json").read_text())


# ---------------------------------------------------------------------------
# registration only from the operator's creation message
# ---------------------------------------------------------------------------

async def test_a_managed_bot_update_alone_registers_nothing(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots == {} and world.fetches == []
    assert lc.state("finance") == "no_bot"


async def test_a_creation_sent_by_someone_else_is_ignored(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR + 1)
    assert lc.store.bots == {} and world.fetches == []


async def test_a_redelivered_creation_is_registered_once(world, root):
    lc, _host = make_lifecycle(world, root)
    await _bind_alex(lc, _host)
    fetches = list(world.fetches)
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert world.fetches == fetches and lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


# ---------------------------------------------------------------------------
# an exact match binds without a tap; anything else asks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, username, binds", [
    ("AlexTest", "HeyAlexTestBot", True),
    ("AlexTest", "heyalextestbot", True),       # usernames are case-insensitive
    ("Alex Test", "HeyAlexTestBot", True),      # an edited name: Casa sets the name itself
    ("AlexTest", "HeyAlexBot", False),          # edited username
])
async def test_only_an_exact_match_binds(world, root, name, username, binds):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(ALEX_BOT, name, username), OPERATOR)
    rec = lc.store.bots[ALEX_BOT]
    if binds:
        assert rec["phase"] == ovl.BOUND and rec["role"] == "finance"
        assert host.asks == {}
    else:
        assert rec["phase"] == ovl.UNBOUND and rec["ask"]["state"] == "posted"
        assert host.latest_ask()[1] == ["Fido", "Alex", ovl.ASK_NONE] or \
            host.latest_ask()[1] == ["Alex", "Fido", ovl.ASK_NONE]


async def test_a_stale_suggestion_asks(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.post_offer("finance", "CasaFinance4821Bot")
    # the operator used the first card's form
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.UNBOUND


async def test_two_agents_with_equal_suggestions_ask(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.post_offer("browser", "HeyFidoTestBot")
    lc.store.agents["browser"]["suggestion"] = dict(lc.store.agents["finance"]["suggestion"])
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.UNBOUND


# ---------------------------------------------------------------------------
# held updates leave the journal only through intake, in order
# ---------------------------------------------------------------------------

async def test_held_updates_are_released_in_order_at_binding(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)   # asks
    for uid in (1, 2, 3):
        assert await lc.intake(ALEX_BOT, update(uid)) == "held"
    assert world.released == []
    await host.answer("Alex")
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert world.released == [(ALEX_BOT, 1), (ALEX_BOT, 2), (ALEX_BOT, 3)]
    assert not lc.journal.path(ALEX_BOT).exists()


async def test_no_target_keeps_that_record_and_every_later_one(world, root):
    lc, host = make_lifecycle(world, root)
    world.queues.discard("finance")
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    for uid in (1, 2):
        await lc.intake(ALEX_BOT, update(uid))
    await host.answer("Alex")           # bound, but the agent has no queue
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert world.released == []
    assert [u["update_id"] for u in lc.journal.pending(ALEX_BOT)] == [1, 2]
    # a message while the queue is missing is held too, never dropped
    assert await lc.intake(ALEX_BOT, update(3)) == "held"
    world.queues.add("finance")
    await lc.on_queue("finance")        # the queue registered again
    assert world.released == [(ALEX_BOT, 1), (ALEX_BOT, 2), (ALEX_BOT, 3)]


async def test_a_refused_record_holds_every_later_one_even_if_intake_would_take_them(
        world, root):
    lc, host = make_lifecycle(world, root)
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    for uid in (1, 2, 3):
        await lc.intake(ALEX_BOT, update(uid))
    inner = host.release_update
    refuse = {1}

    async def first_refused(bot_id, upd):
        if upd["update_id"] in refuse:
            refuse.discard(upd["update_id"])
            return "no_target"
        return await inner(bot_id, upd)
    host.release_update = first_refused
    await host.answer("Alex")
    assert world.released == []
    assert [u["update_id"] for u in lc.journal.pending(ALEX_BOT)] == [1, 2, 3]
    await lc.on_queue("finance")
    assert world.released == [(ALEX_BOT, 1), (ALEX_BOT, 2), (ALEX_BOT, 3)]


async def test_a_bound_bot_whose_agent_has_no_queue_holds_a_new_message(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    world.queues.discard("finance")
    assert await lc.intake(ALEX_BOT, update(1)) == "held"
    assert [u["update_id"] for u in lc.journal.pending(ALEX_BOT)] == [1]
    world.queues.add("finance")
    await lc.on_queue("finance")
    assert world.released == [(ALEX_BOT, 1)]


async def test_a_new_update_never_overtakes_a_held_one(world, root):
    lc, host = make_lifecycle(world, root)
    world.queues.discard("finance")
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    await lc.intake(ALEX_BOT, update(1))
    await host.answer("Alex")
    world.queues.add("finance")         # back, but no trigger fired yet
    assert await lc.intake(ALEX_BOT, update(2)) == "held"
    assert world.released == [(ALEX_BOT, 1), (ALEX_BOT, 2)]
    assert await lc.intake(ALEX_BOT, update(3)) == "deliver"


async def test_binding_never_waits_for_a_turn(world, root):
    lc, host = make_lifecycle(world, root)
    world.queues.discard("finance")
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    await lc.intake(ALEX_BOT, update(1))
    await host.answer("Alex")
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


async def test_a_torn_last_line_is_skipped_and_the_next_append_starts_fresh(tmp_path):
    j = ovl.HeldJournal(tmp_path / "held")
    j.append(5, update(1))
    with open(j.path(5), "ab") as fh:
        fh.write(b'{"update": {"update_id": 2, "mes')     # torn by a crash
    assert [u["update_id"] for u in j.pending(5)] == [1]
    j.append(5, update(3))
    assert [u["update_id"] for u in j.pending(5)] == [1, 3]
    assert stat.S_IMODE(os.stat(j.path(5)).st_mode) == 0o600
    j.mark_released(5, 1)
    j.mark_released(5, 3)
    assert not j.path(5).exists()


# ---------------------------------------------------------------------------
# no agent runs for an unbound or dormant bot
# ---------------------------------------------------------------------------

async def test_an_unbound_bot_runs_no_agent_and_says_once_per_boot(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    for uid in (1, 2):
        assert await lc.intake(ALEX_BOT, update(uid)) == "held"
    assert world.released == []
    assert world.notices == [(ALEX_BOT, ovl.SETUP_REPLY)]
    lc2, _ = make_lifecycle(world, root)
    await lc2.boot()
    await lc2.intake(ALEX_BOT, update(3))
    await lc2.intake(ALEX_BOT, update(4))
    assert world.notices == [(ALEX_BOT, ovl.SETUP_REPLY)] * 2
    assert world.released == []


async def test_a_dormant_bot_runs_no_agent(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    del world.configs["finance"]
    world.queues.discard("finance")
    await lc.reconcile_role("finance")
    notices = list(world.notices)
    assert await lc.intake(ALEX_BOT, update(9)) == "dormant_reply"
    assert world.released == [] and lc.journal.pending(ALEX_BOT) == []
    # a live update's dormant line is the endpoint's to send, once
    assert world.notices == notices


# ---------------------------------------------------------------------------
# one bot per agent, never rebound
# ---------------------------------------------------------------------------

async def test_a_second_bot_for_a_bound_agent_is_asked_without_it(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    world.tokens[777] = "777:another-token-zzzzzzzzzzzz"
    await lc.on_created(created(777, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[777]["phase"] == ovl.UNBOUND
    assert "finance" not in lc.store.bots[777]["ask"]["candidates"]
    assert lc.store.agents["finance"]["bot_id"] == ALEX_BOT


async def test_the_registry_refuses_a_second_bot_for_an_agent(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    world.tokens[777] = "777:another-token-zzzzzzzzzzzz"
    await lc.on_created(created(777, "Other", "OtherHomeBot"), OPERATOR)
    assert await lc._enter_binding(777, "finance") is False
    assert lc.store.agents["finance"]["bot_id"] == ALEX_BOT
    assert lc.store.bots[777]["phase"] == ovl.UNBOUND


async def test_a_tap_on_a_stale_question_cannot_bind(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    _rid, options, stale = host.latest_ask()
    lc2, host2 = make_lifecycle(world, root)
    await lc2.boot()                    # asked again, with a new question
    await stale(options.index("Alex"))  # the old process's card
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.UNBOUND
    await host2.answer("Alex")
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


async def test_an_agent_that_gained_a_bot_meanwhile_is_asked_about_again(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance", "HeyAlexTestBot")
    world.tokens[777] = "777:another-token-zzzzzzzzzzzz"
    await lc.on_created(created(777, "Alex", "AlexHomeBot"), OPERATOR)  # asks
    rid, options, answer = host.latest_ask()
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)  # binds
    await answer(options.index("Alex"))
    assert lc.store.bots[777]["phase"] == ovl.UNBOUND
    assert "finance" not in lc.store.bots[777]["ask"]["candidates"]
    old = [m for m, (t, _b) in world.cards.items() if t == "Alex already has @HeyAlexTestBot."]
    assert old


async def test_a_reinstall_reuses_the_same_bot(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    spec = world.configs.pop("finance")
    await lc.reconcile_role("finance")
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.DORMANT
    world.configs["finance"] = spec
    await lc.on_install("finance")
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert lc.store.agents["finance"]["bot_id"] == ALEX_BOT
    assert len(lc.store.bots) == 1


# ---------------------------------------------------------------------------
# every phase and card state is on disk before the call that acts on it
# ---------------------------------------------------------------------------

async def test_every_effect_finds_its_phase_on_disk(world, root):
    lc, host = make_lifecycle(world, root)
    seen: list[tuple[str, dict]] = []

    def wrap(name):
        inner = getattr(host, name)

        async def call(*a, **kw):
            seen.append((name, _disk(root)))
            return await inner(*a, **kw)
        setattr(host, name, call)

    for name in ("post_card", "post_ask", "fetch_token", "start_endpoint"):
        wrap(name)
    op = lc.tokens._run_fn

    def op_call(cmd, **kw):
        if cmd[1:3] in (["item", "create"], ["item", "edit"]):
            seen.append(("op", _disk(root)))
        return op(cmd, **kw)
    lc.tokens._run_fn = op_call

    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    await host.answer("Alex")
    by = {}
    for name, disk in seen:
        by.setdefault(name, disk)
    assert by["post_card"]["agents"]["finance"]["offered_at"]
    assert by["fetch_token"]["bots"][str(ALEX_BOT)]["phase"] == ovl.SEEN
    ask_disk = [d for n, d in seen if n == "post_ask"][0]
    assert ask_disk["bots"][str(ALEX_BOT)]["ask"]["state"] == "posting"
    assert by["op"]["bots"][str(ALEX_BOT)]["phase"] == ovl.BINDING
    assert by["op"]["agents"]["finance"]["bot_id"] == ALEX_BOT


# ---------------------------------------------------------------------------
# open is the first operator turn its agent's queue took, released or live,
# edited once; the turn that opens it is told so when it is built
# ---------------------------------------------------------------------------

async def test_the_ready_card_asks_the_operator_to_say_hi(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    text, buttons = world.cards[lc.store.agents["finance"]["offer_card"][2]]
    assert text == "Alex's chat is ready: @HeyAlexTestBot. Open it and say hi."
    assert "Start" not in text
    assert buttons == [("Open Alex's chat", "https://t.me/HeyAlexTestBot")]


async def test_a_released_held_start_opens_the_chat_once(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    await lc.intake(ALEX_BOT, update(1, "/start"))
    assert lc.state("finance") == "no_bot"
    edits = []
    inner = host.edit_card

    async def count(addr, text, buttons):
        edits.append(text)
        return await inner(addr, text, buttons)
    host.edit_card = count
    await host.answer("Alex")
    assert lc.state("finance") == "open"
    assert world.openings == [True]
    card = lc.store.agents["finance"]["offer_card"]
    assert world.card_text(card) == "Alex's chat is open."
    n = edits.count("Alex's chat is open.")
    assert await lc.intake(ALEX_BOT, update(2)) == "deliver"
    assert edits.count("Alex's chat is open.") == n
    assert world.openings == [True, False]


async def test_a_live_first_message_opens_the_chat_and_a_bot_message_does_not(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    assert lc.state("finance") == "not_open"
    card = lc.store.agents["finance"]["offer_card"]
    assert world.card_text(card).startswith("Alex's chat is ready: @HeyAlexTestBot")
    await lc.intake(ALEX_BOT, {"update_id": 1, "callback_query": {"id": "x"}})
    assert lc.state("finance") == "not_open"
    assert world.openings == []
    assert await lc.intake(ALEX_BOT, update(2, "/start")) == "deliver"
    assert lc.state("finance") == "open"
    assert world.card_text(card) == "Alex's chat is open."
    assert world.openings == [True]


async def test_a_refused_release_leaves_the_chat_unopened_across_a_restart(world, root):
    """A held first message whose release its agent's queue refused opens
    nothing: after a restart its release is still the turn that opens."""
    world.queues.discard("finance")
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    assert await lc.intake(ALEX_BOT, update(1, "/start")) == "held"
    await lc.on_queue("finance")                # the release finds no queue
    assert world.released == []
    assert world.openings == [True]
    assert _disk(root)["agents"]["finance"]["chat_open_at"] is None
    await lc.close()
    lc, host = make_lifecycle(world, root)      # a restart
    world.queues.add("finance")
    await lc.boot()
    assert world.released == [(ALEX_BOT, 1)]
    assert world.openings == [True, True]       # refused, then taken
    assert lc.state("finance") == "open"
    assert await lc.intake(ALEX_BOT, update(2)) == "deliver"
    assert world.openings == [True, True, False]


async def test_an_interrupted_release_still_opens_with_the_greeting_after_a_restart(
        world, root):
    """The process dies inside the release of the first held message: the
    next boot releases it as the turn that opens the chat."""
    world.queues.discard("finance")
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    await lc.intake(ALEX_BOT, update(1, "/start"))
    world.queues.add("finance")

    async def crash(bot_id, upd):
        raise Crash("release")
    host.release_update = crash
    with pytest.raises(Crash):
        await lc.on_queue("finance")
    assert _disk(root)["agents"]["finance"]["chat_open_at"] is None
    lc, host = make_lifecycle(world, root)      # a restart
    await lc.boot()
    assert world.openings == [True]
    assert world.released == [(ALEX_BOT, 1)]
    assert lc.journal.pending(ALEX_BOT) == []
    assert lc.state("finance") == "open"


async def test_intake_never_opens_the_chat_itself(world, root):
    """The lifecycle's intake only lets a first message through: the chat
    opens when the turn built from it is taken."""
    lc, host = make_lifecycle(world, root, channel=False)
    await _bind_alex(lc, host)
    assert lc.chat_opening(ALEX_BOT)
    assert await lc.intake(ALEX_BOT, update(1, "/start")) == "deliver"
    assert lc.state("finance") == "not_open"
    assert lc.chat_opening(ALEX_BOT)
    await lc.mark_open(ALEX_BOT)
    assert lc.state("finance") == "open"
    assert not lc.chat_opening(ALEX_BOT)
    assert not lc.chat_opening(FIDO_BOT)        # no bot of any agent's


# ---------------------------------------------------------------------------
# a token change keeps the bot, its item and its addresses
# ---------------------------------------------------------------------------

async def test_a_live_token_refresh_keeps_the_bot_and_the_item(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    item = lc.store.agents["finance"]["token_item"]
    world.tokens[ALEX_BOT] = ALEX_TOKEN_2
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert host.endpoints[ALEX_BOT].bot_token == ALEX_TOKEN_2
    assert lc.store.agents["finance"]["token_item"] == item
    assert world.op_items[item]["token"] == ALEX_TOKEN_2
    assert len(world.op_items) == 1
    assert lc.bot_id_for("finance") == ALEX_BOT


async def test_a_rotation_while_stopped_is_recovered_at_boot(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    item = lc.store.agents["finance"]["token_item"]
    world.tokens[ALEX_BOT] = ALEX_TOKEN_2            # rotated while Casa was down
    lc2, host2 = make_lifecycle(world, root)
    await lc2.boot()
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert host2.endpoints[ALEX_BOT].bot_token == ALEX_TOKEN_2
    assert world.op_items[item]["token"] == ALEX_TOKEN_2 and len(world.op_items) == 1


# ---------------------------------------------------------------------------
# the handle is Ellen's choice; Casa checks only its format, owner and use
# ---------------------------------------------------------------------------

def test_the_create_link_is_the_trial_form():
    assert ovl.create_link("HeyEllenTestBot", "AlexTest", "HeyAlexTestBot") == \
        "https://t.me/newbot/HeyEllenTestBot/HeyAlexTestBot?name=AlexTest"
    assert ovl.create_link("HeyEllenTestBot", "Al Ex", "") == \
        "https://t.me/newbot/HeyEllenTestBot?name=Al+Ex"


async def test_the_offer_card_carries_ellens_handle(world, root):
    lc, _ = make_lifecycle(world, root)
    res = await lc.offer("finance", "CasaFinance4821Bot")
    assert res == {"status": "offered", "suggested_name": "Alex",
                   "suggested_username": "CasaFinance4821Bot"}
    text, buttons = world.cards[lc.store.agents["finance"]["offer_card"][2]]
    assert text == "Alex can get a separate chat with you. Tap below, then Create."
    assert buttons == [("Create Alex's chat",
                        "https://t.me/newbot/HeyEllenTestBot/CasaFinance4821Bot?name=Alex")]
    assert lc.store.agents["finance"]["handles"] == ["CasaFinance4821Bot"]


@pytest.mark.parametrize("handle", [
    "", "Alex", "AlexBott!", "CasaFinance4821", "Casa" + "x" * 26 + "Bot"])   # the last: 33
async def test_an_invalid_handle_is_refused_with_its_reason(world, root, handle):
    lc, _ = make_lifecycle(world, root)
    res = await lc.offer("finance", handle)
    assert res["status"] == "error" and res["kind"] == "invalid_handle"
    assert "ending in bot" in res["message"]
    assert world.cards == {} and "finance" not in lc.store.agents


async def test_a_handle_once_offered_to_another_specialist_is_refused(world, root):
    lc, _ = make_lifecycle(world, root)
    await lc.offer("finance", "CasaFinance4821Bot")
    cards = dict(world.cards)
    res = await lc.offer("browser", "casafinance4821bot")
    assert res["status"] == "error" and res["kind"] == "handle_owned"
    assert "Alex" in res["message"]
    assert world.cards == cards and "browser" not in lc.store.agents


async def test_two_offers_of_one_handle_at_once_give_it_to_one_specialist(world, root):
    # Both offers pass the ownership check, then wait on the Telegram probe;
    # only one of them may own the handle and post a card.
    lc, host = make_lifecycle(world, root)
    both_probing = asyncio.Event()
    probing = 0

    async def slow_probe(username):
        nonlocal probing
        probing += 1
        if probing == 2:
            both_probing.set()
        await both_probing.wait()
        return False

    host.handle_taken = slow_probe
    results = await asyncio.gather(lc.offer("finance", "CasaShared4821Bot"),
                                   lc.offer("browser", "CasaShared4821Bot"))
    assert sorted(r["status"] for r in results) == ["error", "offered"]
    assert [r["kind"] for r in results if r["status"] == "error"] == ["handle_owned"]
    owners = [role for role, agent in lc.store.agents.items()
              if "casashared4821bot" in {h.lower() for h in agent.get("handles") or []}]
    assert len(owners) == 1
    assert len(world.cards) == 1


async def test_a_taken_handle_is_refused(world, root):
    world.taken_handles = {"CasaFinance4821Bot"}
    lc, _ = make_lifecycle(world, root)
    res = await lc.offer("finance", "CasaFinance4821Bot")
    assert res["status"] == "error" and res["kind"] == "handle_taken"
    assert world.cards == {} and "finance" not in lc.store.agents


async def test_an_unreachable_probe_never_blocks_the_offer(world, root):
    world.probe_down = True
    lc, _ = make_lifecycle(world, root)
    res = await lc.offer("finance", "CasaFinance4821Bot")
    assert res["status"] == "offered"
    assert lc.store.agents["finance"]["suggestion"]["username"] == "CasaFinance4821Bot"


async def test_the_check_reports_each_candidate_and_the_context(world, root):
    world.taken_handles = {"CasaFinance4821Bot"}
    lc, host = make_lifecycle(world, root)
    await lc.offer("browser", "CasaBrowser2222Bot")
    cards = dict(world.cards)
    res = await lc.check_handles(
        "finance", ["x", "CasaFinance4821Bot", "CasaFinance1111Bot", "CasaBrowser2222Bot"])
    assert res["status"] == "checked"
    assert res["candidates"] == {
        "x": "invalid_format", "CasaFinance4821Bot": "taken",
        "CasaFinance1111Bot": "probably_free", "CasaBrowser2222Bot": "owned_by_other"}
    ctx = res["context"]
    assert ctx["role"] == "finance" and ctx["persona_name"] == "Alex"
    assert ctx["resident_bot"] == {"username": "HeyEllenTestBot", "name": "EllenTest"}
    assert ctx["current_handle"] is None
    assert ctx["other_handles"] == ["CasaBrowser2222Bot"]
    assert "ending in bot" in ctx["rules"]
    assert world.cards == cards                 # a check posts nothing
    assert "finance" not in lc.store.agents
    world.probe_down = True
    res = await lc.check_handles("finance", ["CasaFinance1111Bot"])
    assert res["candidates"] == {"CasaFinance1111Bot": "unknown"}
    res = await lc.check_handles("finance", [f"CasaFinance{n}Bot" for n in range(6)])
    assert res["status"] == "error" and res["kind"] == "too_many_candidates"


async def test_the_check_names_the_bound_handles(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    res = await lc.check_handles("finance", [])
    assert res["context"]["current_handle"] == "HeyAlexTestBot"
    res = await lc.check_handles("browser", [])
    assert res["context"]["current_handle"] is None
    assert res["context"]["other_handles"] == ["HeyAlexTestBot"]


async def test_handles_are_kept_across_a_new_offer_and_a_restart(world, root):
    lc, _ = make_lifecycle(world, root)
    await lc.offer("finance", "CasaFinance4821Bot")
    await lc.offer("finance", "CasaFinance1111Bot")
    await lc.offer("finance", "casafinance4821bot")      # its own again: no duplicate
    lc2, _ = make_lifecycle(world, root)
    assert lc2.store.agents["finance"]["handles"] == ["CasaFinance4821Bot", "CasaFinance1111Bot"]
    res = await lc2.offer("browser", "CasaFinance1111Bot")
    assert res["kind"] == "handle_owned"


async def test_the_probe_reads_the_page_title_block(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def page(request):
        name = request.match_info["name"]
        if name == "TakenBot":
            return web.Response(text='<div class="tgme_page_title"><span>Alex</span></div>',
                                content_type="text/html")
        if name == "BrokenBot":
            return web.Response(status=500)
        return web.Response(text=f'<meta property="og:title" content="Telegram: Contact @{name}">',
                            content_type="text/html")
    app = web.Application()
    app.router.add_get("/{name}", page)
    host = ovl.TelegramHost()
    async with TestServer(app) as server:
        monkeypatch.setattr(ovl, "HANDLE_PAGE", str(server.make_url("/")) + "{}")
        assert await host.handle_taken("TakenBot") is True
        assert await host.handle_taken("FreeBot") is False
        assert await host.handle_taken("BrokenBot") is None
    assert await host.handle_taken("TakenBot") is None      # nothing answers


# ---------------------------------------------------------------------------
# a new bot for an agent: offered by its exact handle, applied at a restart
# ---------------------------------------------------------------------------

NEW_BOT = FIDO_BOT + 10
NEW_TOKEN = f"{NEW_BOT}:new-token-eeeeeeeeeeeeeeee"


async def _change_alex(lc, host, world, new="CasaFinance4821Bot"):
    await _bind_alex(lc, host)
    return await lc.replace("finance", new)


async def test_a_change_offers_the_new_handle_and_names_the_old_bot(world, root):
    lc, host = make_lifecycle(world, root)
    res = await _change_alex(lc, host, world)
    assert res == {"status": "change_offered", "suggested_username": "CasaFinance4821Bot",
                   "replaces": "@HeyAlexTestBot"}
    agent = lc.store.agents["finance"]
    assert agent["change"] == {"new_bot": None, "old_bot": ALEX_BOT,
                               "old_username": "HeyAlexTestBot"}
    assert agent["bot_id"] == ALEX_BOT and agent["handles"] == ["HeyAlexTestBot",
                                                                "CasaFinance4821Bot"]
    text, buttons = world.cards[agent["offer_card"][2]]
    assert "@HeyAlexTestBot" in text
    assert buttons == [("Create Alex's chat",
                        "https://t.me/newbot/HeyEllenTestBot/CasaFinance4821Bot?name=Alex")]


async def test_a_change_needs_a_bot_to_replace(world, root):
    lc, _host = make_lifecycle(world, root)
    res = await lc.replace("finance", "CasaFinance4821Bot")
    assert res["status"] == "error" and res["kind"] == "no_bot"
    res = await lc.replace("nobody", "CasaFinance4821Bot")
    assert res["status"] == "error" and res["kind"] == "not_declared"
    assert world.cards == {}


async def test_a_change_is_refused_while_something_waits_on_the_old_chat(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    world.open[ALEX_BOT] = ["a background job"]
    cards = dict(world.cards)
    res = await lc.replace("finance", "CasaFinance4821Bot")
    assert res["status"] == "error" and res["kind"] == "busy"
    assert res["open"] == ["a background job"]
    assert world.cards == cards and lc.store.agents["finance"]["change"] is None


async def test_a_change_to_a_taken_or_owned_handle_is_refused(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    await lc.offer("browser", "CasaBrowser2222Bot")
    world.taken_handles = {"CasaFinance4821Bot"}
    assert (await lc.replace("finance", "CasaFinance4821Bot"))["kind"] == "handle_taken"
    assert (await lc.replace("finance", "CasaBrowser2222Bot"))["kind"] == "handle_owned"
    assert lc.store.agents["finance"]["change"] is None


async def test_only_the_current_suggestion_completes_a_change(world, root):
    lc, host = make_lifecycle(world, root)
    await _change_alex(lc, host, world, "CasaFinanceAaaaBot")
    await lc.replace("finance", "CasaFinanceBbbbBot")
    assert lc.store.agents["finance"]["change"]["old_username"] == "HeyAlexTestBot"
    world.tokens[777] = "777:another-token-zzzzzzzzzzzz"
    await lc.on_created(created(777, "Alex", "CasaFinanceAaaaBot"), OPERATOR)
    assert lc.store.bots[777]["phase"] == ovl.UNBOUND      # asked, nothing switched
    assert lc.store.agents["finance"]["change"]["new_bot"] is None
    assert world.restarts == 0
    world.tokens[NEW_BOT] = NEW_TOKEN
    await lc.on_created(created(NEW_BOT, "Alex", "casafinancebbbbbot"), OPERATOR)
    rec = lc.store.bots[NEW_BOT]
    assert rec["phase"] == ovl.SWITCHING and rec["role"] == "finance"
    agent = lc.store.agents["finance"]
    assert agent["change"]["new_bot"] == NEW_BOT and agent["bot_id"] == ALEX_BOT
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert world.restarts == 1
    assert world.card_text(agent["offer_card"]) == \
        "Alex is moving to @casafinancebbbbbot — Casa restarts for a moment."  # its own name
    assert _disk(root)["bots"][str(NEW_BOT)]["phase"] == ovl.SWITCHING
    world.tokens[779] = "779:third-token-yyyyyyyyyyyy"
    await lc.on_created(created(779, "Alex", "CasaFinanceBbbbBot"), OPERATOR)   # a second one
    assert lc.store.bots[779]["phase"] == ovl.UNBOUND
    assert lc.store.agents["finance"]["change"]["new_bot"] == NEW_BOT


async def test_a_new_offer_racing_the_creation_of_the_last_one_changes_nothing(world, root):
    # A new handle is asked for while the operator creates the bot of the
    # previous offer: the created bot has claimed the change, so the new offer
    # leaves the suggestion and the change as they are and asks for the restart.
    lc, host = make_lifecycle(world, root)
    await _change_alex(lc, host, world, "CasaFinanceAaaaBot")
    probing, created_done = asyncio.Event(), asyncio.Event()

    async def slow_probe(username):
        probing.set()
        await created_done.wait()
        return False

    host.handle_taken = slow_probe
    second = asyncio.create_task(lc.replace("finance", "CasaFinanceBbbbBot"))
    await probing.wait()
    world.tokens[NEW_BOT] = NEW_TOKEN
    await lc.on_created(created(NEW_BOT, "Alex", "CasaFinanceAaaaBot"), OPERATOR)
    created_done.set()
    res = await second
    agent = lc.store.agents["finance"]
    assert agent["change"]["new_bot"] == NEW_BOT
    assert agent["suggestion"]["username"] == "CasaFinanceAaaaBot"
    assert res["status"] in ("restarting", "restart_failed")
    assert _disk(root)["agents"]["finance"]["suggestion"]["username"] == "CasaFinanceAaaaBot"


async def test_an_older_handle_of_the_same_specialist_completes_nothing(world, root):
    lc, host = make_lifecycle(world, root)
    await _change_alex(lc, host, world)
    world.tokens[777] = "777:another-token-zzzzzzzzzzzz"
    await lc.on_created(created(777, "Alex", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[777]["phase"] == ovl.UNBOUND
    assert lc.store.agents["finance"]["change"]["new_bot"] is None
    assert world.restarts == 0


async def test_the_ask_offers_a_specialist_whose_change_waits_for_its_bot(world, root):
    lc, host = make_lifecycle(world, root)
    await _change_alex(lc, host, world)
    world.tokens[777] = "777:another-token-zzzzzzzzzzzz"
    await lc.on_created(created(777, "Alex", "HeyCasaFinanceBot"), OPERATOR)  # edited form
    assert lc.store.bots[777]["ask"]["candidates"] == ["browser", "finance"]
    assert world.restarts == 0


async def test_picking_a_specialist_with_a_pending_change_moves_it_at_a_restart(world, root):
    lc, host = make_lifecycle(world, root)
    await _change_alex(lc, host, world)
    world.tokens[777] = "777:another-token-zzzzzzzzzzzz"
    await lc.on_created(created(777, "Alex", "HeyCasaFinanceBot"), OPERATOR)
    ask_card = lc.store.bots[777]["ask"]["card"]
    await host.answer("Alex")
    rec = lc.store.bots[777]
    assert rec["phase"] == ovl.SWITCHING and rec["role"] == "finance"
    agent = lc.store.agents["finance"]
    assert agent["change"]["new_bot"] == 777 and agent["bot_id"] == ALEX_BOT
    assert world.restarts == 1
    assert world.card_text(ask_card) == "@HeyCasaFinanceBot is Alex's."
    assert world.card_text(agent["offer_card"]) == \
        "Alex is moving to @HeyCasaFinanceBot — Casa restarts for a moment."
    assert _disk(root)["bots"]["777"]["phase"] == ovl.SWITCHING


async def test_a_pick_after_the_change_was_dropped_moves_nothing(world, root):
    lc, host = make_lifecycle(world, root)
    await _change_alex(lc, host, world)
    world.tokens[777] = "777:another-token-zzzzzzzzzzzz"
    await lc.on_created(created(777, "Alex", "HeyCasaFinanceBot"), OPERATOR)
    _rid, options, answer = host.latest_ask()
    assert (await lc.drop_change("finance"))["status"] == "dropped"
    await answer(options.index("Alex"))
    assert lc.store.bots[777]["phase"] == ovl.UNBOUND
    assert lc.store.agents["finance"]["change"] is None
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND and world.restarts == 0
    assert "finance" not in lc.store.bots[777]["ask"]["candidates"]


async def test_a_question_asked_before_the_change_was_offered_is_asked_again_with_it(
        world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    await lc.offer("browser", "CasaBrowser2222Bot")
    world.tokens[777] = "777:another-token-zzzzzzzzzzzz"
    await lc.replace("finance", "CasaFinance4821Bot")
    lc.store.bots[777] = {"phase": ovl.UNBOUND, "role": None, "username": "HeyCasaFinanceBot",
                          "name": "Alex", "created_at": "2026-11-11T22:31:43+00:00",
                          "setup_digest": None, "photo_set": False,
                          "ask": {"state": "posted", "card": None, "rid": "old",
                                  "candidates": []}}
    await lc.store.save()
    lc2, host2 = make_lifecycle(world, root)
    await lc2.boot()                    # the old question is asked again
    assert "finance" in lc2.store.bots[777]["ask"]["candidates"]
    await host2.answer("Alex")
    assert lc2.store.bots[777]["phase"] == ovl.SWITCHING


async def test_a_failed_restart_request_says_the_move_waits_for_the_next_restart(world, root):
    world.restart_ok = False
    lc, host = make_lifecycle(world, root)
    await _change_alex(lc, host, world)
    world.tokens[NEW_BOT] = NEW_TOKEN
    await lc.on_created(created(NEW_BOT, "Alex", "CasaFinance4821Bot"), OPERATOR)
    assert lc.store.bots[NEW_BOT]["phase"] == ovl.SWITCHING
    assert world.restarts == 1
    assert world.card_text(lc.store.agents["finance"]["offer_card"]) == \
        "Alex moves to @CasaFinance4821Bot when Casa next restarts."


async def _created_switch(lc, host, world, new="CasaFinance4821Bot"):
    await _change_alex(lc, host, world, new)
    world.tokens[NEW_BOT] = NEW_TOKEN
    await lc.on_created(created(NEW_BOT, "Alex", new), OPERATOR)
    assert lc.store.bots[NEW_BOT]["phase"] == ovl.SWITCHING


async def test_asking_again_finishes_a_created_switch_with_another_restart(world, root):
    lc, host = make_lifecycle(world, root)
    await _created_switch(lc, host, world)
    cards = len(world.cards)
    world.open[ALEX_BOT] = ["a scheduled question"]
    res = await lc.replace("finance", "CasaFinance4821Bot")
    assert res["status"] == "error" and res["kind"] == "busy"
    del world.open[ALEX_BOT]
    res = await lc.replace("finance", "CasaFinance4821Bot")
    assert res["status"] == "restarting"
    assert world.restarts == 2 and len(world.cards) == cards
    world.restart_ok = False
    res = await lc.replace("finance", "CasaFinance4821Bot")
    assert res["status"] == "restart_failed"


async def test_dropping_a_change_before_create_keeps_the_old_bot(world, root):
    lc, host = make_lifecycle(world, root)
    await _change_alex(lc, host, world)
    res = await lc.drop_change("finance")
    assert res["status"] == "dropped"
    agent = lc.store.agents["finance"]
    assert agent["change"] is None and agent["bot_id"] == ALEX_BOT
    assert world.card_text(agent["offer_card"]) == "Alex keeps @HeyAlexTestBot."
    world.tokens[NEW_BOT] = NEW_TOKEN
    await lc.on_created(created(NEW_BOT, "Alex", "CasaFinance4821Bot"), OPERATOR)
    assert lc.store.bots[NEW_BOT]["phase"] == ovl.UNBOUND
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert (await lc.drop_change("finance"))["status"] == "nothing_to_drop"


async def test_a_change_cannot_be_dropped_once_its_bot_exists(world, root):
    lc, host = make_lifecycle(world, root)
    await _created_switch(lc, host, world)
    res = await lc.drop_change("finance")
    assert res["status"] == "error" and res["kind"] == "already_created"
    assert lc.store.agents["finance"]["change"]["new_bot"] == NEW_BOT


async def test_an_uninstall_drops_a_pending_change(world, root):
    lc, host = make_lifecycle(world, root)
    await _change_alex(lc, host, world)
    del world.configs["finance"]
    await lc.reconcile_role("finance")
    assert lc.store.agents["finance"]["change"] is None
    assert _disk(root)["agents"]["finance"]["change"] is None
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.DORMANT


async def test_a_switching_bot_holds_what_it_receives_and_says_setup_once(world, root):
    lc, host = make_lifecycle(world, root)
    await _created_switch(lc, host, world)
    assert host.endpoints[NEW_BOT].default_agent == ""
    for uid in (1, 2):
        assert await lc.intake(NEW_BOT, update(uid)) == "held"
    assert world.notices == [(NEW_BOT, ovl.SETUP_REPLY)]
    assert [u["update_id"] for u in lc.journal.pending(NEW_BOT)] == [1, 2]
    assert world.released == []
    lc2, host2 = make_lifecycle(world, root)       # a boot that does not apply it
    await lc2.boot()
    assert lc2.store.bots[NEW_BOT]["phase"] == ovl.SWITCHING
    assert host2.endpoints[NEW_BOT].default_agent == ""
    assert lc2.store.agents["finance"]["bot_id"] == ALEX_BOT


# ---------------------------------------------------------------------------
# a pending switch is applied at boot, before anything can start a bot
# ---------------------------------------------------------------------------

async def _restart_applying_switches(world, root, **kw):
    lc, host = make_lifecycle(world, root, **kw)
    await lc.apply_switches()
    await lc.boot()
    return lc, host


async def test_a_pending_switch_retires_the_old_bot_and_binds_the_new_one_at_boot(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    assert await lc.intake(ALEX_BOT, update(1, "/start")) == "deliver"     # the chat opens
    assert lc.store.agents["finance"]["chat_open_at"]
    await lc.replace("finance", "CasaFinance4821Bot")
    world.tokens[NEW_BOT] = NEW_TOKEN
    await lc.on_created(created(NEW_BOT, "Alex", "CasaFinance4821Bot"), OPERATOR)
    lc.journal.append(ALEX_BOT, update(2))          # something held for the old bot
    lc2, host2 = make_lifecycle(world, root)
    await lc2.apply_switches()
    disk = _disk(root)
    assert str(ALEX_BOT) not in disk["bots"]
    assert disk["bots"][str(NEW_BOT)]["phase"] == ovl.BINDING
    assert disk["agents"]["finance"]["bot_id"] == NEW_BOT
    assert world.op_items == {}
    await lc2.boot()
    assert ALEX_BOT not in lc2.store.bots
    assert not lc2.journal.path(ALEX_BOT).exists()
    assert [i["token"] for i in world.op_items.values()] == [NEW_TOKEN]
    agent = lc2.store.agents["finance"]
    assert agent["bot_id"] == NEW_BOT and lc2.store.bots[NEW_BOT]["phase"] == ovl.BOUND
    assert agent["chat_open_at"] is None and agent["change"] is None
    assert agent["moved_from"] == "HeyAlexTestBot"
    assert host2.endpoints[NEW_BOT].bot_token == NEW_TOKEN
    assert host2.endpoints[NEW_BOT].default_agent == "finance"
    assert ALEX_BOT not in host2.endpoints
    assert lc2.state("finance") == "not_open"


async def test_a_switch_with_a_durable_open_item_stays_pending_and_the_old_bot_boots(world, root):
    lc, host = make_lifecycle(world, root)
    await _created_switch(lc, host, world)
    world.open[ALEX_BOT] = ["a scheduled question"]
    lc2, host2 = await _restart_applying_switches(world, root)
    agent = lc2.store.agents["finance"]
    assert agent["change"]["new_bot"] == NEW_BOT and agent["bot_id"] == ALEX_BOT
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert host2.endpoints[ALEX_BOT].default_agent == "finance"
    assert lc2.store.bots[NEW_BOT]["phase"] == ovl.SWITCHING
    assert host2.endpoints[NEW_BOT].default_agent == ""
    assert world.card_text(agent["offer_card"]) == (
        "Alex still had something open in @HeyAlexTestBot, so the move to "
        "@CasaFinance4821Bot waits. Ask Ellen again once it's done.")
    del world.open[ALEX_BOT]
    assert (await lc2.replace("finance", "CasaFinance4821Bot"))["status"] == "restarting"
    lc3, _host3 = await _restart_applying_switches(world, root)
    assert lc3.store.agents["finance"]["bot_id"] == NEW_BOT
    assert lc3.store.bots[NEW_BOT]["phase"] == ovl.BOUND


async def test_the_switch_step_touches_no_telegram(world, root):
    lc, host = make_lifecycle(world, root)
    await _created_switch(lc, host, world)
    lc2, host2 = make_lifecycle(world, root)

    def refuse(name):
        async def call(*_a, **_kw):
            raise AssertionError(f"{name} called before the channels exist")
        return call
    for name in ("fetch_token", "start_endpoint", "stop_endpoint", "post_card", "edit_card",
                 "post_ask", "notify", "manager_me", "manager_check", "probe", "release_update",
                 "restrict_access", "set_description", "set_short_description", "set_commands",
                 "set_photo", "remove_photo", "request_restart", "handle_taken"):
        setattr(host2, name, refuse(name))
    await lc2.apply_switches()
    assert ALEX_BOT not in lc2.store.bots
    assert lc2.store.bots[NEW_BOT]["phase"] == ovl.BINDING
    assert lc2.store.agents["finance"]["bot_id"] == NEW_BOT


async def test_without_onepassword_the_old_token_file_is_removed(world, root):
    lc, host = make_lifecycle(world, root, onepassword=False)
    await _created_switch(lc, host, world)
    path = root / "agent-voices" / "tokens" / "finance"
    assert path.read_text() == ALEX_TOKEN
    lc2, _host2 = make_lifecycle(world, root, onepassword=False)
    await lc2.apply_switches()
    assert not path.exists()
    await lc2.boot()
    assert path.read_text() == NEW_TOKEN


async def test_a_token_that_cannot_be_deleted_does_not_stop_the_switch(world, root):
    lc, host = make_lifecycle(world, root)
    await _created_switch(lc, host, world)
    (item,) = world.op_items
    world.op_delete_fails = True
    lc2, host2 = await _restart_applying_switches(world, root)
    assert lc2.store.bots[NEW_BOT]["phase"] == ovl.BOUND
    assert list(world.op_items) == [item]               # rewritten in place
    assert world.op_items[item]["token"] == NEW_TOKEN
    assert host2.endpoints[NEW_BOT].bot_token == NEW_TOKEN


def test_a_token_item_already_gone_counts_as_deleted(tmp_path):
    world = World()
    store = ovl.TokenStore(tmp_path / "tokens", env=OP_ENV, run=FakeOp(world))
    item = store.store("finance", ALEX_TOKEN, None)
    store.delete("finance", item)
    assert world.op_items == {}
    store.delete("finance", item)                     # a re-run: nothing left, done
    store.delete("finance", None)
    item = store.store("finance", ALEX_TOKEN, None)
    world.op_delete_fails = True
    with pytest.raises(ovl.TokenStoreError):
        store.delete("finance", item)
    files = ovl.TokenStore(tmp_path / "files", env={})
    files.store("finance", ALEX_TOKEN, None)
    files.delete("finance", None)
    files.delete("finance", None)
    assert not (tmp_path / "files" / "finance").exists()


# ---------------------------------------------------------------------------
# a moved chat: its cards say so, and its opening offers to delete the old bot
# ---------------------------------------------------------------------------

async def _moved(world, root):
    lc, host = make_lifecycle(world, root)
    await _created_switch(lc, host, world)
    return await _restart_applying_switches(world, root)


async def test_the_new_chats_card_says_it_is_new(world, root):
    lc, _host = await _moved(world, root)
    text, buttons = world.cards[lc.store.agents["finance"]["offer_card"][2]]
    assert text == "Alex's new chat is ready: @CasaFinance4821Bot. Open it and say hi."
    assert buttons == [("Open Alex's chat", "https://t.me/CasaFinance4821Bot")]


async def test_opening_the_new_chat_offers_to_delete_the_old_bot(world, root):
    lc, _host = await _moved(world, root)
    assert await lc.intake(NEW_BOT, update(1, "hi")) == "deliver"
    assert lc.state("finance") == "open"
    text, buttons = world.cards[lc.store.agents["finance"]["offer_card"][2]]
    assert text == "Alex has moved to @CasaFinance4821Bot. You can delete @HeyAlexTestBot now."
    assert buttons == [("Delete @HeyAlexTestBot", ovl.BOTFATHER_DELETE)]
    disk = _disk(root)["agents"]["finance"]
    assert disk["moved_from"] is None and disk["chat_open_at"]
    assert await lc.intake(NEW_BOT, update(2)) == "deliver"     # said once
    assert world.card_text(lc.store.agents["finance"]["offer_card"]) == text


async def test_a_moved_chat_is_reported_until_it_opens(world, root):
    lc, _host = await _moved(world, root)
    assert lc.chat_moved(NEW_BOT) and lc.chat_opening(NEW_BOT)
    await lc.intake(NEW_BOT, update(1, "hi"))
    assert not lc.chat_moved(NEW_BOT) and not lc.chat_opening(NEW_BOT)


async def test_a_first_chat_is_not_a_moved_one(world, root):
    lc, host = make_lifecycle(world, root, channel=False)
    await _bind_alex(lc, host)
    assert lc.chat_opening(ALEX_BOT) and not lc.chat_moved(ALEX_BOT)
    assert not lc.chat_moved(FIDO_BOT)


# ---------------------------------------------------------------------------
# every unbound bot has an answerable question after a restart
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ask_state", ["null", "posting", "posted"])
async def test_an_unbound_bot_is_asked_again_after_a_restart(world, root, ask_state):
    lc, host = make_lifecycle(world, root)
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    rec = lc.store.bots[ALEX_BOT]
    old_card = rec["ask"]["card"]
    if ask_state == "null":
        rec["ask"] = None
    elif ask_state == "posting":
        rec["ask"] = {**rec["ask"], "state": "posting", "card": None}
    await lc.store.save()
    lc2, host2 = make_lifecycle(world, root)
    await lc2.boot()
    ask = lc2.store.bots[ALEX_BOT]["ask"]
    assert ask["state"] == "posted" and ask["rid"] in host2.asks
    if ask_state == "posted":
        assert world.card_text(old_card) == "Asked again below."
    await host2.answer("Alex")
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


async def test_a_question_that_ended_unanswered_is_asked_again(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    rid, _o, answer = host.latest_ask()
    await answer(None)
    assert lc.store.bots[ALEX_BOT]["ask"]["rid"] != rid
    assert lc.store.bots[ALEX_BOT]["ask"]["state"] == "posted"


# ---------------------------------------------------------------------------
# released stays released; recovery exits only on a successful fetch
# ---------------------------------------------------------------------------

async def test_none_releases_the_bot_and_points_at_botfather(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    await lc.intake(ALEX_BOT, update(1))
    card = lc.store.bots[ALEX_BOT]["ask"]["card"]
    await host.answer(ovl.ASK_NONE)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.RELEASED
    assert ALEX_BOT not in host.endpoints
    assert not lc.journal.path(ALEX_BOT).exists()
    assert world.cards[card[2]][1] == [("Delete it in BotFather", ovl.BOTFATHER_DELETE)]
    assert world.op_items == {}         # a bot never bound was never stored
    fetches = len(world.fetches)
    await lc.on_updated(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    lc2, _ = make_lifecycle(world, root)
    await lc2.boot()
    assert len(world.fetches) == fetches
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.RELEASED


@pytest.mark.parametrize("error", [
    NetworkError("timed out"),
    BadRequest("Bad Request: user is not a bot"),
    Forbidden("Forbidden: bot was blocked by the user"),
])
async def test_recovery_never_releases_on_another_failure(world, root, error):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    world.tokens[ALEX_BOT] = ALEX_TOKEN_2
    world.fetch_errors[ALEX_BOT] = error
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.RECOVERING
    assert lc.state("finance") == "no_bot" and lc.is_recovering("finance")
    lc2, _ = make_lifecycle(world, root)
    await lc2.boot()
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.RECOVERING


async def test_a_bot_deleted_in_botfather_is_released_and_the_agent_waits_again(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    await lc.intake(ALEX_BOT, update(1, "/start"))
    world.deleted.add(ALEX_BOT)          # deleted while Casa was down
    lc2, host2 = make_lifecycle(world, root)
    await lc2.boot()
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.RELEASED
    assert lc2.state("finance") == "no_bot" and lc2.bot_id_for("finance") is None
    assert lc2.store.agents["finance"]["chat_open_at"] is None
    card = lc2.store.agents["finance"]["offer_card"]
    assert world.card_text(card) == "Alex's chat was deleted — ask Ellen for a new one."
    assert len(world.op_items) == 1      # kept, overwritten by the next binding


async def test_a_successful_fetch_exits_recovery_with_an_unchanged_token(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    lc.store.bots[ALEX_BOT]["phase"] = ovl.RECOVERING   # a crash after the store
    await lc.store.save()
    lc2, host2 = make_lifecycle(world, root)
    await lc2.boot()
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert host2.endpoints[ALEX_BOT].bot_token == ALEX_TOKEN


async def test_recovery_that_began_dormant_lands_dormant(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    del world.configs["finance"]
    await lc.reconcile_role("finance")
    world.tokens[ALEX_BOT] = ALEX_TOKEN_2
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.DORMANT
    assert host.endpoints[ALEX_BOT].bot_token == ALEX_TOKEN_2


async def test_with_the_resident_unauthorized_children_wait_and_the_operator_is_told_once(
        world, root, caplog):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    world.tokens[FIDO_BOT] = FIDO_TOKEN
    await lc.on_created(created(FIDO_BOT, "Fido", "FidoHomeBot"), OPERATOR)  # unbound
    await lc.intake(FIDO_BOT, update(1))
    lc.store.bots[ALEX_BOT]["phase"] = ovl.RECOVERING
    lc.store.bots[555] = {**lc.store.bots[FIDO_BOT], "phase": ovl.SEEN, "ask": None,
                          "username": "X", "name": "X"}
    await lc.store.save()
    before = {b: dict(r) for b, r in lc.store.bots.items()}
    world.manager_ok = False
    world.fetches.clear()
    lc2, host2 = make_lifecycle(world, root)
    with caplog.at_level(logging.ERROR, logger="own_voice_lifecycle"):
        await lc2.boot()
        await lc2.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert world.fetches == []
    assert {b: r["phase"] for b, r in lc2.store.bots.items()} == \
        {b: r["phase"] for b, r in before.items()}
    assert [u["update_id"] for u in lc2.journal.pending(FIDO_BOT)] == [1]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "not authorized" in errors[0].getMessage()


# ---------------------------------------------------------------------------
# RELEASE_ON: exactly the deleted-bot error, as python-telegram-bot shapes it
# ---------------------------------------------------------------------------

_PTB_PROBE = textwrap.dedent("""\
    import asyncio, json, sys
    sys.path.insert(0, sys.argv[1])
    from telegram.request import BaseRequest
    import own_voice_lifecycle as ovl

    class Answer(BaseRequest):
        async def initialize(self): ...
        async def shutdown(self): ...
        @property
        def read_timeout(self):
            return 5.0
        async def do_request(self, url, method, request_data=None, **kw):
            return int(sys.argv[2]), json.dumps({"ok": False, "error_code": int(sys.argv[2]),
                                                 "description": sys.argv[3]}).encode()

    async def main():
        try:
            await Answer().post("https://api.telegram.org/botX/getManagedBotToken")
        except Exception as exc:
            print(json.dumps([type(exc).__name__, str(exc), ovl.establishes_deletion(exc)]))
    asyncio.run(main())
    """)


@pytest.mark.parametrize("status, description, releases", [
    (403, "Forbidden: user is deactivated", True),
    (403, "Forbidden: USER IS DEACTIVATED", True),
    (400, "Bad Request: user is not a bot", False),
    (400, "Bad Request: user is deactivated", False),     # not a Forbidden
    (403, "Forbidden: bot was blocked by the user", False),
    (401, "Unauthorized", False),
    (502, "Bad Gateway", False),
])
def test_only_the_deleted_bot_error_establishes_a_deletion(status, description, releases):
    """Through python-telegram-bot's own request layer (a separate process:
    the unit session stubs ``telegram``): a 403 keeps its "Forbidden: "
    prefix and becomes ``Forbidden``, which is all the rule matches."""
    code = Path(ovl.__file__).parent
    out = subprocess.run([sys.executable, "-c", _PTB_PROBE, str(code), str(status), description],
                         capture_output=True, text=True, timeout=60, check=True)
    cls, message, released = json.loads(out.stdout)
    assert released is releases
    if status == 403:
        assert cls == "Forbidden" and message == description


def test_the_rule_reads_the_class_and_the_text_in_the_unit_session():
    assert ovl.establishes_deletion(Forbidden("Forbidden: user is deactivated"))
    assert not ovl.establishes_deletion(BadRequest("Bad Request: user is deactivated"))
    assert not ovl.establishes_deletion(Forbidden("Forbidden: bot was blocked by the user"))


# ---------------------------------------------------------------------------
# the committed declaration is truth
# ---------------------------------------------------------------------------

async def test_a_message_in_the_uninstall_gap_gets_the_dormant_reply(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    del world.configs["finance"]          # committed; the hook has not run yet
    world.queues.discard("finance")
    assert await lc.intake(ALEX_BOT, update(1)) == "held"
    await lc.reconcile_role("finance")     # the hook
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.DORMANT
    assert world.notices[-1] == (ALEX_BOT, "Alex isn't installed right now.")
    assert lc.journal.pending(ALEX_BOT) == [] and world.released == []
    assert lc.disclosure("finance") == {"bot": "@HeyAlexTestBot",
                                        "delete_link": ovl.BOTFATHER_DELETE}


async def test_a_crash_between_the_uninstall_commit_and_its_hook_ends_dormant(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    del world.configs["finance"]
    world.queues.discard("finance")
    await lc.intake(ALEX_BOT, update(1))
    lc2, _ = make_lifecycle(world, root)   # the hook never ran
    await lc2.boot()
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.DORMANT
    assert world.notices[-1] == (ALEX_BOT, "Alex isn't installed right now.")


@pytest.mark.parametrize("crash", [False, True])
async def test_an_upgrade_that_drops_the_declaration_goes_dormant_and_back(world, root, crash):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    world.configs["finance"] = specialist("Alex", own=False)
    assert await lc.intake(ALEX_BOT, update(1)) == "held"     # between commit and hook
    if crash:
        lc, host = make_lifecycle(world, root)
        await lc.boot()
    else:
        await lc.on_upgrade("finance")
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.DORMANT
    assert lc.state("finance") == "dormant"
    assert world.notices[-1] == (ALEX_BOT, "Alex no longer has a separate chat — talk to Ellen.")
    notices = list(world.notices)
    assert await lc.intake(ALEX_BOT, update(2)) == "dormant_reply"
    assert world.notices == notices
    world.configs["finance"] = specialist("Alex")
    await lc.on_upgrade("finance")         # the rollback / upgrade that declares it again
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


async def test_an_install_or_upgrade_posts_no_offer_by_itself(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.on_install("finance")
    world.configs["browser"] = specialist("Fido", own=False)
    await lc.on_upgrade("browser")
    world.configs["browser"] = specialist("Fido")
    await lc.on_upgrade("browser")
    assert world.cards == {} and lc.store.agents == {}


async def test_boot_posts_no_offer_by_itself(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.boot()
    assert world.cards == {} and lc.store.agents == {}


# ---------------------------------------------------------------------------
# one token item per role, edited in place
# ---------------------------------------------------------------------------

async def test_one_item_per_role_through_bind_rotate_delete_and_recreate(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    (item,) = world.op_items
    world.tokens[ALEX_BOT] = ALEX_TOKEN_2
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    world.deleted.add(ALEX_BOT)
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.RELEASED
    world.tokens[8888] = "8888:recreated-token-dddddddddd"
    await lc.offer("finance", "HeyAlexTestBot")
    sugg = lc.store.agents["finance"]["suggestion"]
    await lc.on_created(created(8888, sugg["name"], sugg["username"]), OPERATOR)
    assert lc.store.bots[8888]["phase"] == ovl.BOUND
    assert list(world.op_items) == [item]
    assert world.op_items[item]["token"] == "8888:recreated-token-dddddddddd"
    assert world.op_items[item]["title"] == "Telegram Finance"
    for argv in world.op_argv:
        assert not any("token" in a for a in argv)


async def test_a_create_that_timed_out_is_found_by_lookup(world, root):
    world.op_create_timeout_once = True
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    assert len(world.op_items) == 1
    assert lc.store.agents["finance"]["token_item"] == next(iter(world.op_items))


async def test_a_missing_stored_id_is_found_by_title_and_tag(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    lc.store.agents["finance"]["token_item"] = None
    world.tokens[ALEX_BOT] = ALEX_TOKEN_2
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert len(world.op_items) == 1
    assert next(iter(world.op_items.values()))["token"] == ALEX_TOKEN_2


async def test_without_onepassword_the_token_is_a_private_file_replaced_in_place(world, root):
    lc, host = make_lifecycle(world, root, onepassword=False)
    await _bind_alex(lc, host)
    path = root / "agent-voices" / "tokens" / "finance"
    assert path.read_text() == ALEX_TOKEN
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    world.tokens[ALEX_BOT] = ALEX_TOKEN_2
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert path.read_text() == ALEX_TOKEN_2
    assert sorted(p.name for p in path.parent.iterdir()) == ["finance"]
    assert world.op_argv == []


def test_the_real_op_binary_gets_the_token_on_stdin_only(tmp_path, monkeypatch):
    """The store against an ``op`` executable on PATH (a fake): create once,
    then edit in place, the token never in argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    db = tmp_path / "db.json"
    log = tmp_path / "argv.log"
    script = bin_dir / "op"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        db_path, log_path = {str(db)!r}, {str(log)!r}
        args = sys.argv[1:]
        open(log_path, "a").write(json.dumps(args) + "\\n")
        try:
            db = json.load(open(db_path))
        except FileNotFoundError:
            db = {{}}
        def save():
            json.dump(db, open(db_path, "w"))
        if args[:2] == ["item", "list"]:
            print(json.dumps([{{"id": k, "title": v["title"], "tags": v["tags"]}}
                              for k, v in db.items()]))
        elif args[:2] == ["item", "create"]:
            tpl = json.load(sys.stdin)
            k = "id%d" % (len(db) + 1)
            db[k] = {{"title": tpl["title"], "tags": tpl["tags"],
                     "v": tpl["fields"][0]["value"]}}
            save(); print(json.dumps({{"id": k}}))
        elif args[:2] == ["item", "edit"]:
            if args[2] not in db:
                sys.exit(1)
            db[args[2]]["v"] = json.load(sys.stdin)["fields"][0]["value"]
            save(); print("{{}}")
        elif args[:2] == ["item", "get"]:
            if args[2] not in db:
                sys.exit(1)
            print(db[args[2]]["v"])
        else:
            sys.exit(2)
        """))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    store = ovl.TokenStore(tmp_path / "tokens", env=OP_ENV)
    item = store.store("finance", ALEX_TOKEN, None)
    assert store.load("finance", item) == ALEX_TOKEN
    assert store.store("finance", ALEX_TOKEN_2, item) == item
    assert store.load("finance", None) == ALEX_TOKEN_2
    assert len(json.loads(db.read_text())) == 1
    assert "alex-token" not in log.read_text()


# ---------------------------------------------------------------------------
# the picture follows the persona
# ---------------------------------------------------------------------------

async def test_the_persona_picture_is_set_changed_and_removed(world, root, tmp_path):
    avatar = tmp_path / "avatar.jpg"
    avatar.write_bytes(b"\xff\xd8\xff" + b"one")
    world.configs["finance"] = specialist("Alex", avatar=avatar)
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    photos = [c for c in world.setup if c[0] == "photo"]
    assert photos == [("photo", ALEX_TOKEN, avatar.read_bytes())]
    digest = lc.store.bots[ALEX_BOT]["setup_digest"]
    n = len(world.setup)
    await lc.on_persona("finance")         # unchanged: nothing is called
    assert len(world.setup) == n and lc.store.bots[ALEX_BOT]["setup_digest"] == digest
    avatar.write_bytes(b"\xff\xd8\xff" + b"two")
    await lc.on_persona("finance")
    assert world.setup[-1] == ("photo", ALEX_TOKEN, b"\xff\xd8\xfftwo")
    assert lc.store.bots[ALEX_BOT]["setup_digest"] != digest
    world.configs["finance"] = specialist("Alex", avatar=None)
    await lc.on_persona("finance")
    assert world.setup[-1][0] == "remove_photo"
    n = len(world.setup)
    await lc.on_persona("finance")
    assert len(world.setup) == n


async def test_a_missing_picture_logs_once_and_binding_completes(world, root, tmp_path, caplog):
    world.configs["finance"] = specialist("Alex", avatar=tmp_path / "gone.jpg")
    lc, host = make_lifecycle(world, root)
    with caplog.at_level(logging.WARNING, logger="own_voice_lifecycle"):
        await _bind_alex(lc, host)
        await lc.on_persona("finance")
    assert [c for c in world.setup if c[0] == "photo"] == []
    assert len([r for r in caplog.records if "avatar unreadable" in r.getMessage()]) == 1
    assert {c[0] for c in world.setup} >= {"description", "short", "commands", "access"}


async def test_a_refused_picture_never_fails_the_binding(world, root, tmp_path):
    avatar = tmp_path / "avatar.jpg"
    avatar.write_bytes(b"\xff\xd8\xffx")
    world.configs["finance"] = specialist("Alex", avatar=avatar)
    lc, host = make_lifecycle(world, root)

    async def refuse(token, photo):
        raise BadRequest("Bad Request: photo is invalid")
    host.set_photo = refuse
    await _bind_alex(lc, host)
    assert lc.state("finance") == "not_open"


async def test_self_setup_names_the_bot_after_the_persona(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)               # created as "AlexTest"
    assert [c for c in world.setup if c[0] == "name"] == [("name", ALEX_TOKEN, "Alex")]


async def test_a_persona_rename_sets_the_name_again(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    world.configs["finance"] = specialist("Alexa")
    await lc.on_persona("finance")
    assert [c for c in world.setup if c[0] == "name"] == [
        ("name", ALEX_TOKEN, "Alex"), ("name", ALEX_TOKEN, "Alexa")]
    await lc.on_persona("finance")           # unchanged: not set again
    assert len([c for c in world.setup if c[0] == "name"]) == 2


async def test_self_setup_restricts_the_bot_to_its_owner(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    assert ("access", ALEX_BOT, True) in world.setup
    assert ("commands", ALEX_TOKEN, (("new", "Start a fresh conversation"),)) in world.setup
    assert ("short", ALEX_TOKEN, "Keeps the household's finances") in world.setup


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

async def test_a_failed_fetch_is_retried_with_backoff(world, root):
    import asyncio
    world.fetch_errors[ALEX_BOT] = NetworkError("timed out")
    lc, host = make_lifecycle(world, root)
    lc.retry_delays = (1.0, 2.0)
    delays: list[float] = []
    gate = asyncio.Event()

    async def sleep(d):
        delays.append(d)
        await gate.wait()
    lc.sleep = sleep
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.SEEN
    await asyncio.sleep(0)
    del world.fetch_errors[ALEX_BOT]
    gate.set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert delays == [1.0]
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.UNBOUND
    await lc.close()


# ---------------------------------------------------------------------------
# The records file
# ---------------------------------------------------------------------------

def test_a_corrupt_or_foreign_record_file_is_set_aside(tmp_path):
    path = tmp_path / "agent-voices.json"
    path.write_text(json.dumps({"agents": {}, "bots": {"1": {"phase": "nonsense"}}}))
    store = ovl.VoiceStore(path)
    assert store.agents == {} and store.bots == {}
    assert (tmp_path / "agent-voices.json.corrupt").exists()


def _record_file(**agent_fields) -> dict:
    agent = {"offered_at": None, "offer_card": None, "bot_id": ALEX_BOT,
             "chat_open_at": None, "label": "Alex",
             "suggestion": {"name": "Alex", "username": "CasaFinance4821Bot"},
             "token_item": None, "handles": ["CasaFinance4821Bot"], "change": None,
             "moved_from": None}
    agent.update(agent_fields)
    agent = {k: v for k, v in agent.items() if v is not _ABSENT}
    bot = {"phase": "switching",
           "role": "finance", "username": "CasaFinance4821Bot", "name": "Alex",
           "ask": None, "setup_digest": None, "created_at": None, "photo_set": False}
    return {"agents": {"finance": agent}, "bots": {str(ALEX_BOT): bot}}


_ABSENT = object()


@pytest.mark.parametrize("fields", [
    {},
    {"change": {"new_bot": FIDO_BOT, "old_bot": ALEX_BOT, "old_username": "HeyAlexTestBot"}},
    {"change": {"new_bot": None, "old_bot": ALEX_BOT, "old_username": "HeyAlexTestBot"},
     "moved_from": "HeyAlexTestBot", "handles": []},
])
def test_a_record_file_with_the_new_agent_fields_loads(tmp_path, fields):
    path = tmp_path / "agent-voices.json"
    data = _record_file(**fields)
    path.write_text(json.dumps(data))
    store = ovl.VoiceStore(path)
    assert store.snapshot() == data
    assert not (tmp_path / "agent-voices.json.corrupt").exists()


@pytest.mark.parametrize("fields", [
    {"handles": _ABSENT, "change": _ABSENT, "moved_from": _ABSENT},   # before the upgrade
    {"handles": "CasaFinance4821Bot"},
    {"handles": [7]},
    {"change": {"new_bot": None, "old_bot": ALEX_BOT}},
    {"change": {"new_bot": None, "old_bot": ALEX_BOT, "old_username": "x", "extra": 1}},
])
def test_a_record_file_without_the_new_agent_fields_is_set_aside(tmp_path, fields):
    path = tmp_path / "agent-voices.json"
    path.write_text(json.dumps(_record_file(**fields)))
    store = ovl.VoiceStore(path)
    assert store.agents == {} and store.bots == {}
    assert (tmp_path / "agent-voices.json.corrupt").exists()


def test_a_token_belongs_to_the_bot_whose_id_it_starts_with():
    assert ovl.token_belongs_to(f"{ALEX_BOT}:abc", ALEX_BOT)
    assert not ovl.token_belongs_to(f"{FIDO_BOT}:abc", ALEX_BOT)
    assert not ovl.token_belongs_to(f"{ALEX_BOT}0:abc", ALEX_BOT)
    assert not ovl.token_belongs_to("garbage", ALEX_BOT)


async def test_a_stored_token_of_another_bot_is_never_used(world, root):
    """A stored token that works but is another bot's is never started under
    this bot: the right one is fetched again and stored in its place."""
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    item = lc.store.agents["finance"]["token_item"]
    world.op_items[item]["token"] = FIDO_TOKEN          # live, but Fido's
    world.fetches.clear()
    lc2, host2 = make_lifecycle(world, root)
    await lc2.boot()
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert host2.endpoints[ALEX_BOT].bot_token == world.tokens[ALEX_BOT]
    assert world.fetches == [ALEX_BOT]
    assert world.op_items[item]["token"] == ALEX_TOKEN


async def test_the_record_file_is_owner_only(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.post_offer("finance", "HeyAlexTestBot")
    assert stat.S_IMODE(os.stat(root / "agent-voices.json").st_mode) == 0o600


# ---------------------------------------------------------------------------
# The crash sweep
# ---------------------------------------------------------------------------

async def _scenario_ask_then_bind(lc, host, world):
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    await lc.intake(ALEX_BOT, update(1, "/start"))
    await lc.intake(ALEX_BOT, update(2))
    await host.answer("Alex")


async def _finish_ask_then_bind(lc, host, world):
    rec = lc.store.bots.get(ALEX_BOT)
    if rec is None:
        await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
        rec = lc.store.bots[ALEX_BOT]
    if rec["phase"] == ovl.UNBOUND:
        assert rec["ask"]["state"] == "posted" and rec["ask"]["rid"] in host.asks
        await host.answer("Alex")


def _check_ask_then_bind(lc, host, world, held):
    rec = lc.store.bots[ALEX_BOT]
    assert rec["phase"] == ovl.BOUND and rec["role"] == "finance"
    assert host.endpoints[ALEX_BOT].default_agent == "finance"
    assert len(world.op_items) == 1
    assert next(iter(world.op_items.values()))["token"] == ALEX_TOKEN
    firsts = []
    for _bot, uid in world.released:
        if uid not in firsts:
            firsts.append(uid)
    assert firsts == [u for u in (1, 2) if u in held]
    assert lc.journal.pending(ALEX_BOT) == []
    if 1 in held:
        assert lc.state("finance") == "open"


async def _scenario_match(lc, host, world):
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)


async def _finish_match(lc, host, world):
    if "finance" not in lc.store.agents:
        await lc.post_offer("finance", "HeyAlexTestBot")
    if ALEX_BOT not in lc.store.bots:
        await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    rec = lc.store.bots[ALEX_BOT]
    if rec["phase"] == ovl.UNBOUND:     # the offer card was lost with the crash
        await host.answer("Alex")


def _check_match(lc, host, world, held):
    rec = lc.store.bots[ALEX_BOT]
    assert rec["phase"] == ovl.BOUND
    assert len(world.op_items) == 1
    assert lc.store.agents["finance"]["offer_card"] is not None


async def _scenario_rotate(lc, host, world):
    await _bind_alex(lc, host)
    world.tokens[ALEX_BOT] = ALEX_TOKEN_2
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)


async def _finish_rotate(lc, host, world):
    if ALEX_BOT not in lc.store.bots or lc.store.bots[ALEX_BOT]["phase"] != ovl.BOUND:
        await _finish_match(lc, host, world)


def _check_rotate(lc, host, world, held):
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert host.endpoints[ALEX_BOT].bot_token == world.tokens[ALEX_BOT]
    assert len(world.op_items) == 1
    assert next(iter(world.op_items.values()))["token"] == world.tokens[ALEX_BOT]


async def _scenario_none(lc, host, world):
    await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    await lc.intake(ALEX_BOT, update(1))
    await host.answer(ovl.ASK_NONE)


async def _finish_none(lc, host, world):
    if ALEX_BOT not in lc.store.bots:
        await lc.on_created(created(ALEX_BOT, "Alex", "AlexHomeBot"), OPERATOR)
    if lc.store.bots[ALEX_BOT]["phase"] == ovl.UNBOUND:
        await host.answer(ovl.ASK_NONE)


def _check_none(lc, host, world, held):
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.RELEASED
    assert ALEX_BOT not in host.endpoints and lc.journal.pending(ALEX_BOT) == []
    assert world.op_items == {}


async def _scenario_delete(lc, host, world):
    await _bind_alex(lc, host)
    world.deleted.add(ALEX_BOT)
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)


async def _finish_delete(lc, host, world):
    if ALEX_BOT not in world.deleted:   # crashed before the deletion
        await _finish_match(lc, host, world)
        world.deleted.add(ALEX_BOT)
        await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    elif ALEX_BOT in lc.store.bots and lc.store.bots[ALEX_BOT]["phase"] == ovl.UNBOUND:
        await host.answer(ovl.ASK_NONE)


def _check_delete(lc, host, world, held):
    rec = lc.store.bots.get(ALEX_BOT)
    assert rec is None or rec["phase"] == ovl.RELEASED
    assert lc.bot_id_for("finance") is None
    assert len(world.op_items) <= 1


async def _scenario_uninstall(lc, host, world):
    await _bind_alex(lc, host)
    world.queues.discard("finance")
    del world.configs["finance"]
    await lc.intake(ALEX_BOT, update(1))
    await lc.reconcile_role("finance")


async def _finish_uninstall(lc, host, world):
    if "finance" in world.configs:      # crashed before the uninstall
        await _finish_match(lc, host, world)
        world.queues.discard("finance")
        del world.configs["finance"]
        await lc.reconcile_role("finance")


def _check_uninstall(lc, host, world, held):
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.DORMANT
    assert lc.journal.pending(ALEX_BOT) == []


async def _scenario_switch(lc, host, world):
    await _bind_alex(lc, host)
    await lc.replace("finance", "CasaFinance4821Bot")
    world.tokens[NEW_BOT] = NEW_TOKEN
    await lc.on_created(created(NEW_BOT, "Alex", "CasaFinance4821Bot"), OPERATOR)
    await lc.apply_switches()               # the restart, in this process
    await lc.boot()


async def _finish_switch(lc, host, world):
    world.tokens[NEW_BOT] = NEW_TOKEN
    if NEW_BOT not in lc.store.bots:        # crashed before the new bot was created
        found = lc.store.bot_of("finance")
        if found is None or found[1]["phase"] != ovl.BOUND:
            await _finish_match(lc, host, world)
        if lc.store.agents["finance"]["change"] is None:
            await lc.replace("finance", "CasaFinance4821Bot")
        await lc.on_created(created(NEW_BOT, "Alex", "CasaFinance4821Bot"), OPERATOR)
    if lc.store.bots[NEW_BOT]["phase"] == ovl.SWITCHING:
        await lc.apply_switches()
        await lc.boot()


def _check_switch(lc, host, world, held):
    agent = lc.store.agents["finance"]
    if agent["change"] is not None:          # still pending: the old bot runs
        assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
        assert lc.store.bots[NEW_BOT]["phase"] == ovl.SWITCHING
    else:
        assert ALEX_BOT not in lc.store.bots
        assert agent["bot_id"] == NEW_BOT and agent["moved_from"] == "HeyAlexTestBot"
        assert lc.store.bots[NEW_BOT]["phase"] == ovl.BOUND
        assert lc.store.bots[NEW_BOT]["role"] == "finance"
        assert host.endpoints[NEW_BOT].bot_token == NEW_TOKEN
    assert len(world.op_items) <= 1
    for item in world.op_items.values():
        assert ovl.token_belongs_to(item["token"], agent["bot_id"])


SCENARIOS = {
    "ask_then_bind": (_scenario_ask_then_bind, _finish_ask_then_bind, _check_ask_then_bind),
    "match": (_scenario_match, _finish_match, _check_match),
    "rotate": (_scenario_rotate, _finish_rotate, _check_rotate),
    "ask_none": (_scenario_none, _finish_none, _check_none),
    "deleted": (_scenario_delete, _finish_delete, _check_delete),
    "uninstall": (_scenario_uninstall, _finish_uninstall, _check_uninstall),
    "switch": (_scenario_switch, _finish_switch, _check_switch),
}


def _fresh_world() -> World:
    w = World()
    w.configs = {"finance": specialist("Alex"), "browser": specialist("Fido")}
    w.queues = {"finance", "browser"}
    w.tokens = {ALEX_BOT: ALEX_TOKEN, FIDO_BOT: FIDO_TOKEN}
    return w


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_the_crash_sweep(tmp_path, name):
    """Kill the process after every durable write and every effect of the
    transition; the next boot must reach a defined state."""
    run, finish, check = SCENARIOS[name]
    clean = _fresh_world()
    lc, host = make_lifecycle(clean, tmp_path / "clean")
    await run(lc, host, clean)
    total = clean.ticks
    phases_seen: set[str] = set()
    assert total > 5
    for k in range(1, total + 1):
        w = _fresh_world()
        w.crash_at = k
        root = tmp_path / f"{name}-{k}"
        lc, host = make_lifecycle(w, root)
        crashed = False
        try:
            await run(lc, host, w)
        except Crash:
            crashed = True
        assert crashed, f"step {k} of {total} did not crash"
        await lc.close()
        if (root / "agent-voices.json").exists():
            for rec in _disk(root)["bots"].values():
                phases_seen.add(rec["phase"])
        held = {u["update_id"] for u in lc.journal.pending(ALEX_BOT)} | {
            uid for _b, uid in w.released}
        w.crash_at = None
        lc2, host2 = make_lifecycle(w, root)
        await lc2.apply_switches()
        await lc2.boot()
        await finish(lc2, host2, w)
        check(lc2, host2, w, held)
    assert phases_seen, "no phase was ever persisted"


def test_every_phase_has_a_boot_reconciliation():
    assert set(ovl.BOOT_STEPS) == set(ovl.PHASES)
    for phase, method in ovl.BOOT_STEPS.items():
        assert callable(getattr(ovl.Lifecycle, method)), phase


async def test_the_sweep_reaches_every_phase_but_released_ones_alone(tmp_path):
    """Across the sweep's scenarios, the crash points cover every phase that
    has a transition out of it."""
    seen: set[str] = set()
    for name in sorted(SCENARIOS):
        run, _f, _c = SCENARIOS[name]
        w = _fresh_world()
        root = tmp_path / name
        lc, host = make_lifecycle(w, root)
        save = lc.store.save

        async def record(save=save, lc=lc):
            await save()
            seen.update(r["phase"] for r in lc.store.bots.values())
        lc.store.save = record
        await run(lc, host, w)
    assert seen == set(ovl.PHASES)


# ---------------------------------------------------------------------------
# The production host: agent endpoints on the real transport
# ---------------------------------------------------------------------------

class _Ep:
    def __init__(self, bot_id, token, role):
        self.bot_id, self.bot_token, self.default_agent = bot_id, token, role or ""
        self.has_bot_id, self.started, self.stopped, self.on_identity = True, 0, 0, None

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


async def test_the_host_builds_registers_binds_swaps_and_removes_agent_endpoints(
        tmp_path, monkeypatch):
    from channels.telegram_transport import CasaBotIdStore, TelegramTransport

    transport = TelegramTransport(casa_bot_ids=CasaBotIdStore(tmp_path / "c.json", "t"))
    casa = _Ep(7000, "casa-token", "assistant")
    transport.add_endpoint(casa, casa=True)
    host = ovl.TelegramHost()
    monkeypatch.setattr(host, "_transport", lambda: transport)
    monkeypatch.setattr(host, "make_endpoint", lambda b, t, r: _Ep(b, t, r))

    await host.start_endpoint(ALEX_BOT, ALEX_TOKEN, None)       # holding
    await host.start_endpoint(FIDO_BOT, FIDO_TOKEN, None)       # two at once
    alex = transport.endpoint(ALEX_BOT)
    assert alex.started == 1 and transport.route_for("finance") == transport.casa_route()
    await host.start_endpoint(ALEX_BOT, ALEX_TOKEN, "finance")  # bound: same endpoint
    assert transport.endpoint(ALEX_BOT) is alex and alex.started == 1
    assert transport.route_for("finance") == ALEX_BOT and alex.default_agent == "finance"
    await host.start_endpoint(ALEX_BOT, ALEX_TOKEN_2, "finance")  # a new token: swapped
    swapped = transport.endpoint(ALEX_BOT)
    assert swapped is not alex and alex.stopped == 1 and swapped.bot_token == ALEX_TOKEN_2
    assert transport.route_for("finance") == ALEX_BOT
    await host.start_endpoint(ALEX_BOT, ALEX_TOKEN_2, None)     # dormant: no agent
    assert transport.route_for("finance") == transport.casa_route()
    await host.stop_endpoint(ALEX_BOT)
    assert host.endpoint(ALEX_BOT) is None and swapped.stopped == 1
    assert swapped not in transport._endpoints
    await transport.stop()                                      # stops what is left
    assert transport.endpoint(FIDO_BOT).stopped == 1


def test_an_agent_endpoint_is_configured_like_casas(monkeypatch):
    from channels.telegram import TelegramChannel

    secret = lambda bot: f"secret-{bot}"  # noqa: E731
    casa = TelegramChannel(bot_token="7000:casa", chat_id=str(OPERATOR),
                           default_agent="assistant", bus=object(),
                           webhook_url="https://casa.example", is_casa=True,
                           known_bot_id=7000, webhook_secret_for=secret)
    casa._session_registry = object()
    host = ovl.TelegramHost()
    monkeypatch.setattr(host, "_casa", lambda: casa)
    ep = host.make_endpoint(ALEX_BOT, ALEX_TOKEN, None)
    assert (ep.bot_id, ep.bot_token, ep.chat_id, ep.default_agent) == (
        ALEX_BOT, ALEX_TOKEN, str(OPERATOR), "")
    assert ep.is_casa is False and ep._bus is casa._bus
    assert ep._webhook_url == "https://casa.example" and ep._webhook_secret_for is secret
    assert ep._session_registry is casa._session_registry


async def test_the_host_releases_through_the_endpoints_release_update_and_edits_by_index(
        monkeypatch):
    calls = []

    class Ep:
        async def release_update(self, update):
            calls.append(("release", update["update_id"]))
            return "no_target"

        async def edit_dm_message(self, chat, mid, text, *, buttons=None, urls=None):
            calls.append(("edit", chat, mid, text, buttons, urls))
            return True

    host = ovl.TelegramHost()
    monkeypatch.setattr(host, "endpoint", lambda bot_id: Ep())
    assert await host.release_update(ALEX_BOT, update(4)) == "no_target"
    await host.edit_card(TgAddress(7000, OPERATOR, 5), "ready",
                         [("Open Alex's chat", "https://t.me/HeyAlexTestBot")])
    await host.edit_card(TgAddress(7000, OPERATOR, 5), "open", [])
    assert calls == [
        ("release", 4),
        ("edit", OPERATOR, 5, "ready", [("Open Alex's chat", "noop")],
         ["https://t.me/HeyAlexTestBot"]),
        ("edit", OPERATOR, 5, "open", None, None)]


async def test_the_graceful_stop_cancels_pending_retries_before_the_channels_stop(
        world, root, monkeypatch):
    """A retry of an agent's bot waits up to an hour, then starts, stops or
    edits endpoints. Casa's graceful stop cancels it before the channels
    stop, so nothing wakes during the teardown."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    import casa_core
    import tools
    lc, _host = make_lifecycle(world, root)
    lc.retry_delays = (3600.0,)
    lc._schedule_retry(ALEX_BOT, "token not fetched")
    (retry,) = list(lc._tasks)
    monkeypatch.setattr(ovl, "_CURRENT", lc)
    seen = {}

    async def drain(_cm):
        seen["retry_done_at_channel_stop"] = retry.done()

    monkeypatch.setattr(casa_core, "_drain_broker_before_channel_shutdown", drain)
    monkeypatch.setattr(tools, "stop_engagement_launches", AsyncMock())
    monkeypatch.setattr(tools, "drain_delegation_settlements", AsyncMock())
    monkeypatch.setattr(casa_core, "_close_butler_ha_facade", AsyncMock())
    engagement_registry = MagicMock(begin_launch_shutdown=MagicMock(return_value=0),
                                    drain_launches=AsyncMock())
    await casa_core._shutdown_cleanup(
        job_registry=MagicMock(close=AsyncMock()), engagement_registry=engagement_registry,
        scheduler=MagicMock(shutdown=MagicMock()), session_sweeper=MagicMock(stop=AsyncMock()),
        runtime=MagicMock(agents={}), ha_facade=None,
        bus=MagicMock(begin_shutdown=MagicMock(), agent_loop_tasks=MagicMock(return_value=[]),
                      fail_pending=MagicMock(), live_dispatch_tasks=MagicMock(return_value=[])),
        loop_tasks=[], channel_manager=MagicMock(stop_all=AsyncMock()), runners=[])
    assert seen == {"retry_done_at_channel_stop": True}
    assert retry.cancelled() and not lc._tasks
    await asyncio.sleep(0)



# ---------------------------------------------------------------------------
# a managed bot's token never reaches a log record
# ---------------------------------------------------------------------------

_TOKEN_LOG_PROBE = textwrap.dedent("""\
    import asyncio, io, json, logging, sys
    sys.path.insert(0, sys.argv[1])
    mode, root = sys.argv[2], sys.argv[3]
    import httpx
    from telegram import Bot
    from telegram.request import HTTPXRequest
    import log_cid, log_redact
    import own_voice_lifecycle as ovl

    RESIDENT = "100000001:" + "R" * 35
    MANAGED = "200000002:" + "M" * 35
    ME = {"id": 200000002, "is_bot": True, "first_name": "Alex", "username": "AlexBot"}
    CASA_ME = {"id": 100000001, "is_bot": True, "first_name": "Casa", "username": "CasaBot"}

    def api(results):
        # a real PTB request over an in-process httpx transport
        def answer(request):
            method = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"ok": True, "result": results[method]})
        return HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(answer)})

    class Raw(logging.Handler):
        # every raw record, before any redaction runs
        lines = []
        def emit(self, record):
            Raw.lines.append(record.name + " " + record.getMessage())

    out = io.StringIO()
    log_cid.install_logging(stream=out, level=logging.DEBUG)
    root_logger = logging.getLogger()
    root_logger.handlers.insert(0, Raw(level=logging.DEBUG))
    for name in ("telegram", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)

    async def main():
        async with Bot(RESIDENT, request=api({"getManagedBotToken": MANAGED,
                                             "getMe": CASA_ME})) as resident:
            host = ovl.TelegramHost()
            host._casa = lambda: type("Casa", (), {"bot": resident})()
            Raw.lines.clear()
            if mode == "fetch":
                token = await host.fetch_token(200000002)
                return {"token": token == MANAGED,
                        "called": sum("getManagedBotToken" in l for l in Raw.lines),
                        "raw": sum(MANAGED in l for l in Raw.lines)}
            ovl.TelegramHost._bot = staticmethod(lambda t: Bot(t, request=api({"getMe": ME})))
            lc = ovl.Lifecycle(root=root, host=host, retry_delays=None)
            token, outcome = await lc._fetch(200000002)
            probed = await host.probe(token)
            for h in root_logger.handlers:
                h.flush()
            text = out.getvalue()
            return {"token": token == MANAGED and outcome == "ok" and probed == "ok",
                    "called": text.count("getMe"),
                    "rendered": text.count(MANAGED)}

    print(json.dumps(asyncio.run(main())))
""")


def _token_log_probe(mode: str, tmp_path: Path) -> dict:
    code = Path(ovl.__file__).parent
    r = subprocess.run([sys.executable, "-c", _TOKEN_LOG_PROBE, str(code), mode,
                        str(tmp_path / "data")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_fetching_a_managed_bot_token_logs_no_record_carrying_it(tmp_path):
    """Real python-telegram-bot over a fake HTTP transport, DEBUG on every
    logger: fetching the token leaves no raw record (any logger, any level,
    before any redaction) that carries it. A fresh interpreter, because the
    unit session stubs ``telegram``."""
    out = _token_log_probe("fetch", tmp_path)
    assert out["token"] is True
    assert out["called"] >= 1          # the call itself was logged, by URL
    assert out["raw"] == 0


def test_a_fetched_token_is_redacted_from_the_managed_bots_own_calls(tmp_path):
    """Through the lifecycle's fetch the token is registered for redaction
    before the managed bot's own calls log their URLs (PTB's base URL,
    httpx's request line): Casa's log output carries it zero times."""
    out = _token_log_probe("lifecycle", tmp_path)
    assert out["token"] is True
    assert out["called"] >= 1
    assert out["rendered"] == 0
