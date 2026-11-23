"""The contacts store: a mirror of each address-book source plus Casa's marks.

Every test runs against a real SQLite file in a temporary directory. The people
and numbers are invented.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from contacts import store as cstore
from contacts.store import ContactsStore, ContactsUnavailable, Person


@pytest.fixture
def opened(tmp_path: Path) -> ContactsStore:
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open() is True, store.unavailable_reason
    return store


def _person(uid: str, name: str, *numbers: str, note: str = "",
            source: str = "icloud", org: str = "") -> Person:
    return Person(source=source, uid=uid, name=name,
                  first_name=name.split()[0], org=org, note=note,
                  numbers=tuple(numbers), groups=(), related=())


U1 = _person("U1", "Zoe Example", "+31201110001", note="likes tea")
U2 = _person("U2", "Adam Sample", "+31201110002", note="speaks Italian")
U3 = _person("U3", "Bea Fictional", "+31201110003", note="new friend")
F1 = _person("F1", "Carl Invented", "+31201110009", source="file")


def test_replace_keeps_operator_marks_and_other_sources(
    opened: ContactsStore,
) -> None:
    with opened.transaction(write=True) as conn:
        cstore.replace_source(conn, "icloud", [U1, U2])
        cstore.replace_source(conn, "file", [F1])
        cstore.set_mark(conn, "icloud", "U1", "priority", "1", "operator")
        cstore.set_mark(conn, "icloud", "U2", "relation", "neighbour",
                        "operator")
        cstore.set_mark(conn, "icloud", "U2", "language", "it", "note")

    with opened.transaction(write=True) as conn:
        result = cstore.replace_source(conn, "icloud", [U1, U3])
    assert result == {"added": 1, "removed": 1, "kept": 1,
                      "note_changed": ["U3"]}

    with opened.transaction() as conn:
        # U2 is gone from the mirror, its number points at nobody.
        assert cstore.by_number(conn, "+31201110002") == []
        assert cstore.count(conn, "icloud") == 2
        # Its note mark went with it; the operator's mark did not.
        rows = conn.execute(
            "SELECT field, origin, value FROM marks WHERE uid = 'U2'"
        ).fetchall()
        assert rows == [("relation", "operator", "neighbour")]
        # U1 keeps the operator's priority.
        marks = cstore.marks_for(conn, "icloud", "U1")
        assert (marks.priority, marks.priority_origin) == (True, "operator")
        # The other source is untouched.
        assert cstore.by_number(conn, "+31201110009") == [F1]
        assert cstore.count(conn, "file") == 1
        assert cstore.count(conn) == 3

    # The same uid comes back: the operator's mark applies again.
    with opened.transaction(write=True) as conn:
        result = cstore.replace_source(conn, "icloud", [U1, U2, U3])
    assert result == {"added": 1, "removed": 0, "kept": 2,
                      "note_changed": ["U2"]}
    with opened.transaction() as conn:
        assert cstore.by_number(conn, "+31201110002") == [U2]
        back = cstore.marks_for(conn, "icloud", "U2")
        assert back.relation == "neighbour"
        assert back.language == ""
        stored = conn.execute(
            "SELECT note_hash FROM people WHERE uid = 'U2'").fetchone()[0]
    assert stored == hashlib.sha256(b"speaks Italian").hexdigest()


def test_operator_wins_over_group_and_note(opened: ContactsStore) -> None:
    with opened.transaction(write=True) as conn:
        cstore.replace_source(conn, "icloud", [U1])
        cstore.set_mark(conn, "icloud", "U1", "language", "it", "note")
        cstore.set_mark(conn, "icloud", "U1", "language", "nl", "operator")
        assert cstore.marks_for(conn, "icloud", "U1").language == "nl"
        cstore.clear_mark(conn, "icloud", "U1", "language", "operator")
        assert cstore.marks_for(conn, "icloud", "U1").language == "it"

        # Priority: the group says yes, the operator's "0" says no.
        cstore.set_mark(conn, "icloud", "U1", "priority", "1", "group")
        group = cstore.marks_for(conn, "icloud", "U1")
        assert (group.priority, group.priority_origin) == (True, "group")
        cstore.set_mark(conn, "icloud", "U1", "priority", "0", "operator")
        op = cstore.marks_for(conn, "icloud", "U1")
        assert (op.priority, op.priority_origin) == (False, "operator")

        unset = cstore.marks_for(conn, "icloud", "nobody")
    assert unset == cstore.Marks(priority=False, priority_origin=None,
                                 relation="", language="")


def test_priority_never_from_note(opened: ContactsStore) -> None:
    with opened.transaction(write=True) as conn:
        cstore.replace_source(conn, "icloud", [U1])
        with pytest.raises(ValueError):
            cstore.set_mark(conn, "icloud", "U1", "priority", "1", "note")
        assert conn.execute("SELECT count(*) FROM marks").fetchone()[0] == 0
        assert cstore.marks_for(conn, "icloud", "U1").priority is False


def test_by_number_shared_number_priority_first(opened: ContactsStore) -> None:
    shared = "+31301234567"
    zoe = _person("U1", "Zoe Example", shared)
    adam = _person("U2", "Adam Sample", shared)
    with opened.transaction(write=True) as conn:
        cstore.replace_source(conn, "icloud", [zoe, adam])
        # Without marks the order is by name.
        assert cstore.by_number(conn, shared) == [adam, zoe]
        cstore.set_mark(conn, "icloud", "U1", "priority", "1", "group")
        assert cstore.by_number(conn, shared) == [zoe, adam]


def test_find_by_name_org_and_number(opened: ContactsStore) -> None:
    mobile = _person("U1", "Dora Madeup", "+31612345678")
    worker = _person("U2", "Eli Placeholder", "+31201110002",
                     org="Acme Widgets")
    with opened.transaction(write=True) as conn:
        cstore.replace_source(conn, "icloud", [mobile, worker])
        assert cstore.find(conn, "dora", default_region="NL") == [mobile]
        assert cstore.find(conn, "WIDGETS", default_region="NL") == [worker]
        assert cstore.find(conn, "06 12345678",
                           default_region="NL") == [mobile]
        assert cstore.find(conn, "06 12345678", default_region="") == []
        for region in ("NL", ""):
            assert cstore.find(conn, "+31 6 1234 5678",
                               default_region=region) == [mobile]
        assert cstore.find(conn, "e", default_region="NL", limit=1) == [mobile]


def test_record_sync_tells_once(opened: ContactsStore) -> None:
    with opened.transaction(write=True) as conn:
        assert cstore.record_sync(conn, "icloud", ok=True) is False
        assert cstore.record_sync(conn, "icloud", ok=False,
                                  error="401") is True
        assert cstore.record_sync(conn, "icloud", ok=False,
                                  error="timeout") is False
        state = cstore.sync_state(conn, "icloud")
        assert state["failing"] is True
        assert state["last_error"] == "timeout"
        assert state["last_ok"]
        assert cstore.record_sync(conn, "icloud", ok=True) is False
        assert cstore.sync_state(conn, "icloud")["failing"] is False
        assert cstore.record_sync(conn, "icloud", ok=False,
                                  error="401") is True
        assert cstore.sync_state(conn, "other") == {
            "last_ok": None, "last_error": "", "failing": False}


def test_open_refuses_newer_version(tmp_path: Path) -> None:
    newer = ContactsStore(str(tmp_path / "contacts"))
    assert newer.open() is True
    conn = sqlite3.connect(newer.path, isolation_level=None)
    try:
        conn.execute("PRAGMA user_version = 99")
    finally:
        conn.close()
    before = Path(newer.path).read_bytes()

    again = ContactsStore(newer.directory)
    assert again.open() is False
    assert "newer" in (again.unavailable_reason or "")
    assert Path(newer.path).read_bytes() == before
    with pytest.raises(ContactsUnavailable):
        with again.transaction():
            pass
