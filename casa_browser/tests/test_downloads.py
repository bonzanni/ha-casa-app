"""Task 10: downloads through the enforcing proxy, safe file metadata, archives and delivery
records. Link fetches run against the real proxy (``proxyharness``: production addon, a TLS
fixture behind a counting relay), so "not sent" is counted where bytes would arrive."""
from __future__ import annotations

import asyncio
import io
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import proxyharness as h
from casa_browser.downloads import (
    DOCUMENT_TYPES,
    PARTIAL_PREFIX,
    Downloader,
    DownloadRefused,
    Quarantined,
    detect_mime,
    safe_name,
)
from casa_browser.handles import HandleRegistry, PrivateTarget
from casa_browser.protocol import (
    Admission,
    FileRecord,
    ProtocolError,
    RunRecord,
    RunSpec,
    SiteGrant,
)
from casa_browser.store import Store

SITE, LOGIN, CAP, EVIL, LAN = ("site.example", "login.example", "cap.example", "evil.example",
                               "lan.example")
HOSTS = [SITE, LOGIN, CAP, EVIL, LAN]
PDF = b"%PDF-1.7\n% synthetic\n" + b"0" * 2000 + b"\n%%EOF\n"
TOKEN = "Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg"  # gitleaks:allow - synthetic fixture; this test exists to prove masking works


def policy():
    """The account session of the site (origins SITE and LAN, login origin LOGIN): a page on
    those origins belongs to the session (design §4), any other page does not."""
    site = h.site(origins=(f"https://{SITE}", f"https://{LAN}"), login=(f"https://{LOGIN}",))
    return h.account_policy(sites=(site,))


class FakeContext:
    def __init__(self, cookies=()):
        self._cookies = list(cookies)
        self.asked = []

    async def cookies(self, urls):
        self.asked.extend(urls)
        out = []
        for c in self._cookies:
            if any(f"//{c['domain']}/" in u or u.endswith(f"//{c['domain']}") for u in urls):
                out.append(c)
        return out


class FakeFrame:
    def __init__(self, url, local=None):
        self.url = url
        self.local = local
        self.calls = 0

    async def evaluate(self, script, arg=None):
        self.calls += 1
        if callable(self.local):
            return self.local(arg)
        return self.local


class FakePage:
    def __init__(self, url, local=None):
        self.url = url
        self.main_frame = FakeFrame(url, local)
        self.frames = [self.main_frame]

    async def evaluate(self, script):
        return "Mozilla/5.0 (X11; Linux x86_64) synthetic"


def session(page_url=f"https://{SITE}/invoices", cookies=(), local=None):
    return SimpleNamespace(kind="account", site_id="s1", context=FakeContext(cookies),
                           page=FakePage(page_url, local), view_generation=7)


def run_record(store, run_id="r1", requester="req-1", request_id=None):
    spec = RunSpec(request_id or f"rq-{run_id}", requester, "account", "get invoice", "invoice",
                   (SiteGrant("s1", "account"),), "", Admission("operator_telegram", "m1",
                                                               "sha256:" + "0" * 64))
    run, _ = store.create_run(spec, run_id)
    return run


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "state.db")
    reg = HandleRegistry()
    return SimpleNamespace(store=store, reg=reg, tmp=tmp_path, run=run_record(store))


def link(env, href, frame_url=f"https://{SITE}/invoices"):
    return env.reg.issue("r1", "s1", 7, PrivateTarget("link", "link", None, href, frame_url))


def downloader(env, harness=None, **kw):
    if harness is not None:
        kw.setdefault("proxy_url", harness.download_url)
        kw.setdefault("ca_file", env.tmp / "ca" / "mitmproxy-ca-cert.pem")
    return Downloader(env.store, env.reg, env.tmp / "artifacts", **kw)


def with_proxy(env, body, resolver=None):
    async def go():
        async with h.Harness(env.tmp, HOSTS, resolver=resolver) as hz:
            return await body(hz)
    return h.run(go)


def leftovers(env):
    d = env.tmp / "artifacts" / "files"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


# --------------------------------------------------------------------------- redirects


