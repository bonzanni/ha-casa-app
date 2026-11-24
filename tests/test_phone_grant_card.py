"""Widening who is put through on the phone line waits for the operator's tap
on Casa's card.

The card's words are written by code from the stored change; the change it
carries (caller class, ability, the level before and after) is frozen when
the card is posted, and only the operator's Approve applies it, once, and
only while the level is still the one the card was about."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import verdict_broker
from phone import service
from phone import settings
from phone import store as pstore
from phone.store import PhoneStore
from verdict_broker import VerdictBroker

#: Casa's own bot and the operator's chat on it (a private chat's id is the
#: operator's user id).
CASA_BOT = 111
OPERATOR = 500
SCOPE = f"authz:{CASA_BOT}:{OPERATOR}"

ASKED = ("Asked the operator to confirm on a card. "
         "The card expires after five minutes.")
CARD = ("Phone: let other contacts be put through whenever they ask "
        "(now: when it is urgent)?")
WIDEN = {"action": "set_grant", "caller_class": "known", "level": "open"}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PHONE_ENABLED", "true")
    monkeypatch.delenv("PHONE_TRANSFER_NUMBER", raising=False)
    monkeypatch.delenv("PHONE_LINE_NUMBER", raising=False)
    monkeypatch.delenv("LIVE_VOICES", raising=False)
    monkeypatch.setenv("CONTACTS_DEFAULT_REGION", "NL")
    settings.reset()
    yield
    settings.reset()


@pytest.fixture(autouse=True)
def broker(monkeypatch: pytest.MonkeyPatch) -> VerdictBroker:
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture
def opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PhoneStore:
    store = PhoneStore(str(tmp_path / "phone"))
    assert store.open() is True, store.unavailable_reason
    monkeypatch.setattr(service, "_store", store)
    return store


class _FakeChannel:
    """Casa's endpoint double: a positive chat_id makes the operator
    resolve."""

    chat_id = str(OPERATOR)
    default_agent = "assistant"
    has_bot_id = True
    bot_id = CASA_BOT
    on_identity = None

    def __init__(self) -> None:
        self.posts: list[tuple] = []
        self.edits: list[tuple] = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False, danger=()):
        self.posts.append((chat_id, request_id, getattr(text, "text", text),
                           tuple(options)))
        return 42

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True


@pytest.fixture
def channel() -> _FakeChannel:
    import tools as tools_mod
    from telegram_channel_double import make_transport
    ch = _FakeChannel()
    tools_mod.init_tools(
        channel_manager=make_transport(ch), bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock())
    return ch


def _grants(store: PhoneStore) -> list[tuple]:
    with store.transaction() as conn:
        return conn.execute(
            "SELECT caller_class, ability, level FROM grants "
            "ORDER BY caller_class").fetchall()


def _set(store: PhoneStore, caller_class: str, level: str) -> None:
    with store.transaction(write=True) as conn:
        pstore.set_grant(conn, caller_class, "transfer", level)


@contextmanager
def _turn(clearance: str = "private", **overrides):
    """A turn in the operator's direct Telegram chat, stamped *clearance*."""
    import agent as agent_mod
    origin = {
        "role": "assistant", "execution_role": "assistant",
        "channel": "telegram", "source": "telegram",
        "message_type": "channel_in", "chat_id": str(OPERATOR),
        "user_id": OPERATOR, "cid": "c1", "_origin_route": "telegram",
        "_origin_clearance": clearance,
    }
    origin.update(overrides)
    tok = agent_mod.origin_var.set(origin)
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


async def _call(args: dict) -> dict:
    import tools as tools_mod
    with _turn():
        res = await tools_mod.phone_settings.handler(args)
    return json.loads(res["content"][0]["text"])


async def _ask(**args) -> dict:
    got = await _call({**WIDEN, **args})
    assert got["status"] == "awaiting_user", got
    return got


async def _answer(broker: VerdictBroker, rid: str, idx: int) -> None:
    """The operator's tap as the callback route commits it."""
    claim = broker.claim(namespace="resident_ask", scope=SCOPE,
                         request_id=rid, option_index=idx, actor_id=OPERATOR)
    assert not isinstance(claim, str), claim
    assert broker.commit(claim)
    await asyncio.wait_for(broker.drain_hooks(), 5)


async def test_wider_grant_posts_card_and_changes_nothing_yet(
        opened, channel, broker) -> None:
    got = await _ask()
    assert got == {"status": "awaiting_user", "request_id": got["request_id"],
                   "message": ASKED, "caller_class": "known",
                   "level": "open"}
    assert channel.posts == [(OPERATOR, got["request_id"], CARD,
                              ("Approve", "Cancel"))]
    assert _grants(opened) == []
    # The registration the tap handler and the broker check, with the change
    # frozen at request time.
    assert broker.pending(namespace="resident_ask", scope=SCOPE) == [
        got["request_id"]]
    req = broker._live[("resident_ask", SCOPE, got["request_id"])]
    assert req.timeout_s == 300.0
    assert {k: v for k, v in req.meta.items() if k != "message_id"} == {
        "options": ["Approve", "Cancel"], "bot_id": CASA_BOT,
        "chat_id": OPERATOR, "operator_id": OPERATOR,
        "kind": "phone_grant", "_scope": SCOPE,
        "change": {"caller_class": "known", "ability": "transfer",
                   "before": "urgent", "after": "open"}}


