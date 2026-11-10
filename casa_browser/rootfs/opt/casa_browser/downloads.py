"""Downloads, safe file metadata and delivery records (Plan 1 Task 10).

Three ways a file enters a run, all through the same policy boundary:

* ``Downloader.fetch(run, handle, session, policy)``: a link handle from the current
  observation. The controller — never the model — fetches its address itself, hop by hop,
  through the enforcing proxy with the proxy's CA (it never relies on Patchright's request
  client inheriting a proxy) on its own listener. Before a hop is sent, the run's policy is
  asked about it as a download hop (``policy.decide(…, hop=True)``, the function the proxy
  applies on that listener): the floor, the infrastructure rules and Chrome's background
  refusals, so an off-site CDN hop is fetched; a hop the policy would not admit is not sent at
  all. The proxy then decides the same hop again on the policy it holds. A link to a
  ``blob:`` or ``data:`` address is read inside the page that holds it, and only while that
  page belongs to the session (``page_view_allowed``).
* ``Downloader.capture_browser_download(run, download, session, policy)``: a file the browser
  itself downloaded (a button that builds a ``blob:`` and clicks it; a form ``POST`` the proxy
  admitted). Kept only when the page it came from is the session's current page and belongs
  to the session.
* ``Downloader.make_archive(run, file_ids)``: a zip of the run's own files.
* ``Downloader.fetch_plain(run, url, policy, agent)``: a public read with no browser. The same
  hops as a link's, with an address the model holds, the browser's User-Agent the controller
  kept, and never a cookie: it takes no session, so Chrome is not touched.

Every file is streamed into a private temporary file, bounded (100 MiB a file, 500 MiB an
archive: ``file_too_large``, never a truncated file), hashed, and renamed into place only
when complete; a cancelled or failed transfer leaves nothing behind, and ``cleanup_partials``
removes what a crash left. A file is stored under its generated id; its ``safe_name`` is a
sanitised base name whose extension follows the type the content itself shows (a login page
served as ``invoice.pdf`` is ``text/html``), and a received name that observation masking
would change anywhere becomes ``download``. Raw addresses, cookies and paths stay private.

Cookies: a hop carries the browser context's cookies for its address only when the hop's
origin is the origin of the page the link was on or a registered origin of the active account
site; any other hop — the cross-origin step of a redirect included — carries none. No hop
carries an ``Authorization`` header. A plain fetch's scope is empty, whatever the destination.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import AsyncIterator, Callable
from urllib.parse import unquote, urljoin, urlsplit

from casa_browser.handles import HandleRegistry, StaleHandle
from casa_browser.identity import IdentityError, address
from casa_browser.masking import mask_text
from casa_browser.observe import page_view_allowed
from casa_browser.policy import RequestMeta, RunPolicy, decide
from casa_browser.protocol import DELIVERY_OUTCOMES, MAX_START_URL_BYTES, FileRecord, ProtocolError

PROXY_URL = "http://127.0.0.1:3130"     # the downloader's own listener (netfloor)
CA_PUBLIC = Path("/run/casa-browser/ca/ca.pem")
BROWSER_DOWNLOADS = Path("/data/downloads")  # Chrome's downloads_path (browser.DOWNLOADS)
WATCH_INTERVAL = 0.5
MAX_FILE = 100 * 1024 * 1024
MAX_ARCHIVE = 500 * 1024 * 1024
MAX_HOPS = 10
MAX_ARCHIVE_FILES = 200
HOP_TIMEOUT = 120.0
CHUNK = 64 * 1024
PARTIAL_PREFIX = ".partial-"
SNIFF_BYTES = 1024
# A plain fetch's User-Agent until the browser's own has been read.
FALLBACK_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/151.0.0.0 Safari/537.36")

class DownloadRefused(ProtocolError):
    """A typed refusal (a protocol error code)."""

    def __init__(self, code: str):
        super().__init__(code)

    def __repr__(self) -> str:
        return f"DownloadRefused({self.code!r})"


# --------------------------------------------------------------------------- content types

_SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_EXT = {"application/pdf": ".pdf", "application/zip": ".zip", "image/png": ".png",
        "image/jpeg": ".jpg", "image/gif": ".gif", "text/html": ".html", "text/csv": ".csv",
        "text/plain": ".txt", "application/json": ".json", "application/xml": ".xml",
        "text/xml": ".xml", "text/calendar": ".ics", "application/octet-stream": ".bin"}
# Declared types kept as declared when the content carries no signature of its own.
_TEXT_TYPES = frozenset({"text/csv", "text/plain", "application/json", "application/xml",
                         "text/xml", "text/calendar"})
# Extensions that claim a type the content itself would show: never kept on a file that
# does not show it.
_SNIFFED_EXTS = frozenset({".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".html", ".htm",
                           ".xhtml", ".svg"})
_HTML = re.compile(rb"\A(?:\xef\xbb\xbf)?\s*(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html|head|body|script)[\s>]",
                   re.I | re.S)


# Zip-based types a declaration may name on a zip; any other declaration on a zip is a zip.
_ZIP_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/epub+zip": ".epub",
}


# The document set (design §3): a public link download of any other type is not kept until the
# check says so (``Quarantined``). PDF, images, text, CSV, calendar and office documents.
DOCUMENT_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg", "image/gif",
                            "text/html", *_TEXT_TYPES, *_ZIP_TYPES})


@dataclass
class Quarantined:
    """A fetched file whose type is outside the document set: complete and hashed, but still a
    private partial file, never registered (``put_file``) until ``keep``; ``discard`` deletes
    it, and an app start's ``cleanup_partials`` removes one a crash left."""
    record: FileRecord
    path: Path = field(repr=False)

    def packet(self) -> dict:
        """The packet's ``downloaded_file`` (design §2.2): kept name, sniffed type, size."""
        return {"name": self.record.safe_name, "type": self.record.mime,
                "size": self.record.size}


