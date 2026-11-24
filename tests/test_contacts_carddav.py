"""The iCloud CardDAV source, against hand-written answers in iCloud's shape.

No network: every request goes to an ``httpx.MockTransport`` that serves the
XML fixtures. The account, people and numbers are invented.
"""
from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from contacts.carddav import ICloudSource
from contacts.sync import SourceError

FIX = Path(__file__).parent / "fixtures" / "contacts"
USER = "fixture-apple-id"
PASSWORD = "abcd-efgh-ijkl-mnop"
INCOMPLETE = "iCloud returned an incomplete address book"


def _fixture(name: str) -> str:
    return (FIX / name).read_text()


def _report(cards: dict[str, str]) -> str:
    """A 200 REPORT answer holding *cards* (resource name -> address-data)."""
    responses = "".join(
        f"<response><href>/1234567/carddav/card/{name}</href><propstat><prop>"
        f'<getetag>"e"</getetag>'
        f'<address-data xmlns="urn:ietf:params:xml:ns:carddav">{data}'
        "</address-data></prop><status>HTTP/1.1 200 OK</status></propstat>"
        "</response>"
        for name, data in cards.items())
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<multistatus xmlns="DAV:">{responses}</multistatus>')


def _source(report: str, *, statuses: dict[str, int] | None = None,
            seen: list[httpx.Request] | None = None) -> ICloudSource:
    """An ICloudSource whose client serves the discovery fixtures and
    *report*; *statuses* overrides the status of one step by its name."""
    statuses = statuses or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        host, path = request.url.host, request.url.path
        if request.method == "PROPFIND" and host == "contacts.icloud.com" \
                and path == "/":
            step, body = "principal", _fixture("propfind_principal.xml")
        elif request.method == "PROPFIND" and host == "contacts.icloud.com" \
                and path == "/1234567/principal/":
            step, body = "home", _fixture("propfind_home_set.xml")
        elif request.method == "PROPFIND" \
                and host == "p00-contacts.example.invalid" \
                and path == "/1234567/carddav/":
            step, body = "collections", _fixture("propfind_home.xml")
        elif request.method == "REPORT" \
                and host == "p00-contacts.example.invalid" \
                and path == "/1234567/carddav/card/":
            step, body = "report", report
        else:
            return httpx.Response(404, text="unexpected request")
        return httpx.Response(statuses.get(step, 207), text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ICloudSource(USER, PASSWORD, client=client)


async def test_fetch_walks_discovery_and_parses_cards() -> None:
    seen: list[httpx.Request] = []
    source = _source(_fixture("report_addressbook.xml"), seen=seen)
    cards = await source.fetch()

    assert source.name == "icloud"
    assert [(r.method, r.url.host, r.url.path, r.headers["Depth"])
            for r in seen] == [
        ("PROPFIND", "contacts.icloud.com", "/", "0"),
        ("PROPFIND", "contacts.icloud.com", "/1234567/principal/", "0"),
        ("PROPFIND", "p00-contacts.example.invalid", "/1234567/carddav/",
         "1"),
        ("REPORT", "p00-contacts.example.invalid", "/1234567/carddav/card/",
         "1"),
    ]
    expected_auth = "Basic " + base64.b64encode(
        f"{USER}:{PASSWORD}".encode()).decode()
    assert all(r.headers["Authorization"] == expected_auth for r in seen)
    assert b"address-data" in seen[-1].content
    assert b"getetag" in seen[-1].content

    assert len(cards) == 3
    by_uid = {c.uid: c for c in cards}
    assert by_uid["AAAA-1111"].name == "Marco Example"
    assert by_uid["AAAA-1111"].tels == (("+39 333 0000001", "CELL"),)
    group = by_uid["GGGG-0001"]
    assert group.is_group and group.name == "Priority"
    assert group.members == ("AAAA-1111",)


async def test_401_is_a_source_error_without_credentials_in_text() -> None:
    source = _source(_fixture("report_addressbook.xml"),
                     statuses={"principal": 401})
    with pytest.raises(SourceError) as info:
        await source.fetch()
    text = str(info.value)
    assert "app-specific password" in text
    assert USER not in text and PASSWORD not in text
    assert USER not in repr(info.value.args)
    assert PASSWORD not in repr(info.value.args)


async def test_non_2xx_names_the_step() -> None:
    source = _source(_fixture("report_addressbook.xml"),
                     statuses={"report": 503})
    with pytest.raises(SourceError) as info:
        await source.fetch()
    assert "503" in str(info.value)
    assert "contacts report" in str(info.value)

    source = _source(_fixture("report_addressbook.xml"),
                     statuses={"home": 500})
    with pytest.raises(SourceError) as info:
        await source.fetch()
    assert "500" in str(info.value)
    assert "address book home" in str(info.value)


async def test_timeout_is_a_source_error_naming_the_step() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    source = ICloudSource(USER, PASSWORD, client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    with pytest.raises(SourceError) as info:
        await source.fetch()
    assert "did not answer" in str(info.value)
    assert "principal" in str(info.value)


async def test_card_without_uid_takes_uid_from_href() -> None:
    cards = await _source(_fixture("report_addressbook.xml")).fetch()
    (nora,) = [c for c in cards if c.name == "Nora Sample"]
    assert nora.uid == "ABC-123"


async def test_resource_with_no_parseable_card_is_a_source_error() -> None:
    good = "BEGIN:VCARD\r\nFN:Zoe Example\r\nUID:U1\r\nEND:VCARD\r\n"
    source = _source(_report({"U1.vcf": good, "U2.vcf": "garbage"}))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_blank_card_is_a_source_error() -> None:
    good = "BEGIN:VCARD\r\nFN:Zoe Example\r\nUID:U1\r\nEND:VCARD\r\n"
    source = _source(_report({"U1.vcf": good,
                              "U2.vcf": "BEGIN:VCARD\r\nEND:VCARD"}))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_partly_failed_report_is_a_source_error() -> None:
    source = _source(_fixture("report_partial.xml"))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_failed_propstat_with_a_readable_card_is_a_source_error() -> None:
    good = "BEGIN:VCARD\r\nFN:Zoe Example\r\nUID:U1\r\nEND:VCARD\r\n"
    failed = (
        "<response><href>/1234567/carddav/card/U2.vcf</href><propstat><prop>"
        '<address-data xmlns="urn:ietf:params:xml:ns:carddav">'
        "BEGIN:VCARD\r\nFN:Adam Sample\r\nUID:U2\r\nEND:VCARD\r\n"
        "</address-data></prop><status>HTTP/1.1 404 Not Found</status>"
        "</propstat></response>")
    report = _report({"U1.vcf": good}).replace(
        "</multistatus>", failed + "</multistatus>")
    with pytest.raises(SourceError, match=INCOMPLETE):
        await _source(report).fetch()


GOOD = "BEGIN:VCARD\r\nFN:Zoe Example\r\nUID:U1\r\nEND:VCARD\r\n"
COLLECTION = "/1234567/carddav/card/"


def _with(report: str, *responses: str) -> str:
    """*report* with *responses* appended to its multistatus."""
    return report.replace("</multistatus>",
                          "".join(responses) + "</multistatus>")


async def test_collection_own_entry_is_skipped_when_it_did_not_fail() -> None:
    own = (f"<response><href>{COLLECTION}</href><propstat><prop>"
           "<getetag/></prop><status>HTTP/1.1 404 Not Found</status>"
           "</propstat></response>")
    cards = await _source(_with(_report({"U1.vcf": GOOD}), own)).fetch()
    assert [c.uid for c in cards] == ["U1"]


async def test_response_without_href_is_a_source_error() -> None:
    for bare in ("<response/>", "<response><href>  </href><status>"
                 "HTTP/1.1 200 OK</status></response>"):
        source = _source(_with(_report({"U1.vcf": GOOD}), bare))
        with pytest.raises(SourceError, match=INCOMPLETE):
            await source.fetch()


async def test_collection_level_failure_status_is_a_source_error() -> None:
    truncated = (f"<response><href>{COLLECTION}</href>"
                 "<status>HTTP/1.1 507 Insufficient Storage</status>"
                 "</response>")
    source = _source(_with(_report({"U1.vcf": GOOD}), truncated))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_member_level_failure_status_is_a_source_error() -> None:
    failed = ("<response><href>/1234567/carddav/card/U2.vcf</href>"
              "<status>HTTP/1.1 404 Not Found</status></response>")
    source = _source(_with(_report({"U1.vcf": GOOD}), failed))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_response_naming_more_than_one_href_is_a_source_error() -> None:
    # The DAV form ``href, href*, status``: the collection's own href first
    # must not hide a member named after it.
    several = (f"<response><href>{COLLECTION}</href>"
               f"<href>{COLLECTION}U2.vcf</href>"
               "<status>HTTP/1.1 200 OK</status></response>")
    source = _source(_with(_report({"U1.vcf": GOOD}), several))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_member_with_two_address_data_is_a_source_error() -> None:
    second = ("<propstat><prop>"
              '<address-data xmlns="urn:ietf:params:xml:ns:carddav">'
              "BEGIN:VCARD\r\nFN:Adam Sample\r\nUID:U2\r\nEND:VCARD\r\n"
              "</address-data></prop><status>HTTP/1.1 200 OK</status>"
              "</propstat></response>")
    report = _report({"U1.vcf": GOOD}).replace("</response>", second, 1)
    with pytest.raises(SourceError, match=INCOMPLETE):
        await _source(report).fetch()


async def test_answer_that_is_not_a_dav_multistatus_is_a_source_error() -> None:
    for report in (
            '<error xmlns="DAV:"><number-of-matches-within-limits/></error>',
            _with(_report({"U1.vcf": GOOD}),
                  '<response xmlns="urn:example:other">'
                  f"<href>{COLLECTION}U2.vcf</href></response>")):
        with pytest.raises(SourceError, match=INCOMPLETE):
            await _source(report).fetch()


@pytest.mark.parametrize("data", [
    "BEGIN:VCARD\r\nUID:U1\r\nFN:Zoe Example\r\n",
    "BEGIN:VCARD\r\nUID:U1\r\nFN:Zoe Example\r\n"
    "BEGIN:VCARD\r\nUID:U1\r\nFN:Zoe Example\r\nEND:VCARD\r\n",
], ids=["unterminated", "nested"])
async def test_card_that_is_not_well_formed_is_a_source_error(
    data: str,
) -> None:
    source = _source(_report({"U2.vcf": GOOD.replace("U1", "U2"),
                              "U1.vcf": data}))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_two_responses_for_one_href_are_a_source_error() -> None:
    again = ("<response><href>/1234567/carddav/card/U1.vcf</href><propstat>"
             '<prop><address-data xmlns="urn:ietf:params:xml:ns:carddav">'
             "BEGIN:VCARD\r\nFN:Adam Sample\r\nUID:U9\r\nEND:VCARD\r\n"
             "</address-data></prop><status>HTTP/1.1 200 OK</status>"
             "</propstat></response>")
    source = _source(_with(_report({"U1.vcf": GOOD}), again))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_two_resources_with_one_uid_are_a_source_error() -> None:
    clash = "BEGIN:VCARD\r\nFN:Adam Sample\r\nUID:U1\r\nEND:VCARD\r\n"
    source = _source(_report({"U1.vcf": GOOD, "U2.vcf": clash}))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_blank_uid_falling_back_onto_a_taken_uid_is_a_source_error(
) -> None:
    # U2's card has no UID, so it takes "U1" from its href, which is taken.
    blank = "BEGIN:VCARD\r\nFN:Adam Sample\r\nEND:VCARD\r\n"
    source = _source(_report({"U2.vcf": GOOD, "U1.vcf": blank}))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()


async def test_resource_with_two_cards_is_a_source_error() -> None:
    two = GOOD + GOOD.replace("U1", "U2").replace("Zoe", "Adam")
    source = _source(_report({"U1.vcf": two}))
    with pytest.raises(SourceError, match=INCOMPLETE):
        await source.fetch()