def test_link_fetch_uses_proxy_and_each_redirect(env):
    """Every hop is decided twice — by the run's policy as a download hop (the floor, the
    infrastructure rules, Chrome's background refusals) before it is sent, and by the proxy on
    the downloader's own listener — and a hop the floor refuses never reaches an upstream. An
    off-site hop (any public host) is fetched (S4-5)."""
    start, mid = f"https://{SITE}/inv/1", f"https://{SITE}/files/1"
    pol = policy()

    async def body(hz):
        hz.addon.install(pol)
        fx = hz.fixture
        fx.pages["/inv/1"] = ("text/plain", b"", {"Location": mid})
        fx.status["/inv/1"] = 302
        fx.pages["/files/1"] = ("application/pdf", PDF, {})
        fx.pages["/steal"] = ("application/pdf", PDF, {})
        fx.pages["/file"] = ("application/pdf", PDF, {})
        out = {}
        dl = downloader(env, hz)
        rec = await dl.fetch(env.run, link(env, start), session(), pol)
        out["ok"] = (rec.mime, rec.size, [(e.listener, e.allow, e.reason) for e in hz.addon.events],
                     [r[1:3] for r in fx.receipts.requests])
        # Off the site set (fetched); to a name resolving to a private address (the floor, at
        # the proxy); to a private address literal (the floor, before the hop leaves).
        for path, target in (("/inv/2", f"https://{EVIL}/steal"),
                             ("/inv/3", f"https://{LAN}/x"),
                             ("/inv/4", f"https://{CAP}/file?sig={TOKEN}"),
                             ("/inv/5", "https://" + h.ip4(10, 1, 2, 3) + "/x")):
            fx.pages[path] = ("text/plain", b"", {"Location": target})
            fx.status[path] = 302
            fx.receipts.reset()
            hz.addon.events.clear()
            try:
                await dl.fetch(env.run, link(env, f"https://{SITE}{path}"), session(), pol)
                code = None
            except DownloadRefused as err:
                code = err.code
            out[path] = (code, [r[1:3] for r in fx.receipts.requests],
                         [(e.allow, e.reason) for e in hz.addon.events])
        return out

    resolver = h.fixed_resolver({**{x: [h.FIXTURE_IP] for x in HOSTS}, LAN: [h.PRIVATE_IP]})
    out = with_proxy(env, body, resolver)
    assert out["ok"] == ("application/pdf", len(PDF), [("download", True, "download_hop")] * 2,
                         [(SITE, "/inv/1"), (SITE, "/files/1")])
    hop = (True, "download_hop")
    assert out["/inv/2"] == (None, [(SITE, "/inv/2"), (EVIL, "/steal")], [hop, hop])
    # LAN: the local check cannot see the address; the proxy's floor refuses it.
    assert out["/inv/3"] == ("blocked", [(SITE, "/inv/3")], [hop, (False, "floor")])
    assert out["/inv/4"] == (None, [(SITE, "/inv/4"), (CAP, f"/file?sig={TOKEN}")], [hop, hop])
    # A private literal: refused by the local check, so the proxy sees nothing of it.
    assert out["/inv/5"] == ("blocked", [(SITE, "/inv/5")], [hop])


def test_a_refused_hop_is_blocked_and_carries_nothing(env):
    """A hop the floor refuses is ``blocked``, and the refusal names no address."""
    target = "https://" + h.ip4(10, 1, 2, 3) + f"/file?sig={TOKEN}"
    pol = policy()

    async def body(hz):
        hz.addon.install(pol)
        hz.fixture.pages["/inv/9"] = ("text/plain", b"", {"Location": target})
        hz.fixture.status["/inv/9"] = 302
        with pytest.raises(DownloadRefused) as err:
            await downloader(env, hz).fetch(env.run, link(env, f"https://{SITE}/inv/9"), session(), pol)
        return err.value

    err = with_proxy(env, body)
    assert err.code == "blocked" and not hasattr(err, "need")
    assert TOKEN not in repr(err) and TOKEN not in str(err)


def test_offsite_hop_fetched_cookies_scoped(env):
    """S4-5, through the real proxy: an account site's link redirecting to an off-site CDN host
    is fetched on the downloader's listener; the account cookie goes on the site's own hop and
    never on the CDN hop, and the CDN's own cookie in the profile is not sent either."""
    start, cdn = f"https://{SITE}/invoice/7", f"https://{CAP}/blob/7.pdf?sig={TOKEN}"
    pol = policy()
    jar = [{"name": "sess", "value": "SiteSession123", "domain": SITE},
           {"name": "cdn", "value": "CdnCookie456", "domain": CAP}]

    async def body(hz):
        hz.addon.install(pol)
        fx = hz.fixture
        fx.pages["/invoice/7"] = ("text/plain", b"", {"Location": cdn})
        fx.status["/invoice/7"] = 302
        fx.pages["/blob/7.pdf"] = ("application/octet-stream", PDF, {})
        rec = await downloader(env, hz).fetch(env.run, link(env, start), session(cookies=jar), pol)
        return (rec, list(fx.receipts.cookies), [r[1:3] for r in fx.receipts.requests],
                [(e.listener, e.allow) for e in hz.addon.events])

    rec, cookies, requests, events = with_proxy(env, body)
    assert rec.mime == "application/pdf" and Path(rec.private_path).read_bytes() == PDF
    assert env.store.get_file(rec.file_id) is not None
    assert requests == [(SITE, "/invoice/7"), (CAP, f"/blob/7.pdf?sig={TOKEN}")]
    assert cookies == [(SITE, "/invoice/7", "sess=SiteSession123")]
    assert events == [("download", True), ("download", True)]


