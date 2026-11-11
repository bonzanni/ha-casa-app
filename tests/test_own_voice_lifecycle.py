"""The own-chat lifecycle: an agent's bot from offer to release.

Driven through a fake Telegram world (``own_voice_world``) that survives
simulated restarts: a "restart" abandons the lifecycle and its host and
builds new ones over the same disk. The crash sweep kills the process after
every durable write and every external effect of each transition and checks
that boot completes or rolls it back."""
from __future__ import annotations

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

ALEX_TOKEN = "111111:alex-token-aaaaaaaaaaaaaaaa"
ALEX_TOKEN_2 = "111111:alex-token-rotated-cccccccc"
FIDO_TOKEN = "222222:fido-token-bbbbbbbbbbbbbbbb"


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
    await lc.post_offer("finance")
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


def _disk(root: Path) -> dict:
    return json.loads((root / "agent-voices.json").read_text())


# ---------------------------------------------------------------------------
# registration only from the operator's creation message
# ---------------------------------------------------------------------------

async def test_a_managed_bot_update_alone_registers_nothing(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.post_offer("finance")
    await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots == {} and world.fetches == []
    assert lc.state("finance") == "no_bot"


async def test_a_creation_sent_by_someone_else_is_ignored(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.post_offer("finance")
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
    ("Alex Test", "HeyAlexTestBot", False),     # edited name
    ("AlexTest", "HeyAlexBot", False),          # edited username
])
async def test_only_an_exact_match_binds(world, root, name, username, binds):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance")
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
    await lc.post_offer("finance")
    world.manager_first_name, world.manager_username = "Ellen", "EllenCasaBot"
    await lc.post_offer("finance")      # the operator used the first card's form
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.UNBOUND


