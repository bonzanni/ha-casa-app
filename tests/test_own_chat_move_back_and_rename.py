"""A specialist moved back to a bot it was moved away from, and its bot's
display name chosen by the operator.

Driven through the fake Telegram world (``own_voice_world``): a "restart"
abandons the lifecycle and its host and builds new ones over the same disk."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import own_voice_lifecycle as ovl
from own_voice_world import (
    ALEX_BOT, FIDO_BOT, OPERATOR, NetworkError, World, created, make_lifecycle,
    specialist, update,
)

ALEX_TOKEN = f"{ALEX_BOT}:alex-token-aaaaaaaaaaaaaaaa"
FIDO_TOKEN = f"{FIDO_BOT}:fido-token-bbbbbbbbbbbbbbbb"
NEW_BOT = FIDO_BOT + 10
NEW_TOKEN = f"{NEW_BOT}:new-token-eeeeeeeeeeeeeeee"
OLD = "HeyAlexTestBot"
NEW = "CasaFinance4821Bot"


@pytest.fixture
def world() -> World:
    w = World()
    w.configs = {"finance": specialist("Alex"), "browser": specialist("Fido")}
    w.queues = {"finance", "browser"}
    w.tokens = {ALEX_BOT: ALEX_TOKEN, FIDO_BOT: FIDO_TOKEN, NEW_BOT: NEW_TOKEN}
    return w


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "data"


def _disk(root: Path) -> dict:
    return json.loads((root / "agent-voices.json").read_text())


async def _bind_alex(lc, username=OLD) -> None:
    await lc.post_offer("finance", username)
    await lc.on_created(created(ALEX_BOT, "AlexTest", username), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND


async def _restart(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.apply_switches()
    await lc.boot()
    return lc, host


async def _moved_to_new(world, root, old=OLD):
    """Alex bound to @old, then moved to @new at a restart."""
    lc, _host = make_lifecycle(world, root)
    await _bind_alex(lc, old)
    await lc.replace("finance", NEW)
    await lc.on_created(created(NEW_BOT, "Alex", NEW), OPERATOR)
    assert lc.store.bots[NEW_BOT]["phase"] == ovl.SWITCHING
    return await _restart(world, root)


def _create_links(world) -> list[str]:
    return [url for _text, buttons in world.cards.values() for _label, url in buttons
            if "/newbot/" in url]


# ---------------------------------------------------------------------------
# a switch keeps the old bot's record, released
# ---------------------------------------------------------------------------

async def test_a_switch_keeps_the_old_bot_released_and_it_is_not_the_agents_bot(world, root):
    lc, host = await _moved_to_new(world, root)
    old = lc.store.bots[ALEX_BOT]
    assert old["phase"] == ovl.RELEASED
    assert old["role"] == "finance" and old["username"] == OLD
    assert _disk(root)["bots"][str(ALEX_BOT)]["phase"] == ovl.RELEASED
    assert lc.store.bot_of("finance")[0] == NEW_BOT
    assert lc.bot_id_for("finance") == NEW_BOT
    assert lc.link_for("finance") == f"https://t.me/{NEW}"
    assert lc.disclosure("finance") == {"bot": f"@{NEW}", "delete_link": ovl.BOTFATHER_DELETE}
    assert ALEX_BOT not in host.endpoints and ALEX_BOT not in lc._tokens
    assert await lc.intake(ALEX_BOT, update(1)) == "held"
    assert not lc.journal.path(ALEX_BOT).exists()
    assert lc.chat_opening(ALEX_BOT) is False and lc.chat_moved(ALEX_BOT) is False
    assert lc.chat_opening(NEW_BOT) is True and lc.chat_moved(NEW_BOT) is True
    # another restart neither fetches its token nor starts it
    world.fetches.clear()
    lc2, host2 = await _restart(world, root)
    assert ALEX_BOT not in world.fetches and ALEX_BOT not in host2.endpoints
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.RELEASED
    assert lc2.bot_id_for("finance") == NEW_BOT
    # a new bot under that name is never matched to it
    assert lc2._match({"username": OLD}) is None


async def test_a_switch_cut_short_after_releasing_the_old_bot_completes(world, root):
    # The process died after the old bot was released and before the new one
    # was bound: the next boot finishes the move, whatever the released bot
    # still has open.
    lc, _host = make_lifecycle(world, root)
    await _bind_alex(lc)
    await lc.replace("finance", NEW)
    await lc.on_created(created(NEW_BOT, "Alex", NEW), OPERATOR)
    lc.store.bots[ALEX_BOT]["phase"] = ovl.RELEASED
    await lc.store.save()
    world.open[ALEX_BOT] = ["a scheduled question"]
    lc2, _host2 = await _restart(world, root)
    agent = lc2.store.agents["finance"]
    assert agent["change"] is None and agent["bot_id"] == NEW_BOT
    assert lc2.store.bots[NEW_BOT]["phase"] == ovl.BOUND


async def test_the_check_says_a_handle_of_a_bot_the_agent_used_before(world, root):
    lc, _host = await _moved_to_new(world, root)
    world.taken_handles = {OLD, NEW}
    res = await lc.check_handles("finance", [OLD, NEW])
    assert res["candidates"] == {OLD: "used_before", NEW: "taken"}
    assert res["context"]["current_handle"] == NEW
    assert res["context"]["previous_handles"] == [OLD]
    other = await lc.check_handles("browser", [OLD])
    assert other["candidates"] == {OLD: "owned_by_other"}
    assert other["context"]["previous_handles"] == []


# ---------------------------------------------------------------------------
# moving back: the token is fetched, the change recorded, Casa restarts
# ---------------------------------------------------------------------------

async def test_moving_back_fetches_the_token_and_switches_without_a_create_link(world, root):
    lc, _host = await _moved_to_new(world, root)
    world.taken_handles = {OLD, NEW}
    links = _create_links(world)
    world.fetches.clear()
    restarts = world.restarts
    res = await lc.replace("finance", OLD)
    assert res == {"status": "restarting", "new": f"@{OLD}"}
    assert world.fetches == [ALEX_BOT]
    assert world.restarts == restarts + 1
    assert _create_links(world) == links                 # no Create link posted
    agent = lc.store.agents["finance"]
    assert agent["change"] == {"new_bot": ALEX_BOT, "old_bot": NEW_BOT, "old_username": NEW}
    assert agent["bot_id"] == NEW_BOT
    assert agent["suggestion"]["username"] == OLD
    disk = _disk(root)
    assert disk["bots"][str(ALEX_BOT)]["phase"] == ovl.SWITCHING
    assert disk["bots"][str(ALEX_BOT)]["role"] == "finance"
    assert disk["bots"][str(NEW_BOT)]["phase"] == ovl.BOUND
    # the restart binds it again, sets it up and keeps the bot it left
    world.setup.clear()
    lc2, host2 = await _restart(world, root)
    agent = lc2.store.agents["finance"]
    assert agent["bot_id"] == ALEX_BOT and agent["change"] is None
    assert agent["moved_from"] == NEW
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert lc2.store.bots[NEW_BOT]["phase"] == ovl.RELEASED
    assert host2.endpoints[ALEX_BOT].bot_token == ALEX_TOKEN
    assert host2.endpoints[ALEX_BOT].default_agent == "finance"
    assert NEW_BOT not in host2.endpoints
    assert ("name", ALEX_TOKEN, "Alex") in world.setup
    assert [i["token"] for i in world.op_items.values()] == [ALEX_TOKEN]


async def test_a_move_back_waits_while_something_is_open_on_the_current_bot(world, root):
    lc, _host = await _moved_to_new(world, root)
    world.open[NEW_BOT] = ["a background job"]
    world.fetches.clear()
    res = await lc.replace("finance", OLD)
    assert res["status"] == "error" and res["kind"] == "busy"
    assert world.fetches == [] and lc.store.agents["finance"]["change"] is None


@pytest.mark.parametrize("failure", ["deleted", "network"])
async def test_a_bot_telegram_will_not_hand_over_changes_nothing(world, root, failure):
    lc, _host = await _moved_to_new(world, root)
    world.taken_handles = {OLD, NEW}
    if failure == "deleted":
        world.deleted.add(ALEX_BOT)
    else:
        world.fetch_errors[ALEX_BOT] = NetworkError("timed out")
    before = _disk(root)
    cards = dict(world.cards)
    restarts = world.restarts
    res = await lc.replace("finance", OLD)
    assert res["status"] == "error"
    assert res["kind"] == ("bot_gone" if failure == "deleted" else "bot_unreachable")
    assert f"@{OLD}" in res["message"]
    assert world.restarts == restarts and world.cards == cards
    after = _disk(root)
    assert after["agents"] == before["agents"]
    assert after["bots"][str(NEW_BOT)] == before["bots"][str(NEW_BOT)]
    if failure == "network":
        assert after["bots"] == before["bots"]
        return
    # a deleted bot is forgotten: its handle is an ordinary one again
    assert str(ALEX_BOT) not in after["bots"]
    world.taken_handles = {NEW}
    again = await lc.replace("finance", OLD)
    assert again["status"] == "change_offered"
    assert _create_links(world)[-1].endswith(f"/{OLD}?name=Alex")


async def test_a_released_bot_of_another_role_is_never_offered(world, root):
    # Alex's first bot came from an edited form, so its handle was never
    # offered to anyone; Alex then moved on and left it released.
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance", "CasaFinance1111Bot")
    await lc.on_created(created(ALEX_BOT, "Alex", "HeyCasaFinanceBot"), OPERATOR)
    await host.answer("Alex")
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    await lc.replace("finance", NEW)
    await lc.on_created(created(NEW_BOT, "Alex", NEW), OPERATOR)
    lc, _host = await _restart(world, root)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.RELEASED
    world.taken_handles = {"HeyCasaFinanceBot"}
    world.fetches.clear()
    res = await lc.check_handles("browser", ["HeyCasaFinanceBot"])
    assert res["candidates"] == {"HeyCasaFinanceBot": "taken"}
    assert res["context"]["previous_handles"] == []
    res = await lc.offer("browser", "HeyCasaFinanceBot")
    assert res["status"] == "error" and res["kind"] == "handle_taken"
    assert world.fetches == []
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.RELEASED
    assert (lc.store.agents.get("browser") or {}).get("change") is None
    # its own agent can have it back
    res = await lc.check_handles("finance", ["HeyCasaFinanceBot"])
    assert res["candidates"] == {"HeyCasaFinanceBot": "used_before"}


async def test_a_handle_once_offered_to_another_agent_is_not_moved_back_to(world, root):
    lc, host = make_lifecycle(world, root)
    await lc.post_offer("finance", "CasaFinance1111Bot")
    await lc.on_created(created(ALEX_BOT, "Alex", "HeyCasaFinanceBot"), OPERATOR)
    await host.answer("Alex")
    world.probe_down = True
    await lc.offer("browser", "HeyCasaFinanceBot")        # offered to Fido meanwhile
    world.probe_down = False
    await lc.replace("finance", NEW)
    await lc.on_created(created(NEW_BOT, "Alex", NEW), OPERATOR)
    lc, _host = await _restart(world, root)
    world.fetches.clear()
    res = await lc.check_handles("finance", ["HeyCasaFinanceBot"])
    assert res["candidates"] == {"HeyCasaFinanceBot": "owned_by_other"}
    res = await lc.replace("finance", "HeyCasaFinanceBot")
    assert res["status"] == "error" and res["kind"] == "handle_owned"
    assert world.fetches == [] and lc.store.agents["finance"]["change"] is None


async def test_a_bot_created_while_the_token_is_fetched_keeps_its_claim(world, root):
    # Alex has an offer out for a new handle; while Casa fetches the old
    # bot's token, the operator creates the offered bot: that bot wins the
    # change, and the old one stays released.
    import asyncio
    lc, host = await _moved_to_new(world, root)
    await lc.replace("finance", "CasaFinance9999Bot")
    fetching, created_done = asyncio.Event(), asyncio.Event()
    fetch = host.fetch_token

    async def slow_fetch(bot_id):
        if bot_id == ALEX_BOT:
            fetching.set()
            await created_done.wait()
        return await fetch(bot_id)
    host.fetch_token = slow_fetch
    back = asyncio.create_task(lc.replace("finance", OLD))
    await fetching.wait()
    world.tokens[777] = "777:third-token-yyyyyyyyyyyy"
    await lc.on_created(created(777, "Alex", "CasaFinance9999Bot"), OPERATOR)
    created_done.set()
    res = await back
    assert res["status"] == "restarting" and res["new"] == "@CasaFinance9999Bot"
    agent = lc.store.agents["finance"]
    assert agent["change"]["new_bot"] == 777
    assert agent["suggestion"]["username"] == "CasaFinance9999Bot"
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.RELEASED
    assert lc.store.bots[777]["phase"] == ovl.SWITCHING


async def test_an_agent_whose_bot_was_deleted_can_move_back_to_an_earlier_one(world, root):
    lc, _host = await _moved_to_new(world, root)
    world.deleted.add(NEW_BOT)
    await lc.on_updated(created(NEW_BOT, "Alex", NEW), OPERATOR)
    assert lc.store.bots[NEW_BOT]["phase"] == ovl.RELEASED
    assert lc.bot_id_for("finance") is None
    world.taken_handles = {OLD}
    res = await lc.offer("finance", OLD)
    assert res["status"] == "restarting"
    assert lc.store.agents["finance"]["change"] == {
        "new_bot": ALEX_BOT, "old_bot": None, "old_username": ""}
    lc2, _host2 = await _restart(world, root)
    assert lc2.bot_id_for("finance") == ALEX_BOT
    assert lc2.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    assert lc2.state("finance") == "not_open"


# ---------------------------------------------------------------------------
# the bot's display name
# ---------------------------------------------------------------------------

def _names(world) -> list[tuple[str, str]]:
    return [(token, name) for what, token, name in world.setup if what == "name"]


async def test_a_rename_sets_the_bound_bots_name_at_once_without_a_restart(world, root):
    lc, _host = make_lifecycle(world, root)
    await _bind_alex(lc)
    assert _names(world) == [(ALEX_TOKEN, "Alex")]
    res = await lc.rename("finance", "Alex the Accountant")
    assert res == {"status": "renamed", "name": "Alex the Accountant"}
    assert _names(world) == [(ALEX_TOKEN, "Alex"), (ALEX_TOKEN, "Alex the Accountant")]
    assert world.restarts == 0 and lc.bot_id_for("finance") == ALEX_BOT
    assert _disk(root)["agents"]["finance"]["suggestion"]["name"] == "Alex the Accountant"
    # the cards keep the persona's name
    assert lc._bound_card("finance")[0].startswith("Alex's chat is ready")
    # the same name again changes nothing; a restart keeps it
    assert (await lc.rename("finance", "Alex the Accountant"))["status"] == "renamed"
    lc2, _host2 = await _restart(world, root)
    assert len(_names(world)) == 2
    # a persona change does not undo the chosen name
    world.configs["finance"] = specialist("Alexa")
    await lc2.on_persona("finance")
    assert "Alexa" not in [name for _t, name in _names(world)]


async def test_without_a_chosen_name_the_bot_takes_the_persona_name(world, root):
    lc, host = make_lifecycle(world, root)
    # bound through the question, with no offer and so no suggestion
    await lc.on_created(created(ALEX_BOT, "Whatever", "SomeAlexBot"), OPERATOR)
    await host.answer("Alex")
    assert lc.store.agents["finance"]["suggestion"] is None
    assert _names(world) == [(ALEX_TOKEN, "Alex")]
    world.configs["finance"] = specialist("Alexa")
    await lc.on_persona("finance")
    assert _names(world)[-1] == (ALEX_TOKEN, "Alexa")


async def test_a_stored_persona_name_is_not_a_chosen_one(world, root):
    # The suggestion of an offer made before names could be chosen holds the
    # persona's name of that time: a later persona name still renames the bot.
    lc, _host = make_lifecycle(world, root)
    await _bind_alex(lc)
    agent = lc.store.agents["finance"]
    agent["suggestion"] = {"name": "Alex", "username": OLD}
    assert agent["label"] == "Alex"
    await lc.store.save()
    world.configs["finance"] = specialist("Alexa")
    await lc.on_persona("finance")
    assert _names(world)[-1] == (ALEX_TOKEN, "Alexa")


async def test_a_persona_change_before_the_first_bot_is_followed_after_binding(world, root):
    # An offer stored the persona's name of its time; the persona changes
    # before the bot is created: the bot follows the persona, then and later.
    lc, _host = make_lifecycle(world, root)
    await lc.post_offer("finance", OLD)
    agent = lc.store.agents["finance"]
    agent["suggestion"] = {"name": "Alex", "username": OLD}
    await lc.store.save()
    world.configs["finance"] = specialist("Alexa")
    await lc.on_created(created(ALEX_BOT, "Alex", OLD), OPERATOR)
    assert _names(world) == [(ALEX_TOKEN, "Alexa")]
    lc2, _host2 = await _restart(world, root)
    world.configs["finance"] = specialist("Alexandra")
    await lc2.on_persona("finance")
    assert _names(world) == [(ALEX_TOKEN, "Alexa"), (ALEX_TOKEN, "Alexandra")]


async def test_a_rename_while_the_bot_is_not_bound_waits_for_its_setup(world, root):
    lc, _host = make_lifecycle(world, root)
    await _bind_alex(lc)
    world.fetch_errors[ALEX_BOT] = NetworkError("timed out")
    await lc.on_updated(created(ALEX_BOT, "Alex", OLD), OPERATOR)
    assert lc.store.bots[ALEX_BOT]["phase"] == ovl.RECOVERING
    res = await lc.rename("finance", "Alex the Accountant")
    assert res["status"] == "name_saved"
    assert _names(world) == [(ALEX_TOKEN, "Alex")]


async def test_a_name_chosen_before_the_bot_exists_names_the_form_and_the_bot(world, root):
    lc, _host = make_lifecycle(world, root)
    res = await lc.rename("finance", "Alex F")
    assert res["status"] == "name_saved"
    assert world.cards == {}
    await lc.offer("finance", OLD)
    assert _create_links(world) == [f"https://t.me/newbot/HeyEllenTestBot/{OLD}?name=Alex+F"]
    text = next(iter(world.cards.values()))[0]
    assert text.startswith("Alex can get a separate chat")
    await lc.on_created(created(ALEX_BOT, "Alex F", OLD), OPERATOR)
    assert _names(world) == [(ALEX_TOKEN, "Alex F")]
    # a new handle keeps the chosen name
    await lc.replace("finance", NEW)
    assert _create_links(world)[-1] == f"https://t.me/newbot/HeyEllenTestBot/{NEW}?name=Alex+F"
    assert lc.store.agents["finance"]["suggestion"] == {"name": "Alex F", "username": NEW}


async def test_a_rename_telegram_refuses_is_applied_at_the_next_restart(world, root):
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc)

    async def refuse(token, name):
        raise NetworkError("timed out")
    host.set_name = refuse
    res = await lc.rename("finance", "Alex the Accountant")
    assert res["status"] == "error" and res["kind"] == "not_applied"
    assert lc.store.agents["finance"]["suggestion"]["name"] == "Alex the Accountant"
    await _restart(world, root)
    assert _names(world)[-1] == (ALEX_TOKEN, "Alex the Accountant")


async def test_a_rename_back_after_a_partly_applied_setup_reaches_telegram(world, root):
    # The new name was set but another setup call failed: the digest of the
    # earlier, fully applied setup no longer describes the bot.
    lc, host = make_lifecycle(world, root)
    await _bind_alex(lc)
    set_description = host.set_description

    async def refuse(token, text):
        raise NetworkError("timed out")
    host.set_description = refuse
    await lc.rename("finance", "Alex the Accountant")
    host.set_description = set_description
    assert (await lc.rename("finance", "Alex"))["status"] == "renamed"
    assert _names(world) == [(ALEX_TOKEN, "Alex"), (ALEX_TOKEN, "Alex the Accountant"),
                             (ALEX_TOKEN, "Alex")]


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
async def test_a_name_telegram_cannot_take_is_refused(world, root, name):
    lc, _host = make_lifecycle(world, root)
    await _bind_alex(lc)
    res = await lc.rename("finance", name)
    assert res["status"] == "error" and res["kind"] == "invalid_name"
    assert _names(world) == [(ALEX_TOKEN, "Alex")]


async def test_a_rename_needs_an_agent_with_a_chat_of_its_own(world, root):
    lc, _host = make_lifecycle(world, root)
    res = await lc.rename("nobody", "Somebody")
    assert res["status"] == "error" and res["kind"] == "not_declared"
    assert "nobody" not in lc.store.agents
