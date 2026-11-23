"""Sync from a source into the contacts store.

Real SQLite files in a temporary directory; sources are fakes, or the iCloud
source over an ``httpx.MockTransport``. The people and numbers are invented.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from contacts import store as cstore
from contacts import sync
from contacts.carddav import ICloudSource
from contacts.store import ContactsStore
from contacts.sync import SourceError
from contacts.vcard import Card

FIX = Path(__file__).parent / "fixtures" / "contacts"


@pytest.fixture
def opened(tmp_path: Path) -> ContactsStore:
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open() is True, store.unavailable_reason
    return store


def _card(uid: str, name: str, *tels: str, note: str = "") -> Card:
    return Card(uid=uid, name=name, first_name=name.split()[0], org="",
                note=note, tels=tuple((t, "CELL") for t in tels),
                related=(), is_group=False, members=())


def _group(uid: str, name: str, *members: str) -> Card:
    return Card(uid=uid, name=name, first_name="", org="", note="", tels=(),
                related=(), is_group=True, members=members)


class FakeSource:
    def __init__(self, name: str, *answers: list[Card] | SourceError) -> None:
        self.name = name
        self._answers = list(answers)

    async def fetch(self) -> list[Card]:
        answer = self._answers.pop(0) if len(self._answers) > 1 \
            else self._answers[0]
        if isinstance(answer, SourceError):
            raise answer
        return answer


MARCO = _card("U1", "Marco Example", "+39 333 0000001")
NORA = _card("U2", "Nora Sample", "020 000 0002")
PRIORITY = _group("G1", sync.PRIORITY_GROUP, "U1")
FRIENDS = _group("G2", "Friends", "U1", "U2")


def _snapshot(store: ContactsStore) -> tuple:
    """People, numbers and marks, as rows, for before/after comparison."""
    with store.transaction() as conn:
        return tuple(
            tuple(conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3"))
            for table in ("people", "numbers", "marks"))


async def test_first_sync_fills_mirror_and_group_priority(
    opened: ContactsStore,
) -> None:
    source = FakeSource("icloud", [MARCO, NORA, PRIORITY, FRIENDS])
    result = await sync.run(opened, source, default_region="NL")

    assert result.ok and result.error == "" and not result.tell_operator
    assert (result.added, result.removed, result.kept) == (2, 0, 0)
    assert set(result.note_changed) == {"U1", "U2"}
    with opened.transaction() as conn:
        marco = cstore.marks_for(conn, "icloud", "U1")
        assert marco.priority and marco.priority_origin == "group"
        assert cstore.marks_for(conn, "icloud", "U2").priority is False
        (p,) = cstore.by_number(conn, "+393330000001")
        assert set(p.groups) == {"Priority", "Friends"}
        (n,) = cstore.by_number(conn, "+31200000002")
        assert n.groups == ("Friends",)
        assert cstore.sync_state(conn, "icloud")["last_ok"] is not None


async def test_leaving_the_group_removes_group_priority_but_not_operator_priority(
    opened: ContactsStore,
) -> None:
    both = _group("G1", sync.PRIORITY_GROUP, "U1", "U2")
    await sync.run(opened, FakeSource("icloud", [MARCO, NORA, both]),
                   default_region="NL")
    with opened.transaction(write=True) as conn:
        cstore.set_mark(conn, "icloud", "U2", "priority", "1", "operator")

    empty = _group("G1", sync.PRIORITY_GROUP)
    result = await sync.run(opened, FakeSource("icloud", [MARCO, NORA, empty]),
                            default_region="NL")
    assert result.ok and result.kept == 2
    with opened.transaction() as conn:
        assert cstore.marks_for(conn, "icloud", "U1").priority is False
        nora = cstore.marks_for(conn, "icloud", "U2")
        assert nora.priority and nora.priority_origin == "operator"
        assert conn.execute(
            "SELECT count(*) FROM marks WHERE origin = 'group'"
        ).fetchone()[0] == 0


async def test_source_error_leaves_store_untouched_and_tells_once(
    opened: ContactsStore,
) -> None:
    await sync.run(opened, FakeSource("icloud", [MARCO, NORA, PRIORITY]),
                   default_region="NL")
    before = _snapshot(opened)
    failing = FakeSource("icloud", SourceError("iCloud is down"))

    first = await sync.run(opened, failing, default_region="NL")
    second = await sync.run(opened, failing, default_region="NL")

    assert (first.ok, first.error, first.tell_operator) == (
        False, "iCloud is down", True)
    assert (second.ok, second.tell_operator) == (False, False)
    assert _snapshot(opened) == before
    with opened.transaction() as conn:
        assert cstore.count(conn, "icloud") == 2
        assert cstore.sync_state(conn, "icloud")["failing"] is True


async def test_empty_result_is_a_failure_when_mirror_has_people(
    opened: ContactsStore,
) -> None:
    await sync.run(opened, FakeSource("icloud", [MARCO, NORA, PRIORITY]),
                   default_region="NL")
    before = _snapshot(opened)

    # A group card alone still means zero people.
    first = await sync.run(opened, FakeSource("icloud", [FRIENDS]),
                           default_region="NL")
    second = await sync.run(opened, FakeSource("icloud", []),
                            default_region="NL")

    assert (first.ok, first.error, first.tell_operator) == (
        False, "the address book came back empty", True)
    assert (second.ok, second.tell_operator) == (False, False)
    assert _snapshot(opened) == before


async def test_bad_numbers_are_skipped_not_fatal(
    opened: ContactsStore,
) -> None:
    card = _card("U1", "Marco Example", "not a number", "+39 333 0000001",
                 "+39 3330000001")
    result = await sync.run(opened, FakeSource("icloud", [card]),
                            default_region="NL")

    assert result.ok and result.skipped_numbers == 1
    with opened.transaction() as conn:
        (p,) = cstore.by_number(conn, "+393330000001")
        assert p.numbers == ("+393330000001",)


def _icloud(report: str) -> ICloudSource:
    """The iCloud source over a mock transport serving *report*."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "REPORT":
            body = report
        elif path == "/":
            body = (FIX / "propfind_principal.xml").read_text()
        elif path.endswith("/principal/"):
            body = (FIX / "propfind_home_set.xml").read_text()
        else:
            body = (FIX / "propfind_home.xml").read_text()
        return httpx.Response(207, text=body)

    return ICloudSource("fixture-apple-id", "abcd-efgh-ijkl-mnop",
                        client=httpx.AsyncClient(
                            transport=httpx.MockTransport(handler)))


