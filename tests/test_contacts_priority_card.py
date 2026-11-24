"""Making a contact priority waits for the operator's tap on Casa's card.

The card's words are written by code from the stored contact; the change it
carries is frozen when the card is posted and applied, by (source, uid), only
on the operator's Approve. Invented people and numbers only."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import verdict_broker
from contacts import service
from contacts import store as cstore
from contacts.store import ContactsStore, Person
from verdict_broker import VerdictBroker

#: Casa's own bot and the operator's chat on it (a private chat's id is the
#: operator's user id).
CASA_BOT = 111
OPERATOR = 500
SCOPE = f"authz:{CASA_BOT}:{OPERATOR}"

ASKED = ("Asked the operator to confirm on a card. "
         "The card expires after five minutes.")
CARD = ("Make priority?\n\n"
        "Marco Example\n+31201110001\n+31201110009\n\n"
        "Casa treats priority contacts as people you always want to hear "
        "from.")


@pytest.fixture(autouse=True)
def broker(monkeypatch: pytest.MonkeyPatch) -> VerdictBroker:
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture
def opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContactsStore:
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open() is True, store.unavailable_reason
    monkeypatch.setattr(service, "_store", store)
    monkeypatch.setenv("CONTACTS_DEFAULT_REGION", "NL")
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


def _person(uid: str, name: str, *numbers: str,
            groups: tuple[str, ...] = ()) -> Person:
    return Person(source="icloud", uid=uid, name=name,
                  first_name=name.split()[0], org="", note="",
                  numbers=tuple(numbers), groups=groups, related=())


MARCO = _person("A", "Marco Example", "+31201110009", "+31201110001")


def _fill(store: ContactsStore, people: list[Person], marks=()) -> None:
    with store.transaction(write=True) as conn:
        cstore.replace_source(conn, "icloud", people)
        for uid, field, value, origin in marks:
            cstore.set_mark(conn, "icloud", uid, field, value, origin)


def _rows(store: ContactsStore) -> list[tuple]:
    with store.transaction() as conn:
        return conn.execute(
            "SELECT uid, field, origin, value FROM marks "
            "ORDER BY uid, field, origin").fetchall()


def _marks(store: ContactsStore, uid: str) -> cstore.Marks:
    with store.transaction() as conn:
        return cstore.marks_for(conn, "icloud", uid)


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
        res = await tools_mod.set_contact_marks.handler(args)
    return json.loads(res["content"][0]["text"])


async def _ask(contact: str = "marco example", **more) -> dict:
    got = await _call({"contact": contact, "priority": True, **more})
    assert got["status"] == "awaiting_user", got
    return got


async def _answer(broker: VerdictBroker, rid: str, idx: int) -> None:
    """The operator's tap as the callback route commits it."""
    claim = broker.claim(namespace="resident_ask", scope=SCOPE,
                         request_id=rid, option_index=idx, actor_id=OPERATOR)
    assert not isinstance(claim, str), claim
    assert broker.commit(claim)
    await asyncio.wait_for(broker.drain_hooks(), 5)


async def test_priority_true_posts_card_and_changes_nothing_yet(
        opened, channel, broker) -> None:
    _fill(opened, [MARCO, _person("B", "Zoe Example", "+31201110003")])
    got = await _ask()
    assert got == {"status": "awaiting_user", "request_id": got["request_id"],
                   "message": ASKED, "contact": "Marco Example",
                   "applied": {}}
    assert channel.posts == [(OPERATOR, got["request_id"], CARD,
                              ("Approve", "Cancel"))]
    assert _marks(opened, "A").priority is False
    assert _rows(opened) == []
    # The registration the tap handler and the broker check, with the change
    # frozen at request time.
    assert broker.pending(namespace="resident_ask", scope=SCOPE) == [
        got["request_id"]]
    req = broker._live[("resident_ask", SCOPE, got["request_id"])]
    assert req.timeout_s == 300.0
    assert {k: v for k, v in req.meta.items() if k != "message_id"} == {
        "options": ["Approve", "Cancel"], "bot_id": CASA_BOT,
        "chat_id": OPERATOR, "operator_id": OPERATOR,
        "kind": "contact_priority", "_scope": SCOPE,
        "change": {"source": "icloud", "uid": "A", "name": "Marco Example",
                   "numbers": ["+31201110001", "+31201110009"]}}


async def test_approve_applies_the_stored_change_once(
        opened, channel, broker) -> None:
    _fill(opened, [MARCO])
    got = await _ask()
    # While the card waits, a sync renames the person and another card takes
    # the old name: Approve applies to the person the card was about (by
    # source and uid), never to whoever is called that now.
    _fill(opened, [_person("A", "Marco Renamed", "+31201110001"),
                   _person("C", "Marco Example", "+31201110005")])
    await _answer(broker, got["request_id"], 0)
    assert _marks(opened, "A") == cstore.Marks(
        priority=True, priority_origin="operator", relation="", language="")
    assert _marks(opened, "C").priority is False
    assert _rows(opened) == [("A", "priority", "operator", "1")]
    assert channel.edits == [
        (OPERATOR, 42, f"{CARD}\n\nMarco Example is now priority.")]
    # A second tap finds the request answered: nothing more is written.
    again = broker.claim(namespace="resident_ask", scope=SCOPE,
                         request_id=got["request_id"], option_index=0,
                         actor_id=OPERATOR)
    assert again == "duplicate"
    await asyncio.wait_for(broker.drain_hooks(), 5)
    assert _rows(opened) == [("A", "priority", "operator", "1")]
    assert len(channel.edits) == 1


