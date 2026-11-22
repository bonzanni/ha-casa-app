"""Secrets kept in one KeePass file, for an install with no 1Password account.

The directory holds two files: `casa.kdbx`, a KeePass (KDBX 4) database, and
`key`, the text that unlocks it, unless the key is supplied from outside. An
item is a KeePass entry in the root group and its id is the entry's UUID.

`username` and `password` are the entry's own username and password, so the
file reads naturally in any KeePass program. Every other field is a custom
property named after its label. What KeePass has no place for (the category,
who wrote the item last, and each field's id, type and order) lives in
custom properties whose names start with "_"; those never appear as fields.

The module depends only on the standard library and pykeepass.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import secrets
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pykeepass import PyKeePass, create_database
from pykeepass.entry import Entry, reserved_keys
from pykeepass.exceptions import CredentialsError

DATABASE_NAME = "casa.kdbx"
KEY_NAME = "key"

_FIELDS = "_fields"
_CATEGORY = "_category"
_WRITER = "_writer"

# Characters a property name cannot carry: pykeepass looks names up with
# XPath built by string formatting, which a double quote breaks, and an XML
# round trip rejects or rewrites control characters.
_UNSAFE_IN_A_NAME = re.compile(r'["\x00-\x1f\x7f-\x9f\ufffe\uffff]')


class StoreError(Exception):
    """The store refused or could not do what was asked."""


class ItemNotFound(StoreError):
    def __init__(self, ref: str):
        super().__init__(f"no item is called or identified by {ref!r}")
        self.ref = ref


class FieldNotFound(StoreError):
    """For callers that look up one field of an item; the store returns whole items."""

    def __init__(self, item_title: str, label: str):
        super().__init__(f"item {item_title!r} has no field {label!r}")
        self.item_title = item_title
        self.label = label


class Ambiguous(StoreError):
    def __init__(self, ref: str, ids: list[str]):
        super().__init__(f"more than one item is called {ref!r}")
        self.ref = ref
        self.ids = ids


class KeyProblem(StoreError):
    """The existing file has no key, or the key does not open it."""


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    type: str
    value: str
    purpose: str | None = None


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    category: str
    tags: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    writer: str
    fields: tuple[Field, ...]


class Store:
    """The items in one directory's KeePass file.

    One process owns the file: the database is read once and kept in memory,
    and every public method holds one lock for its whole read, change and
    save, so concurrent callers cannot lose each other's writes.
    """

    def __init__(self, path: Path, key: str, database: PyKeePass):
        self._path = path
        self._key = key
        self._kp: PyKeePass | None = database
        self._lock = threading.RLock()

    @classmethod
    def open(cls, directory: Path, env_key: str | None) -> Store:
        """Open the store in `directory`, creating it on first use.

        The key is `env_key` when it is given, otherwise the content of the
        `key` file. Only when there is no database yet is anything created: the
        directory, a random key (unless one was supplied) and an empty
        database. An existing database that has no key, or that the key does
        not open, raises `KeyProblem` and nothing on disk is touched: a
        database nobody can read right now may still be somebody's secrets.
        """
        directory = Path(directory)
        path = directory / DATABASE_NAME
        key = (env_key or "").strip() or _key_from_file(directory / KEY_NAME)
        if path.exists():
            if not key:
                raise KeyProblem(f"{path} exists but there is no key to open it with")
            return cls(path, key, _read(path, key))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not key:
            key = _write_new_key(directory / KEY_NAME)
        image = io.BytesIO()
        database = create_database(image, password=key)
        _replace_file(path, image.getvalue())
        return cls(path, key, database)

    def list(self, tags: frozenset[str] | None = None) -> list[Item]:
        """Every item, or those carrying at least one of `tags`."""
        with self._lock:
            items = [_item(entry) for entry in self._entries()]
        if tags:
            items = [item for item in items if tags.intersection(item.tags)]
        return items

    def get(self, ref: str) -> Item:
        with self._lock:
            return _item(self._find(ref))

    def create(self, *, title: str, category: str, tags: Sequence[str],
               fields: Sequence[Field], writer: str) -> Item:
        _check_tags(tags)
        _check_labels(fields)
        with self._lock:
            with self._saved() as database:
                # The entry is added untitled and named afterwards: the title
                # given to add_entry goes into an XPath lookup, which a quote
                # in it would break.
                entry = database.add_entry(database.root_group, "", "", "",
                                           force_creation=True)
                entry.title = title
                entry.tags = list(tags)
                entry.set_custom_property(_CATEGORY, category)
                entry.set_custom_property(_WRITER, writer)
                _write_fields(entry, fields)
            return _item(entry)

    def replace(self, ref: str, *, title: str | None, category: str | None,
                tags: Sequence[str] | None, fields: Sequence[Field],
                writer: str) -> Item:
        """Make `fields` the item's whole field list; fields left out are removed.

        `title`, `category` and `tags` are kept when given as None.
        """
        if tags is not None:
            _check_tags(tags)
        _check_labels(fields)
        with self._lock:
            entry = self._find(ref)
            with self._saved():
                if title is not None:
                    entry.title = title
                if category is not None:
                    entry.set_custom_property(_CATEGORY, category)
                if tags is not None:
                    entry.tags = list(tags)
                entry.set_custom_property(_WRITER, writer)
                _write_fields(entry, fields)
                entry.touch(modify=True)
            return _item(entry)

    def set_field(self, ref: str, *, label: str, type: str, value: str,
                  writer: str) -> Item:
        """Change one field, or add it at the end; every other field stays."""
        with self._lock:
            entry = self._find(ref)
            with self._saved():
                records = _records(entry)
                for record in records:
                    if record["label"] == label:
                        record["type"] = type
                        break
                else:
                    records.append(_record(label, type, None, None))
                _put(entry, label, value)
                entry.set_custom_property(_FIELDS, json.dumps(records))
                entry.set_custom_property(_WRITER, writer)
                entry.touch(modify=True)
            return _item(entry)

    def delete(self, ref: str) -> None:
        """Remove the item for good: no recycle bin, no history."""
        with self._lock:
            entry = self._find(ref)
            with self._saved() as database:
                database.delete_entry(entry)

    # -- inside the lock ----------------------------------------------------

    def _database(self) -> PyKeePass:
        if self._kp is None:
            self._kp = _read(self._path, self._key)
        return self._kp

    def _entries(self) -> list[Entry]:
        return self._database().root_group.entries

    def _find(self, ref: str) -> Entry:
        """The entry whose id is `ref`, else the only one titled `ref`.

        Matching is done here rather than with pykeepass's searches, which
        build XPath from the text they are given.
        """
        entries = self._entries()
        for entry in entries:
            if _item_id(entry) == ref:
                return entry
        titled = [entry for entry in entries if (entry.title or "") == ref]
        if not titled:
            raise ItemNotFound(ref)
        if len(titled) > 1:
            raise Ambiguous(ref, [_item_id(entry) for entry in titled])
        return titled[0]

    @contextlib.contextmanager
    def _saved(self) -> Iterator[PyKeePass]:
        """Change the database in memory, then write it to disk.

        If the change or the write fails, the copy in memory is dropped and
        the next call reads the file again. Otherwise a change the caller was
        told had failed would stay in memory and reach the disk with the next
        successful write.
        """
        try:
            database = self._database()
            yield database
            image = io.BytesIO()
            database.save(image)
            _replace_file(self._path, image.getvalue())
        except BaseException:
            self._kp = None
            raise


# -- the files ----------------------------------------------------------------

def _key_from_file(path: Path) -> str:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return ""
    key = text.strip()
    if not key:
        raise KeyProblem(f"{path} is empty")
    return key


def _write_new_key(path: Path) -> str:
    """Create the key file with a fresh random key and return the key.

    The key is on disk, synced, before any database is encrypted with it. The
    file is created exclusively, so a key already there is never overwritten.
    A key file whose write fails is removed again: an empty one left behind
    would make every later open fail although no database exists yet.
    """
    key = secrets.token_hex(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as file:
            file.write(key)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    _sync_directory(path.parent)
    return key


def _read(path: Path, key: str) -> PyKeePass:
    try:
        return PyKeePass(str(path), password=key)
    except CredentialsError:
        raise KeyProblem(f"the key does not open {path}") from None


def _replace_file(path: Path, data: bytes) -> None:
    """Put `data` at `path` so that a crash leaves the old file or the new one.

    pykeepass's own save to a path is not used: it creates its temporary file
    with whatever mode the umask allows and renames it without syncing. Here
    the temporary file is private from its first byte and is on disk before
    it takes the name. Its name is fixed, so one left by an interrupted write
    is overwritten by the next write instead of piling up.
    """
    temporary = path.with_name(path.name + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            os.fchmod(file.fileno(), 0o600)  # a leftover file keeps its old mode
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    # The new file has the name now. A failing directory sync must not report
    # the write as failed: a caller that tried again would store it twice.
    with contextlib.suppress(OSError):
        _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    """Make a new or renamed name in `directory` survive a power cut."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# -- an entry as an item ------------------------------------------------------