def test_redirect_cookies_follow_the_destination(env):
    """The site's cookies go to the site's own origins only; a cross-origin hop (a capability
    host) carries none; a bearer link to that host directly carries none either."""
    start, cap = f"https://{SITE}/inv/1", f"https://{CAP}/file?sig={TOKEN}"
    pol = policy()
    jar = [{"name": "sess", "value": "SiteSession123", "domain": SITE},
           {"name": "cap", "value": "CapCookie456", "domain": CAP}]

    async def body(hz):
        hz.addon.install(pol)
        fx = hz.fixture
        fx.pages["/inv/1"] = ("text/plain", b"", {"Location": cap})
        fx.status["/inv/1"] = 302
        fx.pages["/file"] = ("application/octet-stream", PDF, {})
        dl = downloader(env, hz)
        await dl.fetch(env.run, link(env, start), session(cookies=jar), pol)
        first = list(fx.receipts.cookies)
        fx.receipts.reset()
        await dl.fetch(env.run, link(env, cap), session(cookies=jar), pol)
        return first, list(fx.receipts.cookies), [r[1] for r in fx.receipts.requests]

    first, direct, hosts = with_proxy(env, body)
    assert first == [(SITE, "/inv/1", "sess=SiteSession123")]
    assert direct == [] and hosts == [CAP]


# --------------------------------------------------------------------------- content


def test_octet_stream_pdf_sniff(env):
    """The measured S3 shape (application/octet-stream holding a PDF) is a PDF; an HTML login
    page served as application/pdf with a .pdf name is not."""
    pdf_url, html_url = f"https://{SITE}/s3obj", f"https://{SITE}/fake"
    pol = policy()
    html = b"\n  <!DOCTYPE html><html><body><form>Sign in</form></body></html>"

    async def body(hz):
        hz.addon.install(pol)
        hz.fixture.pages["/s3obj"] = ("application/octet-stream", PDF,
                                      {"Content-Disposition": 'attachment; filename="INV_1_2.pdf"'})
        hz.fixture.pages["/fake"] = ("application/pdf", html,
                                     {"Content-Disposition": 'attachment; filename="invoice.pdf"'})
        dl = downloader(env, hz)
        a = await dl.fetch(env.run, link(env, pdf_url), session(), pol)
        b = await dl.fetch(env.run, link(env, html_url), session(), pol)
        return a, b

    a, b = with_proxy(env, body)
    assert (a.mime, a.safe_name) == ("application/pdf", "INV_1_2.pdf")
    assert b.mime == "text/html" and not b.safe_name.endswith(".pdf") and b.safe_name == "invoice.html"
    assert Path(a.private_path).read_bytes() == PDF


def test_detect_mime_table():
    assert detect_mime(b"PK\x03\x04rest", "application/vnd.openxmlformats-officedocument."
                       "spreadsheetml.sheet").startswith("application/vnd.openxmlformats")
    assert detect_mime(b"PK\x03\x04rest", "application/pdf") == "application/zip"
    # A declared type outside the fixed list is never echoed (it could carry anything).
    assert detect_mime(b"PK\x03\x04rest", f"application/vnd.{TOKEN.lower()}+zip") == "application/zip"
    assert detect_mime(b"PK\x03\x04rest", "application/vnd.oasis.opendocument.x" + TOKEN) == \
        "application/zip"
    assert detect_mime(b"plain", "text/" + TOKEN) == "application/octet-stream"
    assert safe_name("sheet.xlsx", "application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet") == "sheet.xlsx"
    assert detect_mime(b"a,b\n1,2\n", "text/csv; charset=utf-8") == "text/csv"
    assert detect_mime(b"not a pdf", "application/pdf") == "application/octet-stream"
    assert detect_mime(b"\xef\xbb\xbf<!-- x --> <html lang=en>", None) == "text/html"


@pytest.mark.parametrize("raw,mime,expected", [
    ("../../etc/passwd", "application/octet-stream", "passwd.bin"),
    ("..\\..\\boot.ini", "text/plain", "boot.txt"),
    ("C:\\Users\\x\\invoice 3.pdf", "application/pdf", "invoice 3.pdf"),
    (".hidden.pdf", "application/pdf", "hidden.pdf"),
    (f"{TOKEN}.pdf", "application/pdf", "download.pdf"),
    (f"invoice-{TOKEN}.pdf", "application/pdf", "download.pdf"),
    ("in\x00vo\nice\u202e.pdf", "application/pdf", "invoice.pdf"),
    ("report.xlsx", "application/octet-stream", "report.xlsx"),
    ("fake.pdf", "application/octet-stream", "fake.bin"),
    ("", "application/pdf", "download.pdf"),
    (None, "image/png", "download.png"),
    ("a" * 300 + ".pdf", "application/pdf", "a" * 80 + ".pdf"),
    ("Zq81Yt7Wm2Pk5Rn9.xs4bv6cd", "application/octet-stream", "download.bin"),
    ("Zq81Yt7Wm2Pk5Rn9.xs4bv6cd", "application/pdf", "download.pdf"),
    ("invoice " + "a" * 75 + " " + TOKEN + ".pdf", "application/pdf", "download.pdf"),
    # Judged as received, before the base name is taken or characters are dropped (r4).
    ("Zq81Yt7Wm2Pk5Rn9/Xs4Bv6Cd3Lh0Fg2Pk5R.pdf", "application/pdf", "download.pdf"),
    ("https://private.example.pdf", "application/pdf", "download.pdf"),
    ("https://private.example/invoice.pdf", "application/pdf", "download.pdf"),
])
def test_safe_name_cases(raw, mime, expected):
    assert safe_name(raw, mime) == expected