def detect_mime(head: bytes, declared: str | None) -> str:
    """The type the content shows; a declared type is believed only where the content cannot
    contradict it, and only from a fixed list (a zip-based office type on a zip, a text type
    on text) — the result is always one of a fixed set of names."""
    declared = (declared or "").split(";")[0].strip().lower()
    for magic, mime in _SIGNATURES:
        if head.startswith(magic):
            if mime == "application/zip" and declared in _ZIP_TYPES:
                return declared
            return mime
    if _HTML.match(head):
        return "text/html"
    if declared in _TEXT_TYPES:
        return declared
    return "application/octet-stream"


def _extension(mime: str, raw_ext: str) -> str:
    if mime in _EXT and mime != "application/octet-stream":
        return _EXT[mime]
    if mime in _ZIP_TYPES:
        return _ZIP_TYPES[mime]
    if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", raw_ext) and raw_ext.lower() not in _SNIFFED_EXTS:
        return raw_ext.lower()
    return ".bin"


def safe_name(raw: str | None, mime: str) -> str:
    """A base name safe to show and to put in an archive: no directory part, no control or
    path characters, no leading dot, at most 80 characters before the extension, and the
    extension the detected type has.

    Masking first (operator ruling E-C3-1 = A): observation masking runs on the name exactly
    as received — before any basename, filter or cut, each of which could shorten or unshape a
    value until masking no longer recognised it. If masking changes it anywhere, the name is
    ``download`` with the type's extension; otherwise it is the sanitised name."""
    received = raw or ""
    if mask_text(received).text != received:
        return "download" + _extension(mime, "")
    text = unicodedata.normalize("NFC", received)
    text = re.split(r"[/\\]", text)[-1]
    text = "".join(ch for ch in text if ch.isalnum() or ch in " ._-()+,")
    text = re.sub(r"\s+", " ", text).strip(" .")
    stem, dot, ext = text.rpartition(".")
    if not dot:
        stem, ext = text, ""
    stem = stem.strip(" .")[:80].strip(" .")
    if not stem:
        return "download" + _extension(mime, "")
    return stem + _extension(mime, "." + ext if ext else "")


def _name_from_url(url: str) -> str:
    try:
        return unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    except ValueError:
        return ""


def _origin(url: str) -> str | None:
    try:
        return address(url)[0]
    except IdentityError:
        return None


# --------------------------------------------------------------------------- plain reading


