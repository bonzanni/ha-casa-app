"""Notes read by a model into ``note``-origin relation and language marks.

Real SQLite files in a temporary directory; the source and the model are
fakes, so no model is ever called. The people and notes are invented.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import resolve_model
from contacts import notes
from contacts import store as cstore
from contacts import sync
from contacts.store import ContactsStore
from contacts.vcard import Card


@pytest.fixture
def opened(tmp_path: Path) -> ContactsStore:
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open() is True, store.unavailable_reason
    return store


def _card(uid: str, name: str, note: str) -> Card:
    return Card(uid=uid, name=name, first_name=name.split()[0], org="",
                note=note, tels=(("+31 20 111 0001", "CELL"),), related=(),
                is_group=False, members=())


class FakeSource:
    def __init__(self, cards: list[Card]) -> None:
        self.name = "icloud"
        self.cards = cards

    async def fetch(self) -> list[Card]:
        return self.cards


class FakeModel:
    """Records every prompt; answers with *reply* (raised if an exception)."""

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        assert system == notes.system_prompt()
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def sent_uids(self) -> list[set[str]]:
        return [{p["uid"] for p in json.loads(prompt)}
                for prompt in self.prompts]


def _reply(**readings: tuple[str, str]) -> str:
    return json.dumps({uid: {"relation": r, "language": lang}
                       for uid, (r, lang) in readings.items()})


async def _sync(store: ContactsStore, cards: list[Card],
                model: FakeModel | None) -> sync.Result:
    result = await sync.run(store, FakeSource(cards), default_region="NL",
                            model=model)
    assert result.ok
    return result


def _marks(store: ContactsStore, uid: str) -> cstore.Marks:
    with store.transaction() as conn:
        return cstore.marks_for(conn, "icloud", uid)


def _note_rows(store: ContactsStore) -> list[tuple]:
    with store.transaction() as conn:
        return list(conn.execute(
            "SELECT uid, field, value FROM marks WHERE origin = 'note' "
            "ORDER BY uid, field"))


async def test_only_changed_nonempty_notes_are_sent(
    opened: ContactsStore,
) -> None:
    cards = [_card("U1", "Marco Example", "My brother, speaks Italian"),
             _card("U2", "Nora Sample", ""),
             _card("U3", "Carl Invented", "Colleague at the office")]
    model = FakeModel(_reply(U1=("brother", "it"), U3=("colleague", "")))

    await _sync(opened, cards, model)
    await _sync(opened, cards, model)
    assert model.sent_uids() == [{"U1", "U3"}]

    cards[2] = _card("U3", "Carl Invented", "Former colleague")
    await _sync(opened, cards, model)
    assert model.sent_uids() == [{"U1", "U3"}, {"U3"}]
    sent = json.loads(model.prompts[-1])
    assert sent == [{"uid": "U3", "name": "Carl Invented", "org": "",
                     "related": [], "note": "Former colleague"}]


async def test_reading_becomes_note_marks_and_operator_still_wins(
    opened: ContactsStore,
) -> None:
    cards = [_card("U1", "Marco Example", "My brother, speaks Italian")]
    await _sync(opened, cards, FakeModel(_reply(U1=("brother", "IT"))))
    marks = _marks(opened, "U1")
    assert (marks.relation, marks.language) == ("brother", "it")

    with opened.transaction(write=True) as conn:
        cstore.set_mark(conn, "icloud", "U1", "relation", "cousin",
                        "operator")
    cards = [_card("U1", "Marco Example", "My older brother, speaks Italian")]
    await _sync(opened, cards, FakeModel(_reply(U1=("older brother", "it"))))

    marks = _marks(opened, "U1")
    assert (marks.relation, marks.language) == ("cousin", "it")
    assert _note_rows(opened) == [("U1", "language", "it"),
                                  ("U1", "relation", "older brother")]


async def test_emptied_note_clears_note_marks_without_calling_model(
    opened: ContactsStore,
) -> None:
    await _sync(opened, [_card("U1", "Marco Example", "My brother")],
                FakeModel(_reply(U1=("brother", "it"))))
    assert len(_note_rows(opened)) == 2

    model = FakeModel(_reply(U1=("brother", "it")))
    await _sync(opened, [_card("U1", "Marco Example", "")], model)

    assert model.prompts == []
    assert _note_rows(opened) == []


async def test_valid_empty_reading_clears_note_marks(
    opened: ContactsStore,
) -> None:
    await _sync(opened, [_card("U1", "Marco Example", "My brother")],
                FakeModel(_reply(U1=("brother", "it"))))

    await _sync(opened, [_card("U1", "Marco Example", "Met at a party")],
                FakeModel(_reply(U1=("", ""))))

    assert _note_rows(opened) == []


@pytest.mark.parametrize("reply", [
    "this is not JSON",
    _reply(U9=("someone else", "en")),
    json.dumps({"U1": {"relation": 3, "language": "it"}}),
    _reply(U1=("brother", "italian")),
    RuntimeError("model backend down"),
], ids=["garbage", "missing-uid", "malformed-entry", "bad-language",
        "model-error"])
async def test_garbage_reply_missing_uid_and_model_error_keep_marks(
    opened: ContactsStore, reply: str | Exception,
) -> None:
    await _sync(opened, [_card("U1", "Marco Example", "My brother")],
                FakeModel(_reply(U1=("brother", "it"))))
    before = _note_rows(opened)

    model = FakeModel(reply)
    result = await _sync(
        opened, [_card("U1", "Marco Example", "My younger brother")], model)

    assert len(model.prompts) == 1
    assert result.note_changed == ("U1",)
    assert _note_rows(opened) == before == [("U1", "language", "it"),
                                            ("U1", "relation", "brother")]


async def test_priority_is_never_written_from_a_reading(
    opened: ContactsStore,
) -> None:
    reply = json.dumps({
        "U1": {"relation": "boss", "language": "", "priority": True},
        "priority": True})
    await _sync(opened, [_card("U1", "Marco Example", "My boss, VIP")],
                FakeModel(reply))

    marks = _marks(opened, "U1")
    assert marks.relation == "boss" and marks.priority is False
    with opened.transaction() as conn:
        assert conn.execute(
            "SELECT count(*) FROM marks WHERE field = 'priority'"
        ).fetchone()[0] == 0


def test_note_model_uses_the_small_model_and_no_tools() -> None:
    opts = notes.model_options("x")
    assert notes.MODEL_SHORTNAME == "haiku"
    assert opts.model == resolve_model("haiku")
    assert opts.tools == [] and opts.allowed_tools == []
    assert opts.system_prompt == "x"