def test_filename_traversal_and_token(env):
    """Stored under its generated id; the safe name holds no path and no token; the private
    path never serialises."""
    url = f"https://{SITE}/dl/{TOKEN}"
    pol = policy()

    async def body(hz):
        hz.addon.install(pol)
        hz.fixture.pages[f"/dl/{TOKEN}"] = ("application/pdf", PDF, {
            "Content-Disposition": "attachment; filename*=UTF-8''..%2F..%2Fsecret%2Fx.pdf"})
        a = await downloader(env, hz).fetch(env.run, link(env, url), session(), pol)
        del hz.fixture.pages[f"/dl/{TOKEN}"]
        hz.fixture.pages[f"/dl/{TOKEN}"] = ("application/pdf", PDF, {})
        b = await downloader(env, hz).fetch(env.run, link(env, url), session(), pol)
        return a, b

    a, b = with_proxy(env, body)
    assert a.safe_name == "x.pdf" and b.safe_name == "download.pdf"
    for rec in (a, b):
        assert Path(rec.private_path).parent == env.tmp / "artifacts" / "files"
        assert Path(rec.private_path).name == rec.file_id
        assert "private_path" not in rec.to_wire() and TOKEN not in str(rec.to_wire())
        assert TOKEN not in repr(rec)
        assert oct(os.stat(rec.private_path).st_mode & 0o777) == "0o600"
    assert env.store.get_file(a.file_id).safe_name == "x.pdf"


def test_file_too_large_leaves_nothing(env):
    url = f"https://{SITE}/big"
    pol = policy()

    async def body(hz):
        hz.addon.install(pol)
        hz.fixture.pages["/big"] = ("application/pdf", PDF * 10, {})
        with pytest.raises(DownloadRefused) as err:
            await downloader(env, hz, max_file=4096).fetch(env.run, link(env, url), session(), pol)
        return err.value.code

    assert with_proxy(env, body) == "file_too_large"
    assert leftovers(env) == [] and env.store.files_for_run("r1") == []


def test_cancelled_transfer_leaves_nothing(env):
    gate = asyncio.Event()

    async def chunks():
        yield PDF[:100]
        await gate.wait()
        yield PDF[100:]

    async def go():
        dl = downloader(env)
        task = asyncio.create_task(dl._store_stream(env.run, chunks(), raw_name="x.pdf",
                                                    declared=None, source_handle=None, limit=10**6))
        for _ in range(20):
            await asyncio.sleep(0)
        assert [n for n in leftovers(env) if n.startswith(PARTIAL_PREFIX)]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert leftovers(env) == [] and env.store.files_for_run("r1") == []


def test_cleanup_partials_at_start(env):
    d = env.tmp / "artifacts" / "files"
    d.mkdir(parents=True)
    (d / f"{PARTIAL_PREFIX}fold").write_bytes(b"x")
    (d / "fkept").write_bytes(b"y")
    assert downloader(env).cleanup_partials() == 1
    assert leftovers(env) == ["fkept"]


# --------------------------------------------------------------------------- local addresses


def test_blob_and_data_links_read_in_the_sessions_page(env):
    import base64
    pol = policy()
    local = {"b64": base64.b64encode(PDF).decode(), "type": "application/pdf",
             "name": "runpod invoice 2026-10.pdf"}

    async def go():
        dl = downloader(env)
        s = session(local=local)
        rec = await dl.fetch(env.run, link(env, f"blob:https://{SITE}/0b1c-uuid"), s, pol)
        out = [(rec.mime, rec.safe_name, rec.size)]
        # A page that is not the session's (off the account site): nothing is read.
        s2 = session(page_url=f"https://{EVIL}/settings", local=local)
        for href in (f"blob:https://{EVIL}/x", "data:application/pdf;base64,JVBERi0="):
            try:
                await dl.fetch(env.run, link(env, href, f"https://{EVIL}/settings"), s2, pol)
            except DownloadRefused as err:
                out.append(err.code)
        out.append(s2.page.main_frame.calls)
        for href in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings",
                     "about:blank"):
            try:
                await dl.fetch(env.run, link(env, href), s, pol)
            except DownloadRefused as err:
                out.append(err.code)
        too_big = session(local={"too_large": True})
        try:
            await dl.fetch(env.run, link(env, f"blob:https://{SITE}/big"), too_big, pol)
        except DownloadRefused as err:
            out.append(err.code)
        return out

    out = asyncio.run(go())
    assert out == [("application/pdf", "runpod invoice 2026-10.pdf", len(PDF)),
                   "stale_handle", "stale_handle", 0,
                   "unsupported", "unsupported", "unsupported", "unsupported", "file_too_large"]


def test_stale_or_foreign_handle_refused(env):
    pol = policy()
    href = f"https://{SITE}/x"
    other = env.reg.issue("r2", "s1", 7, PrivateTarget("link", "link", None, href, None))
    old = env.reg.issue("r1", "s1", 6, PrivateTarget("link", "link", None, href, None))
    el = env.reg.issue("r1", "s1", 7, PrivateTarget("element", "button", None, None, None))

    async def go():
        out = []
        for hd in (other, old, "hnope", el):
            try:
                await downloader(env).fetch(env.run, hd, session(), pol)
            except DownloadRefused as err:
                out.append(err.code)
        return out

    assert asyncio.run(go()) == ["stale_handle", "stale_handle", "stale_handle", "unsupported"]