class _PlainText(HTMLParser):
    """A page's text, one block a line, its links inline: resolved from its address and its first
    ``<base>`` when the address is known, as a browser would, else as the page writes them."""

    _DROP = frozenset({"script", "style", "template"})
    _BLOCK = frozenset({"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
                        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
                        "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
                        "table", "td", "th", "title", "tr", "ul"})

    def __init__(self, address: str = ""):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.dropping = 0
        self.links: list[str] = []
        self.base = address
        self.base_seen = False

    def _link(self, href: str) -> str:
        try:
            return urljoin(self.base, href)
        except ValueError:          # a malformed address stands as the page wrote it
            return href

    def handle_starttag(self, tag, attrs):
        if tag in self._DROP:
            self.dropping += 1
        elif self.dropping:
            return
        elif tag == "base" and not self.base_seen:
            self.base_seen = True
            href = (dict(attrs).get("href") or "").strip()
            if href and len(href) <= MAX_START_URL_BYTES:   # no page multiplies its text through it
                try:
                    self.base = urljoin(self.base, href)
                except ValueError:
                    pass
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "a":
            self.links.append((dict(attrs).get("href") or "").strip())

    def handle_endtag(self, tag):
        if tag in self._DROP:
            self.dropping = max(0, self.dropping - 1)
        elif self.dropping:
            return
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "a" and self.links:
            href = self.links.pop()
            if href and not href.lower().startswith(("#", "javascript:")):
                self.parts.append(f" ({self._link(href)})")

    def handle_data(self, data):
        if not self.dropping:
            self.parts.append(data)


def html_text(raw: bytes, address: str = "") -> str:
    """``raw`` HTML as plain text: script, style and template dropped, links inline, whole when
    the ``address`` that answered is known. The bytes a page claims are never trusted to be clean
    UTF-8: what cannot be decoded is replaced."""
    parser = _PlainText(address)
    parser.feed(raw.decode("utf-8", "replace"))
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line)


_ABSENT = object()


def _project(value, wanted: dict):
    if not wanted:
        return value
    if isinstance(value, list):
        picked = [p for p in (_project(item, wanted) for item in value) if p is not _ABSENT]
        return picked if picked or not value else _ABSENT
    if isinstance(value, dict):
        picked = {k: p for k, sub in wanted.items()
                  if k in value and (p := _project(value[k], sub)) is not _ABSENT}
        return picked if picked else _ABSENT
    return _ABSENT


def pick_fields(raw: bytes, fields) -> str | None:
    """The dotted ``fields`` of a JSON body as JSON, keeping its shape, so a list's objects stay
    whole and each value stays beside its neighbours: a step onto a list maps over it, and a
    path that is not there is simply absent. ``None``: the body is not JSON."""
    wanted: dict = {}
    for path in fields:
        node = wanted
        for key in path.split("."):
            node = node.setdefault(key, {})
    try:
        picked = _project(json.loads(raw), wanted)
        return json.dumps({} if picked is _ABSENT else picked, ensure_ascii=False)
    except (ValueError, RecursionError):        # too deep to narrow reads as the whole text
        return None


@dataclass
class _LastHop:
    """What the hop that ended a fetch answered, so a caller need not ask twice."""
    status: int | None = None
    url: str | None = field(default=None, repr=False)


@dataclass
class PlainAnswer:
    """A plain fetch: the file and the address that served it, or the reason there is none."""
    reason: str | None = None
    record: FileRecord | None = None
    url: str | None = field(default=None, repr=False)


# --------------------------------------------------------------------------- the downloader


@dataclass
class _Partial:
    path: Path = field(repr=False)
    size: int = 0
    head: bytes = b""


