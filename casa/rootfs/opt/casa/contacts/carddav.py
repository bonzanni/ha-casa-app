"""iCloud contacts over CardDAV, as a :class:`contacts.sync.Source`.

Discovery follows the CardDAV standard: ``PROPFIND /`` for the
``current-user-principal``, ``PROPFIND`` that principal for its
``addressbook-home-set``, ``PROPFIND`` the home (Depth 1) for the collection
whose resource type is ``addressbook``, then one ``REPORT addressbook-query``
on it for every card's ``address-data``. The absolute URLs iCloud answers with
are followed (the home set lives on a ``pNN-contacts`` host).

The fetch is all-or-nothing: any failed step, a REPORT response that cannot
be accounted for (no href or several, a failing status, the collection's own
entry included), a member resource without exactly one readable, well-formed
card body holding exactly one card, a card with no content, or a member href
or uid that appears twice raises
:class:`~contacts.sync.SourceError` before any card is returned, because a
card missing from the result would read as a deleted contact. Error text never
holds the credentials.
"""
from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urljoin

import httpx

from contacts import vcard
from contacts.sync import SourceError

BASE_URL = "https://contacts.icloud.com"

#: Seconds each request may take.
TIMEOUT_S = 30.0

_DAV = "{DAV:}"
_CARD = "{urn:ietf:params:xml:ns:carddav}"

_PROPFIND_PRINCIPAL = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop><d:current-user-principal/></d:prop>"
    "</d:propfind>")
_PROPFIND_HOME_SET = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><card:addressbook-home-set/></d:prop>"
    "</d:propfind>")
_PROPFIND_COLLECTIONS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop><d:resourcetype/></d:prop>"
    "</d:propfind>")
_REPORT_CARDS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<card:addressbook-query xmlns:d="DAV:" '
    'xmlns:card="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><d:getetag/><card:address-data/></d:prop>"
    "</card:addressbook-query>")

REFUSED = "iCloud refused the sign-in; check the app-specific password"
INCOMPLETE = "iCloud returned an incomplete address book"


def _is_200(status: str | None) -> bool:
    parts = (status or "").split()
    return len(parts) >= 2 and parts[1] == "200"


def _status_ok(element: ET.Element) -> bool:
    return _is_200(element.findtext(f"{_DAV}status"))


def _ok_props(response: ET.Element) -> list[ET.Element]:
    """The ``prop`` elements of a response's 200 propstats."""
    return [prop for propstat in response.findall(f"{_DAV}propstat")
            if _status_ok(propstat)
            for prop in propstat.findall(f"{_DAV}prop")]


def _path(url: str) -> str:
    path = httpx.URL(url).path
    return path if path.endswith("/") else path + "/"