def test_card_text_reads_for_every_class_and_level() -> None:
    import tools as tools_mod
    assert tools_mod.phone_grant_question("unknown", "off", "emergency") == (
        "Phone: let unknown callers be put through only in an emergency "
        "(now: never)?")
    assert tools_mod.phone_grant_question("priority", "urgent", "open") == (
        "Phone: let priority contacts be put through whenever they ask "
        "(now: when it is urgent)?")


async def test_approve_applies_the_stored_change_once(
        opened, channel, broker) -> None:
    got = await _ask()
    # While the card waits the assistant asks for a different level: that is
    # a second card, and Approve on the first applies what the first stored.
    other = await _ask(level="open", caller_class="unknown")
    assert other["request_id"] != got["request_id"]
    await _answer(broker, got["request_id"], 0)
    assert _grants(opened) == [("known", "transfer", "open")]
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nChanged.")]
    # A second tap finds the request answered: nothing more is written.
    again = broker.claim(namespace="resident_ask", scope=SCOPE,
                         request_id=got["request_id"], option_index=0,
                         actor_id=OPERATOR)
    assert again == "duplicate"
    await asyncio.wait_for(broker.drain_hooks(), 5)
    assert _grants(opened) == [("known", "transfer", "open")]
    assert len(channel.edits) == 1


async def test_cancel_and_timeout_change_nothing(
        opened, channel, broker) -> None:
    got = await _ask()
    await _answer(broker, got["request_id"], 1)        # Cancel
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nNot changed.")]
    assert _grants(opened) == []

    channel.edits.clear()
    got = await _ask()
    broker._on_timeout(("resident_ask", SCOPE, got["request_id"]))  # 5 min on
    await asyncio.wait_for(broker.drain_hooks(), 5)
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nNot changed.")]
    assert _grants(opened) == []


async def test_approve_after_the_grant_changed_applies_nothing(
        opened, channel, broker) -> None:
    got = await _ask()
    # While the card waits the grant is narrowed, which applies at once.
    narrowed = await _call({**WIDEN, "level": "emergency"})
    assert narrowed["status"] == "ok", narrowed
    await _answer(broker, got["request_id"], 0)
    assert _grants(opened) == [("known", "transfer", "emergency")]
    assert channel.edits == [(
        OPERATOR, 42, f"{CARD}\n\nNot changed: the setting changed in the "
        "meantime. Other contacts are put through only in an emergency.")]


async def test_approve_after_the_grant_was_widened_elsewhere_says_so(
        opened, channel, broker) -> None:
    """Already at the card's level by another way: nothing is written and
    the card does not claim it changed anything."""
    got = await _ask()
    _set(opened, "known", "open")
    await _answer(broker, got["request_id"], 0)
    assert _grants(opened) == [("known", "transfer", "open")]
    assert channel.edits == [(
        OPERATOR, 42, f"{CARD}\n\nNot changed: the setting changed in the "
        "meantime. Other contacts are put through whenever they ask.")]


async def test_approve_after_the_grant_went_back_still_applies(
        opened, channel, broker) -> None:
    """What is compared is the level itself: narrowed and set back to the
    level the card was about, Approve applies."""
    got = await _ask()
    _set(opened, "known", "emergency")
    _set(opened, "known", "urgent")
    await _answer(broker, got["request_id"], 0)
    assert _grants(opened) == [("known", "transfer", "open")]
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nChanged.")]


async def test_approve_that_cannot_be_written_says_not_changed(
        opened, channel, broker, monkeypatch) -> None:
    got = await _ask()

    def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pstore, "set_grant", broken)
    await _answer(broker, got["request_id"], 0)
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nNot changed.")]
    assert _grants(opened) == []


async def test_approve_with_the_line_off_says_not_changed(
        opened, channel, broker, monkeypatch) -> None:
    got = await _ask()
    monkeypatch.setattr(service, "_store", None)
    await _answer(broker, got["request_id"], 0)
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nNot changed.")]
    assert _grants(opened) == []


async def test_a_change_landing_before_the_write_is_what_the_tool_compares_with(
        opened, channel, broker, monkeypatch) -> None:
    """Another change narrows the grant after any earlier read and just
    before the tool's write: the request, no longer narrower, is not written
    and becomes a card about the level found then."""
    _set(opened, "known", "urgent")
    real = opened.transaction
    landed: list[bool] = []

    def transaction(*, write: bool = False):
        if write and not landed:
            landed.append(True)
            with real(write=True) as conn:       # the other change
                pstore.set_grant(conn, "known", "transfer", "off")
        return real(write=write)

    monkeypatch.setattr(opened, "transaction", transaction)
    got = await _call({**WIDEN, "level": "emergency"})
    assert landed == [True]
    assert _grants(opened) == [("known", "transfer", "off")]
    assert got["status"] == "awaiting_user", got
    assert broker.pending(namespace="resident_ask", scope=SCOPE) == [
        got["request_id"]]
    req = broker._live[("resident_ask", SCOPE, got["request_id"])]
    assert req.meta["change"] == {"caller_class": "known",
                                  "ability": "transfer", "before": "off",
                                  "after": "emergency"}
    assert [p[2] for p in channel.posts] == [
        "Phone: let other contacts be put through only in an emergency "
        "(now: never)?"]