async def _browser_user_agent(session) -> str:
    return await asyncio.wait_for(session.page.evaluate("() => navigator.userAgent"), 10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Downloader:
    def __init__(self, store, registry: HandleRegistry, artifacts: Path, *,
                 proxy_url: str = PROXY_URL, ca_file: Path = CA_PUBLIC,
                 user_agent: Callable = _browser_user_agent, clock: Callable = _now,
                 token: Callable = lambda: secrets.token_urlsafe(12),
                 max_file: int = MAX_FILE, max_archive: int = MAX_ARCHIVE,
                 browser_downloads: Path = BROWSER_DOWNLOADS):
        self.store = store
        self.registry = registry
        self.files = Path(artifacts) / "files"
        self.proxy_url = proxy_url
        self.ca_file = Path(ca_file)
        self.user_agent = user_agent
        self.clock = clock
        self.token = token
        self.max_file = max_file
        self.max_archive = max_archive
        self.browser_downloads = Path(browser_downloads)

    # -- private storage ---------------------------------------------------------------

    def _dir(self) -> Path:
        self.files.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.files.is_symlink() or self.files.parent.is_symlink():
            raise DownloadRefused("internal")
        return self.files

    def _new_id(self) -> str:
        return "f" + self.token().replace("=", "")[:40]

    def cleanup_partials(self) -> int:
        """Remove what an interrupted transfer left (app start). Returns the count."""
        if not self.files.is_dir() or self.files.is_symlink():
            return 0
        removed = 0
        for entry in self.files.iterdir():
            if entry.name.startswith(PARTIAL_PREFIX):
                entry.unlink(missing_ok=True)
                removed += 1
        return removed

    async def _store_stream(self, run, chunks: AsyncIterator[bytes], *, raw_name: str | None,
                            declared: str | None, source_handle: str | None,
                            limit: int, quarantine: bool = False) -> FileRecord | Quarantined:
        """Stream ``chunks`` into a private partial file, bounded by ``limit``; hash; rename
        into place only when complete; then record. Nothing remains on any failure or
        cancellation. With ``quarantine`` (a public link download, design §3), a type outside
        the document set stays a partial file, returned as ``Quarantined``."""
        file_id = self._new_id()
        directory = self._dir()
        partial = _Partial(directory / f"{PARTIAL_PREFIX}{file_id}")
        digest = hashlib.sha256()
        fd = os.open(partial.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        final = directory / file_id
        done = False
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    partial.size += len(chunk)
                    if partial.size > limit:
                        raise DownloadRefused("file_too_large")
                    if len(partial.head) < SNIFF_BYTES:
                        partial.head += chunk[:SNIFF_BYTES - len(partial.head)]
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            mime = detect_mime(partial.head, declared)
            record = FileRecord(file_id=file_id, run_id=run.run_id,
                                requester_id=run.spec.requester_id,
                                safe_name=safe_name(raw_name, mime), mime=mime,
                                size=partial.size, sha256=digest.hexdigest(),
                                private_path=str(final), source_handle=source_handle,
                                created_at=self.clock())
            if quarantine and mime not in DOCUMENT_TYPES:
                done = True
                return Quarantined(record, partial.path)
            os.replace(partial.path, final)
            try:
                self.store.put_file(record)
            except BaseException:
                final.unlink(missing_ok=True)
                raise
            done = True
            return record
        finally:
            if not done:
                partial.path.unlink(missing_ok=True)

    def keep(self, quarantined: Quarantined) -> FileRecord:
        """A quarantined file the check (or the operator) let through: registered as fetched."""
        final = Path(quarantined.record.private_path)
        try:
            os.replace(quarantined.path, final)
        except OSError:
            raise DownloadRefused("blocked") from None
        try:
            self.store.put_file(quarantined.record)
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        return quarantined.record

    @staticmethod
    def discard(quarantined: Quarantined) -> None:
        quarantined.path.unlink(missing_ok=True)

    # -- link handles ----------------------------------------------------------------------

    async def fetch(self, run, handle: str, session, policy: RunPolicy | None, *,
                    on_send=None, quarantine: bool = False) -> FileRecord | Quarantined:
        """Fetch a link handle's file. ``on_send`` (a judged download, action model G2) is
        called immediately before the first request bytes can leave: from then on an uncertain
        response is ``outcome_unknown`` for the caller, never a retry. ``quarantine``: see
        ``_store_stream``."""
        try:
            target = self.registry.resolve(handle, run.run_id, session.view_generation)
        except StaleHandle:
            raise DownloadRefused("stale_handle") from None
        page = session.page
        if page is None or not page_view_allowed(page.url, policy):
            raise DownloadRefused("stale_handle")
        href = target.href if target.kind == "link" else None
        if not isinstance(href, str):
            raise DownloadRefused("unsupported")
        scheme = href.split(":", 1)[0].lower() if ":" in href else ""
        if scheme in ("http", "https"):
            return await self._fetch_http(run, href, handle, session, policy,
                                          _origin(target.frame_url or page.url), on_send=on_send,
                                          quarantine=quarantine)
        if scheme in ("blob", "data"):
            return await self._fetch_in_page(run, href, handle, session, policy, target.frame_url,
                                             on_send=on_send, quarantine=quarantine)
        raise DownloadRefused("unsupported")

    async def fetch_plain(self, run, url: str, policy: RunPolicy | None,
                          agent: str) -> PlainAnswer:
        """A public read with no browser: no session, no cookie. A refusal the model can act on
        is a reason; a type outside the document set is not kept, as a public link download's
        would not be before the check, and stays the browser's to fetch."""
        last = _LastHop()
        try:
            got = await self._fetch_http(run, url, None, None, policy, None, agent=agent,
                                         cookie_scope=frozenset(), quarantine=True, last_hop=last)
        except DownloadRefused as err:
            if err.code != "blocked":
                raise
            return PlainAnswer("blocked" if last.status is None else f"http_{last.status}")
        if isinstance(got, Quarantined):
            self.discard(got)
            return PlainAnswer("not_document")
        return PlainAnswer(record=got, url=last.url)

    def _cookie_scope(self, session, policy: RunPolicy, page_origin: str | None) -> set[str]:
        scope = {page_origin} if page_origin else set()
        if session.kind == "account":
            for site in policy.sites:
                if site.site_id == session.site_id:
                    scope.update(site.origins)
                    scope.update(site.login_origins)
        return scope

    @staticmethod
    def _hop_meta(url: str) -> RequestMeta:
        return RequestMeta("GET", url, resource_type="document", is_navigation=True)

    def _check_hop(self, meta: RequestMeta, policy: RunPolicy) -> None:
        if not decide(meta, policy, hop=True).allow:
            raise DownloadRefused("blocked")

    async def _fetch_http(self, run, url: str, handle: str, session, policy: RunPolicy | None,
                          page_origin: str | None, *, on_send=None,
                          quarantine: bool = False, agent: str | None = None,
                          cookie_scope=None, last_hop: _LastHop | None = None
                          ) -> FileRecord | Quarantined:
        """``agent`` and ``cookie_scope``, when given, leave ``session`` unread; ``last_hop``
        receives what the hop that ended the fetch answered."""
        import aiohttp

        if policy is None:
            raise DownloadRefused("blocked")
        scope = (self._cookie_scope(session, policy, page_origin) if cookie_scope is None
                 else cookie_scope)
        if agent is None:
            try:
                agent = await self.user_agent(session)
            except Exception:
                raise DownloadRefused("internal") from None
        ctx = ssl.create_default_context(cafile=str(self.ca_file))
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=HOP_TIMEOUT)
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                                         timeout=timeout, auto_decompress=False) as client:
            # One input, one send (action model G2): aiohttp resends an idempotent request whose
            # connection broke after it was written (``_retry_connection``, set True by the
            # session and exposed by no constructor argument). A judged download is sent once;
            # a lost response is the caller's outcome_unknown, never a second send.
            client._retry_connection = False
            for _hop in range(MAX_HOPS + 1):
                if _origin(url) is None or not url.lower().startswith(("https://", "http://")):
                    raise DownloadRefused("blocked")
                # The policy judges the address exactly as it goes on the wire: built from the
                # same parts the HTTP client writes (its authority and its request target — no
                # fragment, an empty query dropped, an empty path sent as "/"), and that value
                # is what is sent.
                try:
                    import yarl
                    parsed = yarl.URL(url, encoded=True)
                    sent = f"{parsed.scheme}://{parsed.raw_authority}{parsed.raw_path_qs}"
                    wire = yarl.URL(sent, encoded=True)
                except (ValueError, TypeError):
                    raise DownloadRefused("blocked") from None
                if f"{wire.scheme}://{wire.raw_authority}{wire.raw_path_qs}" != sent:
                    raise DownloadRefused("blocked")
                meta = self._hop_meta(sent)
                self._check_hop(meta, policy)
                # Identity encoding only: a body is complete when its declared length or its
                # chunked framing says so (the client refuses a short one); a compressed body's
                # end could not be told apart from its truncation.
                headers = {"User-Agent": agent, "Accept": "*/*", "Accept-Encoding": "identity",
                           "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                           "Sec-Fetch-User": "?1", "Upgrade-Insecure-Requests": "1"}
                if _origin(url) in scope:
                    jar = await session.context.cookies([url])
                    if jar:
                        headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in jar)
                if on_send is not None:
                    on_send()
                    on_send = None
                try:
                    response = await client.get(wire, headers=headers,
                                                proxy=self.proxy_url, ssl=ctx,
                                                allow_redirects=False)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                    raise DownloadRefused("blocked") from None
                async with response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise DownloadRefused("blocked")
                        url = urljoin(url, location)
                        continue
                    if last_hop is not None:
                        last_hop.status, last_hop.url = response.status, sent
                    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                    if response.status != 200 or encoding not in ("", "identity"):
                        # Only a complete representation is a file: a 206 part, a 204, a
                        # compressed body or any other answer is not.
                        raise DownloadRefused("blocked")
                    disposition = response.content_disposition
                    raw = (disposition.filename if disposition is not None else None) \
                        or _name_from_url(str(url))

                    async def body():
                        try:
                            async for chunk in response.content.iter_chunked(CHUNK):
                                yield chunk
                        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                            raise DownloadRefused("blocked") from None

                    return await self._store_stream(
                        run, body(), raw_name=raw, declared=response.headers.get("Content-Type"),
                        source_handle=handle, limit=self.max_file, quarantine=quarantine)
            raise DownloadRefused("blocked")  # too many hops

    async def _fetch_in_page(self, run, href: str, handle: str, session, policy, frame_url, *,
                             on_send=None, quarantine: bool = False) -> FileRecord | Quarantined:
        """Read a ``blob:``/``data:`` link inside the frame that holds it (no network): the
        page must still be the page the handle was issued on, in its session. The bytes cross the
        page boundary base64-encoded and bounded."""
        page = session.page
        frame = next((f for f in page.frames if f.url == frame_url), None) if frame_url else page.main_frame
        if frame is None or not page_view_allowed(frame.url, policy):
            raise DownloadRefused("stale_handle")
        generation = session.view_generation
        if on_send is not None:
            on_send()
        try:
            data = await frame.evaluate(_READ_LOCAL, [href, self.max_file])
        except Exception:
            raise DownloadRefused("blocked") from None
        if session.view_generation != generation or not page_view_allowed(page.url, policy):
            raise DownloadRefused("stale_handle")
        if not isinstance(data, dict):
            raise DownloadRefused("blocked")
        if data.get("too_large"):
            raise DownloadRefused("file_too_large")
        try:
            content = base64.b64decode(data["b64"], validate=True)
        except (KeyError, TypeError, ValueError):
            raise DownloadRefused("blocked") from None

        async def body():
            for i in range(0, len(content), CHUNK):
                yield content[i:i + CHUNK]

        return await self._store_stream(run, body(), raw_name=data.get("name"),
                                        declared=data.get("type"), source_handle=handle,
                                        limit=self.max_file, quarantine=quarantine)

    # -- browser downloads --------------------------------------------------------------------

    async def capture_browser_download(self, run, download, session,
                                       policy: RunPolicy | None) -> FileRecord:
        """Keep a file the browser downloaded, only when it came from the session's current
        page and that page belongs to the session. The browser's copy is always deleted."""
        finished = False
        try:
            page = session.page
            generation = session.view_generation
            if (page is None or download.page is not page
                    or not page_view_allowed(page.url, policy)):
                raise DownloadRefused("blocked")
            source = await self._finished(download)
            finished = True
            if await download.failure() is not None:
                raise DownloadRefused("blocked")
            # The page must still be that page when the download completes.
            if (session.page is not page or session.view_generation != generation
                    or not page_view_allowed(page.url, policy)):
                raise DownloadRefused("blocked")
            if source is None:
                raise DownloadRefused("blocked")
            if Path(source).stat().st_size > self.max_file:
                raise DownloadRefused("file_too_large")

            async def body():
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as fh:
                    while chunk := fh.read(CHUNK):
                        yield chunk

            return await self._store_stream(
                run, body(), raw_name=download.suggested_filename, declared=None,
                source_handle=None, limit=self.max_file)
        except DownloadRefused:
            raise
        except Exception:
            raise DownloadRefused("internal") from None
        finally:
            if not finished:  # refused, failed or cancelled before the browser finished
                try:
                    await download.cancel()
                except Exception:
                    pass
            try:
                await download.delete()
            except Exception:
                pass

    def _browser_bytes(self) -> int:
        total = 0
        try:
            for entry in self.browser_downloads.iterdir():
                try:
                    total += entry.lstat().st_size
                except OSError:
                    pass
        except OSError:
            pass
        return total

    async def _finished(self, download):
        """Wait for the browser to finish, cancelling it once what it has written passes the
        bound — the bound holds while Chrome writes, not only after."""
        waiter = asyncio.ensure_future(download.path())
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=WATCH_INTERVAL)
                if done:
                    return waiter.result()
                if self._browser_bytes() > self.max_file:
                    await download.cancel()
                    raise DownloadRefused("file_too_large")
        finally:
            if not waiter.done():
                waiter.cancel()

    # -- archives ---------------------------------------------------------------------------------

    def make_archive(self, run, file_ids) -> FileRecord:
        """A zip of the run's own files (this run, this requester), entries named by their
        safe names (made unique), bounded at 500 MiB of content. Remote archives are stored as
        files, never extracted."""
        ids = list(dict.fromkeys(file_ids))
        if not 1 <= len(ids) <= MAX_ARCHIVE_FILES:
            raise DownloadRefused("unsupported")
        records = []
        for file_id in ids:
            rec = self.store.get_file(file_id)
            if rec is None or rec.run_id != run.run_id or rec.requester_id != run.spec.requester_id:
                raise DownloadRefused("not_found")
            records.append(rec)
        if sum(r.size for r in records) > self.max_archive:
            raise DownloadRefused("file_too_large")
        used: set[str] = set()
        entries = []
        for rec in records:
            stem, dot, ext = rec.safe_name.rpartition(".")
            stem, ext = (stem, "." + ext) if dot else (rec.safe_name, "")
            entry, n = rec.safe_name, 1
            while entry.lower() in used:  # one set of names, compared as a file system might
                n += 1
                entry = f"{stem} ({n}){ext}"
            used.add(entry.lower())
            entries.append((entry, rec))
        file_id = self._new_id()
        directory = self._dir()
        partial = directory / f"{PARTIAL_PREFIX}{file_id}"
        final = directory / file_id
        done = False
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out:
                with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for entry, rec in entries:
                        info = zipfile.ZipInfo(entry, date_time=(2000, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o600 << 16
                        src = os.open(rec.private_path, os.O_RDONLY | os.O_NOFOLLOW)
                        with os.fdopen(src, "rb") as fh, zf.open(info, "w", force_zip64=True) as dst:
                            while chunk := fh.read(CHUNK):
                                dst.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            size = partial.stat().st_size
            if size > self.max_archive:  # the zip itself, not only its content, is bounded
                raise DownloadRefused("file_too_large")
            digest = hashlib.sha256()
            with open(partial, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    digest.update(chunk)
            record = FileRecord(file_id=file_id, run_id=run.run_id,
                                requester_id=run.spec.requester_id, safe_name="files.zip",
                                mime="application/zip", size=size, sha256=digest.hexdigest(),
                                private_path=str(final), source_handle=None,
                                created_at=self.clock())
            os.replace(partial, final)
            try:
                self.store.put_file(record)
            except BaseException:
                final.unlink(missing_ok=True)
                raise
            done = True
            return record
        except OSError:
            raise DownloadRefused("internal") from None
        finally:
            if not done:
                partial.unlink(missing_ok=True)

    # -- delivery ------------------------------------------------------------------------------

    def ack_delivery(self, file_id: str, requester_id: str, delivery_id: str, outcome: str) -> dict:
        if outcome not in DELIVERY_OUTCOMES:
            raise DownloadRefused("unsupported")
        recorded_outcome, recorded = self.store.record_delivery(
            file_id, requester_id, delivery_id, outcome, now=self.clock())
        return {"file_id": file_id, "delivery_id": delivery_id, "outcome": recorded_outcome,
                "recorded": recorded}


# Reads a blob:/data: address inside the page (isolated world, fixed code): its bytes as
# base64, its type and the link's download name; refuses past the size bound before reading.
_READ_LOCAL = r"""async ([href, limit]) => {
  const res = await fetch(href);
  const blob = await res.blob();
  if (blob.size > limit) return {too_large: true};
  const buf = new Uint8Array(await blob.arrayBuffer());
  let s = '';
  for (let i = 0; i < buf.length; i += 32768) s += String.fromCharCode.apply(null, buf.subarray(i, i + 32768));
  let name = null;
  for (const a of document.querySelectorAll('a[download]')) { if (a.href === href) { name = a.getAttribute('download'); break; } }
  return {b64: btoa(s), type: blob.type || null, name};
}"""