class ICloudSource:
    """The address book of one iCloud account (Apple ID and an app-specific
    password)."""

    name = "icloud"

    def __init__(self, username: str, password: str, *,
                 client: httpx.AsyncClient | None = None) -> None:
        self._auth = httpx.BasicAuth(username, password)
        self._client = client

    async def fetch(self) -> list[vcard.Card]:
        if self._client is not None:
            return await self._fetch(self._client)
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            return await self._fetch(client)

    async def _request(self, client: httpx.AsyncClient, step: str,
                       method: str, url: str, body: str,
                       depth: str) -> ET.Element:
        """One DAV request; the parsed multistatus, or SourceError naming
        *step*."""
        try:
            response = await client.request(
                method, url, content=body.encode("utf-8"), auth=self._auth,
                timeout=TIMEOUT_S, headers={
                    "Depth": depth,
                    "Content-Type": "application/xml; charset=utf-8"})
        except httpx.TimeoutException:
            raise SourceError(
                f"iCloud did not answer within {TIMEOUT_S:.0f} s "
                f"at the {step} step") from None
        except httpx.HTTPError as exc:
            raise SourceError(
                f"could not reach iCloud at the {step} step "
                f"({type(exc).__name__})") from None
        if response.status_code == 401:
            raise SourceError(REFUSED)
        if response.status_code != 207:
            raise SourceError(
                f"iCloud answered {response.status_code} "
                f"at the {step} step")
        try:
            return ET.fromstring(response.content)
        except ET.ParseError:
            raise SourceError(
                f"iCloud sent an unreadable answer at the {step} step"
            ) from None

    async def _fetch(self, client: httpx.AsyncClient) -> list[vcard.Card]:
        url = BASE_URL + "/"
        root = await self._request(client, "principal", "PROPFIND", url,
                                   _PROPFIND_PRINCIPAL, "0")
        href = root.findtext(
            f".//{_DAV}current-user-principal/{_DAV}href")
        if not href or not href.strip():
            raise SourceError("iCloud did not name the account's principal")
        principal = urljoin(url, href.strip())

        root = await self._request(client, "address book home", "PROPFIND",
                                   principal, _PROPFIND_HOME_SET, "0")
        href = root.findtext(
            f".//{_CARD}addressbook-home-set/{_DAV}href")
        if not href or not href.strip():
            raise SourceError("iCloud did not name the address book home")
        home = urljoin(principal, href.strip())

        root = await self._request(client, "address book list", "PROPFIND",
                                   home, _PROPFIND_COLLECTIONS, "1")
        collection = None
        for response in root.findall(f"{_DAV}response"):
            if any(prop.find(f"{_DAV}resourcetype/{_CARD}addressbook")
                   is not None for prop in _ok_props(response)):
                collection = urljoin(
                    home, (response.findtext(f"{_DAV}href") or "").strip())
                break
        if collection is None:
            raise SourceError("iCloud has no address book for this account")

        root = await self._request(client, "contacts report", "REPORT",
                                   collection, _REPORT_CARDS, "1")
        return self._cards(root, collection)

    @staticmethod
    def _cards(root: ET.Element, collection: str) -> list[vcard.Card]:
        """Every card in a REPORT multistatus; SourceError unless every
        response is accounted for: each names exactly one href, carries no
        failing response-level status, and is either the collection's own
        entry or a member resource with exactly one readable, well-formed
        card body holding exactly one card with content. No member href and
        no uid (after the href fallback) may appear twice."""
        if root.tag != f"{_DAV}multistatus":
            raise SourceError(INCOMPLETE)
        cards: list[vcard.Card] = []
        seen_hrefs: set[str] = set()
        seen_uids: set[str] = set()
        for response in root:
            if response.tag.rpartition("}")[2] != "response":
                continue    # responsedescription, sync-token
            if response.tag != f"{_DAV}response":
                raise SourceError(INCOMPLETE)
            hrefs = response.findall(f"{_DAV}href")
            if len(hrefs) != 1 or not (hrefs[0].text or "").strip():
                raise SourceError(INCOMPLETE)
            href = (hrefs[0].text or "").strip()
            if not all(_is_200(status.text)
                       for status in response.findall(f"{_DAV}status")):
                raise SourceError(INCOMPLETE)
            member = _path(urljoin(collection, href))
            if member == _path(collection):
                continue
            if member in seen_hrefs:
                raise SourceError(INCOMPLETE)
            seen_hrefs.add(member)
            found = [(propstat, data) for propstat in
                     response.findall(f"{_DAV}propstat")
                     for data in propstat.findall(
                         f"{_DAV}prop/{_CARD}address-data")]
            if len(found) != 1 or not _status_ok(found[0][0]):
                raise SourceError(INCOMPLETE)
            data = found[0][1].text or ""
            if not vcard.well_formed(data):
                raise SourceError(INCOMPLETE)
            parsed = vcard.parse_cards(data)
            # An address-book resource is one vCard.
            if len(parsed) != 1:
                raise SourceError(INCOMPLETE)
            card = parsed[0]
            if not (card.is_group or card.name or card.org or card.tels):
                raise SourceError(INCOMPLETE)
            if card.uid == "":
                fallback_uid = unquote(href.rstrip("/").rsplit("/", 1)[-1])
                if fallback_uid.lower().endswith(".vcf"):
                    fallback_uid = fallback_uid[:-len(".vcf")]
                card = dataclasses.replace(card, uid=fallback_uid)
            if card.uid in seen_uids:
                raise SourceError(INCOMPLETE)
            seen_uids.add(card.uid)
            cards.append(card)
        return cards