async def test_a_grant_that_cannot_be_written_is_not_applied(
        opened, channel, broker, monkeypatch) -> None:
    def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pstore, "set_grant", broken)
    got = await _call({**WIDEN, "level": "off"})
    assert got["status"] == "unavailable", got
    assert _grants(opened) == []
    assert channel.posts == []


async def test_a_grant_that_is_not_wider_posts_no_card(
        opened, channel, broker) -> None:
    for level in ("urgent", "emergency", "off"):
        got = await _call({**WIDEN, "level": level})
        assert got["status"] == "ok", got
    assert _grants(opened) == [("known", "transfer", "off")]
    assert channel.posts == []
    assert broker.pending(namespace="resident_ask", scope=SCOPE) == []


async def test_no_operator_chat_returns_unavailable(
        opened, channel, broker) -> None:
    import tools as tools_mod
    channel.chat_id = ""     # nobody is the operator
    assert tools_mod._casa_card_target()[2] == "no_operator"
    got = await _call(WIDEN)
    assert got["status"] == "unavailable", got
    assert got["caller_class"] == "known"
    assert channel.posts == []
    assert broker._live == {}
    assert _grants(opened) == []


async def test_before_casas_bot_has_an_id_no_card_is_registered_or_posted(
        opened, broker) -> None:
    """The card's identity names Casa's bot: before that bot has an id
    nothing is keyed, registered or posted."""
    import tools as tools_mod
    from channels.tg_address import EndpointNotReady
    from telegram_channel_double import MemoryBotIdStore, make_transport

    class _Unready(_FakeChannel):
        has_bot_id = False

        @property
        def bot_id(self) -> int:
            raise EndpointNotReady("Casa's bot has no id yet")

    ch = _Unready()
    tools_mod.init_tools(
        channel_manager=make_transport(ch, casa_bot_ids=MemoryBotIdStore()),
        bus=MagicMock(), specialist_registry=MagicMock(),
        mcp_registry=MagicMock())
    assert tools_mod._casa_card_target()[2] == "not_ready"
    got = await _call(WIDEN)
    assert got["status"] == "unavailable", got
    assert ch.posts == []
    assert broker._live == {}
    assert _grants(opened) == []


async def test_a_card_that_cannot_be_posted_registers_nothing(
        opened, channel, broker, monkeypatch) -> None:
    async def unresolved(**_k):
        return None

    monkeypatch.setattr(channel, "post_dm_keyboard", unresolved)
    got = await _call(WIDEN)
    assert got["status"] == "error", got
    assert broker._live == {}
    assert _grants(opened) == []


async def test_refused_call_registers_nothing(
        opened, channel, broker) -> None:
    import tools as tools_mod
    for clearance in ("family", "public"):
        with _turn(clearance):
            res = await tools_mod.phone_settings.handler(WIDEN)
        assert json.loads(res["content"][0]["text"])["status"] == "error"
    with _turn(execution_role="butler"):          # not a direct turn
        res = await tools_mod.phone_settings.handler(WIDEN)
    assert json.loads(res["content"][0]["text"])["status"] == "error"
    assert channel.posts == []
    assert broker._live == {}
    assert _grants(opened) == []


async def test_second_identical_request_posts_no_second_card(
        opened, channel, broker) -> None:
    first = await _ask()
    second = await _ask()
    assert second["request_id"] == first["request_id"]
    assert second["message"] == ("A card asking the operator to confirm is "
                                 "already waiting.")
    assert len(channel.posts) == 1
    assert broker.pending(namespace="resident_ask", scope=SCOPE) == [
        first["request_id"]]


def _callback(user_id: int, chat_id: int = OPERATOR):
    return SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(return_value=None))


async def test_operator_tap_through_the_real_telegram_callback(
        opened, channel, broker, fake_telegram_bot) -> None:
    from channels.telegram import TelegramChannel
    got = await _ask()
    rid = got["request_id"]
    tg = TelegramChannel(known_bot_id=CASA_BOT, bot=fake_telegram_bot,
                         chat_id=OPERATOR, bus=None)

    other = _callback(user_id=42)
    await tg._on_resident_callback(other, rid, 0)
    other.answer.assert_awaited_once_with("not for you")
    assert broker.pending(namespace="resident_ask", scope=SCOPE) == [rid]
    assert _grants(opened) == []

    tap = _callback(user_id=OPERATOR)
    await tg._on_resident_callback(tap, rid, 0)
    tap.answer.assert_awaited_once_with("✔")
    await asyncio.wait_for(broker.drain_hooks(), 5)
    assert _grants(opened) == [("known", "transfer", "open")]
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nChanged.")]