# --------------------------------------------------------------------------- browser downloads


class FakeDownload:
    def __init__(self, page, path, name="invoice.pdf", failure=None):
        self.page = page
        self._path = path
        self.suggested_filename = name
        self._failure = failure
        self.deleted = False
        self.cancelled = asyncio.Event() if path == "growing" else None

    async def failure(self):
        return self._failure

    async def path(self):
        if self.cancelled is not None:
            await self.cancelled.wait()
            raise RuntimeError("canceled")
        return self._path

    async def cancel(self):
        if self.cancelled is not None:
            self.cancelled.set()

    async def delete(self):
        self.deleted = True


def test_capture_browser_download(env):
    src = env.tmp / "browser-download"
    src.write_bytes(PDF)
    pol = policy()

    async def go():
        dl = downloader(env)
        s = session()
        ok = FakeDownload(s.page, src, "runpod-invoice.pdf")
        rec = await dl.capture_browser_download(env.run, ok, s, pol)
        out = [(rec.mime, rec.safe_name, rec.size, rec.source_handle, ok.deleted)]
        cases = [
            FakeDownload(FakePage(f"https://{SITE}/invoices"), src),           # another page
            FakeDownload(s.page, src, failure="canceled"),
            FakeDownload(s.page, None),
        ]
        off_session = session(page_url=f"https://{EVIL}/settings")   # not the session's page
        cases.append(FakeDownload(off_session.page, src))
        for i, d in enumerate(cases):
            try:
                await dl.capture_browser_download(env.run, d, off_session if i == 3 else s, pol)
            except DownloadRefused as err:
                out.append((err.code, d.deleted))
        big = FakeDownload(s.page, src)
        try:
            await downloader(env, max_file=100).capture_browser_download(env.run, big, s, pol)
        except DownloadRefused as err:
            out.append((err.code, big.deleted))
        return out

    out = asyncio.run(go())
    assert out[0] == ("application/pdf", "runpod-invoice.pdf", len(PDF), None, True)
    assert out[1:] == [("blocked", True)] * 4 + [("file_too_large", True)]
    assert len(env.store.files_for_run("r1")) == 1
    assert [n for n in leftovers(env) if n.startswith(PARTIAL_PREFIX)] == []


# --------------------------------------------------------------------------- archives


def _put(env, run_id, name, data, requester="req-1"):
    from casa_browser.protocol import FileRecord
    import hashlib
    d = env.tmp / "artifacts" / "files"
    d.mkdir(parents=True, exist_ok=True)
    fid = f"f{run_id}{len(list(d.iterdir()))}"
    (d / fid).write_bytes(data)
    rec = FileRecord(fid, run_id, requester, name, "application/pdf", len(data),
                     hashlib.sha256(data).hexdigest(), str(d / fid))
    env.store.put_file(rec)
    return fid


def test_archive_requester_scope(env):
    run_record(env.store, "r2", requester="req-1")
    run_record(env.store, "r3", requester="req-2")
    a1 = _put(env, "r1", "x.pdf", PDF)
    a2 = _put(env, "r1", "x.pdf", PDF + b"2")
    a3 = _put(env, "r1", "X.PDF", PDF + b"3")
    a4 = _put(env, "r1", "x (2).pdf", PDF + b"4")
    b1 = _put(env, "r2", "y.pdf", PDF)
    c1 = _put(env, "r3", "z.pdf", PDF, requester="req-2")
    dl = downloader(env)
    for ids in ([a1, b1], [a1, c1], [a1, "fnope"]):
        with pytest.raises(ProtocolError) as err:
            dl.make_archive(env.run, ids)
        assert err.value.code == "not_found"
    tricky = dl.make_archive(env.run, [a4, a1, a2])
    with zipfile.ZipFile(io.BytesIO(Path(tricky.private_path).read_bytes())) as zf:
        assert zf.namelist() == ["x (2).pdf", "x.pdf", "x (3).pdf"]
        assert zf.read("x (2).pdf") == PDF + b"4" and zf.read("x (3).pdf") == PDF + b"2"
    rec = dl.make_archive(env.run, [a1, a2, a3])
    assert (rec.mime, rec.safe_name, rec.run_id, rec.requester_id) == (
        "application/zip", "files.zip", "r1", "req-1")
    with zipfile.ZipFile(io.BytesIO(Path(rec.private_path).read_bytes())) as zf:
        names = zf.namelist()
        assert names == ["x.pdf", "x (2).pdf", "X (3).PDF"]
        assert all("/" not in n and "\\" not in n and not n.startswith(".") for n in names)
        assert zf.read("x (2).pdf") == PDF + b"2"
    with pytest.raises(ProtocolError) as err:
        downloader(env, max_archive=100).make_archive(env.run, [a1])
    assert err.value.code == "file_too_large"
    assert [n for n in leftovers(env) if n.startswith(PARTIAL_PREFIX)] == []


