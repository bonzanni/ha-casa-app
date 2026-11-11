"""An agent's own-chat state, and the one rule every surface of it asks.

Only an open chat admits a surface. The resident's chat is Casa's own and
always open; every other agent without an open chat of its own refuses with
a typed result — and the refusal itself posts nothing anywhere."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import own_voice
import own_voice_lifecycle as ovl
from own_voice_world import (
    ALEX_BOT, FIDO_BOT, OPERATOR, World, created, make_lifecycle, specialist, update,
)

ALEX_TOKEN = f"{ALEX_BOT}:alex-token-aaaaaaaaaaaaaaaa"
NEW_BOT = FIDO_BOT + 10
NEW_TOKEN = f"{NEW_BOT}:new-token-eeeeeeeeeeeeeeee"


@pytest.fixture
def world():
    w = World()
    w.configs = {"finance": specialist("Alex"), "plain": specialist("Pat", own=False)}
    w.queues = {"finance"}
    w.tokens = {ALEX_BOT: ALEX_TOKEN}
    return w


@pytest.fixture
def lc(world, tmp_path):
    lifecycle, host = make_lifecycle(world, tmp_path / "data")
    ovl.install(lifecycle)
    lifecycle.test_host = host
    yield lifecycle
    ovl.install(None)


async def _to(lc, world, state: str) -> None:
    """Drive Alex's chat to *state*."""
    if state == "no_bot":
        return
    await lc.post_offer("finance", "HeyAlexTestBot")
    if state == "unbound":
        await lc.on_created(created(ALEX_BOT, "Other", "OtherHomeBot"), OPERATOR)
        return
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    if state == "not_open":
        return
    if state == "recovering":
        world.fetch_errors[ALEX_BOT] = ovl_network_error()
        await lc.on_updated(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
        return
    await lc.intake(ALEX_BOT, update(1, "/start"))
    if state == "open":
        return
    if state == "dormant":
        del world.configs["finance"]
        await lc.reconcile_role("finance")
        return
    raise AssertionError(state)


def ovl_network_error():
    from telegram.error import NetworkError
    return NetworkError("timed out")


@pytest.mark.parametrize("state, own_state, kind, text", [
    ("no_bot", "no_bot", "own_chat_missing",
     "Alex's chat isn't set up yet — ask Ellen to create it."),
    ("unbound", "no_bot", "own_chat_missing",
     "Alex's chat isn't set up yet — ask Ellen to create it."),
    ("recovering", "no_bot", "own_chat_unreachable",
     "Alex's chat isn't reachable right now."),
    ("not_open", "not_open", "own_chat_not_open",
     "Alex's chat isn't open yet — tap Open Alex's chat in Ellen's chat."),
    ("open", "open", None, None),
    ("dormant", "dormant", "own_chat_missing", "Alex's chat is closed right now."),
])
async def test_the_refusal_follows_the_state(lc, world, state, own_state, kind, text):
    await _to(lc, world, state)
    assert own_voice.own_chat_state("finance") == own_state
    cards, notices = dict(world.cards), list(world.notices)
    refusal = own_voice.require_open_chat("finance", label="Alex")
    if kind is None:
        assert refusal is None
    else:
        assert refusal == own_voice.Refusal(kind, text)
    # deciding posts nothing, on any bot
    assert world.cards == cards and world.notices == notices


async def test_the_resident_is_never_refused(lc):
    assert own_voice.own_chat_state("assistant") == "not_declared"
    assert own_voice.require_open_chat("assistant", label="Ellen") is None


@pytest.mark.parametrize("role", ["butler", "plain", "configurator", "unknown"])
async def test_an_agent_behind_the_resident_has_no_surface(lc, role):
    assert own_voice.own_chat_state(role) == "not_declared"
    refusal = own_voice.require_open_chat(role, label="Pat")
    assert refusal is not None and refusal.kind == "own_chat_missing"


async def test_a_failed_send_in_an_open_chat_leaves_it_open(lc, world):
    await _to(lc, world, "open")
    # a send failure is the sender's typed failure; nothing here changes
    assert own_voice.own_chat_state("finance") == "open"
    assert own_voice.require_open_chat("finance", label="Alex") is None


async def test_declared_reads_the_committed_declaration(lc, world):
    assert own_voice.declared("finance") is True
    assert own_voice.declared("plain") is False
    world.configs["finance"] = specialist("Alex", own=False)
    assert own_voice.declared("finance") is False


async def test_the_endpoint_is_the_bound_bots_in_any_phase(lc, world):
    assert own_voice.endpoint_for("finance") is None
    await _to(lc, world, "not_open")
    assert own_voice.endpoint_for("finance").bot_id == ALEX_BOT
    del world.configs["finance"]
    await lc.reconcile_role("finance")
    assert own_voice.endpoint_for("finance").bot_id == ALEX_BOT


async def test_offer_own_chat_by_state(lc, world):
    assert (await own_voice.offer_own_chat("plain", username="HeyPatTestBot"))["kind"] \
        == "not_declared"
    first = await own_voice.offer_own_chat("finance", username="HeyAlexTestBot")
    assert first["status"] == "offered"
    assert first["suggested_username"] == "HeyAlexTestBot"
    old = lc.store.agents["finance"]["offer_card"]
    again = await own_voice.offer_own_chat("finance", username="HeyAlexTestBot")
    assert again["status"] == "offered"
    assert world.card_text(old) == "Replaced by the offer below."
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    not_open = await own_voice.offer_own_chat("finance")
    assert not_open == {"status": "open_card_posted", "link": "https://t.me/HeyAlexTestBot"}
    card = lc.store.agents["finance"]["offer_card"]
    assert world.card_text(card).startswith("Alex's chat is ready: @HeyAlexTestBot")
    await lc.intake(ALEX_BOT, update(1, "/start"))
    assert world.card_text(card) == "Alex's chat is open."
    assert await own_voice.offer_own_chat("finance") == {
        "status": "already_open", "link": "https://t.me/HeyAlexTestBot"}


@pytest.fixture
def ellens_tool(monkeypatch):
    """Ellen's ``offer_own_chat`` tool, with the installed specialists it
    resolves an agent's name against. Returns the tool's decoded result."""
    import tools
    monkeypatch.setattr(tools, "_agent_role_map", {
        "finance": SimpleNamespace(kind="specialist"),
        "plain": SimpleNamespace(kind="specialist")}, raising=False)

    async def call(args: dict) -> dict:
        res = await tools.offer_own_chat.handler(args)
        return json.loads(res["content"][0]["text"])
    return call


async def test_offer_own_chat_passes_its_mode_through(lc, world, ellens_tool):
    world.taken_handles = {"CasaFinance1111Bot"}
    checked = await ellens_tool({"agent": "finance", "mode": "check",
                                 "candidates": ["CasaFinance4821Bot", "CasaFinance1111Bot"]})
    assert checked["status"] == "checked"
    assert checked["candidates"] == {"CasaFinance4821Bot": "probably_free",
                                     "CasaFinance1111Bot": "taken"}
    assert world.cards == {}
    offered = await ellens_tool({"agent": "finance", "username": "HeyAlexTestBot"})
    assert offered["status"] == "offered"
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    replaced = await ellens_tool({"agent": "finance", "mode": "replace",
                                  "username": "CasaFinance4821Bot"})
    assert replaced == {"status": "change_offered", "suggested_username": "CasaFinance4821Bot",
                        "replaces": "@HeyAlexTestBot"}
    dropped = await ellens_tool({"agent": "finance", "mode": "drop"})
    assert dropped == {"status": "dropped", "keeps": "@HeyAlexTestBot"}
    assert lc.store.agents["finance"]["change"] is None
    unknown = await ellens_tool({"agent": "finance", "mode": "rename"})
    assert unknown["status"] == "error" and unknown["kind"] == "bad_mode"


async def test_an_install_that_declares_an_own_chat_says_an_offer_is_due(
        lc, world, monkeypatch):
    import tools
    monkeypatch.setattr(tools, "_bound_persona", lambda slug: None)
    out: dict = {"ok": True}
    await tools._after_own_chat_change(out, "finance", "on_install_committed", None)
    assert out["own_chat"] == "offer_due"
    assert world.cards == {}
    plain: dict = {"ok": True}
    await tools._after_own_chat_change(plain, "plain", "on_install_committed", None)
    assert "own_chat" not in plain


async def test_the_uninstall_disclosure(lc, world):
    assert own_voice.own_chat_disclosure("finance") is None
    await _to(lc, world, "not_open")
    assert own_voice.own_chat_disclosure("finance") == {
        "bot": "@HeyAlexTestBot", "delete_link": "https://t.me/BotFather?start=deletebot"}
    # the uninstall result names the bot from these records, not from an
    # endpoint that may already be stopped
    import tools as tools_mod
    assert tools_mod._own_chat_disclosure("finance") == own_voice.own_chat_disclosure("finance")
    assert tools_mod._own_chat_disclosure("plain") is None


async def test_the_entry_points_reach_the_lifecycle(lc, world):
    await own_voice.offer_own_chat("finance", username="HeyAlexTestBot")
    await own_voice.on_managed_bot_created(
        created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert own_voice.own_chat_state("finance") == "not_open"
    assert await own_voice.intake(ALEX_BOT, update(1, "/start")) == "deliver"
    assert own_voice.own_chat_state("finance") == "open"
    world.configs["finance"] = specialist("Alex", own=False)
    await own_voice.on_upgrade_committed("finance")
    assert own_voice.own_chat_state("finance") == "dormant"
    world.configs["finance"] = specialist("Alex")
    await own_voice.on_rollback_committed("finance")
    assert own_voice.own_chat_state("finance") == "open"
    await own_voice.on_persona_applied("finance")
    await own_voice.on_queue_registered("finance")
    del world.configs["finance"]
    await own_voice.on_uninstall_committed("finance")
    assert own_voice.own_chat_state("finance") == "dormant"
    world.configs["finance"] = specialist("Alex")
    await own_voice.on_install_committed("finance")
    assert own_voice.own_chat_state("finance") == "open"
    await own_voice.boot_reconcile()
    assert own_voice.own_chat_state("finance") == "open"


def test_without_a_lifecycle_configured_every_agent_is_not_declared(tmp_path, monkeypatch):
    """Other modules' unit tests read the state with no Telegram and no
    registry: it answers, it never raises."""
    ovl.install(ovl.Lifecycle(root=tmp_path, host=ovl.TelegramHost()))
    try:
        import tools
        monkeypatch.setattr(tools, "_specialist_registry", None, raising=False)
        monkeypatch.setattr(tools, "_channel_manager", None, raising=False)
        assert own_voice.own_chat_state("finance") == "not_declared"
        assert own_voice.endpoint_for("finance") is None
    finally:
        ovl.install(None)