def _base32(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def _item_id(entry: Entry) -> str:
    return _base32(entry.uuid.bytes)


def _item(entry: Entry) -> Item:
    return Item(
        id=_item_id(entry),
        title=entry.title or "",
        category=entry.get_custom_property(_CATEGORY) or "",
        tags=tuple(entry.tags),
        created_at=entry.ctime,
        updated_at=entry.mtime,
        writer=entry.get_custom_property(_WRITER) or "",
        fields=tuple(
            Field(id=record["id"], label=record["label"], type=record["type"],
                  value=_value(entry, record["label"]), purpose=record.get("purpose"))
            for record in _records(entry)
        ),
    )


def _records(entry: Entry) -> list[dict]:
    """The item's fields in order: each one's id, label, type and purpose.

    This list, not the entry's strings, says which fields exist: a KeePass
    entry always has a username and a password, and an empty one must not
    read as a field nobody stored.
    """
    return json.loads(entry.get_custom_property(_FIELDS) or "[]")


def _record(label: str, type: str, id: str | None, purpose: str | None) -> dict:
    if label in ("username", "password"):
        id = label
    record = {"id": id or _base32(secrets.token_bytes(16)), "label": label, "type": type}
    if purpose:
        record["purpose"] = purpose
    return record


def _property_name(label: str) -> str:
    """The custom property that holds the value of the field `label`.

    It is the label itself whenever KeePass and pykeepass can carry that as
    a name. A label that is one of the entry's built-in strings, that starts
    with "_" (the store's own names), or that holds text a name cannot carry
    is stored under a name derived from it instead, so no label can read or
    overwrite anything but its own value.
    """
    plain = (
        label != ""
        and label == label.strip()
        and not label.startswith("_")
        and label not in reserved_keys
        and not _UNSAFE_IN_A_NAME.search(label)
    )
    return label if plain else "_field_" + label.encode().hex()


def _value(entry: Entry, label: str) -> str:
    if label == "username":
        value = entry.username
    elif label == "password":
        value = entry.password
    else:
        value = entry.get_custom_property(_property_name(label))
    return value or ""


def _put(entry: Entry, label: str, value: str) -> None:
    if label == "username":
        entry.username = value
    elif label == "password":
        entry.password = value
    else:
        entry.set_custom_property(_property_name(label), value, protect=True)


def _drop(entry: Entry, label: str) -> None:
    if label == "username":
        entry.username = ""
    elif label == "password":
        entry.password = ""
    else:
        entry.delete_custom_property(_property_name(label))


def _write_fields(entry: Entry, fields: Sequence[Field]) -> None:
    """Make `fields` the entry's whole field list.

    A field given without an id keeps the id the same label had before.
    """
    before = _records(entry)
    known = {record["label"]: record["id"] for record in before}
    for record in before:
        _drop(entry, record["label"])
    records = []
    for field in fields:
        _put(entry, field.label, field.value)
        records.append(_record(field.label, field.type,
                               field.id or known.get(field.label), field.purpose))
    entry.set_custom_property(_FIELDS, json.dumps(records))


def _check_labels(fields: Sequence[Field]) -> None:
    labels = [field.label for field in fields]
    for label in labels:
        if labels.count(label) > 1:
            # One label names one value; the second would silently replace
            # the first.
            raise StoreError(f"two fields are labelled {label!r}")


def _check_tags(tags: Sequence[str]) -> None:
    for tag in tags:
        if "," in tag or ";" in tag:
            # KeePass keeps tags as one separated string and would hand this
            # one back as two.
            raise StoreError(f"a tag cannot contain ',' or ';': {tag!r}")