# --------------------------------------------------------------------------- delivery


def test_delivery_ack_idempotent(env):
    fid = _put(env, "r1", "x.pdf", PDF)
    dl = downloader(env)
    ack = lambda d, o, who="req-1": dl.ack_delivery(fid, who, d, o)
    assert ack("d1", "deposited")["recorded"] is True
    assert ack("d1", "deposited")["recorded"] is False
    assert ack("d1", "delivered") == {"file_id": fid, "delivery_id": "d1", "outcome": "delivered",
                                      "recorded": True}
    assert ack("d1", "delivered")["recorded"] is False
    for d, o, who, code in (("d1", "withheld", "req-1", "conflict"),
                            ("d1", "deposited", "req-1", "conflict"),
                            ("d9", "deposited", "req-2", "not_found"),
                            ("d9", "lost", "req-1", "unsupported")):
        with pytest.raises(ProtocolError) as err:
            ack(d, o, who)
        assert err.value.code == code
    assert ack("d2", "deposited")["recorded"] is True
    assert ack("d2", "withheld")["recorded"] is True
    assert env.store.current_delivery(fid) == ("d2", "withheld")
    with pytest.raises(ProtocolError):
        dl.ack_delivery("fmissing", "req-1", "d1", "delivered")
    assert ack("d3", "handed_off")["recorded"] is True
    with pytest.raises(ProtocolError):
        ack("d3", "delivered")
    # The record shows every attempt, the withheld one included.
    env.store.put_record(RunRecord("r1", "req-1", ("s1",), "done", (), (fid,), (),
                                   datetime.now(timezone.utc)))
    rec, = env.store.records_for("req-1", datetime.now(timezone.utc) - timedelta(days=1))
    assert rec.delivery_outcomes == ((fid, "d1", "delivered"), (fid, "d2", "withheld"),
                                     (fid, "d3", "handed_off"))
    assert env.store.records_for("req-2", datetime.now(timezone.utc) - timedelta(days=1)) == []


def test_migration_keeps_existing_rows(tmp_path):
    """A database at schema 1 migrates to the deliveries table without losing rows."""
    import sqlite3
    from casa_browser import store as store_mod
    path = tmp_path / "s.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.execute("INSERT INTO schema_version VALUES (1)")
    for stmt in store_mod.MIGRATIONS[0].split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    db.close()
    s = Store(path)
    assert s.schema_version() == len(store_mod.MIGRATIONS) >= 2
    run_record(s)
    Store(path).close()


def test_link_on_a_page_not_of_the_session_is_not_fetched(env):
    pol = policy()

    async def body(hz):
        hz.addon.install(pol)
        try:
            await downloader(env, hz).fetch(env.run, link(env, f"https://{SITE}/x", f"https://{EVIL}/settings"),
                                            session(page_url=f"https://{EVIL}/settings"), pol)
        except DownloadRefused as err:
            return err.code, hz.fixture.receipts.connections

    assert with_proxy(env, body) == ("stale_handle", 0)


def test_page_that_moves_while_read_keeps_nothing(env):
    import base64
    pol = policy()
    s = session()

    def moving(_arg):
        s.view_generation += 1
        return {"b64": base64.b64encode(PDF).decode(), "type": "application/pdf", "name": "x.pdf"}

    s.page.main_frame.local = moving

    async def go():
        try:
            await downloader(env).fetch(env.run, link(env, f"blob:https://{SITE}/u"), s, pol)
        except DownloadRefused as err:
            return err.code

    assert asyncio.run(go()) == "stale_handle"
    assert env.store.files_for_run("r1") == []


def test_redirect_loop_ends(env):
    url = f"https://{SITE}/loop"
    pol = policy()

    async def body(hz):
        hz.addon.install(pol)
        hz.fixture.pages["/loop"] = ("text/plain", b"", {"Location": url})
        hz.fixture.status["/loop"] = 302
        try:
            await downloader(env, hz).fetch(env.run, link(env, url), session(), pol)
        except DownloadRefused as err:
            return err.code, len(hz.fixture.receipts.requests)

    assert with_proxy(env, body) == ("blocked", 11)


def test_blob_in_a_frame_not_of_the_session_is_not_read(env):
    import base64
    pol = policy()
    s = session()
    frame = FakeFrame(f"https://{EVIL}/embedded-settings",
                      {"b64": base64.b64encode(PDF).decode(), "type": None, "name": None})
    s.page.frames.append(frame)

    async def go():
        try:
            await downloader(env).fetch(env.run, link(env, f"blob:https://{SITE}/u", frame.url), s, pol)
        except DownloadRefused as err:
            return err.code, frame.calls

    assert asyncio.run(go()) == ("stale_handle", 0)


