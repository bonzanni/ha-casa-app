"""Sync one address-book source into the contacts store.

A source hands over every card it holds, or raises :class:`SourceError`; it
never hands over a partial list, because :func:`contacts.store.replace_source`
reads a missing card as a deleted contact. The import step does not know where
the cards came from: every row it writes carries the source's name, and other
sources are untouched.

Priority from the address book comes only from membership of the group named
:data:`PRIORITY_GROUP`; it is written as ``group``-origin marks, which never
override the operator's own.

After a successful write, every note not yet settled (new, changed, or not
read by an earlier sync) is read by a model (:mod:`contacts.notes`) into
``note``-origin relation and language marks. The model is called outside any
transaction, and note reading never fails the sync.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from contacts import notes
from contacts import numbers as phone_numbers
from contacts import store as cstore
from contacts.store import ContactsStore, Person
from contacts.vcard import Card

logger = logging.getLogger(__name__)

PRIORITY_GROUP = "Priority"

EMPTY_ERROR = "the address book came back empty"


class SourceError(Exception):
    """A source could not deliver its whole address book. The message is
    safe to show the operator and never holds credentials."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class Source(Protocol):
    name: str

    async def fetch(self) -> list[Card]:
        """Every card the source holds; raises :class:`SourceError`."""
        ...


@dataclass(frozen=True)
class Result:
    ok: bool
    added: int
    removed: int
    kept: int
    skipped_numbers: int
    note_changed: tuple[str, ...]
    error: str
    tell_operator: bool


def _failed(error: str, tell_operator: bool) -> Result:
    return Result(ok=False, added=0, removed=0, kept=0, skipped_numbers=0,
                  note_changed=(), error=error, tell_operator=tell_operator)


def _people(source: str, cards: list[Card],
            default_region: str) -> tuple[list[Person], int]:
    """Person cards as :class:`Person` rows, with group names on members,
    one per uid (the last card wins, as in the store); returns them and the
    count of numbers that could not be read."""
    groups: dict[str, list[str]] = {}
    for card in cards:
        if card.is_group:
            for member in card.members:
                names = groups.setdefault(member, [])
                if card.name not in names:
                    names.append(card.name)
    people: dict[str, Person] = {}
    skipped = 0
    for card in cards:
        if card.is_group:
            continue
        numbers: list[str] = []
        for raw, _label in card.tels:
            e164 = phone_numbers.to_e164(raw, default_region)
            if e164 is None:
                skipped += 1
            elif e164 not in numbers:
                numbers.append(e164)
        people[card.uid] = Person(
            source=source, uid=card.uid, name=card.name,
            first_name=card.first_name, org=card.org, note=card.note,
            numbers=tuple(numbers), groups=tuple(groups.get(card.uid, ())),
            related=card.related)
    return list(people.values()), skipped


def _record_failure(store: ContactsStore, source: str, error: str) -> bool:
    with store.transaction(write=True) as conn:
        return cstore.record_sync(conn, source, ok=False, error=error)


def _set_group_priority(conn: sqlite3.Connection, source: str,
                        people: list[Person]) -> None:
    """Make ``group``-origin priority marks match :data:`PRIORITY_GROUP`
    membership; an unchanged mark keeps its original time."""
    marked = {r[0] for r in conn.execute(
        "SELECT uid FROM marks WHERE source = ? AND field = 'priority' "
        "AND origin = 'group'", (source,))}
    for person in people:
        member = PRIORITY_GROUP in person.groups
        if member and person.uid not in marked:
            cstore.set_mark(conn, source, person.uid, "priority", "1",
                            "group")
        elif not member and person.uid in marked:
            cstore.clear_mark(conn, source, person.uid, "priority", "group")


