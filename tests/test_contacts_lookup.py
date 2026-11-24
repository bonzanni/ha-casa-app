"""The caller lookup: a number in, five fields out. Invented people only."""
from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from contacts import store as cstore
from contacts.lookup import CallerInfo, lookup
from contacts.store import ContactsStore, Person


@pytest.fixture
def opened(tmp_path: Path) -> ContactsStore:
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open() is True, store.unavailable_reason
    return store


def _person(uid: str, name: str, *numbers: str) -> Person:
    return Person(source="icloud", uid=uid, name=name,
                  first_name=name.split()[0], org="", note="",
                  numbers=tuple(numbers), groups=(), related=())


def _fill(store: ContactsStore, people: list[Person], marks=()) -> None:
    with store.transaction(write=True) as conn:
        cstore.replace_source(conn, "icloud", people)
        for uid, field, value, origin in marks:
            cstore.set_mark(conn, "icloud", uid, field, value, origin)


def test_known_priority_contact(opened: ContactsStore) -> None:
    _fill(opened, [_person("A", "Marco Rossi", "+39201110001")],
          [("A", "priority", "1", "operator"),
           ("A", "language", "it", "note")])
    got = lookup(opened, "+39 20 111 0001", default_region="NL")
    assert got == CallerInfo(True, True, "Marco Rossi", "Marco", "it")


def test_known_without_language_mark_uses_calling_country(
        opened: ContactsStore) -> None:
    _fill(opened, [_person("A", "Zoe Example", "+31201110001")])
    got = lookup(opened, "+31201110001", default_region="")
    assert got == CallerInfo(True, False, "Zoe Example", "Zoe", "nl")


def test_unknown_number_language_from_country_code(
        opened: ContactsStore) -> None:
    for raw, lang in [("+31201110099", "nl"), ("+39201110099", "it"),
                      ("+4930123456", "en"), ("+33142685300", "en"),
                      ("+81312345678", "en")]:
        assert lookup(opened, raw, default_region="NL") == CallerInfo(
            False, False, "", "", lang), raw


def test_withheld_and_garbage_never_raise(opened: ContactsStore) -> None:
    for raw in ["", "anonymous", "sip:x@y", "   ", "+", "\x00"]:
        assert lookup(opened, raw, default_region="NL") == CallerInfo(
            False, False, "", "", "en"), raw


def test_shared_number_picks_priority_person(opened: ContactsStore) -> None:
    _fill(opened, [_person("A", "Adam Sample", "+31201110005"),
                   _person("B", "Zed Example", "+31201110005")],
          [("B", "priority", "1", "group")])
    got = lookup(opened, "+31201110005", default_region="NL")
    assert (got.name, got.priority) == ("Zed Example", True)


def test_store_unavailable_is_unknown(tmp_path: Path) -> None:
    never_opened = ContactsStore(str(tmp_path / "nothing"))
    assert lookup(never_opened, "+31201110001", default_region="NL") == \
        CallerInfo(False, False, "", "", "nl")


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("database is locked"),
    RuntimeError("an unexpected bug"),
], ids=["sqlite-error", "unexpected-error"])
def test_store_read_that_raises_is_unknown(
        opened: ContactsStore, monkeypatch: pytest.MonkeyPatch,
        error: Exception) -> None:
    _fill(opened, [_person("A", "Zoe Example", "+31201110001")],
          [("A", "priority", "1", "operator")])

    def failing(conn, e164):
        raise error

    monkeypatch.setattr(cstore, "by_number", failing)
    assert lookup(opened, "+31201110001", default_region="NL") == \
        CallerInfo(False, False, "", "", "nl")


def test_callerinfo_has_exactly_five_fields() -> None:
    assert tuple(f.name for f in dataclasses.fields(CallerInfo)) == (
        "known", "priority", "name", "first_name", "language")
