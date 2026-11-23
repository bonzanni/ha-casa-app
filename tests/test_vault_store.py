"""The KeePass-backed store for secrets: real files, the real pykeepass.

What these tests protect, in order of cost: an existing file is never replaced
or rewritten by an open that cannot read it; a write either lands whole or
leaves the previous file; a value comes back exactly as it went in; and two
writers never lose each other's item.
"""

import base64
import os
import re
import stat
import threading
from datetime import datetime, timezone

import pytest
from lxml import etree
from pykeepass import PyKeePass

import vault_store
from vault_store import (
    Ambiguous,
    Field,
    Item,
    ItemNotFound,
    KeyProblem,
    Store,
    StoreError,
)

DB = "casa.kdbx"
KEY = "key"


@pytest.fixture
def directory(tmp_path):
    return tmp_path / "vault"


@pytest.fixture
def store(directory):
    return Store.open(directory, None)


@pytest.fixture
def no_umask():
    """Modes must be chosen by the store, not inherited from a strict umask."""
    previous = os.umask(0)
    yield
    os.umask(previous)


def field(label, value, type="CONCEALED", id="", purpose=None):
    return Field(id=id, label=label, type=type, value=value, purpose=purpose)


def make(store, title="Gmail", fields=None, tags=("casa",), category="LOGIN"):
    if fields is None:
        fields = [field("password", "hunter2")]
    return store.create(title=title, category=category, tags=list(tags),
                        fields=fields, writer="op")


def raw(directory, key=None):
    """The file as any KeePass program sees it."""
    if key is None:
        key = (directory / KEY).read_text()
    return PyKeePass(str(directory / DB), password=key)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def values(item):
    return {f.label: f.value for f in item.fields}


def snapshot(directory):
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


# -- items ------------------------------------------------------------------

def test_create_get_roundtrip_by_id_and_title(store):
    created = store.create(
        title="Gmail", category="LOGIN", tags=["casa", "mail"],
        fields=[
            field("username", "ellen@example.com", type="STRING", id="username",
                  purpose="USERNAME"),
            field("password", "hunter2", id="password", purpose="PASSWORD"),
            field("refresh token", "1//refresh"),
            field("valid from", "0", type="DATE", id="validFrom"),
        ],
        writer="op",
    )

    assert isinstance(created, Item)
    assert (created.title, created.category, created.writer) == ("Gmail", "LOGIN", "op")
    assert created.tags == ("casa", "mail")
    assert created.created_at.tzinfo == timezone.utc
    assert created.updated_at.tzinfo == timezone.utc
    assert created.updated_at >= created.created_at

    assert [(f.label, f.type, f.value, f.purpose) for f in created.fields] == [
        ("username", "STRING", "ellen@example.com", "USERNAME"),
        ("password", "CONCEALED", "hunter2", "PASSWORD"),
        ("refresh token", "CONCEALED", "1//refresh", None),
        ("valid from", "DATE", "0", None),
    ]
    ids = [f.id for f in created.fields]
    assert ids[:2] == ["username", "password"]
    assert re.fullmatch(r"[a-z2-7]{26}", ids[2])  # none given: the store made one
    assert ids[3] == "validFrom"                  # a given id is kept

    assert store.get(created.id) == created
    assert store.get("Gmail") == created
    assert store.list() == [created]


def test_id_is_26_lowercase_base32(store, directory):
    item = make(store)

    assert re.fullmatch(r"[a-z2-7]{26}", item.id)
    (entry,) = raw(directory).entries
    padded = item.id.upper() + "=" * 6
    assert base64.b32decode(padded) == entry.uuid.bytes


def test_username_and_password_are_the_entrys_own_and_the_rest_is_hidden(store, directory):
    make(store, fields=[field("username", "ellen", type="STRING"),
                        field("password", "hunter2"),
                        field("refresh token", "1//refresh")])

    (entry,) = raw(directory).entries
    assert (entry.title, entry.username, entry.password) == ("Gmail", "ellen", "hunter2")
    assert entry.get_custom_property("refresh token") == "1//refresh"
    assert entry.is_custom_property_protected("refresh token")
    hidden = set(entry.custom_properties) - {"refresh token"}
    assert hidden and all(name.startswith("_") for name in hidden)