def _apply(store: ContactsStore, source: str,
           people: list[Person]) -> tuple[dict | None, bool]:
    """The whole write, in one transaction: ``(replace_source's result,
    False)``, or ``(None, tell_operator)`` when the floor refused an empty
    book while the mirror holds people (recorded as a failure)."""
    with store.transaction(write=True) as conn:
        if not people and cstore.count(conn, source) > 0:
            return None, cstore.record_sync(conn, source, ok=False,
                                            error=EMPTY_ERROR)
        changed = cstore.replace_source(conn, source, people)
        _set_group_priority(conn, source, people)
        cstore.record_sync(conn, source, ok=True)
        return changed, False


def _settle_blank(store: ContactsStore, source: str) -> list[Person]:
    """Settle every blank note that needs it (its ``note``-origin marks
    cleared), in one transaction; returns the non-blank notes to read."""
    with store.transaction(write=True) as conn:
        to_read: list[Person] = []
        for person in cstore.notes_to_settle(conn, source):
            if person.note.strip():
                to_read.append(person)
                continue
            for field in ("relation", "language"):
                cstore.clear_mark(conn, source, person.uid, field, "note")
            cstore.settle_note(conn, source, person.uid,
                               cstore.settled_hash(person.note))
        return to_read


def _apply_readings(store: ContactsStore, source: str,
                    readings: dict[str, notes.Reading],
                    read_notes: dict[str, str]) -> None:
    """Rewrite the ``note``-origin marks of each read uid whose note is still
    the one that was read (a later sync may have changed or removed it
    meanwhile), and settle that note."""
    with store.transaction(write=True) as conn:
        for uid, reading in readings.items():
            note = read_notes[uid]
            row = conn.execute(
                "SELECT note_hash FROM people WHERE source = ? AND uid = ?",
                (source, uid)).fetchone()
            if row is None or row[0] != cstore.note_hash(note):
                continue
            for field, value in (("relation", reading.relation),
                                 ("language", reading.language)):
                if value:
                    cstore.set_mark(conn, source, uid, field, value, "note")
                else:
                    cstore.clear_mark(conn, source, uid, field, "note")
            cstore.settle_note(conn, source, uid, cstore.settled_hash(note))


async def _read_notes(store: ContactsStore, source: str,
                      model: notes.Model | None) -> None:
    """Bring ``note``-origin marks in line with the notes not yet settled: a
    blank note loses them without a model call; a non-empty one is read when
    a model is given, and settled only once a valid reading has landed, so a
    note that was not read is tried again by the next sync. Never raises."""
    try:
        to_read = await asyncio.to_thread(_settle_blank, store, source)
        if model is None or not to_read:
            return
        readings = await notes.read(model, to_read)
        if readings:
            await asyncio.to_thread(
                _apply_readings, store, source, readings,
                {p.uid: p.note for p in to_read})
    except Exception:  # noqa: BLE001 — notes never fail the sync
        logger.exception("contact notes from %s not applied", source)


async def run(store: ContactsStore, source: Source, *,
              default_region: str,
              model: notes.Model | None = None) -> Result:
    """Fetch *source* and mirror it into *store*, then read the notes not
    yet settled with *model* (none: they wait for a sync that has one). A
    failed or empty fetch leaves the store's people, numbers and marks as
    they were."""
    try:
        cards = await source.fetch()
    except SourceError as exc:
        tell = await asyncio.to_thread(
            _record_failure, store, source.name, exc.message)
        logger.warning("contacts sync from %s failed: %s", source.name,
                       exc.message)
        return _failed(exc.message, tell)

    people, skipped = _people(source.name, cards, default_region)
    changed, tell = await asyncio.to_thread(
        _apply, store, source.name, people)
    if changed is None:
        logger.warning("contacts sync from %s refused: %s", source.name,
                       EMPTY_ERROR)
        return _failed(EMPTY_ERROR, tell)

    logger.info(
        "contacts sync from %s: %d added, %d removed, %d kept, "
        "%d numbers skipped", source.name, changed["added"],
        changed["removed"], changed["kept"], skipped)
    await _read_notes(store, source.name, model)
    return Result(ok=True, added=changed["added"], removed=changed["removed"],
                  kept=changed["kept"], skipped_numbers=skipped,
                  note_changed=tuple(changed["note_changed"]), error="",
                  tell_operator=False)