def test_browser_download_cancelled_while_it_grows_past_the_bound(env):
    """The bound holds while Chrome writes: a download still growing is cancelled once the
    downloads directory holds more than the limit."""
    landing = env.tmp / "browser-downloads"
    landing.mkdir()
    pol = policy()

    async def go():
        dl = downloader(env, max_file=4096, browser_downloads=landing)
        s = session()
        d = FakeDownload(s.page, "growing")

        async def chrome_writes():
            with open(landing / "0b1c-guid", "wb") as fh:
                for _ in range(20):
                    fh.write(b"x" * 1024)
                    fh.flush()
                    await asyncio.sleep(0.05)

        writer = asyncio.create_task(chrome_writes())
        try:
            await asyncio.wait_for(dl.capture_browser_download(env.run, d, s, pol), 10)
        except DownloadRefused as err:
            return err.code, d.cancelled.is_set(), d.deleted
        finally:
            writer.cancel()

    assert asyncio.run(go()) == ("file_too_large", True, True)
    assert env.store.files_for_run("r1") == []


def test_browser_download_kept_only_if_the_page_stays_the_sessions(env):
    """A page that moves while the download completes — off the session's site, or to a new
    document — keeps nothing."""
    src = env.tmp / "browser-download"
    src.write_bytes(PDF)
    pol = policy()

    class Moving(FakeDownload):
        def __init__(self, s, move):
            super().__init__(s.page, src)
            self.s, self.move = s, move

        async def path(self):
            self.move(self.s)
            return src

    def to_off_session(s):
        s.page.url = f"https://{EVIL}/invoices"

    def new_document(s):
        s.view_generation += 1

    async def go():
        out = []
        for move in (to_off_session, new_document):
            s = session()
            d = Moving(s, move)
            try:
                await downloader(env).capture_browser_download(env.run, d, s, pol)
                out.append("kept")
            except DownloadRefused as err:
                out.append((err.code, d.deleted))
        return out

    assert asyncio.run(go()) == [("blocked", True), ("blocked", True)]
    assert env.store.files_for_run("r1") == []


def test_only_a_complete_response_is_a_file(env):
    """A 206 part (even one the server sends unasked) or a 204 is not a stored file."""
    url = f"https://{SITE}/partial"
    pol = policy()

    async def body(hz):
        hz.addon.install(pol)
        hz.fixture.pages["/partial"] = ("application/pdf", PDF[:100],
                                        {"Content-Range": f"bytes 0-99/{len(PDF)}"})
        hz.fixture.status["/partial"] = 206
        hz.fixture.pages["/empty"] = ("application/pdf", b"", {})
        hz.fixture.status["/empty"] = 204
        import gzip
        hz.fixture.pages["/gz"] = ("application/pdf", gzip.compress(PDF, mtime=0)[:31],
                                   {"Content-Encoding": "gzip"})
        codes = []
        for u in (url, f"https://{SITE}/empty", f"https://{SITE}/gz"):
            try:
                await downloader(env, hz).fetch(env.run, link(env, u), session(), pol)
            except DownloadRefused as err:
                codes.append(err.code)
        return codes

    assert with_proxy(env, body) == ["blocked", "blocked", "blocked"]
    assert leftovers(env) == [] and env.store.files_for_run("r1") == []


def test_the_archive_file_itself_is_bounded(env):
    a = _put(env, "r1", "a.pdf", b"x" * 50)
    b = _put(env, "r1", "b.pdf", b"y" * 50)
    with pytest.raises(ProtocolError) as err:
        downloader(env, max_archive=100).make_archive(env.run, [a, b])
    assert err.value.code == "file_too_large"
    assert [n for n in leftovers(env) if n.startswith(PARTIAL_PREFIX)] == []
    assert len(env.store.files_for_run("r1")) == 2


def _judging(dl):
    """Record every address the local hop check is asked about (the real check still runs)."""
    judged = []
    real = dl._check_hop

    def check(meta, policy):
        judged.append(meta.url)
        return real(meta, policy)
    dl._check_hop = check
    return judged


def test_the_hop_judged_is_the_hop_sent(env):
    """The client sends no fragment and drops an empty query: the policy is asked about exactly
    the address that goes on the wire, and the proxy decides that same request once."""
    pol = policy()

    async def body(hz):
        hz.addon.install(pol)
        hz.fixture.pages["/file"] = ("application/pdf", PDF, {})
        out = []
        for href in (f"https://{SITE}/file#part", f"https://{SITE}/file?"):
            hz.addon.events.clear()
            hz.fixture.receipts.reset()
            dl = downloader(env, hz)
            judged = _judging(dl)
            rec = await dl.fetch(env.run, link(env, href), session(), pol)
            out.append((rec.mime, judged, [r[2] for r in hz.fixture.receipts.requests],
                        len(hz.addon.events)))
        return out

    assert with_proxy(env, body) == [("application/pdf", [f"https://{SITE}/file"], ["/file"], 1)] * 2


def test_a_bare_origin_is_judged_as_its_root_page(env):
    """A redirect to `https://host` (no path) is sent as `/`; the policy is asked about `/`."""
    pol = policy()

    async def body(hz):
        hz.addon.install(pol)
        hz.fixture.pages["/start"] = ("text/plain", b"", {"Location": f"https://{SITE}"})
        hz.fixture.status["/start"] = 302
        dl = downloader(env, hz)
        judged = _judging(dl)
        rec = await dl.fetch(env.run, link(env, f"https://{SITE}/start"), session(), pol)
        return rec.mime, judged, [r[2] for r in hz.fixture.receipts.requests]

    assert with_proxy(env, body) == ("text/plain", [f"https://{SITE}/start", f"https://{SITE}/"],
                                     ["/start", "/"])