def test_replace_drops_fields_not_in_template(store, directory):
    item = make(store, fields=[field("username", "ellen", type="STRING"),
                               field("password", "one"),
                               field("refresh token", "1//refresh"),
                               field("session note", "plain", type="STRING")])
    kept_id = next(f.id for f in item.fields if f.label == "session note")

    replaced = store.replace(
        item.id, title=None, category=None, tags=None,
        fields=[field("password", "two"),
                field("session note", "still here", type="STRING"),
                field("hostname", "example.com", type="STRING")],
        writer="op",
    )

    assert [f.label for f in replaced.fields] == ["password", "session note", "hostname"]
    assert values(replaced) == {"password": "two", "session note": "still here",
                                "hostname": "example.com"}
    assert (replaced.id, replaced.title, replaced.category, replaced.tags) == (
        item.id, "Gmail", "LOGIN", ("casa",))
    assert next(f.id for f in replaced.fields if f.label == "session note") == kept_id
    assert Store.open(directory, None).get(item.id) == replaced

    kp = raw(directory)
    (entry,) = kp.entries
    assert not entry.username
    assert "refresh token" not in entry.custom_properties
    assert "1//refresh" not in etree.tostring(kp.tree).decode()
    assert not entry.history


def test_replace_changes_title_category_and_tags_when_given(store, directory):
    item = make(store)

    replaced = store.replace(item.id, title="Mail", category="PASSWORD", tags=["a", "b"],
                             fields=[field("password", "two")], writer="ellen")

    assert (replaced.title, replaced.category, replaced.tags, replaced.writer) == (
        "Mail", "PASSWORD", ("a", "b"), "ellen")
    assert Store.open(directory, None).get("Mail") == replaced
    with pytest.raises(ItemNotFound):
        store.get("Gmail")


def test_set_field_adds_and_keeps_others(store, directory):
    item = make(store, fields=[field("password", "one", purpose="PASSWORD"),
                               field("refresh token", "old")])
    token_id = item.fields[1].id

    changed = store.set_field("Gmail", label="refresh token", type="CONCEALED",
                              value="new", writer="op")
    assert values(changed) == {"password": "one", "refresh token": "new"}
    assert changed.fields[1].id == token_id
    assert changed.fields[0] == item.fields[0]

    added = store.set_field(item.id, label="session note", type="STRING",
                            value="plain text", writer="op")
    assert [(f.label, f.type, f.value) for f in added.fields] == [
        ("password", "CONCEALED", "one"),
        ("refresh token", "CONCEALED", "new"),
        ("session note", "STRING", "plain text"),
    ]
    assert re.fullmatch(r"[a-z2-7]{26}", added.fields[2].id)

    with_user = store.set_field(item.id, label="username", type="STRING",
                                value="ellen", writer="op")
    assert with_user.fields[3].id == "username"
    assert values(with_user)["password"] == "one"
    assert Store.open(directory, None).get(item.id) == with_user
    assert raw(directory).entries[0].username == "ellen"


@pytest.mark.parametrize("change", [
    pytest.param(lambda s, ref: s.set_field(ref, label="password", type="CONCEALED",
                                            value="two", writer="op"), id="set_field"),
    pytest.param(lambda s, ref: s.replace(ref, title=None, category=None, tags=None,
                                          fields=[field("password", "two")], writer="op"),
                 id="replace"),
])
def test_a_change_moves_updated_at_and_leaves_created_at(directory, change):
    item = make(Store.open(directory, None))
    # Times are kept to the second, so the item is aged on disk rather than
    # waited for.
    long_ago = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    kp = raw(directory)
    (entry,) = kp.entries
    entry.ctime = entry.mtime = long_ago
    kp.save()
    store = Store.open(directory, None)
    aged = store.get(item.id)
    assert (aged.created_at, aged.updated_at) == (long_ago, long_ago)

    changed = change(store, item.id)

    assert changed.created_at == long_ago
    assert changed.updated_at >= item.created_at > long_ago
    assert Store.open(directory, None).get(item.id) == changed


