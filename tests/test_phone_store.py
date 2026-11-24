"""The phone store: settings, voices, grants, calls and transcripts.

Real SQLite in a temporary directory; every value is invented."""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from phone import store as pstore
from phone.store import PhoneStore, PhoneUnavailable


@pytest.fixture
def opened(tmp_path: Path) -> PhoneStore:
    store = PhoneStore(str(tmp_path / "phone"))
    assert store.open() is True, store.unavailable_reason
    return store


def test_open_creates_private_directory(tmp_path: Path) -> None:
    store = PhoneStore(str(tmp_path / "phone"))
    assert store.available is False
    assert store.open() is True
    assert stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700
    assert os.path.exists(os.path.join(store.directory, "phone.db"))
    assert store.open() is True


def test_newer_file_is_refused(tmp_path: Path) -> None:
    store = PhoneStore(str(tmp_path / "phone"))
    assert store.open() is True
    conn = sqlite3.connect(store.path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    again = PhoneStore(store.directory)
    assert again.open() is False
    assert "newer" in again.unavailable_reason
    with pytest.raises(PhoneUnavailable):
        with again.transaction():
            pass


def test_settings_and_voices(opened: PhoneStore) -> None:
    with opened.transaction(write=True) as conn:
        assert pstore.get_setting(conn, "voice_model") is None
        pstore.set_setting(conn, "voice_model", "m1")
        pstore.set_setting(conn, "voice_model", "m2")
        pstore.set_voice(conn, "nl", "voice_a", "warm")
        pstore.set_voice(conn, "nl", "voice_b")
    with opened.transaction() as conn:
        assert pstore.get_setting(conn, "voice_model") == "m2"
        assert pstore.voices(conn) == {"nl": "voice_b"}


def test_set_grant_rejects_unknown_values(opened: PhoneStore) -> None:
    with opened.transaction(write=True) as conn:
        pstore.set_grant(conn, "known", "transfer", "urgent")
        assert pstore.grants(conn) == {("known", "transfer"): "urgent"}
        pstore.set_grant(conn, "known", "transfer", "off")
        assert pstore.grants(conn) == {("known", "transfer"): "off"}
        with pytest.raises(sqlite3.IntegrityError):
            pstore.set_grant(conn, "stranger", "transfer", "open")
        with pytest.raises(sqlite3.IntegrityError):
            pstore.set_grant(conn, "known", "transfer", "always")


def _call(cid: str, session: str, started: float = 1.0) -> dict:
    return {"id": cid, "session_id": session, "started_at": started,
            "number": "+31201110001", "name": "Zoe", "caller_class": "known",
            "language": "nl", "returned": 0}


def test_call_lifecycle(opened: PhoneStore) -> None:
    with opened.transaction(write=True) as conn:
        pstore.insert_call(conn, _call("c1", "live_1", 1.0))
        pstore.insert_call(conn, _call("c2", "live_2", 2.0))
        first = pstore.get_call(conn, "c1")
        assert first["outcome"] is None and first["delivered"] == 0
        assert pstore.finish_call(
            conn, "c1", ended_at=5.0, outcome="message",
            actions=[{"ability": "transfer"}], record={"summary": "hello"})
        assert not pstore.finish_call(conn, "nope", ended_at=5.0,
                                      outcome="x", actions=[], record={})
        done = pstore.get_call(conn, "c1")
        assert done["actions"] == [{"ability": "transfer"}]
        assert done["record"] == {"summary": "hello"}
        assert done["ended_at"] == 5.0
        assert [c["id"] for c in pstore.recent_calls(conn)] == ["c2", "c1"]
        assert [c["id"] for c in pstore.recent_calls(conn, limit=1)] == ["c2"]
        assert {c["id"] for c in pstore.undelivered(conn)} == {"c1", "c2"}
        assert pstore.mark_delivered(conn, "c1")
        assert not pstore.mark_delivered(conn, "nope")
        assert [c["id"] for c in pstore.undelivered(conn)] == ["c2"]
        assert pstore.get_call(conn, "nope") is None


def test_set_record_keeps_what_the_call_wrote(opened: PhoneStore) -> None:
    with opened.transaction(write=True) as conn:
        pstore.insert_call(conn, _call("c1", "live_1", 1.0))
        pstore.finish_call(conn, "c1", ended_at=5.0, outcome="transferred",
                           actions=[{"ability": "transfer", "status": "done"}],
                           record=None)
        assert pstore.set_record(conn, "c1", {"reason": "a parcel"})
        assert not pstore.set_record(conn, "nope", {})
        done = pstore.get_call(conn, "c1")
    assert done["record"] == {"reason": "a parcel"}
    assert done["outcome"] == "transferred"
    assert done["actions"] == [{"ability": "transfer", "status": "done"}]
    assert done["ended_at"] == 5.0 and done["delivered"] == 0


def test_the_first_line_of_a_call_is_marked_once_sent(
        opened: PhoneStore) -> None:
    with opened.transaction(write=True) as conn:
        pstore.insert_call(conn, _call("c1", "live_1", 1.0))
        assert pstore.get_call(conn, "c1")["headline_sent"] == 0
        assert pstore.mark_headline_sent(conn, "c1")
        assert not pstore.mark_headline_sent(conn, "nope")
        done = pstore.get_call(conn, "c1")
    assert done["headline_sent"] == 1 and done["delivered"] == 0


def test_a_v1_store_migrates_to_v2_keeping_its_rows(tmp_path: Path) -> None:
    from phone.schema import MIGRATIONS
    directory = tmp_path / "phone"
    directory.mkdir(mode=0o700)
    path = directory / "phone.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for statement in MIGRATIONS[1]:
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO calls (id, session_id, started_at, ended_at, number, "
        "outcome, record, delivered) VALUES ('c1', 'live_1', 1.0, 5.0, "
        "'+31201110001', 'caller_hung_up', '{\"number_tail\": \"0001\"}', 0)")
    conn.close()
    store = PhoneStore(str(directory))
    assert store.open() is True, store.unavailable_reason
    with store.transaction() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        [row] = pstore.undelivered(conn)
    assert row["id"] == "c1" and row["number"] == "+31201110001"
    assert row["record"] == {"number_tail": "0001"}
    assert row["headline_sent"] == 0 and row["delivered"] == 0
    assert pstore.schema_version() == 2


def test_session_id_is_unique(opened: PhoneStore) -> None:
    with opened.transaction(write=True) as conn:
        pstore.insert_call(conn, _call("c1", "live_1"))
        with pytest.raises(sqlite3.IntegrityError):
            pstore.insert_call(conn, _call("c2", "live_1"))


def test_purge_transcripts_deletes_only_expired(opened: PhoneStore) -> None:
    with opened.transaction(write=True) as conn:
        pstore.insert_call(conn, _call("c1", "live_1"))
        pstore.insert_call(conn, _call("c2", "live_2"))
        pstore.put_transcript(conn, "c1", "old words", 10.0)
        pstore.put_transcript(conn, "c2", "new words", 100.0)
        assert pstore.purge_transcripts(conn, 50.0) == 1
        assert pstore.transcript(conn, "c1") is None
        assert pstore.transcript(conn, "c2") == "new words"


def test_deleting_a_call_removes_its_transcript(opened: PhoneStore) -> None:
    with opened.transaction(write=True) as conn:
        pstore.insert_call(conn, _call("c1", "live_1"))
        pstore.put_transcript(conn, "c1", "words", 10.0)
        conn.execute("DELETE FROM calls WHERE id = 'c1'")
        assert pstore.transcript(conn, "c1") is None