def test_a_cancelled_capture_cancels_the_browser_download(env):
    pol = policy()

    async def go():
        s = session()
        d = FakeDownload(s.page, "growing")
        task = asyncio.create_task(downloader(env).capture_browser_download(env.run, d, s, pol))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return d.cancelled.is_set(), d.deleted

    assert asyncio.run(go()) == (True, True)


def test_judged_download_sends_once_when_the_response_is_lost(env):
    """Action model G2, "one input, one send": aiohttp resends an idempotent request whose
    connection broke after it was written. A judged download's request goes out once: a
    server that reads it and closes without answering receives exactly one request, the
    journal callback ran once before it, and the fetch ends refused (the caller's
    outcome_unknown), never with a second send."""
    ca, _cert, _key = h.make_ca_and_cert(env.tmp, ["unused.example"])

    async def go():
        received = []

        async def handler(reader, writer):
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                data += chunk
            received.append(data.split(b"\r\n", 1)[0])
            writer.close()                     # the response is lost after the send

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/files/invoice.pdf"

        async def agent(_session):
            return "Mozilla/5.0"
        dl = downloader(env, proxy_url=None, ca_file=ca, user_agent=agent)
        dl._check_hop = lambda meta, policy: None        # the policy is not under test here
        sends = []
        try:
            with pytest.raises(DownloadRefused):
                await dl.fetch(env.run, link(env, url, frame_url=f"https://{SITE}/invoices"),
                               session(), policy(), on_send=lambda: sends.append(1))
        finally:
            server.close()
            await server.wait_closed()
        return received, sends

    received, sends = asyncio.run(go())
    assert received == [b"GET /files/invoice.pdf HTTP/1.1"] and sends == [1]


# --------------------------------------------------------------------------- the quarantine (S4-6)

EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\0" * 600       # an unknown binary: octet-stream
ZIP = b"PK\x03\x04" + b"\0" * 300


def _local(data, mime, name):
    import base64
    return {"b64": base64.b64encode(data).decode(), "type": mime, "name": name}


def test_quarantine_holds_a_non_document_out_of_the_store(env):
    """With ``quarantine`` (a public link download, design §3), a type outside the document set
    is complete and hashed but stays a private partial file, never registered; a document is
    registered at once. ``keep`` registers the quarantined file as fetched; ``discard`` deletes
    it; an app start's ``cleanup_partials`` removes one a crash left."""
    import hashlib

    async def fetch(data, mime, name, *, quarantine=True):
        s = session(local=_local(data, mime, name))
        return await downloader(env).fetch(env.run, link(env, f"blob:https://{SITE}/u"), s,
                                           policy(), quarantine=quarantine)

    async def go():
        return (await fetch(EXE, "application/x-msdownload", "setup.exe"),
                await fetch(ZIP, "application/zip", "tool.zip"),
                await fetch(PDF, "application/pdf", "invoice.pdf"),
                await fetch(EXE, None, "plain.exe", quarantine=False))

    exe, zipped, pdf, unquarantined = asyncio.run(go())
    for q, data, mime in ((exe, EXE, "application/octet-stream"), (zipped, ZIP, "application/zip")):
        assert isinstance(q, Quarantined)
        assert q.packet() == {"name": q.record.safe_name, "type": mime, "size": len(data)}
        assert q.path.name.startswith(PARTIAL_PREFIX) and q.path.read_bytes() == data
        assert q.record.sha256 == hashlib.sha256(data).hexdigest()
        assert env.store.get_file(q.record.file_id) is None
    assert exe.packet()["name"] == "setup.exe"
    assert isinstance(pdf, FileRecord) and env.store.get_file(pdf.file_id) is not None
    assert isinstance(unquarantined, FileRecord)          # only a public link download is held
    dl = downloader(env)
    kept = dl.keep(exe)
    assert kept == exe.record and env.store.get_file(kept.file_id) is not None
    assert Path(kept.private_path).read_bytes() == EXE and not exe.path.exists()
    dl.discard(zipped)
    assert not zipped.path.exists() and env.store.get_file(zipped.record.file_id) is None
    leftover = asyncio.run(fetch(EXE, None, "again.exe"))
    assert leftover.path.exists()
    assert dl.cleanup_partials() == 1 and not leftover.path.exists()
    assert [n for n in leftovers(env) if n.startswith(PARTIAL_PREFIX)] == []


def test_the_document_set():
    """PDF, images, text, CSV, calendar and office documents are documents; a zip, an
    executable or an unknown binary is not."""
    for mime in ("application/pdf", "image/png", "image/jpeg", "image/gif", "text/plain",
                 "text/csv", "text/calendar",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "application/vnd.oasis.opendocument.spreadsheet"):
        assert mime in DOCUMENT_TYPES, mime
    for mime in ("application/zip", "application/octet-stream"):
        assert mime not in DOCUMENT_TYPES
    assert detect_mime(EXE, "application/x-msdownload") not in DOCUMENT_TYPES