def test_two_same_titles_get_by_title_is_ambiguous_by_id_is_exact(store):
    first = make(store, title="Dup", fields=[field("password", "one")])
    second = make(store, title="Dup", fields=[field("password", "two")])
    assert first.id != second.id

    with pytest.raises(Ambiguous) as caught:
        store.get("Dup")
    assert caught.value.ref == "Dup"
    assert sorted(caught.value.ids) == sorted([first.id, second.id])

    assert store.get(first.id) == first
    assert store.get(second.id) == second

    # A write addressed by the shared title touches neither.
    with pytest.raises(Ambiguous):
        store.set_field("Dup", label="password", type="CONCEALED", value="x", writer="op")
    with pytest.raises(Ambiguous):
        store.replace("Dup", title=None, category=None, tags=None, fields=[], writer="op")
    with pytest.raises(Ambiguous):
        store.delete("Dup")
    assert store.list() == [first, second]


def test_an_id_wins_over_an_equal_title(store):
    first = make(store, title="First")
    make(store, title=first.id, fields=[field("password", "other")])

    assert store.get(first.id) == first


def test_a_missing_item_is_not_found(store):
    make(store)

    for call in (
        lambda: store.get("nope"),
        lambda: store.delete("nope"),
        lambda: store.set_field("nope", label="x", type="STRING", value="y", writer="op"),
        lambda: store.replace("nope", title=None, category=None, tags=None, fields=[],
                              writer="op"),
    ):
        with pytest.raises(ItemNotFound) as caught:
            call()
        assert caught.value.ref == "nope"
    assert [i.title for i in store.list()] == ["Gmail"]


def test_delete_is_final_and_reopen_does_not_find_it(store, directory):
    doomed = make(store, title="Doomed", fields=[field("password", "burn-after-reading"),
                                                 field("note", "also-burned")])
    kept = make(store, title="Kept")

    assert store.delete(doomed.id) is None

    for current in (store, Store.open(directory, None)):
        with pytest.raises(ItemNotFound):
            current.get(doomed.id)
        with pytest.raises(ItemNotFound):
            current.get("Doomed")
        assert current.list() == [kept]

    kp = raw(directory)
    assert [e.title for e in kp.find_entries(history=True)] == ["Kept"]
    assert len(kp.groups) == 1 and kp.recyclebin_group is None
    xml = etree.tostring(kp.tree).decode()
    assert "burn-after-reading" not in xml and "also-burned" not in xml


def test_list_filters_by_any_of_the_tags(store):
    a = make(store, title="A", tags=["mail", "casa"])
    b = make(store, title="B", tags=["bank"])
    c = make(store, title="C", tags=[])

    assert store.list() == [a, b, c]
    assert store.list(frozenset()) == [a, b, c]
    assert store.list(frozenset({"casa"})) == [a]
    assert store.list(frozenset({"bank", "mail"})) == [a, b]
    assert store.list(frozenset({"nope"})) == []


# The markers are split so secret scanners do not take test data for a key.
PEM = ("-----BEGIN PRIVATE " "KEY-----\n"
       + "".join("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj\n"
                 for _ in range(46))
       + "-----END PRIVATE " "KEY-----\n")
assert len(PEM) > 3000


@pytest.mark.parametrize("value", [
    pytest.param(PEM, id="pem-3000-multiline"),
    pytest.param("pässwörd ☃ 密码 𝄞", id="non-ascii"),
    pytest.param("", id="empty"),
    pytest.param("-starts-with-a-dash", id="leading-dash"),
    pytest.param("a=b]c[d]=e", id="equals-and-bracket"),
    pytest.param("line one\r\nline two\r\n  ", id="crlf-and-trailing-space"),
])
def test_values_survive_reopen(store, directory, value):
    item = make(store, fields=[field("username", value, type="STRING"),
                               field("password", value),
                               field("private key", value, type="SSHKEY")])
    assert values(item) == {"username": value, "password": value, "private key": value}

    reopened = Store.open(directory, None).get(item.id)

    assert values(reopened) == {"username": value, "password": value, "private key": value}
    assert reopened == item