def _report(cards: dict[str, str]) -> str:
    responses = "".join(
        f"<response><href>/1234567/carddav/card/{name}</href><propstat><prop>"
        f'<address-data xmlns="urn:ietf:params:xml:ns:carddav">{data}'
        "</address-data></prop><status>HTTP/1.1 200 OK</status></propstat>"
        "</response>"
        for name, data in cards.items())
    return f'<multistatus xmlns="DAV:">{responses}</multistatus>'


U1_CARD = "BEGIN:VCARD\r\nFN:Zoe Example\r\nTEL:+31 20 111 0001\r\nUID:U1\r\nEND:VCARD\r\n"
U2_CARD = "BEGIN:VCARD\r\nFN:Adam Sample\r\nTEL:+31 20 111 0002\r\nUID:U2\r\nEND:VCARD\r\n"


async def _mirror_u1_u2_with_operator_mark(store: ContactsStore) -> tuple:
    result = await sync.run(
        store, _icloud(_report({"U1.vcf": U1_CARD, "U2.vcf": U2_CARD})),
        default_region="NL")
    assert result.ok and result.added == 2
    with store.transaction(write=True) as conn:
        cstore.set_mark(conn, "icloud", "U2", "priority", "1", "operator")
        cstore.set_mark(conn, "icloud", "U2", "relation", "neighbour",
                        "operator")
    return _snapshot(store)


@pytest.mark.parametrize("unreadable", [
    "garbage", "BEGIN:VCARD\r\nEND:VCARD"], ids=["garbage", "blank"])
async def test_unreadable_card_in_a_good_report_deletes_nobody(
    opened: ContactsStore, unreadable: str,
) -> None:
    before = await _mirror_u1_u2_with_operator_mark(opened)
    source = _icloud(_report({"U1.vcf": U1_CARD, "U2.vcf": unreadable}))

    first = await sync.run(opened, source, default_region="NL")
    second = await sync.run(opened, source, default_region="NL")

    assert (first.ok, first.tell_operator) == (False, True)
    assert first.error == "iCloud returned an incomplete address book"
    assert (second.ok, second.tell_operator) == (False, False)
    assert _snapshot(opened) == before


async def test_incomplete_report_keeps_every_person_and_mark(
    opened: ContactsStore,
) -> None:
    before = await _mirror_u1_u2_with_operator_mark(opened)
    failing = FakeSource(
        "icloud", SourceError("iCloud returned an incomplete address book"))

    first = await sync.run(opened, failing, default_region="NL")
    second = await sync.run(opened, failing, default_region="NL")

    assert (first.ok, first.tell_operator) == (False, True)
    assert (second.ok, second.tell_operator) == (False, False)
    assert _snapshot(opened) == before


async def test_two_sources_coexist(opened: ContactsStore) -> None:
    file_person = _card("F1", "Carl Invented", "+31 20 111 0009")
    await sync.run(opened, FakeSource("icloud", [MARCO, NORA]),
                   default_region="NL")
    await sync.run(opened, FakeSource("file", [file_person]),
                   default_region="NL")
    with opened.transaction() as conn:
        assert cstore.count(conn, "icloud") == 2
        assert cstore.count(conn, "file") == 1