async def test_cancel_and_timeout_change_nothing(
        opened, channel, broker) -> None:
    _fill(opened, [MARCO])
    got = await _ask()
    await _answer(broker, got["request_id"], 1)        # Cancel
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nNot changed.")]
    assert _rows(opened) == []

    channel.edits.clear()
    got = await _ask()
    broker._on_timeout(("resident_ask", SCOPE, got["request_id"]))  # 5 min on
    await asyncio.wait_for(broker.drain_hooks(), 5)
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nNot changed.")]
    assert _rows(opened) == []
    assert _marks(opened, "A").priority is False


async def test_approve_after_contact_removed_applies_nothing(
        opened, channel, broker) -> None:
    _fill(opened, [MARCO, _person("B", "Zoe Example", "+31201110003")])
    got = await _ask()
    _fill(opened, [_person("B", "Zoe Example", "+31201110003")])
    await _answer(broker, got["request_id"], 0)
    assert channel.edits == [(
        OPERATOR, 42, f"{CARD}\n\nMarco Example is no longer in your "
        "contacts. Nothing was changed.")]
    assert _rows(opened) == []


async def test_approve_that_cannot_be_written_says_not_changed(
        opened, channel, broker, monkeypatch) -> None:
    _fill(opened, [MARCO])
    got = await _ask()

    def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cstore, "set_mark", broken)
    await _answer(broker, got["request_id"], 0)
    assert channel.edits == [(OPERATOR, 42, f"{CARD}\n\nNot changed.")]
    assert _rows(opened) == []


async def test_already_priority_posts_no_card(
        opened, channel, broker) -> None:
    _fill(opened, [MARCO, _person("B", "Zoe Example", "+31201110003")],
          [("A", "priority", "1", "group"),
           ("B", "priority", "1", "operator")])
    got = await _call({"contact": "marco", "priority": True,
                       "relation": "brother"})
    assert got == {"status": "ok", "contact": "Marco Example",
                   "applied": {"relation": "brother", "priority": "already"}}
    got = await _call({"contact": "zoe", "priority": True})
    assert got == {"status": "ok", "contact": "Zoe Example",
                   "applied": {"priority": "already"}}
    assert channel.posts == []
    assert broker.pending(namespace="resident_ask", scope=SCOPE) == []


async def test_other_marks_in_same_call_apply_at_once(
        opened, channel, broker) -> None:
    _fill(opened, [MARCO])
    got = await _ask(relation="brother", language="IT")
    assert got["applied"] == {"relation": "brother", "language": "it"}
    assert _rows(opened) == [("A", "language", "operator", "it"),
                             ("A", "relation", "operator", "brother")]
    assert _marks(opened, "A").priority is False
    assert len(channel.posts) == 1


async def test_no_operator_chat_returns_unavailable(
        opened, channel, broker) -> None:
    import tools as tools_mod
    _fill(opened, [MARCO])
    channel.chat_id = ""     # nobody is the operator
    assert tools_mod._casa_card_target()[2] == "no_operator"
    got = await _call({"contact": "marco", "priority": True})
    assert got["status"] == "unavailable", got
    assert got["contact"] == "Marco Example"
    assert channel.posts == []
    assert broker._live == {}
    assert _rows(opened) == []


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
    _fill(opened, [MARCO])
    assert tools_mod._casa_card_target()[2] == "not_ready"
    got = await _call({"contact": "marco", "priority": True})
    assert got["status"] == "unavailable", got
    assert ch.posts == []
    assert broker._live == {}
    assert _rows(opened) == []


async def test_refused_call_registers_nothing(
        opened, channel, broker) -> None:
    import tools as tools_mod
    _fill(opened, [MARCO])
    for clearance in ("family", "public"):
        with _turn(clearance):
            res = await tools_mod.set_contact_marks.handler(
                {"contact": "marco", "priority": True})
        assert json.loads(res["content"][0]["text"])["status"] == "error"
    with _turn(execution_role="butler"):          # not a direct turn
        res = await tools_mod.set_contact_marks.handler(
            {"contact": "marco", "priority": True})
    assert json.loads(res["content"][0]["text"])["status"] == "error"
    assert channel.posts == []
    assert broker._live == {}
    assert _rows(opened) == []


async def test_second_identical_request_posts_no_second_card(
        opened, channel, broker) -> None:
    _fill(opened, [MARCO])
    first = await _ask()
    second = await _ask("+31 20 111 0001")
    assert second["request_id"] == first["request_id"]
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
    _fill(opened, [MARCO])
    got = await _ask()
    rid = got["request_id"]
    tg = TelegramChannel(known_bot_id=CASA_BOT, bot=fake_telegram_bot,
                         chat_id=OPERATOR, bus=None)

    other = _callback(user_id=42)
    await tg._on_resident_callback(other, rid, 0)
    other.answer.assert_awaited_once_with("not for you")
    assert broker.pending(namespace="resident_ask", scope=SCOPE) == [rid]
    assert _marks(opened, "A").priority is False

    tap = _callback(user_id=OPERATOR)
    await tg._on_resident_callback(tap, rid, 0)
    tap.answer.assert_awaited_once_with("✔")
    await asyncio.wait_for(broker.drain_hooks(), 5)
    assert _marks(opened, "A") == cstore.Marks(
        priority=True, priority_origin="operator", relation="", language="")
    assert channel.edits == [
        (OPERATOR, 42, f"{CARD}\n\nMarco Example is now priority.")]