def test_labels_another_reader_would_mangle_survive_reopen(store, directory):
    labels = ["Title", "UserName", "Password", "Notes", "URL", "otp", 'say "hi"',
              "_fields", "_category", "_writer", " padded ", "two\nlines", "", "it's <ok> & fine"]
    fields = [field("username", "the-user", type="STRING"), field("password", "the-pass")]
    fields += [field(label, f"value-{n}") for n, label in enumerate(labels)]
    expected = {"username": "the-user", "password": "the-pass",
                **{label: f"value-{n}" for n, label in enumerate(labels)}}

    item = make(store, title="Tricky", fields=fields)
    assert values(item) == expected

    reopened = Store.open(directory, None).get("Tricky")
    assert reopened == item
    assert (reopened.title, reopened.category, reopened.writer) == ("Tricky", "LOGIN", "op")

    changed = store.set_field("Tricky", label='say "hi"', type="CONCEALED", value="new",
                              writer="op")
    assert values(changed) == {**expected, 'say "hi"': "new"}
    assert values(store.replace("Tricky", title=None, category=None, tags=None,
                                fields=[field("Title", "only")], writer="op")) == {
        "Title": "only"}
    assert Store.open(directory, None).get("Tricky").title == "Tricky"


@pytest.mark.parametrize("value", ["nul\x00inside", "escape\x1binside"])
def test_a_value_the_file_cannot_carry_is_refused_and_nothing_is_kept(store, directory, value):
    kept = make(store, title="Kept")

    with pytest.raises(ValueError):
        make(store, title="Odd", fields=[field("password", "fine"), field("note", value)])
    with pytest.raises(ValueError):
        store.set_field(kept.id, label="note", type="CONCEALED", value=value, writer="op")

    assert store.list() == [kept]
    assert Store.open(directory, None).list() == [kept]


def test_two_fields_with_one_label_are_refused(store):
    with pytest.raises(StoreError):
        make(store, fields=[field("token", "one"), field("token", "two")])

    assert store.list() == []


def test_a_tag_holding_a_separator_is_refused(store):
    for tag in ("a,b", "a;b"):
        with pytest.raises(StoreError):
            make(store, tags=[tag])

    assert store.list() == []


def test_the_errors_are_one_family():
    for error in (ItemNotFound("r"),
                  Ambiguous("r", ["a", "b"]), KeyProblem("k")):
        assert isinstance(error, StoreError)


# -- the file and its key ---------------------------------------------------

def test_first_open_creates_dir_0700_key_0600_file_0600(directory, no_umask):
    assert not directory.exists()

    store = Store.open(directory, None)

    assert mode(directory) == 0o700
    assert mode(directory / KEY) == 0o600
    assert mode(directory / DB) == 0o600
    assert re.fullmatch(r"[0-9a-f]{64}", (directory / KEY).read_text())

    make(store)  # a later write keeps the mode and leaves nothing else behind
    assert mode(directory / DB) == 0o600
    assert sorted(p.name for p in directory.iterdir()) == [DB, KEY]


def test_an_existing_key_file_is_used_and_kept(directory):
    directory.mkdir()
    (directory / KEY).write_text("  hand-written-key\n")

    make(Store.open(directory, None))

    assert (directory / KEY).read_text() == "  hand-written-key\n"
    assert [e.title for e in raw(directory, "hand-written-key").entries] == ["Gmail"]
    assert [i.title for i in Store.open(directory, None).list()] == ["Gmail"]


def test_env_key_is_used_and_no_key_file_is_written(directory):
    item = make(Store.open(directory, "key-from-the-environment"))

    assert sorted(p.name for p in directory.iterdir()) == [DB]
    assert Store.open(directory, "key-from-the-environment").get(item.id) == item
    assert sorted(p.name for p in directory.iterdir()) == [DB]
    assert [e.title for e in raw(directory, "key-from-the-environment").entries] == ["Gmail"]


@pytest.mark.parametrize("env_key", [None, ""])
def test_existing_file_with_missing_key_raises_and_leaves_bytes_unchanged(directory, env_key):
    make(Store.open(directory, None))
    (directory / KEY).unlink()
    before = snapshot(directory)
    assert list(before) == [DB]

    with pytest.raises(KeyProblem):
        Store.open(directory, env_key)

    assert snapshot(directory) == before