async def test_two_agents_with_equal_suggestions_ask(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance")
    await lc.post_offer("browser")
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
    assert await lc.intake(ALEX_BOT, update(9)) == "dormant_reply"
    assert world.released == []
    assert world.notices[-1] == (ALEX_BOT, "Alex isn't installed right now.")


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
    await lc.post_offer("finance")
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

    await lc.post_offer("finance")
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
# open is the first operator message, released or live, edited once
# ---------------------------------------------------------------------------

async def test_a_released_held_start_opens_the_chat_once(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance")
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
    card = lc.store.agents["finance"]["offer_card"]
    assert world.card_text(card) == "Alex's chat is open."
    n = edits.count("Alex's chat is open.")
    assert await lc.intake(ALEX_BOT, update(2)) == "deliver"
    assert edits.count("Alex's chat is open.") == n


async def test_a_live_first_message_opens_the_chat_and_a_bot_message_does_not(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc, host)
    assert lc.state("finance") == "not_open"
    card = lc.store.agents["finance"]["offer_card"]
    assert world.card_text(card).startswith("Alex's chat is ready: @HeyAlexTestBot")
    await lc.intake(ALEX_BOT, {"update_id": 1, "callback_query": {"id": "x"}})
    assert lc.state("finance") == "not_open"
    assert await lc.intake(ALEX_BOT, update(2, "/start")) == "deliver"
    assert lc.state("finance") == "open"
    assert world.card_text(card) == "Alex's chat is open."


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
# the suggestion is derived from the resident's bot
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("first, username, agent, taken, expected", [
    ("EllenTest", "HeyEllenTestBot", "Alex", set(), ("AlexTest", "HeyAlexTestBot")),
    ("Casa", "CasaHomeBot", "Alex", set(), ("Alex", "AlexBot")),
    ("EllenTest", "HeyEllenTestBot", "Zoë", set(), ("ZoëTest", "HeyZoeTestBot")),
    ("Ellen", "Ellen_bot", "Al", set(), ("Al", "Al_bot")),
    ("Ellen", "XEllenX", "Alex", set(), ("Alex", "AlexBot")),         # no "bot" ending
    ("Ellen", "EllenBot", "A", set(), ("A", "")),                     # nothing valid
    ("EllenTest", "HeyEllenTestBot", "Alex", {"heyalextestbot"},
     ("AlexTest", "HeyAlexTest_financeBot")),
])
def test_the_suggestion(first, username, agent, taken, expected):
    assert ovl.suggest(resident_name="Ellen", resident_first_name=first,
                       resident_username=username, agent_name=agent, role="finance",
                       taken=taken) == expected


def test_the_create_link_is_the_trial_form():
    assert ovl.create_link("HeyEllenTestBot", "AlexTest", "HeyAlexTestBot") == \
        "https://t.me/newbot/HeyEllenTestBot/HeyAlexTestBot?name=AlexTest"
    assert ovl.create_link("HeyEllenTestBot", "Al Ex", "") == \
        "https://t.me/newbot/HeyEllenTestBot?name=Al+Ex"


async def test_the_offer_card_carries_the_suggestion(world, root):
    lc, _host = make_lifecycle(world, root)
    result = await lc.post_offer("finance")
    assert result == {"status": "offered", "suggested_name": "AlexTest",
                      "suggested_username": "HeyAlexTestBot"}
    text, buttons = world.cards[lc.store.agents["finance"]["offer_card"][2]]
    assert buttons == [("Create Alex's chat",
                        "https://t.me/newbot/HeyEllenTestBot/HeyAlexTestBot?name=AlexTest")]


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
    if crash:
        lc, host = make_lifecycle(world, root)
        await lc.boot()
    else:
        await lc.on_upgrade("finance")
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.DORMANT
    assert lc.state("finance") == "dormant"
    assert await lc.intake(ALEX_BOT, update(1)) == "dormant_reply"
    assert world.notices[-1] == (ALEX_BOT, "Alex no longer has a separate chat — talk to Ellen.")
    world.configs["finance"] = specialist("Alex")
    await lc.on_upgrade("finance")         # the rollback / upgrade that declares it again
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


async def test_an_upgrade_that_newly_declares_is_offered_once(world, root):
    del world.configs["browser"]
    world.configs["finance"] = specialist("Alex", own=False)
    lc, _host = make_lifecycle(world, root)
    await lc.on_upgrade("finance")
    assert "finance" not in lc.store.agents
    world.configs["finance"] = specialist("Alex")
    await lc.on_upgrade("finance")
    assert lc.store.agents["finance"]["offer_card"] is not None
    cards = len(world.cards)
    await lc.on_upgrade("finance")         # never re-offered on its own
    lc2, _ = make_lifecycle(world, root)
    await lc2.boot()
    assert len(world.cards) == cards


async def test_boot_offers_once_to_an_eligible_agent_with_no_record(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.boot()
    assert set(lc.store.agents) == {"finance", "browser"}
    cards = len(world.cards)
    lc2, _ = make_lifecycle(world, root)
    await lc2.boot()
    assert len(world.cards) == cards


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
    await lc.offer("finance")
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


async def test_the_record_file_is_owner_only(world, root):
    lc, _host = make_lifecycle(world, root)
    await lc.post_offer("finance")
    assert stat.S_IMODE(os.stat(root / "agent-voices.json").st_mode) == 0o600


# ---------------------------------------------------------------------------
# The crash sweep
# ---------------------------------------------------------------------------

async def _scenario_ask_then_bind(lc, host, world):
    await lc.post_offer("finance")
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
    await lc.post_offer("finance")
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)


async def _finish_match(lc, host, world):
    if "finance" not in lc.store.agents:
        await lc.post_offer("finance")
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


SCENARIOS = {
    "ask_then_bind": (_scenario_ask_then_bind, _finish_ask_then_bind, _check_ask_then_bind),
    "match": (_scenario_match, _finish_match, _check_match),
    "rotate": (_scenario_rotate, _finish_rotate, _check_rotate),
    "ask_none": (_scenario_none, _finish_none, _check_none),
    "deleted": (_scenario_delete, _finish_delete, _check_delete),
    "uninstall": (_scenario_uninstall, _finish_uninstall, _check_uninstall),
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