def test_existing_file_with_wrong_env_key_raises_and_leaves_bytes_unchanged(directory):
    make(Store.open(directory, None))  # the key file is right; the environment wins anyway
    before = snapshot(directory)
    assert list(before) == [DB, KEY]

    with pytest.raises(KeyProblem):
        Store.open(directory, "not-the-key")

    assert snapshot(directory) == before


def test_existing_file_with_wrong_key_file_raises_and_leaves_bytes_unchanged(directory):
    make(Store.open(directory, None))
    (directory / KEY).write_text("0" * 64)
    before = snapshot(directory)

    with pytest.raises(KeyProblem):
        Store.open(directory, None)

    assert snapshot(directory) == before


@pytest.mark.parametrize("with_database", [True, False])
def test_an_empty_key_file_is_a_key_problem_and_nothing_is_written(directory, with_database):
    if with_database:
        make(Store.open(directory, None))
    else:
        directory.mkdir()
    (directory / KEY).write_text("\n")
    before = snapshot(directory)

    with pytest.raises(KeyProblem):
        Store.open(directory, None)

    assert snapshot(directory) == before


# -- writes -----------------------------------------------------------------

def test_twenty_concurrent_creates_all_present_after_reopen(store, directory):
    start = threading.Barrier(20)
    failures = []

    def create(n):
        try:
            start.wait()
            make(store, title=f"item-{n}", fields=[field("password", f"secret-{n}")])
        except Exception as exc:  # surfaced below; a thread must not die silently
            failures.append(exc)

    threads = [threading.Thread(target=create, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    items = Store.open(directory, None).list()
    assert len(items) == 20
    assert len({i.id for i in items}) == 20
    assert {i.title: values(i)["password"] for i in items} == {
        f"item-{n}": f"secret-{n}" for n in range(20)}


def test_a_crash_between_temp_write_and_replace_keeps_the_old_file(
        store, directory, monkeypatch):
    kept = make(store, title="Kept")
    before = snapshot(directory)
    real_replace = os.replace
    calls = []

    def replace_once_broken(src, dst):
        calls.append((src, dst))
        if len(calls) == 1:
            raise OSError("the power went out")
        return real_replace(src, dst)

    monkeypatch.setattr(vault_store.os, "replace", replace_once_broken)

    with pytest.raises(OSError, match="the power went out"):
        make(store, title="Lost", fields=[field("password", "never-written")])

    assert len(calls) == 1
    assert snapshot(directory) == before  # the old file, and no temporary one
    assert Store.open(directory, None).list() == [kept]
    # The item whose write failed is not waiting in memory to ride along
    # with the next one.
    assert store.list() == [kept]
    after = make(store, title="After")
    assert Store.open(directory, None).list() == [kept, after]


def test_a_leftover_temporary_file_does_not_block_the_next_write(store, directory, no_umask):
    leftover = directory / (DB + ".tmp")
    leftover.write_bytes(b"half a file from an interrupted write")
    assert mode(leftover) == 0o666

    item = make(store)

    assert sorted(p.name for p in directory.iterdir()) == [DB, KEY]
    assert mode(directory / DB) == 0o600
    assert Store.open(directory, None).list() == [item]


def test_a_key_file_whose_write_fails_is_removed(directory, monkeypatch):
    real_fsync = os.fsync
    calls = []

    def fsync_once_broken(descriptor):
        calls.append(descriptor)
        if len(calls) == 1:
            raise OSError("the disk is full")
        return real_fsync(descriptor)

    monkeypatch.setattr(vault_store.os, "fsync", fsync_once_broken)

    with pytest.raises(OSError, match="the disk is full"):
        Store.open(directory, None)

    assert not (directory / KEY).exists()
    assert not (directory / DB).exists()
    item = make(Store.open(directory, None))
    assert Store.open(directory, None).list() == [item]


def test_a_failing_directory_sync_after_the_rename_does_not_fail_the_write(
        store, directory, monkeypatch):
    def sync_broken(path):
        raise OSError("the directory could not be synced")

    monkeypatch.setattr(vault_store, "_sync_directory", sync_broken)

    item = make(store)

    assert Store.open(directory, None).list() == [item]
    assert sorted(p.name for p in directory.iterdir()) == [DB, KEY]
